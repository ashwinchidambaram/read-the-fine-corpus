# Assess Stage

Status: **Phase 2 real implementation** (native-text PDF, scanned/image-only PDF via OCR,
mixed PDF, HTML, and spreadsheets with triage).
Governing spec: §6.2, §6.4, §12, §18.2. Output contract: `ParseResultBatch` (§12a).

The Assess stage produces one `ParseResult` per inventory item. It never drops an item: every
document gets a result with an explicit `parse_status` — `parsed`, `partial`, `failed`, or
`excluded_pre_parse`. Non-servable content is honestly reported, never fake-succeeded.

---

## Phase 2 scope

| Extension / classification | Outcome | Parser |
|---|---|---|
| `.pdf` (native text) | `parsed` or `partial` — real per-page extraction via pypdf | `pdf_native` |
| `.pdf` (encrypted) | `failed` with `password_protected` finding | `pdf_native` |
| `.pdf` (image-only, 0 text chars on all pages) | `parsed` via OCR — pytesseract; per-page confidence retained | `pdf_scanned` |
| `.pdf` (mixed: native text + embedded scanned pages) | `parsed` with `document_kind=mixed_pdf` — low-text pages with images are OCR'd in place | `pdf_native` |
| `.pdf` (malformed, decompression errors) | `partial` — successful pages parsed, failed pages recorded | `pdf_native` |
| `.html`, `.htm` | `parsed` — structure extraction (headings, prose, code, tables, lists) | `html` |
| `.xlsx`, `.xls`, `.ods` (report kind) | `parsed` — triage then sheet/region extraction | `spreadsheet` |
| `.xlsx`, `.xls`, `.ods` (database kind) | `excluded_pre_parse` — not vectorizable (§6.4) | `spreadsheet` |
| `.xlsx`, `.xls`, `.ods` (model kind) | `excluded_pre_parse` — stale snapshot risk (§6.4) | `spreadsheet` |
| `.csv` | `excluded_pre_parse` — `csv_not_supported` (openpyxl cannot parse CSV) | `spreadsheet` |
| `.wav`, `.mp3`, `.mp4`, `.mov`, `.avi` | `excluded_pre_parse` (unservable content) | `audio/video` |
| `.dwg`, `.dxf`, `.stl` | `excluded_pre_parse` (unservable content) | `cad` |
| Everything else | `excluded_pre_parse` (unrecognized extension) | `fallback` |

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
audio_parser              → .wav, .mp3, etc.   → excluded_pre_parse
video_parser              → .mp4, .mov, etc.   → excluded_pre_parse
cad_parser                → .dwg, .dxf, etc.   → excluded_pre_parse
html_format_parser        → .html, .htm        → parsed
spreadsheet_format_parser → .xlsx / .csv       → parsed (report) / excluded_pre_parse (database/model/csv)
pdf_scanned_parser        → .pdf (image-only)  → parsed via OCR
native_pdf_parser         → .pdf (native/mixed)→ parsed / partial / failed
fallback_parser           → everything else    → excluded_pre_parse
```

`pdf_scanned_parser` is placed before `native_pdf_parser`. Its `can_parse` checks whether
pypdf extracts any text from the PDF:
- Zero text on all pages, no decompression errors → `True` (OCR path)
- Any text extractable, or any error → `False` (native or fallback path)

Mixed PDFs (native text plus embedded scanned pages) fall through to
`native_pdf_parser`, which detects low-text pages carrying images and OCRs them
in place (`document_kind=mixed_pdf`).

---

## HTML parser (`parsers/html.py`)

The HTML parser handles `.html` and `.htm` files using the Python stdlib `html.parser`
module — no external dependency. It converts document structure into typed regions.

### Extraction rules

| Source element | Output region kind | Notes |
|---|---|---|
| `h1`–`h6` | `prose` | Heading text; `detected_class_hint="heading"` |
| `p`, `div`, `section`, `article` | `prose` | Body prose; `detected_class_hint="paragraph"` |
| `pre`, `code` | `code` | Verbatim text |
| `ul`, `ol` (+ `li` items) | `list` | Markdown-serialized (`- item`) |
| `table` | `table` | Markdown-serialized; no blank lines (decompose-safe) |
| `a[href]` | finding (`link_record`) | See §6.1 web-link policy |

### Source location format

Regions carry `LocatorKind.dom_path` locations encoding a simplified XPath-like
path through the document tree, e.g. `body/div[1]/table[2]`.

### Web-link policy (§6.1)

Links found in HTML are **references**, not content. The parser:

1. Records each `<a href="...">` as a `ParseResult.finding` with code `link_record`.
2. Does **not** fetch the linked URL.
3. Flags the finding as `OQ-HTML-1` — a reconciliation note for Phase 3 to move
   these records into `Inventory.links`.

The default fetch policy is **ignore**. Links are inventoried so they can be acted on
by downstream stages, but they do not gate the parse result.

### Nested tables (OQ-9)

When an inner `<table>` appears inside an outer table cell, the parser:

1. Extracts the inner table as a **separate `table` region** with `is_nested=True`.
2. Renders the outer cell that contained the inner table as an empty string (`""`).
3. Emits a `nested_table_detected` finding for traceability.

This matches OQ-9's requirement that nested tables be addressable as independent
table regions rather than embedded inside the outer table's serialized text.

### Decompose compatibility

Phase 1 segmentation splits region text on blank lines. The Markdown table
serialization used by this parser guarantees **no blank lines** within a table
region's text. The `detected_class_hint` field is carried through so Phase 3
taxonomy can promote the segment to the proper type.

---

## Spreadsheet parser (`parsers/spreadsheet.py`)

The spreadsheet parser handles `.xlsx` files using `openpyxl`. It applies a
**triage-first** strategy: classify the workbook kind before deciding whether to
ingest it.

### Triage

Every spreadsheet is classified into one of three kinds:

| Kind | Signal | Default action |
|---|---|---|
| `model` | formula ratio ≥ 0.30 across all non-empty cells | `excluded_pre_parse` |
| `database` | single sheet, no charts, uniform column structure, ≥ 50 data rows | `excluded_pre_parse` |
| `report` | multiple sheets OR chart presence OR ≥ 2 prose text blocks | `parsed` |

Triage is always performed first, and the scores are always recorded as a
`spreadsheet_triage` finding in `ParseResult.findings` — even for excluded results.
This means the classification rationale is visible and auditable.

**Critical implementation note:** Triage must load the workbook with
`data_only=False` so that formula strings (`=SUM(A1:A10)`) are visible in cell
values. Loading with `data_only=True` (the openpyxl default) returns cached
computed values, making formula detection impossible.

### Triage scores per fixture

| Fixture | Kind | Dominant signal | Formula ratio | Sheet count |
|---|---|---|---|---|
| `report_spreadsheet.xlsx` | `report` | chart presence + 3 sheets | ≈ 0.00 | 3 |
| `database_spreadsheet.xlsx` | `database` | 501 uniform data rows | 0.00 | 1 |
| `model_spreadsheet.xlsx` | `model` | formula ratio = 0.77 | 0.77 | 1 |

### Override

Triage classification can be overridden at the Plan stage via
`IngestionConfig.spreadsheet_triage[].source = "user_override"`. When a
`user_override` entry matches the item's source path, the override kind is used
instead of the computed kind, and the triage finding records both the computed
kind and the override kind.

### Report parsing

When triage classifies a workbook as `report`, the parser extracts content
sheet by sheet:

- **Prose blocks** — contiguous cells that look like paragraph text → `prose` region.
- **Table blocks** — contiguous rectangular data with an inferred header row → `table`
  region, Markdown-serialized.
- **Chart objects** — chart titles extracted from the chart's `title` property →
  `figure` region with `detected_class_hint="chart"`.

Each sheet becomes a logical section; regions carry `LocatorKind.cell_range`
locations in A1 notation, e.g. `Sheet1!B2:D5`.

### Decompose compatibility

As with the HTML parser, table regions are Markdown-serialized with no blank lines,
making them safe to pass through Phase 1 segmentation. The `detected_class_hint`
field marks them for Phase 3 taxonomy promotion.
