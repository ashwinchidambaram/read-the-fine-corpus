# Contract 3 — Segment set

Stage boundary: **Decompose → Plan**. Governing spec: §6.3, §7.1, §12 (Segment set invariants),
§6.4 (cross-references).

The Segment set is the typed decomposition of one document. **The segment is the unit of the
system** (§1.4, §7.1): classification, transformation, chunking, and salience are properties of
segments, not documents. Each segment has a type, a salience tier, a structural path, and a
source location (§12). Segments **reassemble to the document in order** (§6.3, §12), and **no
content is lost** between parse result and segment set except **explicitly recorded exclusions**
(§12). Cross-references are either resolved to a followable pointer or recorded as explicitly
unresolved (§6.4) — silent loss is not acceptable.

Root model: `SegmentSet`, one per document.

---

## `SegmentSet` (root)

| Field | Type | Required | Semantics |
|---|---|---|---|
| `schema_version` | `str` (semver) | yes | Contract version. |
| `tenancy` | `TenancyBlock` | yes | Inherited from the document; every segment inherits it in turn (§7.1). |
| `document_id` | `str` | yes | Source document (Inventory `document_id`). |
| `content_hash` | `str` | yes | Version of the document decomposed (Inventory `content_hash`). |
| `segments` | `list[Segment]` | yes | The typed segments, in document order (see `document_order`). |
| `reassembly` | `ReassemblyRecord` | yes | The mechanism that reconstitutes the document from its segments (§6.3 step 4). |
| `exclusions` | `list[ExclusionRecord]` | yes (may be empty) | Explicitly-recorded content exclusions — the *only* permitted form of content loss (§12). |
| `cross_references` | `list[CrossReference]` | yes (may be empty) | Resolved pointers or explicit unresolved records (§6.4). |
| `decomposed_at` | `datetime` (UTC) | yes | When decomposition completed. |

## `Segment`

| Field | Type | Required | Semantics |
|---|---|---|---|
| `segment_id` | `str` (ULID) | yes | Segment identity, stable within this document version. Feeds chunk-ID derivation via `segment_path` (see below). |
| `document_order` | `int` | yes | 0-based ordinal position of this segment in document reading order. **Dense, gapless, unique** within the set — the ordering key for reassembly. |
| `segment_type` | `enum` (segment taxonomy §6.3) | yes | prose, table, list, code, figure_caption, form_field, boilerplate, front_matter, scanned_region, … (taxonomy is Open Decision §20.3). |
| `salience_tier` | `enum{primary, supporting, boilerplate, excluded}` | yes | Default retrieval weight class (§6.3). `excluded` still indexed; tier controls default retrieval, not existence (§6.3 salience-not-pruning). |
| `structural_path` | `list[str]` | yes (may be empty) | Ordered heading breadcrumb from document root (§6.3 step 2). Empty = no heading structure. Component of chunk identity. |
| `segment_path` | `str` | yes | Canonical stable path string for this segment within the document, used in chunk-ID derivation (see [chunk.md](chunk.md)). Derived from `structural_path` + a within-heading ordinal so it is stable across re-runs of the same document version. |
| `location` | `SourceLocation` | yes | Position in the original source (§12). |
| `source_region_ids` | `list[str]` | yes | The `ParseResult.RegionResult`s this segment was assembled from — the trace back that proves no content vanished. |
| `language` | `str` (BCP-47) | yes | Segment language (§7.6). `und` if undetermined. |
| `ocr_confidence` | `float` [0,1] | no | Carried from source regions where applicable (§6.4 scans). |
| `injection_suspicion` | `float` [0,1] | yes | Injection-pattern suspicion for this segment (§14.1). `0.0` if none. Filterable; never used to silently exclude. |
| `invisible_content_flags` | `list[enum]` | yes (may be empty) | Invisible-content flags carried from parse (§14.1). |
| `sensitivity_flags` | `list[enum]` | yes (may be empty) | PII/sensitive flags (§14.4). |
| `salience_signals` | `list[SalienceSignal]` | yes (may be empty) | **All** salience signals that fired for this segment, winning and losing, recorded as contributing evidence (taxonomy §4.3: lower signals are recorded even when they do not win). Enables the §11.5 explain-mode "why this tier" trace. |
| `salience_basis` | `SalienceSignalKind` (enum) | yes | The **winning** signal — the one that actually set the tier (taxonomy §4.3 precedence). Same enum as `SalienceSignal.kind`; MUST equal the `kind` of the `salience_signals` entry that governed. |
| `text` | `str` | no | The segment's source text (byte-identical to source; Tier transformations happen at Build, not here). Null only for non-text segments (e.g. figure whose caption is separate). |

## `SalienceSignal`

One record per firing signal (winning or contributing), mirroring the taxonomy §4.1 signal
matrix exactly so provenance can reproduce the full tier decision (taxonomy §4.3, C-R3).

| Field | Type | Required | Semantics |
|---|---|---|---|
| `kind` | `SalienceSignalKind` (enum) | yes | Which signal fired (see enum below). |
| `implied_tier` | `enum{primary, supporting, boilerplate, excluded}` | yes | The tier this signal argued for. |
| `won` | `bool` | yes | Whether this signal is the one that set `salience_basis`. Exactly one entry in `salience_signals` has `won=true`. |
| `detail` | `str` | no | Human-readable evidence (e.g. "matched across 412 corpus docs", "class description: incident reports are primary"). |

`SalienceSignalKind` is the closed enum covering every taxonomy §4.1 signal plus the default:

```
enum SalienceSignalKind {
    explicit_user_exclusion,   # §4.1 hard excluded, highest precedence
    unservable_detection,      # §7.5 unservable content class
    ocr_confidence_floor,      # OCR below the excluded floor
    class_description,         # §6.5 LLM classification against the class description
    boilerplate_detection,     # §6.2 corpus-wide boilerplate machinery
    ocr_confidence_warn,       # OCR between floor and warning level → supporting + flag
    segment_type_prior,        # taxonomy §4.2 default tier by type
    structural_position,       # heading-hierarchy modifier
    default,                   # no signal fired → supporting
}
```

The prior four-value enum (`class_description, boilerplate_detection, structural_position,
override`) could not represent `explicit_user_exclusion`, `unservable_detection`,
`ocr_confidence_floor`, `ocr_confidence_warn`, `segment_type_prior`, or `default`, so it could
not carry the winning signal for OCR-floor exclusions or type-prior defaults (C-R3). It is
replaced by the enum above. There is no `override` value: an operator override is
`explicit_user_exclusion`; a human tier decision is recorded via `class_description`/config, not
a distinct signal.

## `ReassemblyRecord`

The mechanism that satisfies "segments reassemble to the document in order" (§6.3, §12).

| Field | Type | Required | Semantics |
|---|---|---|---|
| `method` | `enum{document_order_concat}` | yes | v1 mechanism: concatenate every segment's source text ordered by `document_order`, including `excluded`/`boilerplate` segments, yielding the parse-level text. |
| `covered_region_ids` | `list[str]` | yes | Union of all `source_region_ids` across segments plus exclusions. MUST equal the full set of `RegionResult.region_id` from the Parse result. |
| `reassembly_digest` | `str` (sha256 hex) | yes | Hash of the reassembled text, checked by the reassembly property test (§18.2). |

**Reassembly invariant:** ordering all `segments` by `document_order` and concatenating their
`text` (with `ExclusionRecord`s accounting for any non-segmented spans) reproduces the parse-level
document text. The property test in §18.2 asserts this for every fixture. `document_order` is
dense and gapless so the ordering is total and unambiguous.

## `ExclusionRecord`

The **only** permitted form of content loss between Parse and Segment set (§12). Everything not
carried into a segment is accounted for here.

| Field | Type | Required | Semantics |
|---|---|---|---|
| `exclusion_id` | `str` (ULID) | yes | Identity. |
| `location` | `SourceLocation` | yes | The excluded span in the source. |
| `source_region_ids` | `list[str]` | yes | Regions excluded, for coverage accounting. |
| `reason` | `enum{unservable_content, spreadsheet_database, spreadsheet_model, encrypted, empty_region, superseded_version, duplicate, parse_failed, too_short, other}` | yes | Why excluded (§7.5, §6.4). `too_short` = content span below the minimum segment length threshold (added schema_version 1.1.0). |
| `reason_detail` | `str` | no | Plain-language detail for the exclusion report (§7.5). |
| `reversible` | `bool` | yes | Whether the original is retained and the exclusion can be undone (§1.4 principle 1). Always `true` in v1 — exclusion never destroys source. |

## `CrossReference`

Handles "See section 4.2" (§6.4): resolve to a pointer, or record as explicitly unresolved.
Silent loss is not acceptable (§6.4).

| Field | Type | Required | Semantics |
|---|---|---|---|
| `xref_id` | `str` (ULID) | yes | Identity. |
| `from_segment_id` | `str` | yes | Segment containing the reference. |
| `surface_text` | `str` | yes | The literal reference text ("see section 4.2", "Appendix B"). |
| `location` | `SourceLocation` | yes | Where the reference appears. |
| `resolution` | `enum{resolved, unresolved}` | yes | Whether a target was found (§6.4). |
| `target_segment_id` | `str` | no | The referenced segment, when `resolved` (a followable pointer, §6.4). |
| `target_note` | `str` | no | When `unresolved`, an explicit note of what was referenced and why it could not be resolved (§6.4: recorded explicitly). |

---

## Determinism and freezing

Decompose consults LLM-driven signals (segment typing and salience classification, taxonomy
§4.1). LLM output is **not** deterministic by construction: temperature=0 does not guarantee
token-level reproducibility across model, provider, batching, or hardware. Because
`segment_path` and each segment's `document_order`/`chunk_index` position feed chunk-ID
derivation ([chunk.md](chunk.md)), an un-pinned Decompose would let identical inputs yield
different chunk IDs on a re-run — silently breaking §10.5's "same document under the same config
produces the same IDs on every run" (determinism review, attack 3).

The Segment set is therefore a **frozen, content-addressed artifact**:

- **Key.** The artifact is keyed on `(document_id, content_hash, config_version)`. These are the
  same three components that, together with `segment_path` and `chunk_index`, determine chunk
  identity. `content_hash` pins the document version; `config_version` pins every build-affecting
  input including the class-description text and Tier 1/2/3 settings (see
  [ingestion-config.md](ingestion-config.md) `config_version` derivation).
- **Persisted at Decompose time.** When Decompose runs for a key, its full output — segment
  boundaries, types, `segment_path` assignments, `salience_tier`, `salience_signals`, and every
  other field of this contract — is written once to the pipeline artifact store under that key.
- **Reused, never regenerated.** Build MUST consume the frozen Segment set artifact for its key,
  not re-run Decompose. Any later stage that needs the segment set for an existing
  `(document_id, content_hash, config_version)` loads the persisted artifact. The LLM runs **once**
  per (version, config); its output becomes a cached, pinned input to every subsequent Build. This
  is consistent with §15 resumability (a resumed or re-run Build reuses the same segments and so
  re-derives the same chunk IDs).
- **Same key → same artifact.** A new Decompose is performed only when the key is new (new
  document version or new `config_version`). Two Builds over the same key are therefore
  byte-for-byte identical in their chunk IDs.

This converts §10.5's "same IDs every run" from an assertion about LLM determinism into a
property of **artifact reuse**: the IDs are stable because the segment set they derive from is
frozen, not because the LLM is reproducible.

Downstream references: [chunk.md](chunk.md) derivation notes cite this section; the incremental
and rebuild flows in [index-lifecycle.md](../architecture/index-lifecycle.md) consume the frozen
artifact.

**Determinism property test (§18.3-adjacent).** The property test MUST re-run the **full
Decompose→Build pipeline** on a fixture from cold and assert identical `chunk_id` sets across two
runs — it MUST NOT merely re-hash already-fixed segment inputs. Re-hashing fixed inputs would
pass vacuously and miss exactly the classification-drift break this freezing closes (attack 3). If
the freeze is implemented correctly, the second run reuses the persisted artifact and the IDs
match by reuse; the test also asserts that a run with the artifact deleted (forcing a fresh
Decompose) still produces the same IDs for the same key, proving the freeze is the mechanism, not
incidental LLM stability.

---

## Invariants

- Every segment has `segment_type`, `salience_tier`, `structural_path`, and `location` (§12).
- **Reassembly:** ordering segments by `document_order` and concatenating reproduces the
  parse-level document text; `reassembly.covered_region_ids` equals the full Parse-result region
  set (§6.3, §12). Verified by property test (§18.2).
- **No silent content loss:** every `RegionResult.region_id` from Parse appears in exactly one
  segment's `source_region_ids` **or** in exactly one `ExclusionRecord.source_region_ids`. An
  unaccounted region is a defect (§12).
- Exclusions are explicit, reasoned, and `reversible=true` (source retained, §1.4, §7.5).
- Cross-references are `resolved` (with a `target_segment_id`) or `unresolved` (with a
  `target_note`); never dropped (§6.4).
- Salience is a *tier*, not a filter that deletes: `excluded`-tier segments are present in the
  set and carried forward (§6.3 salience-not-pruning).
- **Salience provenance complete:** every segment records **all** firing signals in
  `salience_signals` (taxonomy §4.3), and `salience_basis` equals the `kind` of the single
  `won=true` entry (C-R3).
- **Frozen artifact:** the Segment set is content-addressed on
  `(document_id, content_hash, config_version)`, persisted at Decompose, and reused (never
  regenerated) by Build — the mechanism that makes chunk IDs deterministic across runs
  (see "Determinism and freezing"; §10.5, attack 3).
- `tenancy` present and inherited by every segment (§7.1, Phase 0 MUST).

## Golden-corpus expressibility

- **Bloated manual with mixed types + cross-references:** one `SegmentSet` with prose, table,
  scanned_region, and revision-block (boilerplate) segments; `structural_path` breadcrumbs per
  segment; `CrossReference`s both resolved and unresolved; boilerplate segments at
  `salience_tier=boilerplate`, appendix perhaps `supporting`.
- **Spreadsheet report:** sheets → sections; tables as `table` segments; chart commentary as
  `prose` (§6.4). Database/model spreadsheets never reach a segment set as content — they appear
  as `ExclusionRecord`s with reason `spreadsheet_database`/`spreadsheet_model` (§6.4).
- **Scanned PDF:** `scanned_region` segments carrying `ocr_confidence`; low-confidence regions
  kept (not pruned), tiered and down-weighted downstream.
- **Adversarial doc:** segments carry `injection_suspicion` and `invisible_content_flags`; nothing
  is stripped (§14.1).

## Open questions

1. **`segment_path` construction.** The exact canonical form used in chunk identity. **Proposed
   default:** `"/".join(structural_path) + "#" + within_heading_ordinal`, where the ordinal counts
   segments under the same breadcrumb in `document_order`. Deterministic for a fixed document
   version. **Flagged** — this string is load-bearing for chunk-ID stability
   ([ADR-0006](../adr/0006-chunk-identity-scheme.md)); it must be pinned before Phase 1. Its
   *reproducibility* is now guaranteed by the frozen-artifact rule ("Determinism and freezing"):
   the within-heading ordinal is computed once at Decompose and persisted, so LLM-driven boundary
   drift cannot rotate it on a re-run. The remaining open item is the canonical string *format*,
   not its stability.
2. **Segment type taxonomy — RESOLVED (C-R5).** The definitive type list is
   [`docs/architecture/segment-taxonomy.md`](../architecture/segment-taxonomy.md) (the executor's
   answer to Open Decision §20.3), not a list restated here. The previously-proposed names
   `revision_block` and `chart_commentary` were stale and are removed: the taxonomy uses
   `revision_history` (not `revision_block`) and maps chart-adjacent commentary to `prose` (there
   is no `chart_commentary` type). `Segment.segment_type`, the `Provenance.segment_type` enum, and
   `ClassRule.segment_class` all reference the taxonomy as the single source of truth.
3. **Reassembly and Tier 1 normalization.** Tier 1 (whitespace/encoding repair) runs at Build and
   changes form. The reassembly invariant is defined against **parse-level** (pre-transform) text,
   so it stays exact. **Proposed default:** keep it that way; do not assert reassembly against
   post-transform text. **Flagged** — noted so nobody re-derives it.
4. **Cross-references across documents.** §6.4 examples are intra-document; corpora also reference
   other documents. **Proposed default:** `target_segment_id` may point at a segment in another
   document within the same KB; cross-document resolution is best-effort and may remain
   `unresolved`. **Flagged.**
