# Taxonomy Typing, Language Detection, and Cross-Reference Resolution

**Status:** Phase 2 — implementation complete.
**Governing spec:** §6.3, §6.4, §7.6, segment-taxonomy.md §2.1, §4.1–4.3.
**Source modules:**
  - `src/finecorpus/pipeline/decompose/passes/taxonomy.py`
  - `src/finecorpus/pipeline/decompose/passes/language.py`
  - `src/finecorpus/pipeline/decompose/passes/xref_resolve.py`
**Tests:** `tests/phase2/test_taxonomy_language_xref.py`

---

## Overview

These three passes complete the Phase 2 typing story. They run in the ordered
pass pipeline declared in `passes/__init__.py`, after `boilerplate_pass` and
before `superseded_version_pass`:

```
segmentation_pass  → initial types, salience, cross-reference surface detection
salience_pass      → OCR-confidence overrides
boilerplate_pass   → corpus-wide boilerplate reclassification
taxonomy_pass      → ← hint-driven full taxonomy typing (this document)
language_pass      → ← per-segment language detection (this document)
xref_resolve_pass  → ← cross-reference target resolution (this document)
injection_pass     → injection-suspicion scoring
superseded_version_pass → D-25 tier override (must be last)
```

---

## 1. Taxonomy Pass (`taxonomy_pass`)

### Purpose

Parsers (HTML, spreadsheet, PDF) emit a `detected_class_hint` on each region
(see `parse_result.RegionClassHint`). `SegmentationPass` already promotes
`table` hints to `SegmentType.table` (D-11). `TaxonomyPass` completes the
promotion for the remaining hints.

### Hint-to-type mapping

| `detected_class_hint` | `SegmentType` assigned |
|---|---|
| `code` | `code` |
| `list` | `list_` |
| `figure` | `figure_region` |
| `form_field` | `form_field` |
| `prose` | (no change) |
| `other` | (no change) |
| `table` | (no change — already promoted by SegmentationPass) |
| `None` | (no change) |

### Salience update

When a type changes, `salience_tier` is updated to the new type's default
tier from taxonomy §4.2 (e.g. `code` → `primary`; `figure_region` →
`supporting`), **unless** the winning salience signal is an OCR-confidence
override (`ocr_confidence_floor` or `ocr_confidence_warn`). Those signals
have higher precedence (taxonomy §4.1 precedences 3 and 6) than the
`segment_type_prior` (precedence 7) and must not be overridden.

When salience is locked by an OCR signal, only `segment_type` changes; the
`salience_tier`, `salience_signals`, and `salience_basis` are preserved
unchanged.

### Immutable segment types

The following types are never retyped by this pass:

| Type | Reason |
|---|---|
| `boilerplate` | `BoilerplatePass` wins (taxonomy §4.3 precedence 5 > 7) |
| `heading` | Set by structural heuristic; hint cannot refine it |
| `scanned_region` | OCR path; type is not hint-driven |
| `table` | Already promoted by `SegmentationPass` (D-11) |
| `front_matter` | Structural type; not refined by hint |
| `revision_history` | Structural type; not refined by hint |

### Algorithm

1. Build a `region_id → detected_class_hint` index from `doc_ctx.parse_result["regions"]`.
2. For each segment:
   - If `segment_type` is in the immutable set → pass through.
   - Find the dominant hint from `segment.source_region_ids` (first matching region).
   - If the hint maps to a new type in `_HINT_TO_TYPE`:
     - If salience is OCR-locked → update `segment_type` only.
     - Otherwise → update `segment_type`, `salience_tier`, and `salience_signals`
       (prior winner demoted to contributing; new `segment_type_prior` signal added as winner).
   - Otherwise → pass through.

---

## 2. Language Pass (`language_pass`)

### Purpose

Assigns a BCP-47 language code to every segment (§7.6). Must run after
`TaxonomyPass` so segment types are finalised before tagging.

### Requirements satisfied (§7.6)

- Language detected per segment and stored as a filterable field (`segment.language`).
- Segments with undetermined language carry `"und"` — never omitted, never empty.
- Detection is deterministic: same input → same output on any run.
- No heavyweight external dependency; no network calls.

### Detection algorithm

Two-stage deterministic algorithm, no external dependencies:

**Stage 1: Script detection (fast path)**

Samples up to the first 500 characters. Counts codepoints by Unicode block:

| Unicode block | Language code |
|---|---|
| CJK Unified Ideographs (`U+4E00–U+9FFF`, etc.) | `zh` |
| Hiragana / Katakana | `ja` |
| Hangul | `ko` |
| Arabic | `ar` |
| Cyrillic | `ru` |

A script fires when it accounts for ≥ 30% of alphabetic codepoints in the sample.
When a script fires, its language code is returned immediately.

**Stage 2: Stopword vote (Latin-script text)**

For text that is predominantly Latin-script:

1. Tokenise the text into lowercase alphabetic words.
2. Count stopword hits for each candidate language (English, Spanish, French,
   German, Portuguese, Italian, Dutch).
3. The candidate with the highest hit count wins, provided:
   - At least `_MIN_WORDS_FOR_STOPWORD_VOTE = 4` total tokens are present.
   - The hit ratio is at least `_STOPWORD_RATIO_FLOOR = 0.10` (10% of words).
4. If no candidate exceeds the floor, return `"und"`.

The stopword sets are intentionally small (40–60 function words each): articles,
prepositions, conjunctions, pronouns. They are highly diagnostic and rarely
appear across languages.

**Fallback:** `"und"` when neither stage produces a confident result.

### Limitations and open questions

- Short text (headings, code tokens) reliably returns `"und"` — this is correct
  and honest rather than guessing from too few words.
- Code segments (`SegmentType.code`) with English comments may be tagged `"en"`;
  code with no natural-language words gets `"und"` (see OQ-6 in segment-taxonomy.md).
- The stopword vote cannot reliably distinguish closely related languages (e.g.
  Spanish vs. Portuguese, or Dutch vs. Afrikaans). Phase 3 can add a fastText-based
  implementation by replacing `_detect_language` in `language.py`.

---

## 3. Cross-Reference Resolution Pass (`xref_resolve_pass`)

### Purpose

Resolves intra-document cross-references detected by `SegmentationPass`. The
segmentation pass records all cross-reference surface-text matches (e.g. "see
section 4.2") as `CrossReference` records with `resolution=unresolved`. This
pass attempts to find the target segment.

### Protocol interaction

The `SegmentPass.run` method satisfies the protocol (pass-through for segments
and exclusions) and returns no cross-references from `run`. The actual resolution
is performed by `stage.py` calling `xref_resolve_pass.resolve_cross_references(
cross_references, segments)` after all passes complete — this is necessary because
resolution requires the final typed segment list.

### Resolution strategy (§6.4, OQ-3)

Three strategies are applied in order for each unresolved cross-reference:

**Strategy 1: Heading path label**

Extracts a section/appendix/chapter label from the surface text using:
```
(?:section|sect|appendix|chapter|§)\s*([A-Z0-9]+(?:\.[0-9]+)*)
```
Searches all segments for one whose text or structural path component starts
with the extracted label.

- Exactly 1 match → `resolution=resolved`, `target_segment_id` set.
- Multiple matches → `resolution=unresolved`, `target_note` lists candidate IDs.
- No match → fall through to next strategy.

**Strategy 2: Table ordinal**

Detects `\btable\s+(\d+)\b` and resolves to the N-th `SegmentType.table` segment
in document order (1-based).

**Strategy 3: Figure ordinal**

Detects `\b(?:figure|fig\.?)\s+(\d+)\b` and resolves to the N-th
`SegmentType.figure_region` or `SegmentType.figure_caption` segment.

**Fall-through (§6.4 no-silent-loss rule)**

When no strategy succeeds, the cross-reference remains `resolution=unresolved`
with a `target_note` stating that resolution was attempted and failed. Silent
loss is explicitly prohibited by §6.4.

### Scope (OQ-3)

This pass resolves **intra-document** references only. The proposed resolution
of OQ-3 (segment-taxonomy.md §6):

| Reference type | This pass | Planned phase |
|---|---|---|
| Intra-document (section/table/figure) | Resolves or explicitly records as unresolved | Phase 2 ✓ |
| Inter-document (referenced doc name/number) | Records as `unresolved` with note | Phase 3+ |
| External URL | Records as `unresolved` per fetch policy | Phase 3+ |

---

## Invariants

1. **No silent loss** (§6.4): Every `CrossReference` produced by `SegmentationPass`
   appears in the `SegmentSet.cross_references` list, either resolved or with an
   explicit `target_note`.
2. **Boilerplate wins** (taxonomy §4.3): `TaxonomyPass` never retypes a segment
   whose type is `boilerplate`.
3. **OCR salience locked** (taxonomy §4.1): When the winning salience signal is
   `ocr_confidence_floor` or `ocr_confidence_warn`, `TaxonomyPass` updates only
   `segment_type`; salience is not modified.
4. **Language is always set** (§7.6): After `LanguagePass`, every segment's
   `language` field is a non-empty string (BCP-47 code or `"und"`).
5. **Determinism**: All three passes produce the same output for the same input
   on every run.

---

## Open questions affecting this implementation

| ID | Status | Impact |
|---|---|---|
| OQ-3 | Open | Cross-reference resolution scope: inter-document and URL references are `unresolved` here; Phase 3+ for resolution. |
| OQ-5 | Open | Minority-content-type splitting threshold: this pass uses the dominant-region hint (first source region); no sub-segment splitting. |
| OQ-6 | Open | Equations/formulas are typed `code` with language `"und"` by default. |

---

*Maintained for the life of the project. When any of these three passes change,
this document and the relevant ADR/decision record change in the same commit.*
