# Index Lifecycle

**Scope:** collection naming and alias indirection, shadow build, pre-promotion validation, alias swap, retention, rollback, reindex triggers, incremental upsert, chunk identity consequences, deletion and tombstone replay, and the failure semantics that map into this state machine.

**Spec references:** §4.4, §4.5, §6.6, §9.4, §10 (all), §15, §17.1, §18.3 tests 1, 6, 7.

**Related pages:** `docs/contracts/chunk.md` (chunk identity derivation); `docs/architecture/provider-abstraction.md` (model identity pinning); `docs/runbooks/reindex.md`; `docs/runbooks/rollback.md`.

---

## 1. Invariants

These hold for the lifetime of every knowledge base. Every design decision below is constrained by them.

1. **Clients see only aliases, never collection names.** No client-facing query, agent call, or API response ever references a Qdrant collection name directly. This is §4.2 constraint C-3 and it is not negotiable.
2. **The ingestion path writes only to shadow collections.** The live collection is read-only from the ingestion perspective. This is §4.2 constraint C-4.
3. **A shadow collection is never promoted without passing all validation gates.** A shadow that fails validation is retained for inspection, never silently discarded.
4. **Alias swap is atomic from the client perspective.** There is no window during which the alias points at a partially-constructed or failing collection.
5. **Rollback to N-1 is always available without a restore operation.** The N-1 collection stays hot in memory until at least one successful subsequent promotion occurs.
6. **The tombstone log is replayed before any restored snapshot becomes promotion-eligible.** This is §17.1 and it applies regardless of the restore path.

---

## 2. Collection naming and alias indirection

### 2.1 Collection naming scheme

Collections are named by the platform and never by operators or clients. The naming scheme encodes identity and version without being user-visible.

Format:

```
rtfc_{kb_id}_{build_id}
```

Where:
- `rtfc_` is a constant prefix that namespaces all platform-owned Qdrant collections.
- `kb_id` is the knowledge base's stable UUID (hyphen-stripped, lowercase). Example: `a3f9b2c1d4e5`.
- `build_id` is a monotonically increasing integer, zero-padded to 8 digits, assigned by the control plane at shadow-collection creation time. Example: `00000007`.

Full example: `rtfc_a3f9b2c1d4e5_00000007`

The `build_id` is not a timestamp. Timestamps create race conditions in concurrent builds and are harder to compare. The sequence is managed by the control-plane database.

### 2.2 Alias naming scheme

Each knowledge base has exactly one alias. The alias name is stable for the lifetime of the knowledge base.

Format:

```
rtfc_{kb_id}
```

Example: `rtfc_a3f9b2c1d4e5`

The alias is created when the knowledge base is first created (before any collection exists). Until the first successful promotion, the alias points at nothing — queries against an unprovisioned knowledge base receive a `KB_NOT_READY` error, not a vector-DB error.

There is never more than one alias per knowledge base. There are no staging aliases, preview aliases, or tenant-level aliases. The alias is the stable client address.

### 2.3 Alias metadata in the control plane

The control-plane database maintains a record for every alias:

| Field | Description |
|---|---|
| `alias_name` | The alias string |
| `kb_id` | Foreign key to the knowledge base |
| `current_collection` | The collection name the alias currently points at |
| `current_build_id` | The build ID of the current collection |
| `model_identity` | Denormalized copy of the current collection's model identity (see `docs/architecture/provider-abstraction.md` §3.1) |
| `promoted_at` | Timestamp of the last successful promotion |
| `previous_collection` | Collection name of the N-1 collection (null before second promotion) |

This record is the authority for the retrieval service's model-identity mismatch check. It is updated atomically with the Qdrant alias retarget in a two-phase operation described in §4.

---

## 3. State machine

The lifecycle of a single build attempt, from trigger to retirement. States are named; transitions carry the event that fires them and any preconditions.

```mermaid
stateDiagram-v2
    [*] --> TRIGGERED : reindex trigger fires

    TRIGGERED --> SHADOW_CREATING : control plane allocates build_id,\ncreates collection record
    SHADOW_CREATING --> INGESTING : shadow collection created in Qdrant
    SHADOW_CREATING --> FAILED_SHADOW_CREATE : Qdrant unavailable or naming conflict

    INGESTING --> INGESTING : checkpoint written per batch
    INGESTING --> PAUSED : provider unavailable or rate limit hit
    PAUSED --> INGESTING : provider recovered, backoff elapsed
    INGESTING --> VALIDATING : all documents processed,\nno unresolved worker deaths
    INGESTING --> FAILED_INGESTION : worker dies (detected via heartbeat)

    FAILED_INGESTION --> INGESTING : worker resumes from last checkpoint

    VALIDATING --> PROMOTING : all validation gates pass
    VALIDATING --> VALIDATION_FAILED : any gate fails

    VALIDATION_FAILED --> [*] : shadow retained for inspection,\nalias unchanged, alert raised

    PROMOTING --> LIVE : alias retarget succeeds in Qdrant\nAND control-plane alias record updated atomically
    PROMOTING --> PROMOTE_FAILED : Qdrant alias update fails\nOR control-plane update fails

    PROMOTE_FAILED --> PROMOTING : idempotent retry (previous alias\ntarget remains authoritative)

    LIVE --> RETIRING : subsequent build promoted successfully
    LIVE --> ROLLED_BACK : operator or automated rollback to this collection

    ROLLED_BACK --> LIVE : (this collection becomes live again;\nnew N-1 is the one that was just in LIVE)

    RETIRING --> HOT_STANDBY : becomes N-1
    HOT_STANDBY --> SNAPSHOTTING : subsequent build promoted;\nthis drops from hot retention
    SNAPSHOTTING --> COLD : snapshot written to object storage,\ncollection dropped from Qdrant
    COLD --> RESTORING : operator initiates restore
    RESTORING --> TOMBSTONE_REPLAY : snapshot loaded into Qdrant
    TOMBSTONE_REPLAY --> VALIDATING : tombstone log fully applied;\ncollection enters validation gates
    COLD --> PURGED : retention period elapsed\nOR explicit purge command
    PURGED --> [*]
```

### 3.1 State descriptions

| State | Description |
|---|---|
| `TRIGGERED` | A reindex trigger has fired. The control plane has recorded the trigger reason and is about to create a shadow collection. |
| `SHADOW_CREATING` | The control plane is allocating a build ID and creating the Qdrant collection. No data has been written. |
| `INGESTING` | Workers are embedding and writing chunks to the shadow collection. Checkpoints are written after every batch. |
| `PAUSED` | Ingestion is paused due to provider unavailability or rate limits. The job record is in a `paused` state; no worker is consuming the queue. |
| `FAILED_INGESTION` | A worker died without writing its checkpoint. The job record carries the last confirmed checkpoint. |
| `VALIDATING` | All chunks are written. The validation suite (§10.4) is running. |
| `VALIDATION_FAILED` | One or more validation gates failed. The shadow collection is retained and tagged `validation_failed`. The alias is unchanged. An alert is raised. |
| `PROMOTING` | The platform is executing the two-phase alias swap: Qdrant alias retarget + control-plane alias record update. |
| `PROMOTE_FAILED` | The alias swap failed partway. The previous alias target is authoritative. The operation is retried. |
| `LIVE` | The collection is the current alias target. Query traffic is being served from it. |
| `ROLLED_BACK` | A previously live collection has been re-promoted via rollback. Semantically equivalent to `LIVE`. |
| `RETIRING` | A subsequent promotion has completed. This collection is transitioning to N-1. |
| `HOT_STANDBY` | This is the N-1 collection. Retained in memory. Instant rollback available. |
| `SNAPSHOTTING` | The N-1 collection is being serialised to object storage. |
| `COLD` | The collection is in object storage only. Not in Qdrant. Rollback requires a restore operation. |
| `RESTORING` | The cold snapshot is being loaded back into Qdrant. |
| `TOMBSTONE_REPLAY` | The tombstone log is being applied to the restored collection. This MUST complete before the collection can enter `VALIDATING`. |
| `PURGED` | The collection and its snapshot have been destroyed. No recovery possible. |

---

## 4. Alias swap — atomic two-phase operation

The alias swap is the most critical operation in the lifecycle. Zero query errors during a swap is a hard requirement (§4.5 availability during alias swap: 100%; §18.3 test 1).

### 4.1 Phase 1: Qdrant alias retarget

The platform calls Qdrant's alias update API to atomically retarget the alias from the current collection to the promoted shadow collection. Qdrant guarantees that from the moment the API call returns success, all subsequent alias resolutions point at the new collection. In-flight requests that already resolved the alias continue against the old collection until they complete.

### 4.2 Phase 2: control-plane record update

Immediately after Qdrant confirms the alias retarget, the platform updates the control-plane alias record in a single database transaction:

- `current_collection` → new collection name
- `current_build_id` → new build ID
- `model_identity` → new collection's model identity
- `promoted_at` → now
- `previous_collection` → old `current_collection`

The retrieval service subscribes to alias-change events (published on the control-plane's event bus after Phase 2 commits). On receiving an alias-change event, each retrieval service replica invalidates its cached model identity for the affected alias.

### 4.3 Failure handling

If Phase 1 fails (Qdrant call fails): no state has changed. The operation fails with `ALIAS_SWAP_QDRANT_FAILED`. The build enters `PROMOTE_FAILED`. The operation is idempotent and retried: Phase 1 is retried with the same parameters. The previous alias target is authoritative throughout.

If Phase 2 fails (database transaction fails after Phase 1 succeeds): the Qdrant alias now points at the new collection, but the control-plane record still shows the old collection. This is the inconsistent window. Recovery: the platform detects this on startup and after any promotion attempt by comparing the Qdrant alias target to the control-plane record. If they differ, Phase 2 is retried. The control-plane record is the lagging state; the Qdrant alias is the leading state. The retrieval service reads model identity from the control plane, so during the inconsistency window it still uses the old model identity — meaning queries will use the old model for embedding, which is correct because the new collection's model identity has not been propagated. This is a safe inconsistency.

See open question OQ-L-1 for the proposed mitigation against this inconsistency window.

---

## 5. Pre-promotion validation gates

A shadow collection MUST pass all four gates before entering `PROMOTING`. All gates run sequentially. Failing any gate blocks promotion and moves the collection to `VALIDATION_FAILED`.

### Gate 1: Chunk count bounds

The expected chunk count is estimated from the ingestion config's segment count and the configured chunking parameters. The actual chunk count in the shadow collection must be within ±N% of the expected count (configurable; proposed default: 20%). A count outside bounds indicates a systematic ingestion failure.

What it catches: wholesale embedding failures, a config-change that unexpectedly eliminates a content class, an off-by-one in batching that dropped documents silently.

### Gate 2: No unexpected zero-chunk content class

For every content class declared in the ingestion config that has at least one document assigned to it, the shadow collection must have at least one chunk from that class. A class producing zero chunks is either a systematic parsing failure or a config error, and either is a problem that should block promotion.

"Unexpected" qualifier: if the class has no documents in the source corpus, zero chunks is expected. The gate compares actual class distributions against what the ingestion run's own progress log reported as input.

### Gate 3: Eval baseline run

The platform runs the eval set against the shadow collection using the same query embedding provider that will serve live traffic. The baseline score (context recall, context precision) is computed and stored against the build ID.

If no eval set exists (first build of a new knowledge base), this gate passes vacuously. An alert is raised recommending that the operator generate an eval set.

### Gate 4: Regression threshold

If a previous baseline exists (from the prior live collection), the shadow's baseline score MUST NOT be below the prior baseline by more than the configured regression threshold (proposed default: 5% absolute on context recall). A regression below threshold blocks promotion and raises an alert. The operator can override the block after reviewing the regression — this override is logged as a governance event.

This gate is the runtime enforcement of §9.4 drift detection at promotion time.

---

## 6. Failure rows from §15 mapped to the state machine

| §15 failure row | State machine mapping | Required behaviour |
|---|---|---|
| Embedding provider unavailable mid-ingestion | `INGESTING → PAUSED` | Job pauses; provider health is polled with exponential backoff; job resumes from last checkpoint when provider recovers. No partial shadow promoted (collection is in `INGESTING`, not `VALIDATING`). |
| Embedding provider unavailable at query time | Live collection unaffected. Retrieval service returns `PROVIDER_UNAVAILABLE` error with distinguishable error code. | Fail closed. MUST NOT fall back to a different model. The model identity mismatch check (§3.3 in provider-abstraction.md) and this failure are both enforced in the same retrieval service code path. |
| Vector DB unavailable | All states: if Qdrant is unreachable, retrieval fails closed; ingestion pauses; alias swap is not attempted until Qdrant is reachable. | `INGESTING → PAUSED` (or `VALIDATING`/`PROMOTING` blocked). |
| Single document fails to parse | `INGESTING` (internal) | Recorded, reported, ingestion continues. The document appears in the findings report with its failure reason. The shadow collection may be missing this document's chunks; Gate 2 will catch if a whole class was affected. |
| Whole content class fails | `INGESTING → FAILED_INGESTION` (elevated) | Ingestion halts and alerts. Control plane marks the build `class_failure`. Operator intervention required before resumption. |
| Shadow collection fails validation | `VALIDATING → VALIDATION_FAILED` | Promotion blocked, alias unchanged. Shadow retained for inspection (minimum 7 days; see open question OQ-L-2). Alert raised. |
| Alias swap fails mid-operation | `PROMOTING → PROMOTE_FAILED → PROMOTING` | Previous alias target remains authoritative. Operation is idempotent. Retried by the control plane. See §4.3 for the two-phase failure analysis. |
| Ingestion worker dies | `INGESTING → FAILED_INGESTION → INGESTING` | Worker death detected via missed heartbeat (configurable timeout). Job record carries the last confirmed checkpoint. A new worker picks up the job from that checkpoint. No duplication: chunk IDs are deterministic (§10.5). |
| Rate limit hit at provider | `INGESTING` (internal) | Backoff per provider's `retry-after` signal. Surface remaining budget in the job progress view. Do not fail the job. |

---

## 7. Reindex triggers

Four triggers, all independently configurable per knowledge base.

### 7.1 Manual

Initiated by a KB Editor or Platform Admin via the API or UI. Creates a new build immediately. No content-change detection; always a full consideration of the current corpus.

### 7.2 Change-detected

Fires when the source connector detects changes in the source system. The platform MUST distinguish between content changes and metadata-only changes:

- **Content change:** the document's content hash differs from the ingested version. Triggers an incremental upsert (§8) if the config is unchanged, or a full rebuild if the config has changed.
- **Metadata-only change:** title, tags, last-modified timestamp, or other metadata changed but the content hash is the same. Does NOT trigger reingestion. The metadata fields are updated in the existing chunks' payloads in-place, without a rebuild. This prevents needless rebuilds when a document's title is edited or a permission tag is updated.

The content-hash comparison is done against the hash stored in the chunk payload (or the document record in the control-plane database, which is populated from the inventory contract). The connector's change signal alone is not sufficient; the platform verifies the hash.

#### Metadata-only update path (in-place payload update, no re-embed)

Some changes alter only a chunk's **payload**, not its `text` or `embedding_input`, so they do not
change `config_version` and MUST NOT force a re-embed or a rebuild. These run through an
**in-place payload update job** that patches the affected chunks' payloads in every live/hot
collection (and is recorded in the tombstone-adjacent operation log). The path handles:

- **Document metadata** (title, tags, permission tags) — as above.
- **Salience-tier MAPPING changes (R3, attack 8).** When the *mapping* from signals to tiers is
  remapped (a salience-tier remap, or a version-family-primacy flip below), the job re-derives the
  affected chunks' baked `salience_tier` payload in place. This is payload-only because tier is a
  filter-time weight class, not part of `text`/`embedding_input`. (Contrast: a **class-description**
  edit *does* change embedded augmentation and salience classification, so it rotates
  `config_version` and forces a rebuild — it is **not** on this path; see
  [ingestion-config.md](../contracts/ingestion-config.md) `config_version` derivation.)
- **Version-family primacy flip (R5, attack 5).** See §7.5.

### 7.3 Scheduled

Cron-style per knowledge base. Examples: nightly for a fast-changing corpus, weekly for a stable one. Respects budget caps (§16) — a scheduled reindex that would exceed the KB's budget cap is paused and alerts rather than running and hitting the cap mid-run. Repeated cap-hits from scheduled reindexes alert the Workspace Owner.

### 7.4 Config-change

Any change to the following configuration fields MUST automatically schedule a full rebuild:

- Chunking strategy or chunking parameters
- Transformation tier (any tier setting)
- Embedding model or embedding provider
- Segment type taxonomy version (see open question OQ-L-3)

The UI MUST warn before saving a config change that will trigger a rebuild, including an estimated cost for the rebuild. The UI MUST NOT silently enqueue a rebuild without the operator's acknowledgement.

Config-change reindexes are always full rebuilds. Incremental upsert is not applicable because the config version is part of chunk identity (§10.5); old chunks under the old config version cannot be partially retained.

### 7.5 Version-family membership or primacy changed (R5)

A fifth trigger fires when a **version-family's primary changes** — a newer member arrives, or an
operator overrides `primary` — with the affected documents' **bytes unchanged**. Because
`dedup_role` and the resulting `salience_tier` are not part of `config_version` and the documents'
`content_hash` is unchanged, this flip rotates **no chunk IDs** and requires **no re-embed**. If
left unhandled, the previously-primary document's chunks would remain at `primary`/`supporting`
tier and be served by default retrieval alongside the new primary — the "stale content served as
authoritative" failure (determinism review attack 5).

Handling: the primacy flip runs a **metadata-only update job** (§7.2) that re-bakes
`salience_tier` on the affected documents' chunks in place — the newly-superseded document's chunks
become `excluded`-tier, the new primary's chunks take full tier. Bytes and IDs are unchanged, so no
full rebuild is needed. A §18.3-style test SHOULD assert that after a primacy flip, the
superseded-document chunks are `excluded`-tier and absent from default retrieval. See
[inventory.md](../contracts/inventory.md) version-family section.

---

## 8. Incremental upsert vs full rebuild

> **PROPOSED — pending product-owner ruling on OQ-L-10.** This entire section describes the
> orchestrator's proposed resolution of the incremental-upsert / C-4 / §10.5 conflict. The v1
> DEFAULT below (clone-and-swap) honors both MUSTs; the opt-in direct-to-live mode explicitly
> relaxes them. The spec-text disposition of C-4 still requires owner sign-off (see OQ-L-10 and the
> decision ledger).

### 8.1 When incremental upsert applies

Incremental upsert applies when:
1. The ingestion config (including embedding model and chunking parameters) is unchanged — so
   `config_version` is unchanged.
2. Only a subset of documents have changed content (detected via content hash).
3. No documents have been deleted (or if deletions are pending, they are applied as part of the same incremental run).

### 8.2 v1 DEFAULT — clone-and-swap incremental (shadow)

The default incremental path is **clone-and-swap**, and it satisfies C-4 (ingestion writes only to
a shadow) and §10.5 (no partial-state window) exactly as a full rebuild does:

1. **Snapshot live → shadow.** The current live collection is cloned into a new shadow collection
   (new `build_id` per §2.1). The shadow is a coherent copy of live; the live collection is not
   touched.
2. **Apply document-granular replaces in the shadow.** For each changed document, replace-by-document
   is applied *to the shadow*: delete all points whose payload `provenance.source_document_id`
   matches the document, then write the new version's chunks. Chunk IDs and vectors come from the
   **frozen Segment set artifact** for `(document_id, content_hash, config_version)`
   ([segment-set.md](../contracts/segment-set.md) "Determinism and freezing" — Build consumes the
   frozen artifact, it does not re-run Decompose).
3. **Lightweight post-upsert checks (per OQ-L-9).** Run the light validation on the shadow: orphan
   scan (§9.3) and chunk-count delta within expected bounds for the changed documents, plus a log
   entry. The full eval baseline is not run per incremental (too expensive); it runs on the
   drift-detection cadence (§9.4).
4. **Atomic alias retarget.** Promote the shadow with the same two-phase atomic alias swap as a full
   build (§4). Because the alias resolves to exactly one collection at any instant, **there is no
   dual-visibility window**: a query sees the old collection or the new one, never a mix. The
   incremental path is thereby covered by §18.3 test 1 (alias swap under load — zero stale reads),
   which the direct-to-live path evaded.

**Cost amortization.** Cloning per single-document change would be wasteful, so change sets are
**debounced**: pending replaces are batched and one clone-and-swap serves the batch. This keeps the
per-swap cost bounded while preserving the no-partial-state guarantee.

### 8.3 Opt-in performance mode — direct-to-live upsert

Direct-to-live upsert is an **explicitly opt-in** performance mode, not the default. When enabled,
replace-by-document is applied directly to the live collection: the new version's chunks are
written and the old chunks (matched by `source_document_id`) are deleted in a sequential two-step
(Qdrant offers no cross-point transaction; OQ-L-4).

This mode **relaxes §10.5's no-partial-state window to the duration of the delete step** — during
that window a query can momentarily see both the old and new version of the changed document — and
it **bypasses C-4** (it writes to live, not to a shadow) and the pre-promotion validation gates
(there is no alias swap, so §18.3 test 1's protection does not apply). Enabling it is a deliberate
trade of the §10.5/C-4 guarantees for lower per-change latency and cost, and the UI/config MUST
state this trade-off plainly at the point of opt-in.

### 8.4 Orphan detection

After every incremental ingestion run (either mode), the platform scans for orphaned chunks:
chunks in the target collection whose `source_document_id` no longer corresponds to a document in
the corpus inventory, or whose chunk ID is not present in the set of IDs that the current config
would generate for that document.

Non-zero orphans is an alert, not a log line (§10.5). The alert names the orphaned document IDs and the orphan count. Orphans are removed as part of the alert resolution flow. The operator can also run `corpus orphan-scan --kb <id> --fix` to resolve manually.

---

## 9. Chunk identity and lifecycle consequences

Chunk IDs are deterministic and derived from stable inputs. The full derivation is specified in `docs/contracts/chunk.md`. This page covers only the lifecycle consequences.

### 9.1 Why deterministic IDs matter

Random or sequence-assigned IDs make correct incremental updates impossible. Without deterministic IDs:
- You cannot identify which chunks to delete when a document is updated.
- You cannot detect orphaned chunks without a full scan.
- You cannot resume an interrupted ingestion without risk of duplication.

With deterministic IDs, the replace-by-document operation is safe: recomputing the document's chunks under the current config (from the frozen Segment set artifact) gives you the exact set of IDs to **write**. **Removal, however, is keyed on `document_id`, not on recomputed IDs.** Build deletes *all* points whose payload `provenance.source_document_id` matches the document, then writes the new version's chunks in the same operation. This is essential: a heading rename or a mid-segment insertion changes `content_hash` and can change `segment_path`/`chunk_index`, so the old chunks' IDs are **not** reconstructable by "recompute the new IDs and delete those" — that naive strategy would orphan the prior version. Sweeping by `document_id` removes the prior version whatever its old IDs were (chunk.md states this correctly; determinism review attack 1). No lookup of the live collection's current contents is needed.

### 9.2 Config version in chunk identity

The config version is part of chunk identity (§10.5). This has one important consequence: **changing the config version forces a full rebuild** because there is no way to map old-config chunk IDs to new-config chunk IDs. The incremental upsert path is gated on config-unchanged; config-change always routes to a full shadow build.

For details on what constitutes a config version change and how the config version hash is derived, see `docs/contracts/chunk.md`.

### 9.3 Orphan detection after incremental runs

Every incremental run reports orphan count. The target is always zero. Non-zero orphans mean one of:
- A document was deleted from the source without the deletion being propagated to the platform.
- A bug in the replace-by-document logic left old chunks behind.
- A previous run was interrupted mid-delete.

All three are bugs or operational problems that require attention. Normalising non-zero orphans as acceptable is the failure mode §10.5 is designed to prevent.

---

## 10. Retention and rollback

### 10.1 Retention tiers

| Tier | State | Storage | Rollback method |
|---|---|---|---|
| Live (N) | `LIVE` | Qdrant memory | N/A — it is live |
| Hot standby (N-1) | `HOT_STANDBY` | Qdrant memory | Alias swap only — instant, no restore |
| Cold (N-2, N-3, …) | `COLD` | Object storage | Restore → tombstone replay → validate → promote |

Default retention: 1 hot standby + 2 cold snapshots. The executor may propose a different default; this is flagged in open question OQ-L-5.

### 10.2 Memory cost display

Qdrant's HNSW index lives in memory. Each hot copy costs approximately the same memory as the live collection. The UI MUST display the estimated memory cost of the current hot-copy configuration when the setting is displayed, and MUST update the estimate when the operator changes the hot-copy count before they save. The estimate is based on the collection's current vector count × dimensions × bytes-per-float, plus HNSW graph overhead (documented Qdrant formula; see open question OQ-L-6).

### 10.3 Instant rollback (N-1)

Rollback to N-1 is an alias swap. It follows the same two-phase protocol as promotion (§4). After rollback:

- The former N-1 becomes the new live collection.
- The former live collection enters `HOT_STANDBY` as the new N-1.
- No data is moved or rebuilt.

§18.3 test 6 asserts that after rollback, the alias serves correctly (queries return the expected chunks from the N-1 collection). The test exercises the model identity check: if the N-1 collection was built with a different embedding model, the retrieval service must also roll back the embedding provider to the one matching N-1 — or fail closed if the N-1 model is no longer available. See open question OQ-L-7.

### 10.4 Cold restore

Restoring a cold snapshot follows this sequence, enforced by the state machine:

1. Operator initiates restore via `corpus restore --kb <id> --build <build_id>` or via the UI.
2. Platform downloads the snapshot from object storage and loads it into Qdrant as a new collection (does not reuse the original collection name — a new collection is created using the naming scheme in §2.1 with a new build ID assigned by the control plane).
3. Collection enters `TOMBSTONE_REPLAY`: the tombstone log (§11) is applied in full. Every deletion that occurred since the snapshot was taken is reapplied to the restored collection. This step is not optional and cannot be skipped.
4. Tombstone replay complete → collection enters `VALIDATING`. The standard validation gates run.
5. Validation passes → collection enters `PROMOTING`. Standard two-phase alias swap.

If tombstone replay fails (tombstone log record is corrupted or missing), the restore is aborted and the operator is alerted with the specific failure. The restored collection is retained as `VALIDATION_FAILED` for inspection. See open question OQ-L-8.

### 10.5 Tombstone log

The tombstone log is an append-only record in the control-plane database. Each entry records:

| Field | Description |
|---|---|
| `tombstone_id` | Monotonically increasing sequence ID |
| `kb_id` | Knowledge base |
| `deletion_type` | `document` \| `knowledge_base` \| `workspace` |
| `subject_id` | **The deletion key: `document_id` (plus `kb_id`/`workspace_id` scope), never a chunk ID.** For document deletions this is the `document_id`. |
| `chunk_ids_affected` | Chunk IDs observed at deletion time — **audit record only, never the deletion key.** Config-version-fragile (see below); retained for the erasure audit trail, not used to drive removal. |
| `created_at` | When the deletion was requested |
| `applied_to_builds` | List of build IDs the deletion has been applied to (updated as each collection is updated) |

**The tombstone keys on `document_id`, and replay deletes by payload `source_document_id` match**
— never by `chunk_ids_affected`. `document_id` is **config-invariant**: it survives every
`config_version` rotation, whereas a chunk's ID includes `content_hash` and `config_version`, so
`chunk_ids_affected` recorded under one config will **not** match the same document's chunks under
a different config. Keying replay on chunk IDs would silently fail to delete when a config change
occurred between the deletion and the collection being swept (determinism review attack 6; S-R10).
Because removal is a `source_document_id` payload-filter delete, it removes the document's chunks
from **any** collection (live, N-1, and a restored cold snapshot) regardless of the config version
those chunks were built under.

The tombstone log is never truncated, even after all known copies of the deleted content have been destroyed. This provides an auditable record for right-to-erasure requests.

During restore (§10.4 step 3), tombstone entries for the knowledge base created after the snapshot's `promoted_at` timestamp are replayed against the restored collection **by `source_document_id` payload-filter delete** (keyed on `subject_id`), so the deletion applies even if the snapshot was built under a different `config_version` than the one live at deletion time. Entries older than the snapshot are already reflected in the snapshot.

---

## 11. Zero-downtime acceptance tests

Two of the §18.3 non-negotiable tests are acceptance criteria for this design. They are described here as the required observable behaviour that the implementation must produce; the test implementation lives in `tests/`.

### Test 1: Alias swap under sustained load — zero errors

**Setup:** a retrieval endpoint is serving a knowledge base. A load generator sends 100 concurrent queries per second sustained against the alias. A reindex is triggered simultaneously.

**Required behaviour:** from the moment the reindex trigger fires to 30 seconds after the alias swap completes, the load generator records zero HTTP errors (no 5xx, no connection errors, no timeout errors). Query latency may temporarily increase during the swap but must not exceed the §4.5 p99 target of 800 ms.

**Why this test validates the design:** it confirms that the two-phase alias swap (§4) eliminates the error window, that in-flight requests complete against the old collection while new requests resolve to the new collection, and that the retrieval service's model-identity cache invalidation does not cause a window of incorrect mismatch errors.

### Test 6: Rollback serves correctly

**Setup:** a knowledge base has a live collection (N) and a hot standby (N-1). A set of known queries has known expected results against N-1.

**Required behaviour:** after a rollback (alias swap to N-1), all known queries return their expected results. No stale results from N are returned. The model identity for the alias resolves to N-1's model identity after the rollback completes.

**Why this test validates the design:** it confirms that the alias swap correctly retargets to N-1, that the control-plane alias record is updated atomically, that the retrieval service's cached model identity is invalidated, and that the N-1 collection's data is intact and queryable.

---

## 12. Deletion and the tombstone log

This section summarises the deletion-completeness requirement from §17.1 and how it interacts with the lifecycle.

### 12.1 What "deleted" means

When a document is deleted from a knowledge base:
1. The tombstone log is appended, **keyed on the document's `document_id`** (plus kb/workspace
   scope), with the timestamp. The chunk IDs observed at deletion time are recorded as an audit
   field only (§10.5), not as the deletion key.
2. The chunks are removed from the live collection by `source_document_id` payload-filter delete.
3. The chunks are removed from the N-1 (hot standby) collection by the same `source_document_id`
   filter — config-invariant, so N-1 chunks built under a different config are still swept (S-R10).
4. Augmentation fields (stored in separate payload fields) are removed alongside the chunk records.
5. **Eval set questions derived from this document are REMOVED** (§17.1 is a MUST: derived eval
   questions are removed on deletion). A question is derived from this document if **any** of its
   `source_segment_ids` belong to the deleted document. On removal, the eval set version increments
   and the removal is recorded in the audit log — the **count and the removed question IDs**, not
   the question content. There is **no** `source_deleted` retained-with-flag state (R7 supersedes
   the earlier "marked `source_deleted`, not removed" wording, which contradicted §17.1; W-6/W-7).
   See [eval-set.md](../contracts/eval-set.md) removal semantics.
6. The document's `document_status` is set to `deleted` in the control-plane inventory (see
   [inventory.md](../contracts/inventory.md) `InventoryItem.document_status`). It does not appear in
   findings reports or the UI.

Cold snapshots (§10.1) contain the deleted content. They are not immediately modified. The tombstone log ensures that any restore from cold storage replays the deletion before promotion. For right-to-erasure requests that cannot wait for the next natural restore cycle, a `purge` operation destroys the affected snapshots.

### 12.2 Purge vs delete

| Operation | Scope | Snapshots | Audit record |
|---|---|---|---|
| `delete` | Live + N-1 | Untouched; deletion reflected on restore via tombstone | Tombstone log entry |
| `purge` | Live + N-1 + all cold snapshots | Destroyed for affected documents | Tombstone log entry + purge record |

The distinction between "deleted from service" and "purged from all copies" is visible in the KB's deletion history view. An operator cannot present the first as the second.

§18.3 test 8 asserts deletion completeness: deleted content is absent from live, from N-1, and — after a snapshot restore — from the restored collection.

---

## Open questions

| ID | Question | Proposed default | Flagged |
|---|---|---|---|
| OQ-L-1 | The two-phase alias swap has a window where Qdrant has been updated but the control-plane record has not. Recovery relies on comparing states at startup. Should a distributed transaction (saga with compensation) be used instead, or is the startup-reconciliation approach sufficient given that the window is typically sub-second? | Startup reconciliation is sufficient for v1. The control plane checks alias consistency on startup and after any promotion attempt. The saga pattern is correct but requires significant additional infrastructure (transaction log, compensating transactions). Flag for revisit if the inconsistency window causes operational problems. | YES |
| OQ-L-2 | How long should a `VALIDATION_FAILED` shadow collection be retained before it is automatically purged? Too long wastes Qdrant memory; too short makes diagnosis impossible. | 7 days, then automatic cold snapshot and deletion from Qdrant. The operator is notified 24 hours before automatic purge. The shadow is tagged so it is never accidentally promoted. | YES |
| OQ-L-3 | When the segment type taxonomy is revised (new types added, types merged), does this constitute a config version change that forces a full rebuild? For type additions, existing chunks simply don't have the new type and don't need to be invalidated. For type merges, existing chunks may be incorrectly typed. | Type additions: no forced rebuild; existing chunks are unaffected. Type merges or renames: forced rebuild, because the classification of existing chunks may be wrong. The config version hash should include a `taxonomy_version` field that the executor increments only on merge/rename operations. | YES |
| OQ-L-4 | Qdrant does not currently support multi-document transactional writes (replace-by-document atomicity). The delete of old chunks and insert of new chunks is a sequential two-step. Is a partial state (new chunks written, old chunks not yet deleted) tolerable briefly, or must the operation be protected against concurrent reads seeing both versions? | **Applies ONLY to the opt-in direct-to-live mode (§8.3)**, not the v1 default. Under §8.2 clone-and-swap, the replaces happen in a shadow and promotion is an atomic alias swap, so there is **no** dual-visibility window. In the opt-in direct-to-live mode, the brief partial state is tolerable-by-acknowledgement: both old and new chunks are momentarily visible, bounded by the delete step; the opt-in docs state this relaxes §10.5 (§8.3). | YES |
| OQ-L-5 | The spec says "Default: 1 hot + 2 cold. Executor may propose otherwise." Is 1+2 the right default for the reference hardware target (single-node Docker Compose)? The N-1 collection doubles the in-memory index size. For a corpus approaching the 5M-chunk reference scale, this is significant. | 1 hot + 2 cold is the specified default. The memory-cost display (§10.2) makes this explicit to the operator. For the reference hardware target, document this as requiring roughly 2× the baseline index RAM. The operator can reduce to 1+1 or 0+N if RAM is constrained; the UI warns when hot copies are reduced below 1 that instant rollback is unavailable. | YES |
| OQ-L-6 | The Qdrant HNSW memory formula (vectors × dimensions × 4 bytes + HNSW graph) is an approximation that can vary based on quantization settings and HNSW parameters (m, ef_construct). How precisely should the memory estimate be presented? | Present as "approximately X GB" with a note that the estimate assumes no quantization and default HNSW parameters (m=16). If the KB is configured with scalar or product quantization, the estimate is adjusted using Qdrant's documented compression ratios. Flag if the actual usage diverges from the estimate by >20% in testing. | YES |
| OQ-L-7 | During rollback to N-1, if N-1 was built with an embedding model that is no longer configured (e.g., the operator removed the OpenAI provider), queries will fail the model-identity check even after rollback. Should the rollback be blocked in this case, or should the rollback succeed and queries fail closed with a clear error? | Block the rollback with an error message that names the missing model and instructs the operator to re-configure it. A successful alias swap to a collection whose model is unavailable would result in every query failing, which is operationally worse than a blocked rollback. | YES |
| OQ-L-8 | If the tombstone log has gaps (e.g., the database containing it was restored from a backup that predates some deletion events), the cold restore cannot guarantee complete deletion replay. What is the recovery procedure? | Flag this as a critical data integrity condition. The restore is aborted. The operator must manually verify what deletions are missing, apply them, and then re-initiate the restore. The purge operation (§12.2) is the safe path for right-to-erasure cases where tombstone completeness cannot be guaranteed. Document the tombstone log backup requirements in the operations runbook. | YES |
| OQ-L-9 | The spec does not specify whether incremental upserts (§8) bypass the pre-promotion validation gates. Since incremental upserts write to the live collection directly (not a shadow), the validation gates do not apply by definition. Should a post-upsert validation step be run instead (lighter than the full gate suite)? | Yes: run a lightweight post-upsert check after every incremental run: orphan scan (§9.3), chunk count delta within expected bounds for the changed documents, and a log entry for the operation. Do not run the full eval baseline after every incremental upsert (too expensive for frequent small changes). Schedule a full validation run on the configured drift-detection cadence (§9.4). | YES |
| OQ-L-10 | **Orchestrator-raised (review finding): the incremental-upsert design conflicts with two spec MUSTs and needs a product-owner ruling, not a design rationale.** (a) Constraint C-4: "the ingestion path MUST write to a shadow collection, never to the live one." (b) §10.5: "all chunks belonging to the previous version are removed in the same operation that writes the new ones; partial application is not an acceptable intermediate state." | **RULING PROPOSED — owner sign-off pending.** §8 has been rewritten (marked PROPOSED) so the v1 DEFAULT is **clone-and-swap incremental** (§8.2): snapshot live → shadow, apply document-granular replaces in the shadow, run the lightweight post-upsert checks (OQ-L-9), atomically retarget the alias. This honors C-4 and §10.5 and brings the incremental path under §18.3 test 1. **Direct-to-live upsert** is now an explicitly opt-in performance mode (§8.3) whose docs state it relaxes §10.5's no-partial-state window to the duration of the delete step and bypasses C-4. The remaining owner decision is the **spec-text disposition of C-4** — whether to amend C-4's literal wording or record the opt-in as a documented exception. Recorded as a PROPOSED ledger row for sign-off. | **ruling proposed, owner sign-off pending** |
