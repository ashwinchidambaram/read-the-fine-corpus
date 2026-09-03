# Determinism-Breaker Review — Chunk Identity, Replace-by-Document, Deletion Completeness

**Reviewer role:** adversarial. Goal: construct concrete counterexamples that break chunk-ID
determinism (§10.5), replace-by-document correctness (§10.5), or deletion completeness (§17.1).

**Derivation under attack (chunk.md):**
```
canonical = document_id ␟ content_hash ␟ config_version ␟ segment_path ␟ str(chunk_index)
chunk_id  = "chk_" + base32_nopad(sha256(canonical))[:26]
point_id  = UUID(bytes=sha256(canonical)[:16])
```
- `document_id` — Inventory ULID, stable across re-collections of the same logical file.
- `content_hash` — sha256 of raw source bytes; document-WIDE (rotates every chunk on any edit).
- `config_version` — sha256 of build-affecting config fields; **excludes** retrieval-treatment.
- `segment_path` — `"/".join(structural_path) + "#" + within_heading_ordinal` (segment-set.md OQ-1/OQ-1).
- `chunk_index` — 0-based position within the segment.

Replace-by-document keys removal on `provenance.source_document_id` (= `document_id`), atomic with
the write. Tombstones record BOTH `subject_id` (document_id) AND `chunk_ids_affected`
(index-lifecycle §10.5).

---

## Attack 1 — Heading rename (body text unchanged)

**Scenario.** Manual §3 heading "Lubrication" is renamed to "Greasing". The body paragraph under it
is byte-identical. All other sections untouched.

**Trace.**
- A heading rename is a **byte edit to the document**. Inventory recomputes `content_hash`:
  `9f2c…ae → 7a10…`. Per chunk.md case (c), `content_hash` is document-wide, so **every** chunk of
  the document gets a new `chunk_id` regardless of whether its own text changed.
- `segment_path` for the renamed section: `structural_path` was `["Maintenance","Lubrication"]`,
  now `["Maintenance","Greasing"]`. So `segment_path` changes from `Maintenance/Lubrication#3` to
  `Maintenance/Greasing#3` — a second, independent reason the affected chunks' IDs move. (Note the
  heading itself is not independently chunked — taxonomy OQ-1 — so the heading text lives only in
  the breadcrumb `structural_path`, which feeds `segment_path`.)
- Removal is keyed on `document_id`, which is **unchanged** by an edit (Inventory OQ-1: a changed
  `content_hash` at the same `(source_kind, connector_id, source_path)` key reuses the
  `document_id`). Replace-by-document deletes all points with `provenance.source_document_id ==
  document_id`, then writes the new version's chunks in the same atomic op.

**Does anything stale survive?** No. Because removal sweeps by `document_id`, not by matching the
old IDs, it removes the prior version's chunks *whatever their old `content_hash` or `segment_path`
were*. Even the pathological case — a heading rename changes both `content_hash` AND `segment_path`,
so the naive "recompute new IDs, delete those exact IDs" strategy would MISS the old chunks — is
saved because the design explicitly keys removal on `document_id`, not on ID-matching (chunk.md
design note; index-lifecycle §9.1 "recomputing the document's chunks gives you the exact set to
delete… The prior version's IDs are the exact set to delete" is an over-claim, but §8.2 and the
chunk.md design note make removal document-keyed, which is the correct and safe path).

**Is `segment_path` stable given OQ-1?** For a *fixed document version*, yes: the breadcrumb is a
deterministic function of parse structure and the within-heading ordinal counts segments in
`document_order`. It is NOT stable across the rename — but it does not need to be, because IDs are
only promised stable across re-runs of the *same* version.

**Verdict: HOLDS.** Property saved by: removal keyed on `document_id` (not ID-matching), so a
simultaneous `content_hash` + `segment_path` change cannot orphan the old version. §18.3 test 8
(deletion completeness under edit → prior version absent) covers it; §10.5's edit-leaves-no-prior
test (§18.2 deletion layer) is the direct guard.

**Caveat (not a break, but a latent bug magnet):** index-lifecycle §9.1's claim that "recomputing
the new IDs is sufficient to identify the old ones" is FALSE whenever `segment_path` or the segment
count changes between versions (rename, insertion, retyping). If an implementer follows §9.1
literally (delete-the-recomputed-old-IDs) instead of chunk.md's document-keyed sweep, Attack 1
BREAKS. This is a **documentation inconsistency that invites the exact stale-chunk bug §10.5
exists to prevent.** Recommend §9.1 be corrected to state removal is by `document_id`.

---

## Attack 2 — Identical sibling segments (collision probe)

**Scenario A:** two byte-identical tables in different sections ("Specs/Torque" and
"Appendix/Torque"). **Scenario B:** two byte-identical paragraphs in the SAME section, adjacent.

**Trace.**
- Scenario A: `document_id`, `content_hash`, `config_version` are shared (same document). The two
  tables differ in `segment_path`: `Specs/Torque#1` vs `Appendix/Torque#1`. Canonical strings
  differ → distinct digests → distinct IDs. No collision.
- Scenario B: same `segment_path` prefix. Are they two segments under one breadcrumb, or one
  segment? Per segment-set OQ-1, the within-heading ordinal "counts segments under the same
  breadcrumb in `document_order`." So two identical paragraphs = two segments with distinct
  ordinals: `Specs/Intro#1` and `Specs/Intro#2`. `segment_path` differs → distinct IDs.
- Sub-case: what if the two identical paragraphs land in **one** segment (chunker split)? Then
  `segment_path` is identical but `chunk_index` differs (0 and 1). Distinct canonical → distinct IDs.

**Can two chunks collide on one ID?** Only if `(document_id, content_hash, config_version,
segment_path, chunk_index)` collides for two distinct chunks. Given the `\x1f` unit separator that
"cannot occur in the component values," the tuple is injective into the canonical string. Two
distinct chunks would need identical segment_path AND identical chunk_index — which the
ordinal-in-segment_path + index-in-segment design forbids by construction, *provided* the
within-heading ordinal is truly unique per segment (dense/gapless `document_order` guarantees this;
segment-set §37). The only residual is the 130-bit truncation birthday collision (chunk.md OQ-3),
which fails loud on write, never silently overwrites.

**What a collision would do to replace-by-document:** it wouldn't affect removal (that's by
`document_id`), but on *write* two distinct chunks sharing an ID would mean the second silently
overwrites the first → content loss. chunk.md OQ-3 mandates fail-loud on point-ID collision at
write, so even the astronomically unlikely digest collision is caught, not silent.

**Verdict: HOLDS.** Property saved by: `segment_path` within-heading ordinal + `chunk_index` +
`\x1f` injective separator make the identity tuple unique per chunk; digest-truncation collisions
fail loud (OQ-3). §18.3 has no dedicated collision test; the property test asserting re-run
stability (chunk.md invariants) would surface a same-run ID clash. **Recommend** an explicit
property test: "no two chunks of one document share a `chunk_id`."

**Residual risk (flag, not break):** the uniqueness of the within-heading ordinal is only as good as
the determinism of `document_order` and segment boundary detection — which Attack 3 attacks directly.

---

## Attack 3 — Non-deterministic upstream classification (THE structural break)

**Scenario.** Same document version (`content_hash` fixed), same config (`config_version` fixed).
Build is run twice. Segment typing and tier assignment consult an **LLM signal**: taxonomy §4.1
"Class description… Processed via LLM classification against the description text," and §4.1 also
routes tier via LLM. On run 1 the LLM classifies a borderline block as `prose`; on run 2, as
`form_field` (or `table` vs `prose` on a text-wrapped grid).

**Trace.**
- `document_id`, `content_hash`, `config_version` are all identical across runs.
- BUT `segment_path` is derived from `structural_path` + within-heading ordinal, where the ordinal
  "counts segments under the same breadcrumb in `document_order`" (segment-set OQ-1). If the LLM's
  classification changes **segment boundaries or segment count** — e.g., run 2 splits one block
  into two segments, or merges two — then the within-heading ordinals of every later segment under
  that breadcrumb **shift**. `Maintenance/Lubrication#3` on run 1 becomes `Maintenance/Lubrication#4`
  on run 2.
- Different `segment_path` on the same stable inputs → **different `chunk_id`** for chunks that are
  textually identical. This directly violates §10.5's MUST: "The same document under the same config
  produces the same IDs on every run."
- Even if boundaries don't move, a **type change alone** can shift the ordinal if the ordinal counts
  only certain types, or can change `chunk_index` (a block retyped `table` chunks atomically vs
  `prose` chunked by `max_tokens` → different chunk count → different `chunk_index` range).

**What does the design pin down?** segment-set OQ-1 asserts `segment_path` is "deterministic for a
fixed document version" and OQ-3 says "Deterministic for a fixed document version." Taxonomy §11
records all firing signals in provenance. BUT nothing in the reviewed contracts pins the LLM call
itself to determinism: §7.3 exposes `temperature` and per-operation model overrides as *config*,
and even temperature=0 does not guarantee token-level determinism across model/provider versions,
batching, or hardware. The `config_version` folds in the *config* (which model, which temperature)
but NOT the model's *output*, which is where the nondeterminism lives.

**Is it enough?** No. The determinism claim in §10.5 is inherited by `segment_path`, and
`segment_path`'s determinism is *asserted* (OQ-1 "deterministic for a fixed document version") but
not *enforced*, because its upstream (Decompose classification) is an LLM whose output is not pinned
by any hashed input. The chain "stable inputs → stable segment_path → stable chunk_id" has an
un-pinned link: **classification output is a hidden input to `segment_path` that is neither in the
canonical string nor guaranteed reproducible.**

**Verdict: BREAKS.** Invariant failed: §10.5 "same document + same config → same IDs every run."
The break is realized as ID rotation from `segment_path`/`chunk_index` drift driven by
nondeterministic segmentation, with NO change to any of the five canonical inputs' *intended*
meaning. §18.3 has **no test that catches this** — test 3 (provenance completeness) and test 4
(byte-identity) pass regardless; the only guard is the chunk.md "property test asserts re-run
stability," and that test, *if it re-runs the full Decompose+Build pipeline*, WOULD catch it — but
if it re-runs only the ID derivation on already-fixed segment inputs (the more likely unit-test
shape), it MISSES it entirely. This is the most dangerous finding: it is silent under the actual
non-negotiable test list.

**Required design change:** Decompose output (the segment set: boundaries, types, `segment_path`
assignments) MUST be made deterministic by construction — either (a) segmentation/classification
uses no LLM (pure structural rules), or (b) the LLM-derived classification is **frozen into a
content-addressed segment-set artifact keyed on (`document_id`, `content_hash`, `config_version`)**
and reused on every subsequent Build for that key, so the LLM runs once per (version, config) and
its output becomes a cached, pinned input. Option (b) preserves §10.5 and is consistent with §15
resumability. The property test MUST re-run the full Decompose→Build pipeline (not just ID hashing)
and assert identical `chunk_id` sets across two cold runs.

---

## Attack 4 — Tier 3 rewrite (nondeterministic rewrite model)

**Scenario.** A class is Tier-3 opted-in (§7.2). A model rewrites a mangled table into clean
markdown. Rewrite model temperature > 0, or model version drifts between runs.

**Trace.**
- What is the rewritten chunk's `text`? Per chunk.md field `text`: "byte-identical to the source
  span **unless** a `tier=3, changed_text=true` transformation is recorded." So for a Tier-3 chunk,
  `text` = the **rewritten** bytes. The original is retained separately (§7.2 MUST, never overwritten).
- Does the rewritten `text` participate in the hash? **Check the canonical string:** it contains
  `content_hash` (= sha256 of **source document raw bytes**, per Inventory — NOT of chunk text),
  `segment_path`, `chunk_index`, `document_id`, `config_version`. The chunk's `text` is **NOT** a
  component of `chunk_id`. So a rewrite that changes `text` does **not** change `chunk_id`.
- If the rewrite model is nondeterministic: run 1 produces markdown A, run 2 produces markdown B
  (semantically same, byte-different). `content_hash` (source bytes) unchanged, `config_version`
  unchanged (config pins model_ref/temperature but not output), `segment_path`/`chunk_index`
  unchanged → **`chunk_id` identical across runs.** The vector and the returned `text` differ, but
  the ID is stable.

**Is that a break?** For **ID determinism** (§10.5): HOLDS — the ID is stable because `text` is not
hashed. For **retrieval reproducibility / provenance honesty**: it means two builds of the same
(version, config) can return *different chunk text and different vectors under the same ID* — a
distinct correctness problem (a citation to `chk_…` is not reproducible), but NOT one of the three
invariants under review, and §7.2 already fences Tier 3 (default off, per-class opt-in, diff
preview, flagged in provenance). It also weakens replace-by-document idempotency: re-running Build
over an unchanged Tier-3 document writes a *different* vector under the *same* ID — case (a)
idempotency ("re-ingestion is idempotent; no duplication") is technically violated for Tier-3
chunks (no duplication, but not identical output).

**Verdict: HOLDS (for the three named invariants), with a flagged idempotency erosion.** Property
saved by: `text` is excluded from the ID derivation, so rewrite nondeterminism cannot rotate IDs.
§18.3 test 4 (Tier 2 byte-identity) does NOT apply to Tier 3 (Tier 3 legitimately changes text).
**Recommend:** the same freeze-the-artifact fix as Attack 3 for Tier 3 rewrite output, so case (a)
idempotency holds byte-for-byte; and note that Tier-3 nondeterminism makes citations
non-reproducible unless the rewrite is cached per (version, config).

---

## Attack 5 — Near-duplicate primary flip

**Scenario.** VersionFamily F = {doc_A (superseded), doc_B (primary)}. A newer doc_C arrives (or
the user overrides `primary`). doc_B's own bytes are unchanged. Primary flips B → C (or B →
user-chosen A).

**Trace.**
- `dedup_role` is an **Inventory** field, and `salience_tier=excluded` for superseded members is a
  **SegmentSet/payload** field (taxonomy §4.1 explicit exclusion; OQ-11 resolved D-25, 2026-09-03:
  by default, superseded docs produce no chunks; when `ingestion.dedup.index_superseded_versions=true`,
  superseded → tier `excluded`, indexed-but-filtered — this attack applies in that toggled path).
- Neither `dedup_role` nor `salience_tier` is in the chunk-ID canonical string. Neither is in
  `config_version` (config_version excludes retrieval treatment AND does not include per-document
  dedup role). And doc_B's `content_hash` is unchanged.
- Therefore the primary flip rotates **no `chunk_id`**. doc_B's chunks keep their IDs; what changes
  is doc_B's chunks' `salience_tier` payload field (primary/supporting → `excluded`) and doc_C's
  chunks come into being at full tier.
- How does doc_B's salience_tier get updated to `excluded` without a rebuild? This is a
  **metadata-only change** in the §10.3 / index-lifecycle §7.2 sense IF salience is treated as
  retrieval-time payload: "metadata fields are updated in the existing chunks' payloads in-place,
  without a rebuild." But taxonomy §4.1 computes tier at **Decompose** time and writes it into
  provenance/payload — it is build-derived, not a free-floating source metadata tag.

**Can stale primary-tier chunks survive?** This is the real risk. If the primary flip only updates
`salience_tier` payloads in place (no reingest), and the in-place update misses doc_B's chunks (or
is not wired to fire on a dedup-role change at all — there is **no trigger** in §10.3/§7 for
"version-family primacy changed"), then doc_B's chunks remain at `primary`/`supporting` tier and
are returned by default retrieval alongside doc_C — **exactly the "stale content returned as
authoritative" failure §10.5 is about, but via salience rather than via missing deletion.**

**Verdict: BREAKS (partial / conditional).** No ID or replace-by-document invariant fails —
`document_id`-keyed removal and IDs are untouched. What fails is **completeness of the tier update
on a primacy flip**: there is no reindex trigger (§10.3 lists Manual, Change-detected,
Scheduled, Config-change — none fires on a version-family primacy change with unchanged bytes), and
salience is build-time-derived, so an in-place payload patch is out-of-band and unspecified. Result:
superseded content can be served at full tier. §18.3 has **no test** for primacy-flip tier
correctness (test 8 is deletion, not supersession). Severity: high in near-duplicate-heavy corpora
(a named golden fixture, §18.1).

**Required design change:** add a fifth reindex/repair trigger — "version-family membership or
primacy changed" — that re-derives and patches `salience_tier` on affected documents' chunks in
place (bytes/IDs unchanged, so no full rebuild needed), and add a §18.3-style test: after a primacy
flip, superseded-document chunks are `excluded`-tier and absent from default retrieval.

---

## Attack 6 — Tombstone replay vs config change

**Scenario.** t0: config_version = `C1`. doc_D ingested; chunks have IDs derived under `C1`.
t1: doc_D **deleted** (tombstoned). t2: config changes `C1 → C2` → full rebuild; the live/hot
collections now hold everything under `C2` (doc_D already gone). t3: an operator restores a **cold
snapshot from the `C1` era** (taken at t0.5, before the deletion, chunks keyed under `C1`).

**Trace.**
- What does the tombstone reference? index-lifecycle §10.5: it records **both** `subject_id`
  (= doc_D's `document_id`) AND `chunk_ids_affected` (the `C1`-era chunk IDs computed at deletion
  time, t1).
- Restore path (§10.4): snapshot loaded → `TOMBSTONE_REPLAY` → replay all tombstones with
  `created_at` after the snapshot's `promoted_at`. The doc_D tombstone (t1) is after the snapshot
  (t0.5), so it IS replayed.
- **Here is the trap.** If replay deletes by `chunk_ids_affected`, those IDs were computed under
  `C1`. The restored snapshot is *also* `C1`-era, so the IDs **match** → deletion succeeds. Good.
- But if the snapshot being restored were from a **`C2`-era** cold copy (config already bumped) and
  the tombstone's `chunk_ids_affected` were `C1`-era, the IDs would NOT match and replay-by-chunk-ID
  would **silently fail to delete** — a deletion-completeness break. In the scenario as stated
  (C1-era snapshot, C1-era tombstone IDs), they align, so it holds. The break is latent and
  triggered by any config change *between* a deletion and the snapshot being restored.
- The safety net: replay could delete by `subject_id` (document_id) instead of `chunk_ids_affected`.
  document_id is **config-invariant** — it survives every config rotation. If replay is
  document-keyed (matching payload `provenance.source_document_id == subject_id`), it removes doc_D's
  chunks under ANY config version. The tombstone stores document_id precisely for this. But the
  design stores `chunk_ids_affected` and **does not state which key replay uses.**

**Verdict: HOLDS for the stated scenario; BREAKS for the adjacent config-crossing scenario, and the
design is under-specified.** In the exact scenario (C1 snapshot + C1 tombstone IDs) deletion
completes → §17.1 satisfied, §18.3 test 8 passes. BUT the design does not pin replay to
`subject_id`, and `chunk_ids_affected` is config-version-fragile: any deletion followed by a config
change followed by restore of a post-config snapshot would leave the deleted document's chunks
resurrected if replay keys on chunk IDs. §18.3 test 8 as written ("delete → restore → absent")
would MISS this unless the test deliberately interleaves a **config change between deletion and
snapshot**, which the test description does not require.

**Required design change:** specify that tombstone replay deletes by `subject_id` (document_id),
config-invariant, and treat `chunk_ids_affected` as an audit record only, never the deletion key.
Extend §18.3 test 8 to interleave a config change between deletion and the restored snapshot.

---

## Attack 7 — Boilerplate strip + reassembly

**Scenario.** Tier 1 `boilerplate_strip` is enabled. A boilerplate segment's text is stripped from
the chunk. §12 also requires segments reassemble to the document.

**Trace.**
- Where does original text live? SegmentSet `Segment.text` = "byte-identical to source; Tier
  transformations happen at **Build**, not here" (segment-set §50). So the SegmentSet holds
  un-stripped text. Reassembly (`ReassemblyRecord.method = document_order_concat`) concatenates
  every segment's source text "including `excluded`/`boilerplate` segments" → reproduces
  **parse-level** text. segment-set OQ-3 explicitly pins the reassembly invariant to *pre-transform*
  (parse-level) text, "so it stays exact."
- Where does the strip happen? At **Build**, on the chunk's `text` field. taxonomy §2.1
  boilerplate: "Tier 1… default-strips boilerplate from the text field but **retains it in
  provenance so it can be restored**."
- Does stripping affect `content_hash`? `content_hash` = sha256 of **raw source bytes** (Inventory).
  Build never rewrites source bytes. So `content_hash` is unaffected by stripping → chunk IDs of a
  stripped chunk are still derived from un-stripped source identity. No ID drift from stripping.
- Does stripping affect the chunk `text` that is hashed for ID? `text` is not in the ID at all
  (Attack 4). So stripping changes the returned `text` and the `embedding_input`, but not the ID.
- Can both invariants hold? Reassembly is asserted against **parse-level** text (un-stripped,
  from SegmentSet.text); byte-identity of served chunk `text` is asserted against source **unless a
  Tier-3 changed_text record exists**. Boilerplate strip is **Tier 1**, not Tier 3 — so a stripped
  boilerplate chunk's `text` is NOT byte-identical to its source span, yet no Tier-3 record is
  present. **This is a contradiction with the chunk.md byte-identity invariant as literally
  worded** ("`text` equals the source span byte-for-byte unless a `tier=3, changed_text=true`
  record is present").

**Verdict: HOLDS for reassembly and ID stability; BREAKS the byte-identity invariant's wording for
Tier-1 boilerplate strip.** Reassembly is safe (parse-level, includes boilerplate segments,
OQ-3). IDs are safe (content_hash is source bytes; text not hashed). But §7.2 says Tier 1 "changes
form" and boilerplate_strip removes text from the served chunk, while chunk.md ties byte-identity
exceptions ONLY to `tier=3, changed_text=true`. A boilerplate-stripped chunk is a Tier-1
`changed_text` case the invariant doesn't admit. §18.3 test 4 (Tier 2 byte-identity) tests only
Tier-2 augmented chunks return verbatim — it would NOT flag a Tier-1-stripped boilerplate chunk
(different code path), so the contradiction ships silently. Severity: low-medium — boilerplate is
`boilerplate`-tier (filtered from default retrieval), so few callers see stripped text, but the
contract is internally inconsistent.

**Required design change:** reconcile the byte-identity invariant: either boilerplate_strip must
record a Tier-1 `changed_text=true` transformation (making the exception explicit and provenanced),
or boilerplate segments must not have their served `text` stripped (strip only affects
`embedding_input`). The former is preferable and matches taxonomy §2.1 "retains it in provenance."

---

## Attack 8 — Config-version scope (retrieval-treatment exclusion)

**Scenario.** ingestion-config.md derivation **excludes** `retrieval_defaults` and
`class_rules[*].retrieval_treatment` from `config_version`, on the rationale that
salience-weighting is served at query time and does not require rebuilding vectors.

**Trace — the edit that SHOULD force a rebuild but doesn't:**
- **`salience_weights` / `default_salience_filter` edit:** genuinely retrieval-time. Payload carries
  `salience_tier`; the weight is applied at query. Excluding from `config_version` is CORRECT — no
  rebuild needed. HOLDS. This is the intended behavior.
- **Class-description edit (the real attack):** taxonomy §4.1 — the class description feeds an
  **LLM classification** that (a) assigns `salience_tier` at Decompose, and (b) via
  `class_context`, feeds **Tier 2 augmentation** (`Augmentation.class_context`), which is
  concatenated into `embedding_input` and **embedded**. So a class-description change alters the
  **embedded string** → different vectors → a build-affecting change. But where does the class
  description live in the config? It is `basis=class_description` provenance and drives `class_rules`
  behavior; the ingestion-config `config_version` derivation hashes
  `class_rules[*].transformation`, `.chunking`, `.embedding_override`, `.metadata_schema` — it does
  **NOT** list the class-description text itself, nor `retrieval_treatment`. If the class-description
  text is stored as `rationale`/provenance (excluded) or under `retrieval_treatment`, then **editing
  a class description changes embedded augmentation text and vectors but does NOT change
  `config_version`** → no rebuild → stale vectors served under unchanged IDs.

**Is salience tier build-affecting?** It is **written into chunk payloads** at Build (taxonomy §11,
constraint: "every chunk… MUST carry its originating segment's type and tier as payload fields").
So `salience_tier` is baked into the chunk at build time. If a class-description edit changes which
tier a segment gets, the *baked payload* is now wrong, yet `config_version` didn't move and no
rebuild fires. The rationale "retrieval-weighting change is served at query time from the payload"
is TRUE for the *weight* but FALSE for the *tier assignment itself*, which is build-baked.

**Verdict: BREAKS.** Invariant failed: `config_version` under-includes. Two build-affecting inputs —
(1) class-description text feeding Tier-2 `class_context` (embedded → vectors) and (2)
class-description-driven `salience_tier` (baked into payload at Build) — are not demonstrably folded
into `config_version`. Editing a class description can therefore silently produce a corpus whose
vectors/payloads no longer match the config that "fully determines Build output" (§12 invariant),
with no rebuild triggered and **unchanged chunk IDs** masking the drift. ingestion-config OQ-1
already flags exactly this ("getting it wrong… causes stale chunks (under-inclusion)"). §18.3 has no
test for config-version completeness; §18.2 contract layer would only catch it if a
config_version-scope property test exists (it is not enumerated). Severity: high — silent stale
vectors, the §10.5 rot pattern via the augmentation/tier path rather than the deletion path.

**Required design change:** `config_version` MUST include (a) the class-description text for every
class whose `tier2_operations` includes `class_context` or `breadcrumb_augment`, and (b) any input
that determines `salience_tier` baking if tier is written to payload at Build. Either fold class
descriptions into the hash, or make `salience_tier` a purely retrieval-time lookup (not baked) —
but the current design bakes it, so it must be in `config_version`. Add a config-version-scope unit
test (ingestion-config OQ-1 already calls for this "before Phase 1").

**Vice-versa check (over-inclusion):** the only field the derivation includes that arguably
shouldn't is `metadata_schema` if a purely-additive filterable field is added — that would force a
needless full rebuild. Minor; over-inclusion is the safe direction per OQ-1.

---

## Attack 9 — chunk_index instability under mid-segment insertion + dual-visibility interleave

**Scenario.** A paragraph is inserted mid-segment in doc_E. This shifts `chunk_index` of every later
chunk in that segment (0,1,2 → 0,1,2,3 with the tail renumbered). Separately, the incremental-upsert
path (index-lifecycle §8.2) writes new chunks and deletes old ones in a **sequential two-step**
(OQ-L-4: Qdrant has no cross-point transaction), leaving a brief window where old and new coexist.

**Trace.**
- The insertion changes `content_hash` (bytes changed) → per case (c), **every** chunk ID of doc_E
  rotates anyway. So `chunk_index` shift is moot for *ID uniqueness* — the whole document's IDs move
  regardless. No two-version ID collision.
- The dangerous part is the **interleave**. During incremental replace-by-document (§8.2, direct to
  live), OQ-L-4's proposed default explicitly ACCEPTS a "brief dual-visibility window": new chunks
  (new `content_hash`) written, old chunks (old `content_hash`) not yet deleted. A query landing in
  this window can return **stale + current mix** for doc_E — both versions of the inserted region,
  both looking authoritative.
- This is precisely the §10.5 MUST: "all chunks belonging to the previous version are removed **in
  the same operation** that writes the new ones; **partial application is not an acceptable
  intermediate state**." OQ-L-4's "tolerable briefly" default **directly contradicts** this MUST,
  and OQ-L-10 says so in plain text ("§10.5… yet OQ-L-4's proposed default accepts a brief
  dual-visibility window… this is a spec conflict, not an executor degree of freedom").

**Which guarantee does this test?** §10.5 "no partial intermediate state" and §15/§18.3 test 1
(alias swap under sustained load — zero stale-collection reads) plus the Concurrency test layer
(§18.2: "no stale-collection reads"). Direct-to-live upsert has **no alias swap**, so test 1's alias
mechanism doesn't even protect it — the concurrency guarantee is bypassed by the direct-to-live path.

**Is OQ-L-10's clone-and-swap resolution sufficient?** Yes, IF adopted as the v1 default:
clone-and-swap snapshots live → shadow, applies replaces in the shadow, then **atomic alias
retarget**. Because promotion is the same atomic two-phase alias swap as a full build, there is no
dual-visibility window against live — a query resolves the alias to exactly one collection, old or
new, never a mix. This restores the §10.5 "no partial state" guarantee and brings the incremental
path under §18.3 test 1's protection. The **direct-to-live mode** (OQ-L-10's opt-in) remains a
BREAK of §10.5's letter and must be documented as such.

**Verdict: HELD (owner ruling D-10, 2026-09-03).** Clone-and-swap is the decided v1 design
(index-lifecycle §8.2). Direct-to-live opt-in was rejected (§8.3). There is no dual-visibility
window on the decided path: replaces happen in a shadow and promotion is an atomic alias swap,
bringing the incremental path under §18.3 test 1's protection. OQ-L-4 is resolved-by-ruling (moot
for v1). C-4 stands as written and is honored on every path.

---

## Summary table

| # | Attack | Verdict | Severity | Required design change |
|---|---|---|---|---|
| 1 | Heading rename | HOLDS* | — (doc-inconsistency flag) | Correct index-lifecycle §9.1: removal is by `document_id`, not by recomputed-ID match (else rename/insert orphans prior version). |
| 2 | Identical sibling segments | HOLDS | — | Add property test: no two chunks of one doc share a `chunk_id`. |
| 3 | Non-deterministic classification | **BREAKS** | **Critical** | Freeze Decompose output into a content-addressed segment-set artifact keyed on (document_id, content_hash, config_version); property test must re-run full pipeline. |
| 4 | Tier 3 rewrite | HOLDS | low (idempotency erosion) | Cache Tier-3 rewrite output per (version, config) so case-(a) idempotency is byte-exact. |
| 5 | Near-duplicate primary flip | **BREAKS** (partial) | High | Add "version-family primacy changed" repair trigger that re-derives/patches `salience_tier` in place; add supersession-tier test. |
| 6 | Tombstone replay vs config change | HOLDS*/BREAKS (config-crossing) | High | Pin tombstone replay to `subject_id` (document_id), config-invariant; `chunk_ids_affected` is audit-only; extend test 8 with an interleaved config change. |
| 7 | Boilerplate strip + reassembly | HOLDS (reassembly/ID); BREAKS (byte-identity wording) | low-med | Record Tier-1 boilerplate_strip as a `changed_text=true` transformation, OR strip only `embedding_input` not served `text`. |
| 8 | Config-version scope | **BREAKS** | High | Fold class-description text (feeds embedded `class_context` + baked `salience_tier`) into `config_version`; add config-version-scope test. |
| 9 | chunk_index / dual-visibility interleave | **HELD** (owner ruling D-10, 2026-09-03: clone-and-swap is the v1 default; direct-to-live rejected) | High (resolved) | Clone-and-swap adopted as the only v1 mode (index-lifecycle §8.2). Direct-to-live opt-in rejected (§8.3). No dual-visibility window on the decided path. |

\* HOLDS for the invariant, but with a documentation inconsistency (1) or a config-crossing latent
break (6) that must be closed.

---

## Overall: does §10.5 hold as designed?

**No — not as currently written.** The *core arithmetic* of §10.5 is sound: keying `chunk_id` on
`content_hash` (document-wide, so edits rotate all IDs) and keying *removal* on `document_id` is a
genuinely robust pair, and it survives the pure-identity attacks (1, 2) and the text-not-hashed
attacks (4, 7-on-IDs) cleanly. But §10.5's headline MUST — "the same document under the same config
produces the same IDs on every run" — rests on an **un-pinned link**: `segment_path` and
`chunk_index` are outputs of an LLM-driven Decompose stage whose determinism is *asserted* but not
*enforced* (Attack 3), so identical inputs can silently yield different IDs, and no §18.3 test as
listed will catch it. Compounding this, the design has three further high-severity gaps that all
reproduce §10.5's own stated failure mode ("stale content returned as authoritative") through paths
§10.5 doesn't police: config-version under-inclusion of class descriptions/baked salience (Attack
8), and the missing primacy-flip trigger (Attack 5). Attack 9 (dual-visibility window) is resolved
by owner ruling D-10 (2026-09-03): clone-and-swap is the decided v1 design; direct-to-live is
rejected. §10.5's *intent* holds; §10.5 *as designed across these contracts* does not, until
Decompose is made deterministic-by-construction, `config_version` is widened to every
embedded/baked input, and replay and primacy are made config-invariant and triggered.
