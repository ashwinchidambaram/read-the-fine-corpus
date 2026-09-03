# Contract 2 — Parse result

Stage boundary: **Assess → Decompose**. Governing spec: §6.2, §12 (Parse result invariants),
§14.1 (invisible content), §14.4 (sensitive content), §7.6 (language).

The Parse result answers "can we even read this" (§6.2). It carries a per-document quality score,
per-page/per-region confidence **retained, not thresholded** (§6.2), and represents failures
rather than dropping them (§6 rule 6, §12). Every extraction traces to a `SourceLocation` (§12).
Boilerplate candidates, detected content classes, invisible-content detections, encoding issues,
and per-region language detection are all recorded here for Decompose and Plan to consume.

Root model: `ParseResult`, one per `InventoryItem` that Collect passed through.

---

## `ParseResult` (root)

| Field | Type | Required | Semantics |
|---|---|---|---|
| `schema_version` | `str` (semver) | yes | Contract version. |
| `tenancy` | `TenancyBlock` | yes | Inherited from the source document's inventory record. |
| `document_id` | `str` | yes | The parsed document (Inventory `document_id`). |
| `content_hash` | `str` | yes | Version of the document parsed (Inventory `content_hash`). |
| `parser` | `ParserRef` | yes | Which parser/engine and version produced this (audit, reproducibility). |
| `parsed_at` | `datetime` (UTC) | yes | When parsing completed. |
| `parse_status` | `enum{parsed, partial, failed, unreadable, excluded_pre_parse}` | yes | Overall outcome. `partial` = some regions failed but others succeeded; both represented. `failed`/`unreadable` still emit a record (§6 rule 6). |
| `document_kind` | `enum{native_pdf, scanned_pdf, mixed_pdf, html, spreadsheet, plaintext, office_doc, other}` | yes | High-level parsed nature (native vs scanned PDF is a MUST detection, §6.2). |
| `quality` | `QualityScore` | yes | Document-level quality findings. |
| `pages` | `list[PageResult]` | yes (may be empty) | Per-page confidence and findings for paged sources. Empty for non-paged (HTML, spreadsheet) which use `regions`. |
| `regions` | `list[RegionResult]` | yes (may be empty) | Per-region extraction records; the traceable extraction units. |
| `boilerplate_candidates` | `list[BoilerplateCandidate]` | yes (may be empty) | Repeated-text spans flagged as boilerplate (§6.2, reuses dedup machinery). |
| `content_classes` | `list[enum]` (segment-class hints) | yes (may be empty) | Detected content classes requiring special handling/exclusion (§6.2, §7.5). |
| `encoding_issues` | `list[EncodingIssue]` | yes (may be empty) | Encoding corruption detections (§6.2). |
| `language_distribution` | `list[LanguageShare]` | yes (may be empty) | Per-document language mix (§7.6), aggregated from region-level detection. |
| `findings` | `list[Finding]` | yes (may be empty) | Structured findings feeding the plain-language quality report (§6.2). |

## `ParserRef`

| Field | Type | Required | Semantics |
|---|---|---|---|
| `name` | `str` | yes | Parser engine (e.g. `pdfium`, `tesseract`, `unstructured`). |
| `version` | `str` | yes | Engine version, for reproducibility. |
| `ocr_engine` | `str` | no | OCR engine when applicable. |

## `QualityScore`

| Field | Type | Required | Semantics |
|---|---|---|---|
| `overall` | `float` [0,1] | yes | Composite document parse quality. Not a threshold gate — retained for reporting and downstream weighting (§6.2). |
| `text_extraction_ratio` | `float` [0,1] | no | Fraction of the document from which text was extracted. Low values flag near-empty extraction (§6.2). |
| `table_structure_retained` | `enum{n_a, full, partial, lost}` | yes | Table structure loss during parsing (§6.2 MUST detect). `n_a` if no tables. |
| `is_near_empty` | `bool` | yes | Empty or near-empty extraction (§6.2 MUST detect). |
| `mean_ocr_confidence` | `float` [0,1] | no | Mean OCR confidence across pages, null for native text. Detail lives per-page (not thresholded away, §6.2). |

## `PageResult`

| Field | Type | Required | Semantics |
|---|---|---|---|
| `page_number` | `int` | yes | 1-based. |
| `is_scanned` | `bool` | yes | Native-text vs scanned distinction at page granularity (a mixed PDF has both). |
| `ocr_confidence` | `float` [0,1] | no | **Per-page OCR confidence, retained not thresholded** (§6.2 MUST). Null when the page is native text. Propagates toward `Provenance.ocr_confidence`. |
| `extraction_ratio` | `float` [0,1] | no | Text extracted from this page. |
| `invisible_content` | `list[InvisibleContentDetection]` | yes (may be empty) | Per-page invisible-content detections (§14.1). |

## `RegionResult`

The traceable extraction unit. Every extracted span of text is a region with a source location.

| Field | Type | Required | Semantics |
|---|---|---|---|
| `region_id` | `str` (ULID) | yes | Region identity, referenced by Decompose. |
| `location` | `SourceLocation` | yes | **Where in the source this extraction came from** (§12: every extraction traces to a source location). |
| `text` | `str` | no | Extracted text for this region. Null when extraction failed (still represented via `extract_status`). |
| `extract_status` | `enum{ok, low_confidence, failed, empty}` | yes | Per-region outcome. Failures are represented, not dropped (§12). |
| `ocr_confidence` | `float` [0,1] | no | Region-level OCR confidence (finer than page; used for low-confidence down-weighting, §6.4). |
| `language` | `str` (BCP-47) | no | Detected language for this region (§7.6). `und` if undetermined. |
| `detected_class_hint` | `enum` | no | Provisional content-class hint (table, prose, code, …) to seed Decompose typing. |
| `encoding_issue` | `bool` | yes | Whether this region shows encoding corruption. |

## `BoilerplateCandidate`

| Field | Type | Required | Semantics |
|---|---|---|---|
| `candidate_id` | `str` (ULID) | yes | Identity. |
| `text_fingerprint` | `str` | yes | Fingerprint of the repeated span (from dedup machinery, §6.2). |
| `occurrence_count` | `int` | yes | How many documents/positions across the corpus carry it (repetition is the definition of boilerplate, §6.2). |
| `example_locations` | `list[SourceLocation]` | yes | Sample locations in this document. |
| `boilerplate_kind` | `enum{header, footer, legal_preamble, revision_block, nav_chrome, other}` | no | Best-guess category (§6.2). |

## `EncodingIssue`

| Field | Type | Required | Semantics |
|---|---|---|---|
| `location` | `SourceLocation` | yes | Where corruption was detected. |
| `kind` | `enum{mojibake, replacement_chars, control_chars, bidi_confusable, other}` | yes | Type of corruption. |
| `severity` | `enum{low, medium, high}` | yes | Impact estimate. |

## `InvisibleContentDetection`

Detected at parse time (§14.1 MUST — a known PDF injection vector).

| Field | Type | Required | Semantics |
|---|---|---|---|
| `kind` | `enum{white_on_white, zero_size_font, off_page, metadata_only, render_hidden}` | yes | Invisible-content mechanism (§14.1). |
| `location` | `SourceLocation` | yes | Where it was found (bbox / page used for off-page). |
| `text` | `str` | no | The hidden text (retained, not stripped — labelled not sanitized, §14.1). |

## `LanguageShare`

| Field | Type | Required | Semantics |
|---|---|---|---|
| `language` | `str` (BCP-47) | yes | Detected language. |
| `fraction` | `float` [0,1] | yes | Share of document content in this language (§7.6 distribution). |

## `Finding`

| Field | Type | Required | Semantics |
|---|---|---|---|
| `code` | `str` (stable id) | yes | e.g. `scanned_pdf`, `table_structure_lost`, `near_empty`, `encoding_corruption`, `password_protected`, `pii_detected`. |
| `severity` | `enum{info, warning, error}` | yes | For report prioritization. |
| `location` | `SourceLocation` | no | Where, when applicable. |
| `message` | `str` | yes | Plain-language message for the client-presentable findings report (§6.2). |
| `sensitivity_flags` | `list[enum]` | no | PII/sensitive detections attached here (§14.4), advisory, no redaction. |

---

## Invariants

- Every parse carries a `QualityScore` and per-page/per-region confidence (§12). Confidence is
  **retained, never thresholded away** (§6.2): a low-confidence page is present with its low
  number, not dropped.
- Failures are represented, not dropped (§12, §6 rule 6): `parse_status`, `extract_status`, and
  status findings capture every failure with a reason.
- **Every extraction (`RegionResult`) traces to a `SourceLocation`** (§12). A region without a
  resolvable location is a defect.
- Native-text vs scanned PDF is detected and recorded (`is_scanned` per page, `document_kind`),
  as are table-structure loss, near-empty extraction, encoding corruption, boilerplate, and
  unreadable/password-protected files (all §6.2 MUSTs).
- Invisible-content detections are recorded with their hidden text retained (§14.1: labelled,
  not sanitized).
- Language is detected per region and aggregated to `language_distribution` (§7.6).
- `tenancy` present (Phase 0 MUST).

## Golden-corpus expressibility

- **Poorly scanned PDF with known-degraded regions:** `document_kind=scanned_pdf` (or `mixed_pdf`),
  per-page `ocr_confidence` low on the degraded pages and higher elsewhere — the whole point of
  retaining, not thresholding. `RegionResult.extract_status=low_confidence` on the bad regions.
- **Complex/nested tables:** `table_structure_retained=partial|lost` with `Finding` codes; region
  hints of `table`.
- **Adversarial injection doc:** `InvisibleContentDetection` entries (white-on-white, off-page)
  with retained hidden text; injection *scoring* itself is a segment-level provenance field
  produced downstream, but the invisible-content evidence originates here.
- **Confluence-style HTML:** `document_kind=html`, `regions` addressed by `dom_path`.
- **Bloated manual:** many `BoilerplateCandidate`s (repeated headers/footers/revision blocks) and
  a mix of `content_classes`.

## Open questions

1. **Where injection-pattern *scoring* runs.** Invisible-content evidence is unambiguously a
   parse-time detection (§14.1). Imperative/role-marker injection *scoring* (§14.1) reads text
   that only becomes well-formed after segmentation. **Proposed default:** compute
   `injection_suspicion` at Decompose (per segment) using parse evidence as input, and store the
   final score in segment/chunk provenance; Parse only records the raw invisible-content
   detections. **Flagged** — needs confirmation so the field has one owner.
2. **PII detection stage.** §14.4 says "at ingestion" without pinning a stage. **Proposed
   default:** run at Assess so it surfaces in the findings report (§14.4 requires that), attach
   to `Finding.sensitivity_flags`, and propagate to `Provenance.sensitivity_flags`. **Flagged.**
3. **Region granularity.** How finely to cut regions (per paragraph? per layout block?).
   **Proposed default:** layout-block granularity from the parser, refined into segments at
   Decompose. **Flagged** — too coarse loses OCR-confidence resolution; too fine bloats the
   artifact.
4. **`quality.overall` formula.** Same open question as README-4 (composite confidence). **Proposed
   default:** documented weighted combination of extraction ratio, OCR confidence, and structure
   retention. **Flagged.**
