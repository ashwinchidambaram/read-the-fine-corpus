# Assess Stage

Status: **Phase 2 real implementation** (native-text PDF + scanned/image-only PDF; HTML/spreadsheet → Phase 3).
Governing spec: §6.2, §12, §18.2. Output contract: `ParseResultBatch` (§12a, schema v1.0.0).

The Assess stage produces one `ParseResult` per inventory item. It never drops an item: every
document gets a result with an explicit `parse_status` — `parsed`, `partial`, `failed`, or
`excluded_pre_parse`. Non-servable content is honestly reported, never fake-succeeded.

---

## Phase 2 scope

Phase 2 adds scanned PDF support (OCR via tesseract) on top of the Phase 1 native-text parser.

| Extension / classification | Outcome |
|---|---|
| `.pdf` (native text) | `parsed` or `partial` — real per-page extraction via pypdf |
| `.pdf` (image-only, 0 text chars on all pages) | `parsed` via OCR — pytesseract; per-page confidence retained |
| `.pdf` (encrypted) | `failed` with `password_protected` finding |
| `.pdf` (malformed, decompression errors) | `partial` — successful pages parsed, failed pages recorded |
| `.html`, `.htm` | `excluded_pre_parse` — Phase 3 |
| `.xlsx`, `.xls`, `.csv`, `.ods` | `excluded_pre_parse` — Phase 3 |
| `.wav`, `.mp3`, `.mp4`, `.mov`, `.avi` | `excluded_pre_parse` (unservable content) |
| `.dwg`, `.dxf`, `.stl` | `excluded_pre_parse` (unservable content) |
| Everything else | `excluded_pre_parse` (unrecognized extension) |

---

## Scanned PDF parser

**Module:** `src/finecorpus/pipeline/assess/parsers/pdf_scanned.py`
**Parser ref:** `name="pytesseract"`, `ocr_engine="tesseract"`

### Claim ordering

`pdf_scanned_parser` is placed **before** `native_pdf_parser` in the registry. Its
`can_parse` method opens the PDF and checks whether pypdf extracts any text. If zero
text on all pages and no decompression errors, it returns `True` (image-only PDF → OCR
path). Otherwise it returns `False` and `native_pdf_parser` handles the file.

This design:
- Pays only a cheap pypdf-open cost for the native fast path (99% of PDFs).
- Keeps claim logic explicit in each parser — no sentinel values on `ParseResult`.
- Gracefully defers malformed/encrypted PDFs to the native parser (errors in `can_parse`
  return `False`).

### Confidence derivation

Per-page OCR confidence is the **mean of tesseract word-level confidence values**,
normalised to [0.0, 1.0]:

```
word_confs = [c for c in image_to_data(img)["conf"] if c != -1]
page_confidence = mean(word_confs) / 100.0
```

Words reported with `conf == -1` (non-text layout elements such as line separators) are
excluded from the mean. If a page yields zero scoreable words (e.g. a blank page after
rasterization), the page confidence is `0.0`.

**Per-page confidence is RETAINED raw — not thresholded at this stage (§6.2 MUST).**
The salience thresholds (`assessment.ocr_confidence_exclude_floor = 0.60`,
`assessment.ocr_confidence_warn_level = 0.80`) act at the Decompose / `SaliencePass` level.

### Image extraction

Pages are rasterized by extracting the embedded image directly via pypdf's `page.images`
property (no poppler/pdf2image needed). Each page image is a `PIL.Image.Image`. If no
embedded image is found for a page, that page is recorded as a failed region (no silent
content loss — §12).

### System dependency: tesseract

The `tesseract` binary must be on PATH. The parser checks availability via `shutil.which`
before doing any work.

**If tesseract is absent:**
- `parse()` returns an honest `parse_status=failed` result with a `missing_dependency`
  finding naming the absent binary.
- `can_parse()` is unaffected — it does not call tesseract.
- No exception is raised (FormatParser contract invariant).

**Installation:**

| Environment | Command |
|---|---|
| Debian/Ubuntu (CI, Docker) | `apt-get install -y tesseract-ocr` |
| macOS (local dev) | `brew install tesseract` |
| Alpine | `apk add tesseract-ocr` |

### Quality score for OCR documents

```
mean_ocr_confidence  = mean of per-page confidences
text_extraction_ratio = pages_with_substantial_text / total_pages
overall = 0.7 * mean_ocr_confidence + 0.3 * text_extraction_ratio
is_near_empty = text_extraction_ratio < near_empty_threshold (default 0.20)
```

`quality.mean_ocr_confidence` is always populated for scanned PDFs (it is `None` for
native-text PDFs).

### Findings emitted

| Code | Severity | When |
|---|---|---|
| `missing_dependency` | `error` | `tesseract` binary not on PATH |
| `low_ocr_confidence` | `warning` | The page with the lowest confidence is below `ocr_confidence_warn_level` (0.80) |
| `near_empty` | `warning` | `text_extraction_ratio < near_empty_threshold` |
| `ocr_no_text_extracted` | `warning` | All pages yielded zero text after OCR (graphics-only document) |
| `password_protected` | `error` | Encrypted PDF |
| `parse_error` | `error` | PDF could not be opened |

---

## Decompose interaction for scanned regions

### Sub-decomposition (OQ-4)

The threshold `assessment.ocr_sub_decompose_confidence_floor` (default `0.85`) controls
whether OCR pages get paragraph-level segmentation or remain as a single `scanned_region`:

- **confidence >= 0.85** — `SegmentationPass` applies paragraph splitting to the OCR text,
  emitting `prose`, `heading`, and other typed segments. Each segment carries the source
  region's `ocr_confidence` in its `ocr_confidence` field.

- **confidence < 0.85** — one `scanned_region` segment per page. The entire OCR text is
  stored in that segment. `ocr_confidence` is set to the page confidence.

### Salience overrides (OQ-7, §6.4)

`SaliencePass` applies OCR-confidence tier overrides after segmentation. Thresholds from
`DocumentContext` (mirroring `docs/configuration/reference.md §2.5`):

| Condition | Signal kind | Tier |
|---|---|---|
| `confidence < ocr_confidence_exclude_floor` (0.60) | `ocr_confidence_floor` (won=True) | `excluded` |
| `ocr_confidence_exclude_floor <= confidence < ocr_confidence_warn_level` (0.60–0.80) | `ocr_confidence_warn` (won=True) | `supporting` |
| `confidence >= ocr_confidence_warn_level` (0.80) | no OCR override | type-prior tier |

The overriding signal replaces the type-prior as the winning (`won=True`) signal. The
type-prior is retained in `salience_signals` with `won=False` so the §11.5 explain-mode
trace still shows it as a contributing (but losing) signal.

---

## Quality score heuristic (native-text PDF)

Every `ParseResult` for a native-text PDF carries a `QualityScore`. The heuristic combines
two signals that are cheap to compute without any ML:

```
extraction_density   = total_extracted_chars / (total_pages * _MIN_CHARS_PER_PAGE)
                       clamped to [0.0, 1.0]

empty_page_ratio     = count(pages where extracted_chars < _PAGE_NONEMPTY_CHARS) / total_pages

overall              = 0.7 * extraction_density + 0.3 * (1 - empty_page_ratio)
```

Constants:
- `_MIN_CHARS_PER_PAGE = 200` — chars/page a typical text-dense document is expected to yield
- `_PAGE_NONEMPTY_CHARS = 50` — below this threshold a page is counted as empty
- `_NEAR_EMPTY_THRESHOLD = 0.20` — `overall < 0.20` → `is_near_empty = True`

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

| Field | Native-text PDF (Phase 1) | Scanned PDF (Phase 2) |
|---|---|---|
| `page_number` | 1-based page number | 1-based page number |
| `is_scanned` | `False` | `True` |
| `ocr_confidence` | `None` | Mean word confidence [0.0, 1.0] — RETAINED raw |
| `extraction_ratio` | Fraction of expected chars extracted | Fraction of expected chars from OCR text |
| `invisible_content` | Detections per §14.1 | Empty (OCR pages have no invisible-content layer) |

---

## FindingCode values emitted

| Code | Severity | Parser | When |
|---|---|---|---|
| `password_protected` | `error` | native + scanned | Encrypted PDF |
| `image_only_pdf` | — | — | **Removed in Phase 2** — image-only PDFs are now OCR-parsed |
| `decompression_error` | `error` | native | pypdf logs `"Error -N while decompressing"` |
| `encoding_replacement_chars` | `warning` | native | Extracted text contains U+FFFD |
| `encoding_control_chars` | `warning` | native | Extracted text contains unexpected control chars |
| `encoding_mojibake` | `warning` | native | Extracted text contains known mojibake sequences |
| `missing_dependency` | `error` | scanned | `tesseract` binary not on PATH |
| `low_ocr_confidence` | `warning` | scanned | Lowest-confidence page below warn level (0.80) |
| `near_empty` | `warning` | native + scanned | `text_extraction_ratio < near_empty_threshold` |
| `ocr_no_text_extracted` | `warning` | scanned | All OCR pages returned empty text |

---

## Invariants

- Every inventory item produces exactly one `ParseResult`. No items are silently dropped.
- `parse_status` is always one of the four enumerated values — never a silent "best effort".
- `quality` is present for `parsed` and `partial` results; `None` for `failed` and `excluded_pre_parse`.
- `ocr_confidence` is `None` for all native-text pages. It is RETAINED raw (not thresholded)
  for scanned pages — thresholds act at Decompose / salience-pass level (§6.2 MUST).
- Non-PDF files and unservable PDFs produce `excluded_pre_parse` results, not empty successes.

---

## Extension points

The Assess stage uses a **parser registry** declared in
`src/finecorpus/pipeline/assess/parsers/__init__.py`.

### How to add a new parser (Phase 3+)

1. Create `src/finecorpus/pipeline/assess/parsers/<name>.py` and implement the
   `FormatParser` protocol (defined in `parsers/base.py`):
   - `can_parse(item) -> bool` — returns `True` if this parser claims the inventory item.
   - `parse(item, tenancy, parsed_at, ctx) -> ParseResult` — performs extraction.
     Must never raise; all errors are encoded as `parse_status` / `findings`.

2. Import the module-level singleton into `parsers/__init__.py` and insert it
   into `REGISTRY` at the appropriate position.

### Registry ordering

`REGISTRY` is an **ordered list**. For each inventory item, the stage calls
`can_parse` on parsers in order and routes to the **first match**.

Ordering rules:
- More-specific parsers go **before** less-specific ones.
- `FallbackUnsupportedParser` always returns `True` from `can_parse` and **must be last**.

Current registry order (Phase 2):

```
audio_parser          → .wav, .mp3, etc.   → excluded_pre_parse
video_parser          → .mp4, .mov, etc.   → excluded_pre_parse
cad_parser            → .dwg, .dxf, etc.   → excluded_pre_parse
html_parser           → .html, .htm        → excluded_pre_parse (Phase 3 replaces)
spreadsheet_parser    → .xlsx, .csv, etc.  → excluded_pre_parse (Phase 3 replaces)
pdf_scanned_parser    → .pdf (image-only)  → parsed via OCR      ← Phase 2 addition
native_pdf_parser     → .pdf (native text) → parsed / partial / failed
fallback_parser       → everything else    → excluded_pre_parse
```

`pdf_scanned_parser` is placed before `native_pdf_parser`. Its `can_parse` checks whether
pypdf extracts any text from the PDF:
- Zero text on all pages, no decompression errors → `True` (OCR path)
- Any text extractable, or any error → `False` (native or fallback path)
