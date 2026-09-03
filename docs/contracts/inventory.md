# Contract 1 — Inventory

Stage boundary: **Collect → Assess**. Governing spec: §6.1, §12 (Inventory invariants), §14.3.

The Inventory is the durable record of *what files exist*, their stable identities, their
content hashes, their source-system metadata, and the relationships between them (exact
duplicates and version families). It is produced by Collect and consumed by Assess. Web links
found inside documents are recorded here as references with their fetch policy applied — links
are not content (§6.1).

Root model: `Inventory`. It carries the shared [`TenancyBlock`](README.md#tenancy-block) and a
list of `InventoryItem` records, plus link records and relationship records.

---

## `Inventory` (root)

| Field | Type | Required | Semantics |
|---|---|---|---|
| `schema_version` | `str` (semver) | yes | Contract version (see [versioning](README.md#contract-versioning)). |
| `tenancy` | `TenancyBlock` | yes | Owning workspace/KB and default permission facts. |
| `collected_at` | `datetime` (UTC) | yes | When this Collect run completed. |
| `source_run` | `SourceRun` | yes | The Collect job that produced this inventory (source kind, connector id, cursor). |
| `items` | `list[InventoryItem]` | yes | One record per discovered file. May be empty (empty corpus is valid, reported). |
| `links` | `list[LinkRecord]` | yes (may be empty) | Web links found inside documents, with fetch policy applied. |
| `duplicate_groups` | `list[DuplicateGroup]` | yes (may be empty) | Exact-duplicate clusters by content hash. |
| `version_families` | `list[VersionFamily]` | yes (may be empty) | Near-duplicate clusters representing document version families (§6.1). |

## `SourceRun`

| Field | Type | Required | Semantics |
|---|---|---|---|
| `source_kind` | `enum{upload, directory, s3, sharepoint, confluence, jira, gdrive}` | yes | Where files came from (§6.1). |
| `connector_id` | `str` | no | Which configured connector instance, if any. Not the credential (§14.2). |
| `incremental_cursor` | `str` | no | Source-side change cursor for incremental connectors (§6.1). Opaque. |
| `acknowledged_permission_gap` | `bool` | no | Set when the operator acknowledged an `unavailable` permission-fidelity connector (§14.3). |

## `InventoryItem`

| Field | Type | Required | Semantics |
|---|---|---|---|
| `document_id` | `str` (ULID) | yes | **Stable file identity.** Assigned once, stable across re-collections of the same logical file (see identity invariant). Becomes `Provenance.source_document_id`. |
| `content_hash` | `str` (sha256 hex) | yes | Hash of the raw bytes. Becomes `Provenance.source_document_version`. Basis of exact-dedup and change detection (§6.1, §10.3). |
| `source_path` | `str` | yes | Canonical path/URI within the source system (e.g. `s3://bucket/key`, `/mnt/corpus/manual.pdf`, Confluence page URL). |
| `display_name` | `str` | yes | Human-facing name for reports and citations. |
| `media_type` | `str` (MIME) | yes | Detected MIME type. Drives parser selection in Assess. |
| `declared_extension` | `str` | no | File extension as given by source, retained even when it disagrees with `media_type`. |
| `size_bytes` | `int` | yes | Raw size. |
| `source_metadata` | `dict[str, JSON]` | yes (may be empty) | Source-system metadata (§6.1): author, created/modified in source, SharePoint/Confluence properties, labels. Free-form but never secrets (§14.2). |
| `source_created_at` | `datetime` | no | Timestamp from source system. |
| `source_modified_at` | `datetime` | no | Timestamp from source system. Distinguishes content vs metadata change (§10.3). |
| `discovered_at` | `datetime` (UTC) | yes | When Collect first saw this file. |
| `source_permissions` | `SourcePermissions` | no | Raw source-side ACLs when a connector supplies them (§14.3), before resolution into `TenancyBlock.permission_principals`. |
| `dedup_role` | `enum{unique, exact_duplicate, primary, superseded}` | yes | Role in dedup/version relationships. `superseded` = an older version-family member. **Default (D-25, owner ruling 2026-09-03):** produces no segments and no chunks; inventoried, retained in object storage, and reported in the exclusion report with reason "superseded by \<primary document_id\>". When `ingestion.dedup.index_superseded_versions=true`, decomposed and indexed at tier `excluded`. |
| `dedup_group_id` | `str` | no | The `DuplicateGroup` or `VersionFamily` this item belongs to, if any. |
| `collect_status` | `enum{collected, unreadable, access_denied, too_large, skipped_policy}` | yes | Outcome of collection. Failures are represented, not dropped (§6 rule 6). `unreadable` includes password-protected files (detail in `status_detail`). |
| `status_detail` | `str` | no | Reason string for any non-`collected` status. Feeds the exclusion report. |
| `document_status` | `enum{active, superseded, deleted}` | yes | **Lifecycle state of the document in the KB** (distinct from `collect_status`, which is a collection-time outcome). `active`: in the index (or eligible), full participation. `superseded`: a version-family member the newest version replaced — retained in object storage, reported in the exclusion report; **default (D-25, 2026-09-03):** produces no segments and no chunks; when `ingestion.dedup.index_superseded_versions=true`, indexed at `excluded` tier and flippable via the primacy path (index-lifecycle.md §7.5). `deleted`: removed from the KB after collection (§17.1) — its chunks are swept from all collections and it does not appear in findings/UI. Set to `deleted` by the deletion flow (index-lifecycle.md §12.1); resolves W-5, where a deleted document was previously indistinguishable from an active one. |

## `SourcePermissions`

| Field | Type | Required | Semantics |
|---|---|---|---|
| `principals_read` | `list[str]` | yes | Source-side principals with read access. |
| `fidelity` | `enum{authoritative, best_effort, unavailable}` | yes | Reliability of the source ACL data (§14.3). Maps into `TenancyBlock.permission_fidelity`. |
| `raw` | `dict[str, JSON]` | no | Verbatim source ACL payload retained for audit. |

## `LinkRecord`

| Field | Type | Required | Semantics |
|---|---|---|---|
| `link_id` | `str` (ULID) | yes | Identity of this link occurrence. |
| `found_in_document_id` | `str` | yes | Document the link appeared in. |
| `location` | `SourceLocation` | yes | Where in the document the link was found. |
| `url` | `str` | yes | The referenced URL. |
| `fetch_policy` | `enum{ignore, snapshot, crawl}` | yes | Per-KB policy applied (§6.1). `ignore` is the default. |
| `fetch_status` | `enum{not_fetched, snapshotted, crawled, fetch_failed, blocked_by_allowlist}` | yes | Outcome. `not_fetched` for `ignore`. |
| `snapshot_artifact_id` | `str` | no | Object-store id of the dated snapshot when `snapshot`/`crawl` applied. |
| `snapshot_fetched_at` | `datetime` | no | When snapshot was taken; marks the snapshot as potentially stale (§6.1). |
| `crawl_depth` | `int` | no | Depth at which a crawled link was reached (bounded, §6.1). |

## `DuplicateGroup`

| Field | Type | Required | Semantics |
|---|---|---|---|
| `group_id` | `str` (ULID) | yes | Group identity. |
| `content_hash` | `str` | yes | The shared hash defining exact duplication. |
| `member_document_ids` | `list[str]` | yes | Items with identical bytes. |
| `primary_document_id` | `str` | yes | The one retained for processing; others are `exact_duplicate`. |

## `VersionFamily`

| Field | Type | Required | Semantics |
|---|---|---|---|
| `family_id` | `str` (ULID) | yes | Family identity. |
| `member_document_ids` | `list[str]` | yes | Near-duplicate members (§6.1). |
| `primary_document_id` | `str` | yes | Newest member, treated as primary (§6.1). |
| `superseded_document_ids` | `list[str]` | yes | Older members, retained in object storage. **Default (D-25, 2026-09-03):** not indexed; appear in exclusion report. When `ingestion.dedup.index_superseded_versions=true`: indexed at tier `excluded` (§6.1). |
| `similarity_method` | `str` | yes | How near-duplication was determined (e.g. `minhash`, `simhash`), for auditability. |
| `similarity_scores` | `dict[str, float]` | no | Per-member similarity to primary, for inspection and override. |
| `primacy_basis` | `enum{source_modified_at, discovered_at, filename_version, manual}` | yes | Why `primary` was chosen newest — recorded so a wrong pick is explainable and overridable. |

**Superseded document handling (owner ruling 2026-09-03, D-25).** By default
(`ingestion.dedup.index_superseded_versions=false`), superseded members produce no segments and no
chunks. They are inventoried, retained in object storage, and appear in the exclusion report with
reason "superseded by \<primary document_id\>". An `ExclusionRecord`-equivalent entry is written
for each superseded member. When the toggle is `true`, superseded members are decomposed and
indexed at salience tier `excluded` (recoverable via explicit filter).

**Primacy flip (R5).** When the family's `primary` changes — a newer member arrives or an operator
overrides — the change is **payload-only, not a rebuild**: the members' `dedup_role` and
`document_status` flip (the new primary → `active`/`primary`; the previously-primary member →
`superseded`). If the superseded member was previously indexed (toggle was `true` or the member was
the prior primary with full-tier chunks), a **metadata-only update job re-bakes `salience_tier`**
on the affected documents' chunks in place (bytes and chunk IDs unchanged, no re-embed). This is
the fifth reindex trigger in [index-lifecycle.md](../architecture/index-lifecycle.md) §7.5 and
prevents the just-superseded document's chunks from continuing to be served at full tier
(determinism review attack 5). If the toggle is `false`, the newly-superseded member's chunks are
swept and the member is added to the exclusion report.

---

## Invariants

- Every item has a stable `document_id`, a `content_hash`, a `source_path`, and
  `source_metadata` (§12 Inventory invariant).
- Duplicate and version-family relationships are **explicit**: every item's `dedup_role` is set,
  and any non-`unique` role points to a group/family that lists it (§12).
- In a `VersionFamily`, exactly one member is `primary`; all others are `superseded`. Superseded
  members are **retained** in object storage, never deleted (§6.1, design principle 1). **Default
  (D-25, 2026-09-03):** superseded members produce no segments/chunks and appear in the exclusion
  report. When `ingestion.dedup.index_superseded_versions=true`, they are decomposed and indexed at
  tier `excluded`. A `superseded` member carries `document_status=superseded`; a primacy flip
  updates `dedup_role`, `document_status`, and (where applicable) the baked `salience_tier` via the
  metadata-only path (R5).
- `document_status` is present on every item and reflects lifecycle state (`active`/`superseded`/
  `deleted`), distinct from the collection-time `collect_status`.
- Failed and unreadable files are present with a `collect_status` and a reason, not omitted
  (§6 rule 6). Password-protected files appear as `unreadable`.
- `crawl` links MUST have been produced under a config with a domain allowlist; a link with
  `fetch_status=crawled` and no allowlist context is a config-validation failure upstream (§6.1)
  and MUST NOT appear here.
- `tenancy` is present (Phase 0 MUST, §19).

## Golden-corpus expressibility

- **Near-duplicate family fixture:** one `VersionFamily` with `primary` = newest and the rest
  `superseded`; `similarity_scores` populated; `primacy_basis` recorded.
- **Unservable files fixture:** present as `InventoryItem`s that collect fine here; their
  unservability is determined later (Assess/Plan). Password-protected members appear as
  `unreadable` at this stage already.
- **Confluence-style HTML export:** `source_kind=confluence` (or `upload` if uploaded), with
  page properties in `source_metadata` and page URL as `source_path`.
- **Spreadsheets:** three `InventoryItem`s here; triage into report/database/model happens at
  Plan, not Collect.

## Open questions

1. **`document_id` stability across re-collection.** How a re-collected file is recognized as the
   same logical document (to reuse its `document_id`) when its path or bytes change. **Proposed
   default:** identity keyed on `(source_kind, connector_id, source_path)`; a changed
   `content_hash` at the same key is a *new version of the same document* (reuses `document_id`,
   new `content_hash`), enabling replace-by-document (§10.5). A moved file is a new document
   unless a connector supplies a stable source id. **Flagged** — connectors that expose stable
   source ids (Google Drive file id, Confluence page id) should key on those; needs per-connector
   design in Phase 6.
2. **Near-duplicate threshold.** The similarity cutoff for version-family membership. **Proposed
   default:** a configurable threshold per KB with a conservative global default; recorded in
   `similarity_method`. **Flagged** — too loose merges unrelated docs, too tight misses families.
3. **Whether `snapshot` link content ever becomes an `InventoryItem`.** A snapshotted link is a
   dated artifact; does it enter the pipeline as a document? **Proposed default:** no — snapshots
   are referenced artifacts, not corpus documents, in v1; promoting them to documents is a
   post-v1 feature. **Flagged.**
4. **Multi-membership.** Can a file be both an exact duplicate and a version-family member?
   **Proposed default:** exact-dedup runs first; only the exact-dedup `primary` participates in
   version-family clustering, so an item has at most one `dedup_group_id`. **Flagged.**
