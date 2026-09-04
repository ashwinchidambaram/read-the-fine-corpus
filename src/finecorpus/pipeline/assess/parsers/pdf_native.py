"""Native-text PDF parser (Phase 2).

Handles .pdf files using pypdf text extraction.  Image-only PDFs are claimed
by ``ScannedPDFParser`` (placed before this parser in the registry) and never
reach this parser.  Mixed PDFs — where most pages have native text but one or
more pages consist of an embedded raster image with little or no text — are
handled here: scanned pages are OCR'd using the shared helper from
``pdf_scanned``, the document is classified ``document_kind=mixed_pdf``, and a
``mixed_pdf`` finding is emitted.

Behaviour:
  - Encrypted before open  → parse_status=failed, finding=password_protected
  - Encrypted after open   → parse_status=failed, finding=password_protected
  - Unreadable             → parse_status=unreadable, finding=parse_error
  - Zero pages             → parse_status=failed, finding=empty_document
  - All pages failed       → parse_status=failed, finding=parse_error
  - Some pages failed      → parse_status=partial, per-page findings
  - Mixed (native + scanned pages) → parse_status=parsed/partial,
                             document_kind=mixed_pdf, finding=mixed_pdf,
                             scanned pages OCR'd with per-region ocr_confidence
  - Clean native extraction → parse_status=parsed, document_kind=native_pdf

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

# Shared OCR helpers — imported from pdf_scanned to avoid duplication.
# These are kept in the parsers package (not a public API).
from finecorpus.pipeline.assess.parsers.pdf_scanned import (
    _extract_page_image,
    _ocr_page_confidence,
    _tesseract_available,
)
from finecorpus.pipeline.assess.security import detect_invisible_content

# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------

_LOG = logging.getLogger(__name__)

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

    Satisfies the ``FormatParser`` protocol.  Claims all ``.pdf`` files not
    already claimed by ``ScannedPDFParser`` (image-only PDFs are intercepted
    before reaching this parser).  Mixed PDFs — pages with embedded images
    and insufficient native text — are detected per-page and those pages are
    OCR'd via the shared helper from ``pdf_scanned``.
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

        # Determine tesseract availability once up front (cheap shutil.which call).
        tesseract_ok = _tesseract_available()

        # Per-page extraction
        pages: list[PageResult] = []
        regions: list[RegionResult] = []
        encoding_issues: list[EncodingIssue] = []
        all_invisible_findings: list[Finding] = []
        page_char_counts: list[int] = []
        page_failures: list[int] = []
        any_success = False
        # Mixed-PDF tracking: pages that were detected as scanned and OCR'd.
        mixed_page_nums: list[int] = []

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

            # §14.1 invisible-content detection — run on every page regardless
            # of extraction outcome (hidden text in failed streams is still a
            # security signal; detect_invisible_content is best-effort and safe).
            try:
                page_invisible, page_invisible_findings = detect_invisible_content(reader, page_num)
            except Exception:
                page_invisible = []
                page_invisible_findings = []
            all_invisible_findings.extend(page_invisible_findings)

            if decompression_failed and not text.strip():
                page_char_counts.append(0)
                page_failures.append(page_num)
                pages.append(
                    PageResult(
                        page_number=page_num,
                        is_scanned=False,
                        ocr_confidence=None,
                        extraction_ratio=0.0,
                        invisible_content=page_invisible,
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

            char_count = len(text.strip())

            # ------------------------------------------------------------------
            # Mixed-PDF detection: page has an embedded image AND the native
            # text is below threshold (i.e. the page content is primarily the
            # raster image, not native text).  Such pages are OCR'd when
            # tesseract is available.
            # ------------------------------------------------------------------
            pil_image = _extract_page_image(page)
            is_scanned_page = pil_image is not None and char_count < ctx.min_chars_per_page

            if is_scanned_page and tesseract_ok:
                # OCR the embedded image; combine with any native caption text.
                try:
                    ocr_text, ocr_confidence = _ocr_page_confidence(pil_image)
                except Exception as exc:  # noqa: BLE001
                    _LOG.warning("Page %d OCR failed in mixed-PDF: %s", page_num, exc)
                    ocr_text, ocr_confidence = "", 0.0

                # Combined text: native caption (if any) + OCR body.
                combined_parts = [p for p in (text.strip(), ocr_text.strip()) if p]
                combined_text = "\n".join(combined_parts)
                combined_chars = len(combined_text)

                page_char_counts.append(combined_chars)
                if combined_chars > 0:
                    any_success = True

                mixed_page_nums.append(page_num)
                extraction_ratio = (
                    min(1.0, combined_chars / ctx.min_chars_per_page) if combined_chars > 0 else 0.0
                )
                pages.append(
                    PageResult(
                        page_number=page_num,
                        is_scanned=True,
                        ocr_confidence=ocr_confidence,
                        extraction_ratio=round(extraction_ratio, 4),
                        invisible_content=[],
                    )
                )
                extract_status = ExtractStatus.ok if combined_chars > 0 else ExtractStatus.empty
                regions.append(
                    RegionResult(
                        region_id=region_id,
                        location=page_loc,
                        text=combined_text if combined_chars > 0 else None,
                        extract_status=extract_status,
                        ocr_confidence=ocr_confidence,
                        language=None,
                        detected_class_hint=RegionClassHint.other,
                        encoding_issue=False,
                    )
                )
                continue

            # ------------------------------------------------------------------
            # Normal native-text page path.
            # ------------------------------------------------------------------
            full_char_count = len(text)
            page_char_counts.append(full_char_count)

            if full_char_count > 0:
                any_success = True

            page_encoding_issues = _detect_encoding_issues(text, page_num)
            encoding_issues.extend(page_encoding_issues)
            has_encoding_issue = len(page_encoding_issues) > 0

            extraction_ratio = (
                min(1.0, full_char_count / ctx.min_chars_per_page) if full_char_count > 0 else 0.0
            )
            pages.append(
                PageResult(
                    page_number=page_num,
                    is_scanned=False,
                    ocr_confidence=None,
                    extraction_ratio=round(extraction_ratio, 4),
                    invisible_content=page_invisible,
                )
            )

            extract_status = ExtractStatus.ok if full_char_count > 0 else ExtractStatus.empty
            regions.append(
                RegionResult(
                    region_id=region_id,
                    location=page_loc,
                    text=text if full_char_count > 0 else None,
                    extract_status=extract_status,
                    ocr_confidence=None,
                    language=None,
                    detected_class_hint=RegionClassHint.prose if full_char_count > 0 else None,
                    encoding_issue=has_encoding_issue,
                )
            )

        total_extracted_chars = sum(page_char_counts)
        # Note: image_only path is no longer reachable here — image-only PDFs are
        # claimed by ScannedPDFParser before reaching NativePDFParser.  The check
        # is kept as a defensive fallback for edge cases (e.g. tesseract absent on
        # a mixed PDF where all non-scanned pages also have zero text).
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
                            "(scanned or rasterized). Run OCR via ScannedPDFParser."
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

        # Determine document_kind: mixed_pdf when at least one page was OCR'd.
        has_mixed_pages = bool(mixed_page_nums)
        parse_status = ParseStatus.partial if page_failures else ParseStatus.parsed
        document_kind = DocumentKind.mixed_pdf if has_mixed_pages else DocumentKind.native_pdf
        quality = _compute_quality(page_char_counts, total_pages, ctx)

        # For mixed PDFs, populate mean_ocr_confidence from OCR'd pages so
        # downstream consumers can inspect the OCR quality distribution.
        if has_mixed_pages:
            ocr_page_confs = [
                p.ocr_confidence for p in pages if p.is_scanned and p.ocr_confidence is not None
            ]
            if ocr_page_confs:
                mean_ocr = round(sum(ocr_page_confs) / len(ocr_page_confs), 6)
                quality = QualityScore(
                    overall=quality.overall,
                    text_extraction_ratio=quality.text_extraction_ratio,
                    table_structure_retained=quality.table_structure_retained,
                    is_near_empty=quality.is_near_empty,
                    mean_ocr_confidence=mean_ocr,
                )

        findings: list[Finding] = []

        if has_mixed_pages:
            findings.append(
                Finding(
                    code="mixed_pdf",
                    severity=FindingSeverity.info,
                    location=None,
                    message=(
                        f"Document contains {len(mixed_page_nums)} scanned page(s) "
                        f"embedded in a native-text PDF "
                        f"(page(s): {', '.join(str(p) for p in mixed_page_nums)}). "
                        "Those pages were OCR'd; per-region ocr_confidence is set."
                    ),
                )
            )

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
        # §14.1 invisible-content findings accumulated across all pages
        findings.extend(all_invisible_findings)

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
