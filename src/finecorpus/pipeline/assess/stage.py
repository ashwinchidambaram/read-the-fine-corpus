"""Stage 2 — Assess (Phase 1: real native-text PDF parsing via pypdf).

Produces a ParseResultBatch artifact: one ParseResult per inventory item.

Phase 1 scope — native-text PDF prose:
  - Native-text PDFs (.pdf extension, not encrypted, not image-only): real per-page
    text extraction via pypdf, quality scoring, page-level confidence 1.0, near-empty
    detection, encoding issue detection.
  - Scanned PDFs, HTML, spreadsheets, and all other file types:
    HONESTLY excluded with parse_status=excluded_pre_parse or appropriate failure.
    Phase 2 adds OCR confidence, boilerplate dedup, and other content types.

Quality score heuristic (documented here and in docs/pipeline/assess.md):
  - `text_extraction_ratio`: chars extracted / estimated_page_chars_expected.
    Estimated as: non-empty pages / total pages (simpler form for Phase 1 —
    a native-text page that yields 0 chars counts as a failed extraction).
  - `overall`: weighted combination:
      0.7 * extraction_density  +  0.3 * (1 - empty_page_ratio)
    where:
      extraction_density = min(1.0, total_chars / (pages * MIN_CHARS_EXPECTED_PER_PAGE))
      empty_page_ratio   = empty_pages / total_pages
    MIN_CHARS_EXPECTED_PER_PAGE = 200 (a "near-empty" page threshold; executor-defined).
    A document where every page yields >=200 chars gets extraction_density=1.0.
    A document where every page yields 0 chars gets overall=0.0.
  - `is_near_empty`: True if fewer than 20% of pages have >50 chars
    (i.e. nearly nothing was extracted from the document).

Non-PDF / unservable files → parse_status=excluded_pre_parse with a Finding.
Encrypted PDF → parse_status=failed + Finding(code="password_protected").
Malformed / partial PDF → parse_status=partial with per-page failures recorded.
Image-only PDF (all pages yield 0 chars but file is valid) → parse_status=failed
  with Finding(code="unservable_image_only") — cannot recover text without OCR.

D-26 resolution: ParseResultBatch is now an official versioned contract.
DecomposeStage checks schema_version against SUPPORTED_PARSE_RESULT_BATCH.

Nothing is silently dropped — every Inventory item gets exactly one ParseResult
entry (§6 rule 6, §12).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pypdf
import pypdf.errors

from finecorpus.contracts.parse_result import (
    DocumentKind,
    EncodingIssue,
    EncodingIssueKind,
    ExtractStatus,
    Finding,
    FindingSeverity,
    IssueSeverity,
    PageResult,
    ParseResult,
    ParserRef,
    ParseStatus,
    QualityScore,
    RegionClassHint,
    RegionResult,
    TableStructureRetained,
)
from finecorpus.contracts.parse_result_batch import (
    BATCH_SCHEMA_VERSION,
    ParseResultBatch,
)
from finecorpus.contracts.shared.blocks import (
    LocatorKind,
    PermissionFidelity,
    PermissionMode,
    PermissionSource,
    SourceLocation,
    TenancyBlock,
)
from finecorpus.contracts.versions import SUPPORTED_INVENTORY
from finecorpus.pipeline.stage import Stage

# ---------------------------------------------------------------------------
# Constants (executor-defined heuristics — documented in docs/pipeline/assess.md)
# ---------------------------------------------------------------------------

_PYPDF_VERSION = pypdf.__version__

_PARSER_REF = ParserRef(
    name="pypdf",
    version=_PYPDF_VERSION,
    ocr_engine=None,
)

# Phase 1: file extensions treated as native-text PDF candidates.
_PDF_EXTENSIONS = frozenset({".pdf"})

# Extensions we know are non-PDF and must be honestly excluded.
_AUDIO_EXTENSIONS = frozenset({".wav", ".mp3", ".aac", ".flac", ".ogg", ".m4a"})
_VIDEO_EXTENSIONS = frozenset({".mp4", ".mov", ".avi", ".mkv", ".webm"})
_CAD_EXTENSIONS = frozenset({".dwg", ".dxf", ".step", ".stp", ".iges", ".igs"})
_HTML_EXTENSIONS = frozenset({".html", ".htm"})
_SPREADSHEET_EXTENSIONS = frozenset({".xlsx", ".xls", ".csv", ".ods"})

# Quality scoring constants (executor-defined; see module docstring).
_MIN_CHARS_PER_PAGE: int = 200
"""Expected minimum characters per page for a well-extracted native-text page."""

_NEAR_EMPTY_THRESHOLD: float = 0.20
"""Fraction of pages that must have >50 chars to avoid is_near_empty=True."""

_PAGE_NONEMPTY_CHARS: int = 50
"""Minimum chars on a page for it to count as non-empty."""

# Encoding-issue detection patterns.
_REPLACEMENT_CHAR = "�"
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_MOJIBAKE_RE = re.compile(r"â€|Ã©|Ã|â€™|â€œ|â€")

# pypdf decompression-failure pattern in log messages
_DECOMPRESSION_ERROR_RE = re.compile(r"Error -\d+ while decompressing", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Context manager to capture pypdf warnings for per-page failure detection
# ---------------------------------------------------------------------------


class _PyPDFWarningCapture(logging.Handler):
    """Captures pypdf log messages."""

    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


@contextmanager
def _capture_pypdf_warnings() -> Generator[_PyPDFWarningCapture, None, None]:
    """Context manager: capture pypdf WARNING-level log messages."""
    handler = _PyPDFWarningCapture()
    logger = logging.getLogger("pypdf")
    old_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.WARNING)
    try:
        yield handler
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)


# ---------------------------------------------------------------------------
# Helper: classify a file before parsing
# ---------------------------------------------------------------------------


def _classify_extension(source_path: str) -> str:
    """Return a coarse classification based on file extension.

    Returns one of: 'pdf', 'audio', 'video', 'cad', 'html', 'spreadsheet', 'other'.
    """
    ext = Path(source_path).suffix.lower()
    if ext in _PDF_EXTENSIONS:
        return "pdf"
    if ext in _AUDIO_EXTENSIONS:
        return "audio"
    if ext in _VIDEO_EXTENSIONS:
        return "video"
    if ext in _CAD_EXTENSIONS:
        return "cad"
    if ext in _HTML_EXTENSIONS:
        return "html"
    if ext in _SPREADSHEET_EXTENSIONS:
        return "spreadsheet"
    return "other"


# ---------------------------------------------------------------------------
# Helper: detect encoding issues in extracted text
# ---------------------------------------------------------------------------


def _detect_encoding_issues(text: str, page_num: int) -> list[EncodingIssue]:
    """Detect encoding corruption in extracted page text.

    Checks for:
    - Replacement characters (U+FFFD)
    - Mojibake patterns (common UTF-8-as-Latin1 artifacts)
    - Unusual control characters (not tab/newline/CR)
    """
    issues: list[EncodingIssue] = []
    page_loc = SourceLocation(
        locator_kind=LocatorKind.page,
        page_start=page_num,
        page_end=page_num,
    )

    if _REPLACEMENT_CHAR in text:
        issues.append(
            EncodingIssue(
                location=page_loc,
                kind=EncodingIssueKind.replacement_chars,
                severity=IssueSeverity.medium,
            )
        )
    if _MOJIBAKE_RE.search(text):
        issues.append(
            EncodingIssue(
                location=page_loc,
                kind=EncodingIssueKind.mojibake,
                severity=IssueSeverity.low,
            )
        )
    if _CONTROL_CHAR_RE.search(text):
        issues.append(
            EncodingIssue(
                location=page_loc,
                kind=EncodingIssueKind.control_chars,
                severity=IssueSeverity.low,
            )
        )

    return issues


# ---------------------------------------------------------------------------
# Helper: compute quality score from per-page extraction data
# ---------------------------------------------------------------------------


def _compute_quality(
    page_char_counts: list[int],
    total_pages: int,
) -> QualityScore:
    """Compute QualityScore from per-page char counts.

    Heuristic (Phase 1 / executor-defined):
      extraction_density = min(1.0, mean_chars / MIN_CHARS_PER_PAGE)
      empty_page_ratio   = empty_pages / total_pages
      overall            = 0.7 * extraction_density + 0.3 * (1 - empty_page_ratio)
      text_extraction_ratio = non_empty_pages / total_pages
      is_near_empty      = (non_empty_pages / total_pages) < NEAR_EMPTY_THRESHOLD

    "empty" = 0 chars; "non-empty" = > PAGE_NONEMPTY_CHARS chars.

    For a document with 0 pages (e.g. degenerate), overall=0, is_near_empty=True.
    """
    if total_pages == 0:
        return QualityScore(
            overall=0.0,
            text_extraction_ratio=0.0,
            table_structure_retained=TableStructureRetained.n_a,
            is_near_empty=True,
            mean_ocr_confidence=None,
        )

    empty_pages = sum(1 for c in page_char_counts if c == 0)
    nonempty_pages = sum(1 for c in page_char_counts if c > _PAGE_NONEMPTY_CHARS)
    mean_chars = sum(page_char_counts) / total_pages if page_char_counts else 0.0

    extraction_density = min(1.0, mean_chars / _MIN_CHARS_PER_PAGE)
    empty_page_ratio = empty_pages / total_pages
    overall = 0.7 * extraction_density + 0.3 * (1.0 - empty_page_ratio)

    text_extraction_ratio = nonempty_pages / total_pages
    is_near_empty = text_extraction_ratio < _NEAR_EMPTY_THRESHOLD

    return QualityScore(
        overall=round(overall, 4),
        text_extraction_ratio=round(text_extraction_ratio, 4),
        table_structure_retained=TableStructureRetained.n_a,  # Phase 1: no table analysis
        is_near_empty=is_near_empty,
        mean_ocr_confidence=None,  # Phase 1: native text, no OCR
    )


# ---------------------------------------------------------------------------
# Core: parse a single native-text PDF
# ---------------------------------------------------------------------------


def _parse_native_pdf(
    document_id: str,
    content_hash: str,
    source_path: str,
    tenancy: TenancyBlock,
    parsed_at: datetime,
) -> ParseResult:
    """Parse a file that has a .pdf extension.

    Returns a ParseResult reflecting the actual state:
    - encrypted/unreadable → failed or unreadable
    - image-only (all pages 0 chars) → failed with unservable_image_only finding
    - decompression/stream errors on some pages → partial with per-page failures
    - successful native-text extraction → parsed

    Phase 1 scope note: scanned PDFs (no text layer) get parse_status=failed with
    finding code=unservable_image_only. Full OCR is Phase 2.
    """
    try:
        reader = pypdf.PdfReader(source_path)
    except pypdf.errors.FileNotDecryptedError:
        # Encrypted before we can even check
        return _make_failed(
            document_id=document_id,
            content_hash=content_hash,
            source_path=source_path,
            tenancy=tenancy,
            parsed_at=parsed_at,
            parse_status=ParseStatus.failed,
            document_kind=DocumentKind.native_pdf,
            finding_code="password_protected",
            finding_msg=(
                "Document is password-protected and cannot be decrypted. "
                "No text extraction possible without the password."
            ),
            finding_severity=FindingSeverity.error,
        )
    except (pypdf.errors.PdfStreamError, pypdf.errors.PdfReadError, Exception) as exc:
        return _make_failed(
            document_id=document_id,
            content_hash=content_hash,
            source_path=source_path,
            tenancy=tenancy,
            parsed_at=parsed_at,
            parse_status=ParseStatus.unreadable,
            document_kind=DocumentKind.other,
            finding_code="parse_error",
            finding_msg=f"PDF could not be opened: {exc}",
            finding_severity=FindingSeverity.error,
        )

    # Check encryption after open (some encrypted PDFs open but not decrypt)
    if reader.is_encrypted:
        return _make_failed(
            document_id=document_id,
            content_hash=content_hash,
            source_path=source_path,
            tenancy=tenancy,
            parsed_at=parsed_at,
            parse_status=ParseStatus.failed,
            document_kind=DocumentKind.native_pdf,
            finding_code="password_protected",
            finding_msg=(
                "Document is encrypted. No text extraction possible without the password."
            ),
            finding_severity=FindingSeverity.error,
        )

    total_pages = len(reader.pages)
    if total_pages == 0:
        return _make_failed(
            document_id=document_id,
            content_hash=content_hash,
            source_path=source_path,
            tenancy=tenancy,
            parsed_at=parsed_at,
            parse_status=ParseStatus.failed,
            document_kind=DocumentKind.native_pdf,
            finding_code="empty_document",
            finding_msg="PDF has zero pages.",
            finding_severity=FindingSeverity.warning,
        )

    # Extract text per page; record per-page results
    pages: list[PageResult] = []
    regions: list[RegionResult] = []
    encoding_issues: list[EncodingIssue] = []
    page_char_counts: list[int] = []
    page_failures: list[int] = []
    any_success = False

    for page_idx, page in enumerate(reader.pages):
        page_num = page_idx + 1
        page_loc = SourceLocation(
            locator_kind=LocatorKind.page,
            page_start=page_num,
            page_end=page_num,
        )
        region_id = f"page-{page_num}-r1"

        # Capture pypdf warnings during extraction to detect decompression failures.
        # pypdf does not raise on decompression errors — it logs a warning and returns "".
        with _capture_pypdf_warnings() as warn_capture:
            try:
                text = page.extract_text() or ""
            except Exception as exc:
                text = ""
                warn_capture.messages.append(f"extract_text raised: {exc}")

        # Detect decompression / content-stream failures from pypdf warnings
        decompression_failed = any(
            _DECOMPRESSION_ERROR_RE.search(msg) for msg in warn_capture.messages
        )

        if decompression_failed and not text.strip():
            # Page content stream is broken — record as failed extraction
            page_char_counts.append(0)
            page_failures.append(page_num)
            pages.append(
                PageResult(
                    page_number=page_num,
                    is_scanned=False,
                    ocr_confidence=None,
                    extraction_ratio=0.0,
                    invisible_content=[],
                )
            )
            regions.append(
                RegionResult(
                    region_id=region_id,
                    location=page_loc,
                    text=None,
                    extract_status=ExtractStatus.failed,
                    ocr_confidence=None,
                    language=None,
                    detected_class_hint=None,
                    encoding_issue=False,
                )
            )
            encoding_issues.append(
                EncodingIssue(
                    location=page_loc,
                    kind=EncodingIssueKind.other,
                    severity=IssueSeverity.high,
                )
            )
            continue

        char_count = len(text)
        page_char_counts.append(char_count)

        if char_count > 0:
            any_success = True

        # Detect encoding issues in extracted text
        page_encoding_issues = _detect_encoding_issues(text, page_num)
        encoding_issues.extend(page_encoding_issues)
        has_encoding_issue = len(page_encoding_issues) > 0

        # Per-page result
        extraction_ratio = min(1.0, char_count / _MIN_CHARS_PER_PAGE) if char_count > 0 else 0.0
        pages.append(
            PageResult(
                page_number=page_num,
                is_scanned=False,  # Phase 1: all successfully extracted pages are native
                ocr_confidence=None,  # Phase 1: native text, no OCR
                extraction_ratio=round(extraction_ratio, 4),
                invisible_content=[],  # Phase 2: invisible content detection
            )
        )

        # Region result — one region per page (layout-block granularity in Phase 2)
        extract_status = ExtractStatus.ok if char_count > 0 else ExtractStatus.empty
        regions.append(
            RegionResult(
                region_id=region_id,
                location=page_loc,
                text=text if char_count > 0 else None,
                extract_status=extract_status,
                ocr_confidence=None,
                language=None,  # Phase 2: language detection
                detected_class_hint=RegionClassHint.prose if char_count > 0 else None,
                encoding_issue=has_encoding_issue,
            )
        )

    # Determine overall parse status and document kind
    total_extracted_chars = sum(page_char_counts)

    # Determine if this is a native-text PDF or image-only
    # If we have pages but zero chars across all pages (and no page failures),
    # this is likely an image-only PDF (scanned without OCR).
    image_only = total_pages > 0 and total_extracted_chars == 0 and not page_failures

    if image_only:
        # Image-only PDF: cannot serve without OCR (Phase 2)
        # Return a parse result with failed status and image-only finding
        quality = _compute_quality(page_char_counts, total_pages)
        return ParseResult(
            schema_version="1.0.0",
            tenancy=tenancy,
            document_id=document_id,
            content_hash=content_hash,
            parser=_PARSER_REF,
            parsed_at=parsed_at,
            parse_status=ParseStatus.failed,
            document_kind=DocumentKind.scanned_pdf,  # image-only = scanned without OCR
            quality=quality,
            pages=pages,
            regions=regions,
            boilerplate_candidates=[],
            content_classes=["image_only"],
            encoding_issues=encoding_issues,
            language_distribution=[],
            findings=[
                Finding(
                    code="unservable_image_only",
                    severity=FindingSeverity.warning,
                    location=None,
                    message=(
                        "All pages yielded zero text. This appears to be an image-only PDF "
                        "(scanned or rasterized). OCR is required to extract content — "
                        "Phase 2 will add OCR support."
                    ),
                )
            ],
        )

    if page_failures and not any_success:
        # All pages failed
        quality = _compute_quality(page_char_counts, total_pages)
        return ParseResult(
            schema_version="1.0.0",
            tenancy=tenancy,
            document_id=document_id,
            content_hash=content_hash,
            parser=_PARSER_REF,
            parsed_at=parsed_at,
            parse_status=ParseStatus.failed,
            document_kind=DocumentKind.native_pdf,
            quality=quality,
            pages=pages,
            regions=regions,
            boilerplate_candidates=[],
            content_classes=[],
            encoding_issues=encoding_issues,
            language_distribution=[],
            findings=[
                Finding(
                    code="parse_error",
                    severity=FindingSeverity.error,
                    location=None,
                    message=f"All {total_pages} pages failed extraction.",
                )
            ],
        )

    parse_status = ParseStatus.partial if page_failures else ParseStatus.parsed
    document_kind = DocumentKind.native_pdf

    quality = _compute_quality(page_char_counts, total_pages)

    # Build findings list
    findings: list[Finding] = []
    if page_failures:
        for pf in page_failures:
            findings.append(
                Finding(
                    code="parse_error",
                    severity=FindingSeverity.warning,
                    location=SourceLocation(
                        locator_kind=LocatorKind.page,
                        page_start=pf,
                        page_end=pf,
                    ),
                    message=f"Page {pf} content stream could not be decompressed.",
                )
            )
    if quality.is_near_empty:
        findings.append(
            Finding(
                code="near_empty",
                severity=FindingSeverity.warning,
                location=None,
                message=(
                    f"Document is near-empty: only {quality.text_extraction_ratio:.0%} "
                    "of pages yielded substantial text."
                ),
            )
        )
    if encoding_issues:
        findings.append(
            Finding(
                code="encoding_issues",
                severity=FindingSeverity.info,
                location=None,
                message=(
                    f"{len(encoding_issues)} encoding issue(s) detected in extracted text "
                    "(replacement chars, mojibake, or control chars)."
                ),
            )
        )

    return ParseResult(
        schema_version="1.0.0",
        tenancy=tenancy,
        document_id=document_id,
        content_hash=content_hash,
        parser=_PARSER_REF,
        parsed_at=parsed_at,
        parse_status=parse_status,
        document_kind=document_kind,
        quality=quality,
        pages=pages,
        regions=regions,
        boilerplate_candidates=[],  # Phase 2: corpus-wide boilerplate detection
        content_classes=[],
        encoding_issues=encoding_issues,
        language_distribution=[],  # Phase 2: language detection
        findings=findings,
    )


def _make_failed(
    document_id: str,
    content_hash: str,
    source_path: str,
    tenancy: TenancyBlock,
    parsed_at: datetime,
    parse_status: ParseStatus,
    document_kind: DocumentKind,
    finding_code: str,
    finding_msg: str,
    finding_severity: FindingSeverity,
) -> ParseResult:
    """Build a failed ParseResult with a single finding."""
    return ParseResult(
        schema_version="1.0.0",
        tenancy=tenancy,
        document_id=document_id,
        content_hash=content_hash,
        parser=_PARSER_REF,
        parsed_at=parsed_at,
        parse_status=parse_status,
        document_kind=document_kind,
        quality=QualityScore(
            overall=0.0,
            text_extraction_ratio=0.0,
            table_structure_retained=TableStructureRetained.n_a,
            is_near_empty=True,
            mean_ocr_confidence=None,
        ),
        pages=[],
        regions=[],
        boilerplate_candidates=[],
        content_classes=[],
        encoding_issues=[],
        language_distribution=[],
        findings=[
            Finding(
                code=finding_code,
                severity=finding_severity,
                location=None,
                message=finding_msg,
            )
        ],
    )


def _make_excluded(
    document_id: str,
    content_hash: str,
    source_path: str,
    tenancy: TenancyBlock,
    parsed_at: datetime,
    document_kind: DocumentKind,
    finding_code: str,
    finding_msg: str,
) -> ParseResult:
    """Build an honestly-excluded ParseResult for non-parseable file types."""
    return ParseResult(
        schema_version="1.0.0",
        tenancy=tenancy,
        document_id=document_id,
        content_hash=content_hash,
        parser=_PARSER_REF,
        parsed_at=parsed_at,
        parse_status=ParseStatus.excluded_pre_parse,
        document_kind=document_kind,
        quality=QualityScore(
            overall=0.0,
            text_extraction_ratio=None,
            table_structure_retained=TableStructureRetained.n_a,
            is_near_empty=True,
            mean_ocr_confidence=None,
        ),
        pages=[],
        regions=[],
        boilerplate_candidates=[],
        content_classes=[],
        encoding_issues=[],
        language_distribution=[],
        findings=[
            Finding(
                code=finding_code,
                severity=FindingSeverity.info,
                location=None,
                message=finding_msg,
            )
        ],
    )


# ---------------------------------------------------------------------------
# Stage
# ---------------------------------------------------------------------------


class AssessStage(Stage):
    """Stage 2 — Assess (Phase 1: real native-text PDF parsing).

    Consumes Inventory, emits ParseResultBatch.

    Phase 1 scope:
    - Native-text PDFs: real per-page extraction via pypdf.
    - All other file types: honestly excluded with parse_status=excluded_pre_parse.
    - Scanned / image-only PDFs: detected and reported as failed
      (Phase 2 adds OCR support).

    D-26 resolution: ParseResultBatch is now an official versioned contract;
    DecomposeStage version-checks it.

    Args:
        run_id: Pipeline run identifier (§12 traceability), threaded from the orchestrator.
        run_started_at: Timestamp threaded from the orchestrator (not wall-clock).
    """

    name = "assess"
    consumed_contract = "inventory"
    consumed_version_range = SUPPORTED_INVENTORY
    produced_contract = "parse_result_batch"
    output_model = ParseResultBatch

    def __init__(
        self,
        run_id: str = "",
        run_started_at: datetime | None = None,
    ) -> None:
        self._run_id = run_id
        self._run_started_at = run_started_at or datetime.now(tz=UTC)

    def _produce(self, input_data: dict[str, Any] | None) -> dict[str, Any]:
        """Produce one ParseResult per inventory item.

        Nothing is silently dropped — every item gets exactly one entry.
        """
        assert input_data is not None, "Assess requires Inventory input"

        # Reconstruct tenancy from inventory
        tenancy_raw = input_data.get("tenancy", {})
        tenancy = TenancyBlock(
            workspace_id=tenancy_raw.get("workspace_id", ""),
            kb_id=tenancy_raw.get("kb_id", ""),
            permission_mode=tenancy_raw.get("permission_mode", PermissionMode.public_to_kb),
            permission_principals=tenancy_raw.get("permission_principals", []),
            permission_source=tenancy_raw.get("permission_source", PermissionSource.platform),
            permission_fidelity=tenancy_raw.get(
                "permission_fidelity", PermissionFidelity.authoritative
            ),
            permission_resolved_at=None,
        )

        parsed_at = self._run_started_at
        results: list[dict[str, Any]] = []

        for item in input_data.get("items", []):
            document_id = item["document_id"]
            content_hash = item["content_hash"]
            source_path = item["source_path"]

            ext_class = _classify_extension(source_path)

            if ext_class == "pdf":
                parse_result = _parse_native_pdf(
                    document_id=document_id,
                    content_hash=content_hash,
                    source_path=source_path,
                    tenancy=tenancy,
                    parsed_at=parsed_at,
                )
            elif ext_class == "audio":
                parse_result = _make_excluded(
                    document_id=document_id,
                    content_hash=content_hash,
                    source_path=source_path,
                    tenancy=tenancy,
                    parsed_at=parsed_at,
                    document_kind=DocumentKind.other,
                    finding_code="unservable_audio",
                    finding_msg=(
                        f"Audio file ({Path(source_path).suffix}) cannot be parsed as text. "
                        "Excluded — no text extraction possible."
                    ),
                )
            elif ext_class == "video":
                parse_result = _make_excluded(
                    document_id=document_id,
                    content_hash=content_hash,
                    source_path=source_path,
                    tenancy=tenancy,
                    parsed_at=parsed_at,
                    document_kind=DocumentKind.other,
                    finding_code="unservable_video",
                    finding_msg=(
                        f"Video file ({Path(source_path).suffix}) cannot be parsed as text. "
                        "Excluded — no text extraction possible."
                    ),
                )
            elif ext_class == "cad":
                parse_result = _make_excluded(
                    document_id=document_id,
                    content_hash=content_hash,
                    source_path=source_path,
                    tenancy=tenancy,
                    parsed_at=parsed_at,
                    document_kind=DocumentKind.other,
                    finding_code="unservable_cad",
                    finding_msg=(
                        f"CAD/binary file ({Path(source_path).suffix}) cannot be parsed as text. "
                        "Excluded — no text extraction possible."
                    ),
                )
            elif ext_class == "html":
                parse_result = _make_excluded(
                    document_id=document_id,
                    content_hash=content_hash,
                    source_path=source_path,
                    tenancy=tenancy,
                    parsed_at=parsed_at,
                    document_kind=DocumentKind.html,
                    finding_code="excluded_content_type_html",
                    finding_msg=(
                        "HTML file excluded in Phase 1 (native-text PDF scope only). "
                        "Phase 2 adds HTML parsing support."
                    ),
                )
            elif ext_class == "spreadsheet":
                parse_result = _make_excluded(
                    document_id=document_id,
                    content_hash=content_hash,
                    source_path=source_path,
                    tenancy=tenancy,
                    parsed_at=parsed_at,
                    document_kind=DocumentKind.spreadsheet,
                    finding_code="excluded_content_type_spreadsheet",
                    finding_msg=(
                        "Spreadsheet excluded in Phase 1 (native-text PDF scope only). "
                        "Phase 2 adds spreadsheet triage and parsing support."
                    ),
                )
            else:
                parse_result = _make_excluded(
                    document_id=document_id,
                    content_hash=content_hash,
                    source_path=source_path,
                    tenancy=tenancy,
                    parsed_at=parsed_at,
                    document_kind=DocumentKind.other,
                    finding_code="excluded_content_type_other",
                    finding_msg=(
                        f"File type ({Path(source_path).suffix!r}) not supported in Phase 1. "
                        "Phase 2 adds broader content-type support."
                    ),
                )

            results.append(parse_result.model_dump(mode="json"))

        batch = ParseResultBatch(
            schema_version=BATCH_SCHEMA_VERSION,
            contract="parse_result_batch",
            run_id=self._run_id,
            produced_at=self._run_started_at,
            skeleton=None,  # real run — not a skeleton
            results=results,
        )
        return batch.model_dump(mode="json")
