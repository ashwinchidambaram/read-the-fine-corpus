# Decompose Stage

Status: **Phase 1 real implementation** (prose segmentation for native-text PDF only; scanned/HTML/spreadsheet → Phase 2).
Governing spec: §6.3, §12, §18.2. Output contract: `SegmentSetBatch` (§12b, schema v1.0.0).

The Decompose stage produces one `SegmentSet` per `ParseResult`. It never drops a document:
excluded/failed documents receive an empty SegmentSet with an ExclusionRecord, and partial
documents receive segments from successfully-parsed pages only.

---

## Phase 1 scope

Phase 1 handles prose segmentation of native-text PDF content produced by AssessStage. Documents
with `parse_status = excluded_pre_parse` or `failed` produce an empty SegmentSet.

---

## Paragraph segmentation

Text is split on blank lines (one or more consecutive empty lines). Each paragraph becomes a
candidate segment. Short candidates (fewer than `_MIN_SEGMENT_CHARS = 5` chars after stripping)
are discarded. The page number is tracked across segment boundaries to populate
`source_location.page_start` and `source_location.page_end`.

---

## Heading detection

Phase 1 uses a line-level heuristic. A line is treated as a heading if:

1. It is at most `_HEADING_MAX_CHARS = 120` characters.
2. It does not end with sentence-final punctuation (`.`, `?`, `!`).
3. It satisfies at least one of:
   - **Criterion A — Numbering pattern:** matches `_HEADING_NUMBERING_RE`, which covers:
     `1.`, `2.1`, `3.2.1`, `A.`, `Section N`, `§N`, `Appendix X`, `Chapter N`.
   - **Criterion B — ALL CAPS:** the stripped line is all-uppercase and at least
     `_HEADING_ALLCAPS_MIN_LEN = 4` characters (avoids "A.", "OK", etc.).
   - **Criterion C — Isolated first line:** the line is the first (or only) line of a
     paragraph that is immediately preceded by a blank line, and either the paragraph is a
     single line or the next line is blank. This catches section titles that occupy their own
     paragraph in the flow.

**Structural path.** A heading's level is estimated by numbering depth (3.2.1 = depth 3,
2.1 = depth 2, etc.) or `1` for ALL-CAPS and isolated-line headings. The structural path
breadcrumb is maintained as a stack: a heading at level N trims the path to `N-1` elements
and appends the new heading text.

---

## Heading detection accuracy on the golden fixtures

These notes are honest assessments based on the Phase 1 heuristic. They are not claims of
high accuracy.

| Fixture | Heading detection quality | Notes |
|---|---|---|
| `clean_native.pdf` | Good | Numbered sections (`1.`, `2.1`, etc.) detected reliably via Criterion A. |
| `policy_v1/v2/v3.pdf` | Good | Policy documents use numbered sections and ALL-CAPS titles. |
| `bloated_manual.pdf` | Moderate | Large document with mixed numbering; some unnumbered subheadings may be missed (false negatives). |
| `boilerplate_a/b.pdf` | Good | Simple structure; boilerplate pages detected as `front_matter` or `boilerplate` via Criterion C. |
| `form_filled.pdf` | Moderate | Form labels short lines detected as headings; some false positives expected for short field-label fragments. |

**Known false positives.** Short prose sentences without terminal punctuation — for example,
a quoted phrase or an incomplete sentence — can be mis-classified as headings by Criterion C.
This is a Phase 1 limitation.

**Known false negatives.** Mixed-case headings without numbering patterns that are embedded
in flowing text (no leading blank line) are not detected by any Phase 1 criterion.

**Phase 2 improvement path.** pypdf does not expose font-size metadata in the text-extraction
API used by Phase 1. Phase 2 will add font-size detection from the PDF content stream, which
is the most reliable single signal for heading classification.

---

## Segment types produced

### Phase 1 (SegmentationPass — all sources)

| Type | When produced |
|---|---|
| `heading` | Line classified as a heading by the heuristic |
| `front_matter` | Paragraph on page 1, among the first 3 paragraphs, matching a front-matter pattern (title/author/date/abstract/revision) |
| `revision_history` | Heading matching "Revision History", "Change Log", etc. |
| `prose` | All other non-empty native-text paragraphs |
| `unknown` | Paragraphs that cannot be classified (reserved for edge cases; rarely emitted in practice) |
| `table` | Regions with `detected_class_hint=table` (HTML/spreadsheet tables serialised to Markdown) |
| `scanned_region` | OCR page regions below the sub-decompose confidence floor (Phase 2 OCR path) |

### Phase 2 additions (TaxonomyPass, BoilerplatePass)

| Type | When produced |
|---|---|
| `code` | Regions with `detected_class_hint=code` (HTML `<pre>`/`<code>` elements; Phase 2 TaxonomyPass) |
| `list_` | Regions with `detected_class_hint=list` (HTML list elements; Phase 2 TaxonomyPass) |
| `figure_region` | Regions with `detected_class_hint=figure` (chart/diagram regions; Phase 2 TaxonomyPass) |
| `form_field` | Regions with `detected_class_hint=form_field` (Phase 2 TaxonomyPass) |
| `boilerplate` | Segments whose normalised text matches the corpus-wide boilerplate block set (Phase 2 BoilerplatePass) |

Segment types not yet produced (Phase 3+): `figure_caption`, `cross_reference` (direct type — cross-refs are recorded as `CrossReference` objects, not yet promoted to their own segment type).

---

## Salience signals (Phase 1)

Phase 1 uses `segment_type_prior` only — no class-description-based signals (Phase 3).

| Segment type | Salience tier |
|---|---|
| `heading` | `supporting` |
| `front_matter` | `supporting` |
| `cross_reference` | `supporting` |
| `prose` | `primary` |
| `unknown` | `supporting` |

The winning signal is `segment_type_prior`; `won = True`. The full `salience_signals` list
carries exactly one entry per segment in Phase 1.

---

## document_order

Segments are numbered with a dense, gapless, 0-based integer `document_order`. The ordering
follows page order (ascending page_num) within each page, and paragraph order within each page.
The property test `TestReassemblyProperty.test_document_order_is_dense_and_gapless` enforces
this invariant.

---

## Reassembly record

Each SegmentSet carries a `ReassemblyRecord`:

| Field | Value |
|---|---|
| `method` | `document_order_concat` |
| `reassembly_digest` | `sha256(concat(seg.text for seg in sorted_by_document_order))` |
| `covered_region_ids` | IDs of all segments included in the digest |

The property test `TestReassemblyProperty.test_reassembly_for_native_pdf_fixture` verifies that
concatenating segment text in `document_order` reproduces the `reassembly_digest`. This digest
proves round-trip fidelity: segments can be reassembled to recover the full extracted document
text.

---

## Frozen-artifact semantics

The SegmentSet is a **frozen artifact** keyed on `(document_id, content_hash[:16], config_version)`.

- `config_version = "p1.0"` for Phase 1.
- Persisted at `<artifacts_root>/segment_sets/<document_id>__<content_hash[:16]>__<config_version>.json`.
- On a second run with the same key, the stage loads the existing JSON and skips recomputation.
- The reuse path is exercised by `TestFrozenArtifactReuse.test_mtime_unchanged_on_reuse`.

Changing any of `document_id`, `content_hash`, or `config_version` invalidates the cache entry
and forces recomputation. A Phase 2 change that adds new segment types or signals MUST bump
`config_version` to prevent stale cache hits.

---

## Invariants

- `len(segment_sets) == len(parse_results)` — nothing dropped.
- Excluded/failed documents produce an empty SegmentSet (no segments, no reassembly digest over content — digest is `sha256("")`).
- `document_order` is dense and gapless (0, 1, 2, … N-1).
- Every segment has `segment_type`, `salience_tier`, `salience_signals`, `structural_path`, and `source_location`.
- `reassembly_digest` proves round-trip fidelity (property test).

---

## Extension points

The Decompose stage uses an **ordered pass pipeline** declared in
`src/finecorpus/pipeline/decompose/passes/__init__.py`.

### How to add a new pass (Phase 2+)

1. Create `src/finecorpus/pipeline/decompose/passes/<name>.py` and implement the
   `SegmentPass` protocol (defined in `passes/base.py`):
   - `run(doc_ctx, segments, exclusions) -> PassResult` — receives the current segment
     list and accumulated exclusions from previous passes; returns an updated `PassResult`.
   - Must be **pure and deterministic** — same inputs must produce same outputs.
     No I/O, no mutation of arguments.

2. Import the module-level singleton into `passes/__init__.py` and append it to
   `PASSES` (or insert at the correct position).

### Pass ordering

`PASSES` is an **ordered list**. The stage runs passes sequentially; each pass
receives the segment list produced by the previous pass.

Ordering rules:
- `segmentation_pass` must be **first** — it produces the initial segment list.
- `salience_pass` must follow segmentation — it has access to all structural context.
- Future passes (boilerplate, language, injection scoring) append **after** salience.

Current pass order (Phase 2):

```
segmentation_pass     → paragraph/heading splitting, exclusion recording,
                        cross-reference surface detection, segment_type_prior salience
salience_pass         → OCR-confidence overrides (ocr_confidence_floor, ocr_confidence_warn)
boilerplate_pass      → corpus-wide boilerplate reclassification
taxonomy_pass         → hint-driven full taxonomy typing (code/list/figure_region/form_field)
language_pass         → per-segment BCP-47 language detection (§7.6)
xref_resolve_pass     → intra-document cross-reference target resolution (§6.4)
injection_pass        → injection-suspicion scoring and invisible-content flag propagation
superseded_version_pass → D-25 tier override (must be last)
```

See `docs/pipeline/taxonomy-language-xref.md` for full documentation of the three
Phase 2 typing passes.

### `PassResult` fields

Each `run()` call returns a `PassResult` (defined in `passes/base.py`):

| Field | Type | Purpose |
|---|---|---|
| `segments` | `list[Segment]` | Replaces the segment list for the next pass |
| `exclusions` | `list[ExclusionRecord]` | New exclusions produced by this pass (accumulated) |
| `cross_references` | `list[CrossReference]` | New cross-references produced (accumulated; most passes leave this empty) |

### `DocumentContext` fields

Each pass receives a `DocumentContext` (defined in `passes/base.py`):

| Field | Type | Purpose |
|---|---|---|
| `document_id` | `str` | Stable document identifier |
| `content_hash` | `str` | SHA-256 content fingerprint |
| `tenancy` | `TenancyBlock` | Workspace / KB / permission block |
| `parse_result` | `dict` | Full parse result (regions, findings, parse_status, etc.) |
| `decomposed_at` | `datetime` | Run timestamp from the orchestrator |
