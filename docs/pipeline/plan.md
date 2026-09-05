# Plan stage (Phase 3)

Stage 4 of the pipeline. Consumes the `SegmentSetBatch` artifact produced by Decompose; produces an `IngestionConfig` artifact.

Phase 3 real implementation. Prior to Phase 3, this stage emitted a skeleton `IngestionConfig` with placeholder class rules. The Phase 3 implementation runs a heuristic recommender over corpus statistics and emits a complete, provenance-labelled configuration ready for the Build stage.

Governing spec: §6.4 (chunking/augmentation matrix), §7.2–7.6, §14.1 (M-067), §18.3 (T-04, §19 criterion 1).

---

## Overview

```
SegmentSetBatch (decompose.json)
  ↓
CorpusStatsPass         — aggregate per-class statistics (segment count, token
                          lengths, OCR confidence, heading structure, language)
  ↓
LanguagePass            — detect unsupported languages vs embedding provider
                          capabilities; emit LanguageSupportDecision
  ↓
recommend(stats, descs) — heuristic recommender: §6.4 matrix → ClassRule per class
  ↓
config_version_derive() — SHA-256 over build-affecting fields (excludes retrieval-
                          treatment-only changes, M-015)
  ↓
IngestionConfig (plan.json)
```

---

## Recommender matrix (§6.4)

The `recommend()` function in `src/finecorpus/pipeline/plan/recommender.py` translates corpus statistics into per-class rules. One `ClassRule` is emitted for every `SegmentType` observed in the corpus. Every value set by the recommender carries a `RecommendationProvenance` entry with `basis=heuristic` (M-025).

| Segment class | Strategy | Overlap | Notable settings | Tier 2 ops |
|---|---|---|---|---|
| `prose` | `recursive_char` | derived from p90 | `respect_headings=True`; `breadcrumb_augment` when heading structure present | breadcrumb_augment (conditional), class_context (when class description supplied) |
| `table` | `table_atomic` | 0 | `atomic_rows=True`, `repeat_headers_on_split=True`, `table_to_markdown` Tier 1 | table_description |
| `code` | `code_syntax` | 0 | `split_boundaries=["function","class"]`; heuristic regex splitter (D-34) | — |
| `scanned_region` | `recursive_char` | derived from p90 | `confidence_floor` from measured p10 OCR confidence, clamped [0.50, 0.85] | — |
| `heading` | `recursive_char` | derived from p90 | — | breadcrumb_augment |
| `boilerplate` | `recursive_char` | derived from p90 | salience filter: boilerplate tier only | — |
| `front_matter` | `recursive_char` | derived from p90 | salience filter: boilerplate tier only | — |
| `list` | `recursive_char` | derived from p90 | — | — |
| `revision_history` | `recursive_char` | derived from p90 | — | — |
| `cross_reference` | `recursive_char` | derived from p90 | `resolved_target` in metadata_schema | — |
| `unknown` | `recursive_char` | derived from p90 | — | — |

`max_tokens` is derived from the class's p90 token length, rounded up to the nearest 64-token boundary.

### Provenance labelling

Every field set by the recommender emits a `RecommendationProvenance` entry:
- `basis=heuristic` for all matrix-driven values
- `basis=class_description` for values influenced by a supplied `ClassDescription` (e.g., `class_context` op when the user provides a description)
- All entries carry a non-empty `rationale` string (M-025)

---

## Language support decision

The Plan stage evaluates whether the configured embedding model's declared `supports_languages` covers all detected corpus languages. Detection uses `CorpusStats.detected_languages` (aggregated by `CorpusStatsPass` from per-segment BCP-47 language codes).

| Condition | Decision |
|---|---|
| All detected languages in `supports_languages` (or `supports_languages=["*"]`) | `proceed` |
| ≥ 1 unsupported language but fraction < warn_threshold | `warned_proceed` |
| Unsupported language fraction ≥ warn_threshold | `warned_proceed` (no `blocked` in Phase 3) |

`language=None` is treated as `"und"` (undetermined) — fail-closed behavior (M-041). The golden corpus produces ~67% `und` segments (largely scanned/OCR regions and short headings) and ~33% `en`.

---

## Config version derivation

`derive_config_version()` in `src/finecorpus/pipeline/plan/config_version.py` computes a SHA-256 hash over build-affecting fields only:

- All `ClassRule.chunking.*` fields
- All `ClassRule.transformation.*` fields (tier1_ops, tier2_ops, tier3_enabled)
- `embedding.provider`, `embedding.model`, `embedding.dimensions`
- `class_descriptions[].description` (influences Tier 2 prompts)

Fields excluded from the hash (retrieval-treatment-only):
- `retrieval_treatment.confidence_floor`
- `retrieval_treatment.salience_weights`
- `retrieval_treatment.rerank_eligible`
- `language_support.*`

This ensures that changing `confidence_floor` or salience weights does not invalidate the chunk index and force a reindex (M-015).

---

## Config export and import (M-026)

`export_config()` and `import_config()` in `src/finecorpus/pipeline/plan/config_io.py`:

- **Export:** deterministic JSON (sorted keys, no trailing whitespace); runs a denylist secret scan before writing (D-21 resolution); raises `SecretLeakError` if patterns match.
- **Import:** re-derives `config_version` from the imported content and compares to the stored value; any mismatch raises `ConfigImportError` (tamper detection). Unknown top-level keys are rejected (`extra=forbid`).
- **Round-trip:** export → import → re-export is byte-identical (M-026, verified in `tests/phase3/test_config_roundtrip.py` and `tests/phase3/test_acceptance.py::TestConfigRoundtrip`).

---

## Secret-free attestation (M-071)

`IngestionConfig.secret_free_attestation: Literal[True]` is set by the Plan stage. The field is `Literal[True]` — it cannot be `False` at the type level. The runtime export scan adds a defense-in-depth check (D-21).

No provider credentials appear in the config: embedding and LLM provider IDs and model names are not secrets; API keys are environment-variable only and never serialized into `IngestionConfig`.
