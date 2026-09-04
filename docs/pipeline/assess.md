# Assess Stage

Status: **Phase 1 real implementation** (native-text PDF only; scanned/HTML/spreadsheet → Phase 2).
Governing spec: §6.2, §12, §18.2. Output contract: `ParseResultBatch` (§12a, schema v1.0.0).

The Assess stage produces one `ParseResult` per inventory item. It never drops an item: every
document gets a result with an explicit `parse_status` — `parsed`, `partial`, `failed`, or
`excluded_pre_parse`. Non-servable content is honestly reported, never fake-succeeded.

---

## Phase 1 scope

Phase 1 handles one content type: **native-text PDF**. All others are honestly excluded.

| Extension / classification | Outcome |
|---|---|
| `.pdf` (native text) | `parsed` or `partial` — real per-page extraction via pypdf |
| `.pdf` (encrypted) | `failed` with `password_protected` finding |
| `.pdf` (image-only, 0 text chars on all pages) | `failed` with `image_only_pdf` finding |
| `.pdf` (malformed, decompression errors) | `partial` — successful pages parsed, failed pages recorded |
| `.html`, `.htm` | `excluded_pre_parse` — Phase 2 |
| `.xlsx`, `.xls`, `.csv`, `.ods` | `excluded_pre_parse` — Phase 2 |
| `.wav`, `.mp3`, `.mp4`, `.mov`, `.avi` | `excluded_pre_parse` (unservable content) |
| `.dwg`, `.dxf`, `.stl` | `excluded_pre_parse` (unservable content) |
| Everything else | `excluded_pre_parse` (unrecognized extension) |

---

## Quality score heuristic

Every `ParseResult` for a native-text PDF carries a `QualityScore`. The heuristic combines
two signals that are cheap to compute without any ML:

```
extraction_density   = total_extracted_chars / (total_pages * _MIN_CHARS_PER_PAGE)
                       clamped to [0.0, 1.0]

empty_page_ratio     = count(pages where extracted_chars < _PAGE_NONEMPTY_CHARS) / total_pages

overall              = 0.7 * extraction_density + 0.3 * (1 - empty_page_ratio)
```

Constants (documented in `stage.py`):
- `_MIN_CHARS_PER_PAGE = 200` — chars/page a typical text-dense document is expected to yield
- `_PAGE_NONEMPTY_CHARS = 50` — below this threshold a page is counted as empty
- `_NEAR_EMPTY_THRESHOLD = 0.20` — `overall < 0.20` → `is_near_empty = True`

**Design rationale.** Extraction density rewards documents where pypdf yields substantial text
relative to their page count. Empty-page ratio penalizes image-heavy PDFs where some pages have
renderable content but no selectable text. The 0.7 / 0.3 weighting treats density as the primary
signal because a document that is dense on its text-bearing pages should score well even if it
has some blank filler pages (title page, intentional blanks).

**Limits.** The formula makes no claim about linguistic quality, sentence coherence, or reading
level. A page of repeated characters scores highly. Evaluation against the golden fixtures shows
the formula is a reasonable gating signal for downstream processing but should not be used as a
retrieval-quality predictor.

---

## pypdf warning capture

pypdf 6.x does not raise exceptions for decompression failures — it logs a Python
`logging.WARNING` of the form `"Error -3 while decompressing"` and returns an empty string for
the affected page. A custom `_PyPDFWarningCapture` logging handler is attached per-page to
capture these messages and mark affected pages as `ExtractStatus.failed`, setting `parse_status`
to `partial` when at least one page succeeds and at least one fails.

Without this mechanism, a corrupted page would silently return `""` and be treated as empty text
rather than a parse failure — a violation of the nothing-dropped / honest-failure requirement.

---

## Page-level fields

Each `PageResult` carries:

| Field | Value (Phase 1) |
|---|---|
| `page_num` | 1-based page number |
| `extract_status` | `ok` if text extracted without warnings; `failed` if pypdf logged a decompression error |
| `char_count` | Number of Unicode characters extracted |
| `confidence` | `1.0` for native text (OCR confidence is Phase 2) |
| `text` | Extracted text, or `None` for failed pages |

---

## FindingCode values emitted by AssessStage

| Code | Severity | When |
|---|---|---|
| `password_protected` | `error` | `pypdf.errors.PdfStreamError` on open — file is encrypted |
| `image_only_pdf` | `warning` | PDF opened successfully, >0 pages, 0 total chars extracted, no decompression errors |
| `decompression_error` | `error` | pypdf logs `"Error -N while decompressing"` for any page |
| `encoding_replacement_chars` | `warning` | Extracted text contains Unicode replacement chars (U+FFFD) |
| `encoding_control_chars` | `warning` | Extracted text contains unexpected control characters |
| `encoding_mojibake` | `warning` | Extracted text contains known mojibake sequences |

---

## Invariants

- Every inventory item produces exactly one `ParseResult`. No items are silently dropped.
- `parse_status` is always one of the four enumerated values — never a silent "best effort".
- `quality` is present for `parsed` and `partial` results; `None` for `failed` and `excluded_pre_parse`.
- `confidence` is `1.0` for all successfully-extracted pages in Phase 1 (OCR confidence is Phase 2).
- Non-PDF files and unservable PDFs produce `excluded_pre_parse` results, not empty successes.
