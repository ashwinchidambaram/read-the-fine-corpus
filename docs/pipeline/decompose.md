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

## Segment types produced (Phase 1)

| Type | When produced |
|---|---|
| `heading` | Line classified as a heading by the heuristic |
| `front_matter` | Paragraph on page 1, among the first 3 paragraphs, matching a front-matter pattern (title/author/date/abstract/revision) |
| `cross_reference` | Paragraph matching cross-reference patterns (`see section`, `refer to`, `as described in`, `ibid`, etc.) |
| `prose` | All other non-empty paragraphs |
| `unknown` | Paragraphs that cannot be classified (reserved for edge cases; rarely emitted in practice) |

Segment types not produced in Phase 1 (Phase 2+): `table`, `list_`, `code`, `figure_caption`,
`figure_region`, `form_field`, `boilerplate`, `revision_history`, `scanned_region`.

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
