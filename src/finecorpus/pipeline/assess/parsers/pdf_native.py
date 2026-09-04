"""Native-text PDF parser (Phase 1).

Handles .pdf files using pypdf text extraction.

Behaviour (identical to the original monolithic stage.py logic):
  - Encrypted before open  → parse_status=failed, finding=password_protected
  - Encrypted after open   → parse_status=failed, finding=password_protected
  - Unreadable             → parse_status=unreadable, finding=parse_error
  - Zero pages             → parse_status=failed, finding=empty_document
  - Image-only (0 chars)   → parse_status=failed, finding=unservable_image_only,
                             document_kind=scanned_pdf
  - All pages failed       → parse_status=failed, finding=parse_error
  - Some pages failed      → parse_status=partial, per-page findings
  - Clean extraction       → parse_status=parsed

Quality scoring constants are read from ``ParserContext`` (executor-defined;
documented in docs/pipeline/assess.md).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime
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
from finecorpus.contracts.shared.blocks import (
    LocatorKind,
    SourceLocation,
    TenancyBlock,
)
from finecorpus.pipeline.assess.parsers.base import ParserContext

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PDF_EXTENSIONS = frozenset({".pdf"})

# Encoding-issue detection patterns.
_REPLACEMENT_CHAR = "�"
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_MOJIBAKE_RE = re.compile(r"â€|Ã©|Ã|â€™|â€œ|â€")

# pypdf decompression-failure pattern in log messages
_DECOMPRESSION_ERROR_RE = re.compile(r"Error -\d+ while decompressing", re.IGNORECASE)

_PYPDF_VERSION = pypdf.__version__

_PARSER_REF = ParserRef(
    name="pypdf",
    version=_PYPDF_VERSION,
    ocr_engine=None,
)

# ---------------------------------------------------------------------------
# Warning capture context manager
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
# Encoding issue detection
# ---------------------------------------------------------------------------


def _detect_encoding_issues(text: str, page_num: int) -> list[EncodingIssue]:
    """Detect encoding corruption in extracted page text."""
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
# Quality scoring
# ---------------------------------------------------------------------------


def _compute_quality(
    page_char_counts: list[int],
    total_pages: int,
    ctx: ParserContext,
) -> QualityScore:
    """Compute QualityScore from per-page char counts.

    Heuristic (Phase 1 / executor-defined):
      extraction_density = min(1.0, mean_chars / ctx.min_chars_per_page)
      empty_page_ratio   = empty_pages / total_pages
      overall            = 0.7 * extraction_density + 0.3 * (1 - empty_page_ratio)
      text_extraction_ratio = non_empty_pages / total_pages
      is_near_empty      = (non_empty_pages / total_pages) < ctx.near_empty_threshold
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
    nonempty_pages = sum(1 for c in page_char_counts if c > ctx.page_nonempty_chars)
    mean_chars = sum(page_char_counts) / total_pages if page_char_counts else 0.0

    extraction_density = min(1.0, mean_chars / ctx.min_chars_per_page)
    empty_page_ratio = empty_pages / total_pages
    overall = 0.7 * extraction_density + 0.3 * (1.0 - empty_page_ratio)

    text_extraction_ratio = nonempty_pages / total_pages
    is_near_empty = text_extraction_ratio < ctx.near_empty_threshold

    return QualityScore(
        overall=round(overall, 4),
        text_extraction_ratio=round(text_extraction_ratio, 4),
        table_structure_retained=TableStructureRetained.n_a,
        is_near_empty=is_near_empty,
        mean_ocr_confidence=None,
    )


# ---------------------------------------------------------------------------
# Failed / excluded result builders
# ---------------------------------------------------------------------------


def _make_failed(
    document_id: str,
    content_hash: str,
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


# ---------------------------------------------------------------------------
# Parser implementation
# ---------------------------------------------------------------------------


class NativePDFParser:
    """Native-text PDF parser using pypdf.

    Satisfies the ``FormatParser`` protocol.  Claims all ``.pdf`` files.
    Future OCR parser should be inserted **before** this one in the registry
    to handle image-only PDFs (it can detect them and take over).
    """

    def can_parse(self, item: dict[str, Any]) -> bool:
        """Return True for .pdf files."""
        source_path = item.get("source_path", "")
        return Path(source_path).suffix.lower() in _PDF_EXTENSIONS

    def parse(
        self,
        item: dict[str, Any],
        tenancy: TenancyBlock,
        parsed_at: datetime,
        ctx: ParserContext,
    ) -> ParseResult:
        """Parse a native-text PDF.  Returns a ParseResult reflecting actual state."""
        document_id = item["document_id"]
        content_hash = item["content_hash"]
        source_path = item["source_path"]

        try:
            reader = pypdf.PdfReader(source_path)
        except pypdf.errors.FileNotDecryptedError:
            return _make_failed(
                document_id=document_id,
                content_hash=content_hash,
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
                tenancy=tenancy,
                parsed_at=parsed_at,
                parse_status=ParseStatus.unreadable,
                document_kind=DocumentKind.other,
                finding_code="parse_error",
                finding_msg=f"PDF could not be opened: {exc}",
                finding_severity=FindingSeverity.error,
            )

        if reader.is_encrypted:
            return _make_failed(
                document_id=document_id,
                content_hash=content_hash,
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
                tenancy=tenancy,
                parsed_at=parsed_at,
                parse_status=ParseStatus.failed,
                document_kind=DocumentKind.native_pdf,
                finding_code="empty_document",
                finding_msg="PDF has zero pages.",
                finding_severity=FindingSeverity.warning,
            )

        # Per-page extraction
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

            with _capture_pypdf_warnings() as warn_capture:
                try:
                    text = page.extract_text() or ""
                except Exception as exc:
                    text = ""
                    warn_capture.messages.append(f"extract_text raised: {exc}")

            decompression_failed = any(
                _DECOMPRESSION_ERROR_RE.search(msg) for msg in warn_capture.messages
            )

            if decompression_failed and not text.strip():
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

            page_encoding_issues = _detect_encoding_issues(text, page_num)
            encoding_issues.extend(page_encoding_issues)
            has_encoding_issue = len(page_encoding_issues) > 0

            extraction_ratio = (
                min(1.0, char_count / ctx.min_chars_per_page) if char_count > 0 else 0.0
            )
            pages.append(
                PageResult(
                    page_number=page_num,
                    is_scanned=False,
                    ocr_confidence=None,
                    extraction_ratio=round(extraction_ratio, 4),
                    invisible_content=[],
                )
            )

            extract_status = ExtractStatus.ok if char_count > 0 else ExtractStatus.empty
            regions.append(
                RegionResult(
                    region_id=region_id,
                    location=page_loc,
                    text=text if char_count > 0 else None,
                    extract_status=extract_status,
                    ocr_confidence=None,
                    language=None,
                    detected_class_hint=RegionClassHint.prose if char_count > 0 else None,
                    encoding_issue=has_encoding_issue,
                )
            )

        total_extracted_chars = sum(page_char_counts)
        image_only = total_pages > 0 and total_extracted_chars == 0 and not page_failures

        if image_only:
            quality = _compute_quality(page_char_counts, total_pages, ctx)
            return ParseResult(
                schema_version="1.0.0",
                tenancy=tenancy,
                document_id=document_id,
                content_hash=content_hash,
                parser=_PARSER_REF,
                parsed_at=parsed_at,
                parse_status=ParseStatus.failed,
                document_kind=DocumentKind.scanned_pdf,
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
            quality = _compute_quality(page_char_counts, total_pages, ctx)
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
        quality = _compute_quality(page_char_counts, total_pages, ctx)

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
            boilerplate_candidates=[],
            content_classes=[],
            encoding_issues=encoding_issues,
            language_distribution=[],
            findings=findings,
        )


#: Module-level singleton — import and add to REGISTRY in parsers/__init__.py.
native_pdf_parser = NativePDFParser()
