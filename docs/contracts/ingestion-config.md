# Contract 4 — Ingestion config

Stage boundary: **Plan → Build**. Governing spec: §6.4, §12 (Ingestion config invariants),
§14.2 (secrets), §6.4 (recommendation provenance), §10.5 (config version in chunk identity).

The Ingestion config is a single, complete, machine-readable object that **fully determines Build
output** (§6.4, §12). It has **no implicit defaults resolved at build time** (§12): every value
Build needs is present. It is **serializable, diffable, version-controllable, re-importable**
(§6.4), and **secret-free by construction** (§14.2) so it can be committed to version control. It
is the same object an Easy-mode approval produces and a Proficient user edits — there is only one
(§3.3). It carries the provenance of each recommendation — heuristic vs. sweep-backed (§6.4). Its
`config_version` participates in chunk identity (§10.5).

Root model: `IngestionConfig`, one per KB build.

---

## `IngestionConfig` (root)

| Field | Type | Required | Semantics |
|---|---|---|---|
| `schema_version` | `str` (semver) | yes | Contract version (the *shape*). Distinct from `config_version`. |
| `tenancy` | `TenancyBlock` | yes | Owning workspace/KB. |
| `config_version` | `str` (sha256 hex, derived) | yes | **Content hash of all build-affecting fields** (see derivation below). Participates in chunk identity (§10.5). Changing any build-affecting field changes it and invalidates every chunk (§10.3 config-change trigger, §10.5). |
| `created_at` | `datetime` (UTC) | yes | When this config was produced/approved. |
| `naive_baseline` | `NaiveBaselineRef` | yes | The fixed reference configuration deltas are measured against (§9.3). Recorded so reported deltas are interpretable. |
| `class_rules` | `list[ClassRule]` | yes | Per-segment-class routing. Every segment class present in the corpus MUST have a rule (completeness invariant). |
| `default_rule` | `ClassRule` | yes | Fallback for any class not explicitly listed — makes the config total, so Build never resolves an implicit default (§12). |
| `embedding` | `EmbeddingConfig` | yes | Embedding model identity and parameters (KB-wide default; a class rule may override per §6.4). |
| `retrieval_defaults` | `RetrievalTreatment` | yes | KB-level default retrieval treatment; class rules refine per class. |
| `language_support` | `LanguageSupportDecision` | yes | Which detected languages the chosen embedding model supports, and the pre-ingestion decision (§7.6). |
| `spreadsheet_triage` | `list[SpreadsheetTriage]` | yes (may be empty) | Per-spreadsheet report/database/model classification and disposition (§6.4). Visible and overridable. |
| `exclusions_confirmed` | `list[ExclusionDecision]` | yes (may be empty) | Confirmed unservable/excluded content, feeding the exclusion report (§7.5). |
| `provenance` | `list[RecommendationProvenance]` | yes | For each recommendation in this config: heuristic vs sweep-backed, with evidence pointer (§6.4). |
| `secret_free_attestation` | `bool` (const `true`) | yes | Structural guarantee no secrets are present (§14.2); the model forbids secret-bearing fields by construction. |

## `ClassRule`

Per segment class: transformation tiers, chunking, embedding override, metadata schema, retrieval
treatment (§6.4). Complete — no field is optional-with-implicit-build-default.

| Field | Type | Required | Semantics |
|---|---|---|---|
| `segment_class` | `enum` (segment type) | yes | The class this rule governs. |
| `transformation` | `TransformationSettings` | yes | Which tiers are on and their parameters. |
| `chunking` | `ChunkingStrategy` | yes | Strategy + parameters (§6.4). |
| `embedding_override` | `EmbeddingConfig` | no | Overrides the KB default embedding for this class, if any. |
| `metadata_schema` | `list[MetadataField]` | yes | The metadata fields chunks of this class carry, beyond mandatory provenance (§6.4). |
| `retrieval_treatment` | `RetrievalTreatment` | yes | Default salience weighting, filters, rerank eligibility for this class (§6.4). |

## `TransformationSettings`

| Field | Type | Required | Semantics |
|---|---|---|---|
| `tier1_enabled` | `bool` | yes | Structure normalization (default on, §7.2). |
| `tier1_operations` | `list[enum]` | yes | Which Tier 1 ops (`ocr_cleanup`, `table_to_markdown`, `whitespace_repair`, `header_inference`). Explicit — no implicit set. There is **no** `boilerplate_strip` op: boilerplate is handled structurally (a `boilerplate`-typed segment tier-filtered at retrieval), never by removing bytes from another chunk's `text` (R6, C-R1/C-R10). See segment-taxonomy.md boilerplate row. |
| `tier2_enabled` | `bool` | yes | Contextual augmentation (default on, §7.2). Augmentation goes in separate fields, never merged into chunk text. |
| `tier2_operations` | `list[enum]` | yes | breadcrumb_augment, table_description, class_context. |
| `tier3_enabled` | `bool` | yes | Full rewriting (default off, §7.2). Per-class opt-in only. |
| `tier3_settings` | `Tier3Settings` | no | Required when `tier3_enabled`; carries the opt-in acknowledgement and model ref. Absent when off. |

## `ChunkingStrategy`

| Field | Type | Required | Semantics |
|---|---|---|---|
| `strategy` | `enum{recursive_char, structure_aware, table_atomic, code_syntax, semantic}` | yes | Splitter type (§6.4 content matrix). |
| `max_tokens` | `int` | yes | Target chunk size. |
| `overlap_tokens` | `int` | yes | Overlap between adjacent chunks. |
| `respect_headings` | `bool` | yes | Structure-aware boundary respect (prose, §6.4). |
| `atomic_rows` | `bool` | no | Tables: never split rows from headers (§6.4). |
| `repeat_headers_on_split` | `bool` | no | Tables too large to keep atomic: repeat headers into each fragment (§6.4). |
| `split_boundaries` | `list[enum]` | no | Code: function/class boundaries (§6.4). |

## `EmbeddingConfig`

| Field | Type | Required | Semantics |
|---|---|---|---|
| `provider` | `str` | yes | e.g. `openai`, `ollama`. Reference by name; the credential lives elsewhere (§14.2), never here. |
| `model` | `str` | yes | Model identity (§8 chunk provenance ties to it; §15 mismatch fails closed). |
| `dimensions` | `int` | yes | Vector dimensionality, for index sizing and mismatch detection. |
| `normalize` | `bool` | yes | Whether vectors are normalized. |
| `supports_languages` | `list[str]` (BCP-47) | no | Languages the model supports, for the §7.6 capability check. |

## `RetrievalTreatment`

| Field | Type | Required | Semantics |
|---|---|---|---|
| `default_salience_filter` | `list[enum]` | yes | Which salience tiers are returned by default (e.g. exclude `excluded`, down-weight `boilerplate`). |
| `salience_weights` | `dict[enum, float]` | no | Per-tier score weighting. |
| `rerank_eligible` | `bool` | yes | Whether this class participates in reranking. |
| `strategy` | `enum{dense, sparse, hybrid}` | yes | Default retrieval mode for this class. |
| `confidence_floor` | `float` [0,1] | no | Optional minimum confidence/OCR-confidence for default inclusion (§6.4 low-confidence down-weighting). |

## `LanguageSupportDecision`

| Field | Type | Required | Semantics |
|---|---|---|---|
| `detected_languages` | `list[LanguageShare]` | yes | From the Parse-result aggregate (§7.6). |
| `unsupported_languages` | `list[str]` | yes (may be empty) | Detected languages the chosen model does not support (§7.6). |
| `decision` | `enum{proceed, warned_proceed, blocked}` | yes | The pre-ingestion decision. `warned_proceed` requires a recorded acknowledgement — the platform MUST warn before embedding unsupported languages (§7.6). |
| `cross_lingual_supported` | `bool` | no | Whether the model supports cross-lingual retrieval (reported, not added by the platform, §7.6). |

## `SpreadsheetTriage`

| Field | Type | Required | Semantics |
|---|---|---|---|
| `document_id` | `str` | yes | The spreadsheet. |
| `kind` | `enum{report, database, model}` | yes | The fixed triage (§6.4). Only `report` is ingested. |
| `disposition` | `enum{ingest, exclude_unservable}` | yes | `database`/`model` → `exclude_unservable` (§6.4). |
| `source` | `enum{detected, user_override}` | yes | Triage is visible and overridable (§6.4). |
| `reason` | `str` | yes | Plain-language basis, for the report. |

## `ExclusionDecision`

| Field | Type | Required | Semantics |
|---|---|---|---|
| `document_id` | `str` | yes | Excluded document (or segment scope). |
| `reason` | `enum` (matches SegmentSet exclusion reasons) | yes | Why (§7.5). |
| `remediation` | `str` | yes | What the user could do — including "nothing, and that is correct" (§7.5). |

## `RecommendationProvenance`

Carries "why this value" for each recommendation (§6.4, §1.4 principle 2).

| Field | Type | Required | Semantics |
|---|---|---|---|
| `target` | `str` (json-pointer into this config) | yes | Which field the recommendation set. |
| `basis` | `enum{heuristic, sweep_backed, user_set, class_description}` | yes | Evidence class. `heuristic` MUST be labelled as such (§6.4). |
| `sweep_run_id` | `str` | no | The §9.3 sweep that backs it, when `sweep_backed`. |
| `rationale` | `str` | yes | Plain-language why (the "why" affordance, §3.1). |

## `MetadataField`, `Tier3Settings`, `NaiveBaselineRef`

- `MetadataField`: `{name: str, type: enum, filterable: bool, source: enum{provenance, source_metadata, derived}}` — declares extra payload fields; provenance fields are always present regardless.
- `Tier3Settings`: `{model_ref: str, opt_in_ack: bool (required true), diff_preview_required: bool}` — §7.2 Tier 3 MUSTs. `model_ref` is a name, never a secret.
- `NaiveBaselineRef`: `{reference_id: str, description: str}` — pins the fixed §9.3 reference config; if it ever changes, prior baselines are marked against the old reference (§9.3).

---

## `config_version` derivation

`config_version` hashes **EVERYTHING that affects a chunk's `embedding_input` or its `text`**. It
is the **sha256 (hex) of the canonical JSON** of exactly those inputs:

- `class_rules[*].transformation` — **all** of Tier 1, Tier 2, and Tier 3 settings (they determine
  `text` normalization and the augmentation concatenated into `embedding_input`).
- `class_rules[*].chunking` — chunk boundaries.
- `class_rules[*].embedding_override` and the KB-wide `embedding` — the **embedding model
  identity** (provider, model, dimensions, normalize).
- `class_rules[*].metadata_schema`, `default_rule` (same subfields).
- **Class descriptions.** The class-description text for every class (the §6.5 input consumed by
  Tier 2 `class_context` and by the §4.1 LLM salience classification) MUST be folded into the hash.
  A class description is embedded augmentation text: editing it changes `embedding_input` and the
  produced vectors, so it is build-affecting and MUST invalidate the affected chunks (attack 8,
  S-R14). Wherever the class-description text is materialized in the config (e.g. the class-rule's
  description field / the `class_description`-basis recommendation input), its **text** is a hashed
  input, not merely provenance.
- `spreadsheet_triage[*].kind`/`disposition`.

**Excluded** from the hash (do not affect `text` or `embedding_input`): `created_at`,
`provenance`, `retrieval_defaults` and `retrieval_treatment` — the **retrieval-treatment** fields
(salience *weights*, default salience filter, rerank eligibility, strategy) are retrieval-time
only and do not invalidate chunks — plus `exclusions_confirmed` remediation text and `rationale`
strings.

Canonicalization: JSON with sorted keys, no insignificant whitespace, UTF-8, then sha256. The
resulting hex string is `config_version` and is what enters chunk-ID derivation
([chunk.md](chunk.md), [ADR-0006](../adr/0006-chunk-identity-scheme.md)).

Rationale for excluding retrieval treatment: §10.5 says a chunking/transformation change
invalidates chunks. A retrieval-weighting change is served at query time from the payload and does
not require rebuilding vectors — folding it into `config_version` would force needless full
rebuilds (§4.6 "upgrading MUST NOT require reindexing unless the embedding model or chunking
config changed").

### Salience-tier MAPPING changes are payload-only

A change to how salience *tiers are mapped* (a salience-tier remap, or a version-family primacy
flip that flips which members are `excluded`) does **not** change `text` or `embedding_input`, so
it does **not** change `config_version` and does **not** force a re-embed. The baked
`salience_tier` payload is updated in place by the **metadata-only update path** in
[index-lifecycle.md](../architecture/index-lifecycle.md) §7.2 (an in-place payload update job, no
re-embed). This is distinct from a **class-description** edit, which *does* change embedded
augmentation and salience classification and therefore *does* rotate `config_version` and force a
rebuild (above).

---

## Invariants

- **Fully determines Build:** every value Build reads is present; `default_rule` guarantees
  totality so no implicit default is resolved at build time (§12).
- **Secret-free by construction:** no field holds a credential; providers/models are referenced by
  name only (§14.2). Verified by the secret-hygiene test (§18.3.10).
- **Diffable/re-importable:** canonical JSON serialization; round-trips exactly (§6.4). Phase 3
  acceptance test asserts round-trip.
- **Recommendation provenance:** every recommender-set value has a `RecommendationProvenance`
  entry; heuristic values are labelled `heuristic` (§6.4).
- **Config version:** `config_version` is derived deterministically from every input that affects
  a chunk's `text` or `embedding_input` — chunking, all transformation tiers, embedding model
  identity, **and class-description text** — and participates in chunk identity (§10.5). A
  retrieval-treatment-only change (weights/filters) does not alter it; a salience-tier *mapping*
  change is payload-only (metadata-only update path), not a `config_version` bump (R3, attack 8).
- One config object, not two pipelines (§3.3): Easy-mode approval and Proficient edits produce the
  same shape.
- `tenancy` present (Phase 0 MUST).

## Golden-corpus expressibility

- **Three spreadsheet kinds:** three `SpreadsheetTriage` entries — one `report`/`ingest`, one
  `database`/`exclude_unservable`, one `model`/`exclude_unservable`, each with a reason (§6.4).
- **Bloated manual:** distinct `ClassRule`s for prose (structure_aware), table (table_atomic),
  scanned_region (with confidence floor), boilerplate (excluded/boilerplate salience).
- **Adversarial doc:** no special config field needed — injection scoring is provenance, not
  config; but a `ClassRule`'s `retrieval_treatment` can set an injection-suspicion filter default.
- **Multilingual corpus:** `LanguageSupportDecision` with `unsupported_languages` populated and
  `decision=warned_proceed` carrying the acknowledgement (§7.6).

## Open questions

1. **Exact build-affecting field set for `config_version`.** The list above is the proposed set,
   now amended (R3, attack 8) to include **class-description text** as a hashed input and to fold
   in all three transformation tiers and the embedding model identity. **Flagged** — the precise
   field pointers must be pinned and a `config_version`-scope unit test added before Phase 1
   (getting it wrong causes spurious rebuilds on over-inclusion or **stale vectors** on
   under-inclusion — the attack-8 failure). ADR-0006 depends on this list.
2. **Per-class embedding overrides and index topology.** Multiple embedding models in one KB
   implies multiple vector spaces. **Proposed default:** discourage per-class embedding overrides
   in v1 (single KB embedding model); allow the field but validate that mixed dimensions are
   rejected before Build. **Flagged** — ties to §15 mismatch-fails-closed.
3. **Where the sweep writes back.** Whether `sweep_backed` recommendations mutate this config in
   place or produce a candidate config the user promotes. **Proposed default:** the sweep proposes
   a new `IngestionConfig`; the user promotes it (keeps the diffable history). **Flagged.**
4. **Metadata schema vs mandatory provenance overlap.** **Proposed default:** provenance fields are
   always present and MUST NOT be redeclared in `metadata_schema`; validation rejects duplicates.
   **Flagged.**
