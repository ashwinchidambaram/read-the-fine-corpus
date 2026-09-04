"""Scanned-PDF parser (Phase 2) — OCR via pytesseract.

Handles .pdf files that the native-text parser identifies as image-only (zero
extracted text across all pages, no decompression errors).  These previously
landed in a ``failed / unservable_image_only`` result; this parser converts them
to a genuine OCR-extracted ``parsed`` result with per-page confidence.

Strategy
--------
1. ``can_parse`` is intentionally *not* called for every PDF — claim logic is
   handled by registry ordering (see parsers/__init__.py for the full story).
   This parser's ``can_parse`` returns True for any ``.pdf`` file.  The registry
   places it *after* ``native_pdf_parser`` in the pipeline; the native parser
   defers to us by returning a sentinel result (``parse_status=deferred_to_ocr``)
   when it detects an image-only PDF.  Stage.py checks for that sentinel and
   re-routes to the next matching parser (this one).

   Simpler alternative used here: place this parser *before* ``native_pdf_parser``
   and return None / defer from ``can_parse`` when pypdf *can* extract text.
   That requires a pre-flight read that we want to avoid.  Instead:

   Actual registry design (see __init__.py):
   - ``native_pdf_parser`` stays in its existing position.
   - ``pdf_scanned_parser`` is inserted *before* ``native_pdf_parser``.
   - ``pdf_scanned_parser.can_parse`` opens the PDF and checks whether it is
     image-only (zero text on all pages).  If yes, claims it.  If no, returns
     False so ``native_pdf_parser`` handles it.
   - Both parsers open the PDF, but the fast path (native text, 99% of PDFs)
     pays only the cheap pypdf open + page-iteration cost.  The OCR path
     (scanned PDFs) pays the full tesseract cost which dominates anyway.

System dependency
-----------------
This parser requires the ``tesseract`` binary (from the ``tesseract-ocr`` OS
package) and the ``pytesseract`` Python wrapper.  If the binary is absent the
parser returns an honest ``failed`` result with a ``missing_dependency`` finding;
it never raises or silently fails.

See docs/pipeline/assess.md "Scanned PDF parser" and
docs/configuration/reference.md "assessment.*" for configuration thresholds.

Confidence derivation
---------------------
Per-page OCR confidence is the mean of tesseract's word-level confidence values
(the ``conf`` column from ``image_to_data``), divided by 100.0 to normalise to
[0.0, 1.0].  Words reported with ``conf == -1`` (non-text layout elements) are
excluded from the mean.  If a page yields zero scoreable words (blank page after
rasterization) the page confidence is 0.0.

This raw confidence is RETAINED — not thresholded away at this stage (§6.2
MUST).  Salience thresholds (``assessment.ocr_confidence_exclude_floor``,
``assessment.ocr_confidence_warn_level``) act at the Decompose / salience-pass
level.
"""

from __future__ import annotations

import logging
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

import pypdf
import pypdf.errors

from finecorpus.contracts.parse_result import (
    DocumentKind,
    ExtractStatus,
    Finding,
    FindingSeverity,
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
# Logger
# ---------------------------------------------------------------------------

_LOG = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PDF_EXTENSIONS = frozenset({".pdf"})

_TESSERACT_CMD = "tesseract"

# ---------------------------------------------------------------------------
# Tesseract availability check
# ---------------------------------------------------------------------------


def _tesseract_available() -> bool:
    """Return True if the ``tesseract`` binary is on PATH.

    Uses ``shutil.which`` — no subprocess call, no side effects.
    Result is NOT cached so tests can monkeypatch ``shutil.which``.
    """
    return shutil.which(_TESSERACT_CMD) is not None


# ---------------------------------------------------------------------------
# ParserRef
# ---------------------------------------------------------------------------


def _make_parser_ref() -> ParserRef:
    """Build the ParserRef for this parser (version detected at call time)."""
    try:
        import pytesseract  # noqa: PLC0415 (late import for missing-dep safety)

        tess_ver = str(pytesseract.get_tesseract_version())
    except Exception:
        tess_ver = "unknown"

    return ParserRef(
        name="pytesseract",
        version=tess_ver,
        ocr_engine="tesseract",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_image_only_pdf(reader: pypdf.PdfReader) -> bool:
    """Return True if the PDF has pages but zero extractable text on all pages.

    Mirrors the detection logic in ``NativePDFParser`` so the scanned parser
    can make the same decision when claiming a file in ``can_parse``.
    No decompression errors must be present — a file with decompression errors
    is a malformed native PDF, not a scanned PDF.
    """
    if len(reader.pages) == 0:
        return False
    for page in reader.pages:
        try:
            text = page.extract_text() or ""
        except Exception:
            # Any extraction error → not a clean image-only PDF; let native handle it.
            return False
        if text.strip():
            return False
    return True


def _ocr_page_confidence(image: Any) -> tuple[str, float]:
    """OCR a single PIL image; return (text, confidence).

    Confidence is the mean of word-level tesseract confidences normalised to
    [0.0, 1.0].  Words with ``conf == -1`` (non-text elements) are excluded.
    Returns (text, 0.0) if no scoreable words are detected.

    Args:
        image: A PIL.Image.Image object.

    Returns:
        Tuple of (ocr_text, confidence_0_to_1).
    """
    import pytesseract  # noqa: PLC0415

    data = pytesseract.image_to_data(image, output_type=pytesseract.Output.DICT)
    word_confs: list[int] = [c for c in data["conf"] if isinstance(c, int) and c != -1]

    if word_confs:
        confidence = sum(word_confs) / len(word_confs) / 100.0
    else:
        confidence = 0.0

    text = pytesseract.image_to_string(image)
    return text, round(confidence, 6)


def _extract_page_image(page: pypdf.PageObject) -> Any | None:
    """Extract the first embedded image from a PDF page.

    Returns a PIL.Image.Image or None if no image is found.
    pypdf exposes images via ``page.images`` (list of ImageFile objects,
    each with an ``.image`` attribute that is a PIL.Image.Image).
    """
    try:
        images = list(page.images)
        if images:
            return images[0].image
    except Exception as exc:  # noqa: BLE001
        _LOG.debug("Could not extract embedded image from page: %s", exc)
    return None


def _compute_ocr_quality(
    page_confidences: list[float],
    page_texts: list[str],
    ctx: ParserContext,
) -> QualityScore:
    """Compute QualityScore for an OCR-parsed document.

    The quality score reflects the OCR confidence distribution:
      mean_ocr_confidence = mean of per-page confidences
      text_extraction_ratio = fraction of pages that produced any text
      overall = 0.7 * mean_ocr_confidence + 0.3 * text_extraction_ratio
      is_near_empty = text_extraction_ratio < ctx.near_empty_threshold
    """
    total = len(page_confidences)
    if total == 0:
        return QualityScore(
            overall=0.0,
            text_extraction_ratio=0.0,
            table_structure_retained=TableStructureRetained.n_a,
            is_near_empty=True,
            mean_ocr_confidence=None,
        )

    pages_with_text = sum(
        1 for text in page_texts if text and len(text.strip()) >= ctx.page_nonempty_chars
    )
    text_extraction_ratio = pages_with_text / total
    mean_conf = sum(page_confidences) / total

    overall = 0.7 * mean_conf + 0.3 * text_extraction_ratio
    is_near_empty = text_extraction_ratio < ctx.near_empty_threshold

    return QualityScore(
        overall=round(overall, 4),
        text_extraction_ratio=round(text_extraction_ratio, 4),
        table_structure_retained=TableStructureRetained.n_a,
        is_near_empty=is_near_empty,
        mean_ocr_confidence=round(mean_conf, 6),
    )


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
        parser=_make_parser_ref(),
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


class ScannedPDFParser:
    """OCR parser for image-only / scanned PDFs.

    Satisfies the ``FormatParser`` protocol.

    Registry placement
    ------------------
    This parser is placed BEFORE ``native_pdf_parser`` in the registry so it
    can intercept image-only PDFs before the native parser marks them failed.
    ``can_parse`` does a lightweight image-only check (open PDF, attempt text
    extraction on all pages) and returns True only when the file is image-only.
    Native-text PDFs fall through to ``native_pdf_parser``.

    Claim ordering rationale (docs/pipeline/assess.md Extension points):
    - ``pdf_scanned_parser`` (this) → before native_pdf_parser
    - Claims only PDFs where pypdf yields zero text on all pages and no
      decompression errors (i.e. genuine image-only PDFs, not corrupted ones).
    - If tesseract is absent, still claims the file but returns an honest
      failed result with a ``missing_dependency`` finding rather than crashing.
    """

    def can_parse(self, item: dict[str, Any]) -> bool:
        """Return True for .pdf files that are image-only (zero extractable text).

        Opens the PDF with pypdf and checks all pages.  Returns False if:
        - The file is not a .pdf
        - pypdf can extract text from any page (native-text PDF → native parser)
        - The file is encrypted or malformed (native parser handles those too)

        This pre-flight read is cheap relative to OCR; the slow path (tesseract)
        only runs when this method returns True.
        """
        source_path = item.get("source_path", "")
        if Path(source_path).suffix.lower() not in _PDF_EXTENSIONS:
            return False
        try:
            reader = pypdf.PdfReader(source_path)
            return _is_image_only_pdf(reader)
        except Exception:  # noqa: BLE001
            # Any error (encrypted, malformed) → let native parser handle it
            return False

    def parse(
        self,
        item: dict[str, Any],
        tenancy: TenancyBlock,
        parsed_at: datetime,
        ctx: ParserContext,
    ) -> ParseResult:
        """OCR-parse a scanned PDF and return a ParseResult with per-page confidence.

        Invariants:
        - Never raises; all errors encoded in parse_status / findings.
        - Per-page confidence is RETAINED raw (not thresholded; §6.2).
        - Regions are populated even for low-confidence pages.
        - If tesseract is absent, returns honest failed with missing_dependency.
        """
        document_id = item["document_id"]
        content_hash = item["content_hash"]
        source_path = item["source_path"]

        # ------------------------------------------------------------------
        # 1. Check tesseract availability before doing any work
        # ------------------------------------------------------------------
        if not _tesseract_available():
            return _make_failed(
                document_id=document_id,
                content_hash=content_hash,
                tenancy=tenancy,
                parsed_at=parsed_at,
                parse_status=ParseStatus.failed,
                document_kind=DocumentKind.scanned_pdf,
                finding_code="missing_dependency",
                finding_msg=(
                    "The 'tesseract' binary was not found on PATH. "
                    "Install the 'tesseract-ocr' OS package to enable OCR of scanned PDFs. "
                    "See docs/pipeline/assess.md for installation instructions."
                ),
                finding_severity=FindingSeverity.error,
            )

        # ------------------------------------------------------------------
        # 2. Open PDF
        # ------------------------------------------------------------------
        try:
            reader = pypdf.PdfReader(source_path)
        except pypdf.errors.FileNotDecryptedError:
            return _make_failed(
                document_id=document_id,
                content_hash=content_hash,
                tenancy=tenancy,
                parsed_at=parsed_at,
                parse_status=ParseStatus.failed,
                document_kind=DocumentKind.scanned_pdf,
                finding_code="password_protected",
                finding_msg="Scanned PDF is password-protected; OCR cannot proceed.",
                finding_severity=FindingSeverity.error,
            )
        except Exception as exc:  # noqa: BLE001
            return _make_failed(
                document_id=document_id,
                content_hash=content_hash,
                tenancy=tenancy,
                parsed_at=parsed_at,
                parse_status=ParseStatus.failed,
                document_kind=DocumentKind.scanned_pdf,
                finding_code="parse_error",
                finding_msg=f"Scanned PDF could not be opened: {exc}",
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
                document_kind=DocumentKind.scanned_pdf,
                finding_code="empty_document",
                finding_msg="Scanned PDF has zero pages.",
                finding_severity=FindingSeverity.warning,
            )

        # ------------------------------------------------------------------
        # 3. Per-page OCR
        # ------------------------------------------------------------------
        pages: list[PageResult] = []
        regions: list[RegionResult] = []
        page_confidences: list[float] = []
        page_texts: list[str] = []
        any_text = False

        for page_idx, pdf_page in enumerate(reader.pages):
            page_num = page_idx + 1
            page_loc = SourceLocation(
                locator_kind=LocatorKind.page,
                page_start=page_num,
                page_end=page_num,
            )
            region_id = f"page-{page_num}-r1"

            # Try to extract the embedded raster image
            pil_image = _extract_page_image(pdf_page)

            if pil_image is None:
                # No embedded image extractable via pypdf — record as failed page
                pages.append(
                    PageResult(
                        page_number=page_num,
                        is_scanned=True,
                        ocr_confidence=0.0,
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
                        ocr_confidence=0.0,
                        language=None,
                        detected_class_hint=None,
                        encoding_issue=False,
                    )
                )
                page_confidences.append(0.0)
                page_texts.append("")
                _LOG.debug("Page %d: no embedded image found, recorded as failed", page_num)
                continue

            # OCR the page image
            try:
                text, confidence = _ocr_page_confidence(pil_image)
            except Exception as exc:  # noqa: BLE001
                _LOG.warning("Page %d OCR failed: %s", page_num, exc)
                text, confidence = "", 0.0

            # RETAINED raw — no thresholding (§6.2)
            page_confidences.append(confidence)
            page_texts.append(text)

            stripped = text.strip()
            if stripped:
                any_text = True

            extract_status = ExtractStatus.ok if stripped else ExtractStatus.empty
            extraction_ratio = min(1.0, len(stripped) / ctx.min_chars_per_page) if stripped else 0.0

            pages.append(
                PageResult(
                    page_number=page_num,
                    is_scanned=True,
                    ocr_confidence=confidence,  # retained raw
                    extraction_ratio=round(extraction_ratio, 4),
                    invisible_content=[],
                )
            )
            regions.append(
                RegionResult(
                    region_id=region_id,
                    location=page_loc,
                    text=stripped if stripped else None,
                    extract_status=extract_status,
                    ocr_confidence=confidence,  # retained raw
                    language=None,
                    detected_class_hint=RegionClassHint.other,  # scanned_region typed at Decompose
                    encoding_issue=False,
                )
            )

        # ------------------------------------------------------------------
        # 4. Build quality score
        # ------------------------------------------------------------------
        quality = _compute_ocr_quality(page_confidences, page_texts, ctx)

        # ------------------------------------------------------------------
        # 5. Build findings
        # ------------------------------------------------------------------
        findings: list[Finding] = []

        if not any_text:
            findings.append(
                Finding(
                    code="ocr_no_text_extracted",
                    severity=FindingSeverity.warning,
                    location=None,
                    message=(
                        "OCR ran on all pages but extracted no readable text. "
                        "The document may contain only graphics or diagrams without text."
                    ),
                )
            )

        # Report the lowest-confidence page explicitly (§6.4 scans requirement)
        if page_confidences:
            min_conf_page = page_confidences.index(min(page_confidences)) + 1
            min_conf_val = min(page_confidences)
            floor = ctx.ocr_confidence_exclude_floor
            warn = ctx.ocr_confidence_warn_level
            if min_conf_val < floor:
                findings.append(
                    Finding(
                        code="low_ocr_confidence",
                        severity=FindingSeverity.warning,
                        location=SourceLocation(
                            locator_kind=LocatorKind.page,
                            page_start=min_conf_page,
                            page_end=min_conf_page,
                        ),
                        message=(
                            f"Page {min_conf_page} has OCR confidence {min_conf_val:.2f} "
                            f"(below exclude floor {floor:.2f}). Text from this page is unreliable "
                            "and will be assigned 'excluded' salience tier."
                        ),
                    )
                )
            elif min_conf_val < warn:
                findings.append(
                    Finding(
                        code="low_ocr_confidence",
                        severity=FindingSeverity.warning,
                        location=SourceLocation(
                            locator_kind=LocatorKind.page,
                            page_start=min_conf_page,
                            page_end=min_conf_page,
                        ),
                        message=(
                            f"Page {min_conf_page} has OCR confidence {min_conf_val:.2f} "
                            f"(below warn level {warn:.2f}). Text from this page is down-weighted "
                            "to 'supporting' salience tier and flagged."
                        ),
                    )
                )

        if quality.is_near_empty:
            findings.append(
                Finding(
                    code="near_empty",
                    severity=FindingSeverity.warning,
                    location=None,
                    message=(
                        f"OCR document is near-empty: only "
                        f"{quality.text_extraction_ratio:.0%} of pages yielded "
                        "substantial text."
                    ),
                )
            )

        # ------------------------------------------------------------------
        # 6. Determine overall parse status
        # ------------------------------------------------------------------
        parse_status = ParseStatus.parsed if any_text else ParseStatus.failed

        return ParseResult(
            schema_version="1.0.0",
            tenancy=tenancy,
            document_id=document_id,
            content_hash=content_hash,
            parser=_make_parser_ref(),
            parsed_at=parsed_at,
            parse_status=parse_status,
            document_kind=DocumentKind.scanned_pdf,
            quality=quality,
            pages=pages,
            regions=regions,
            boilerplate_candidates=[],
            content_classes=["scanned"],
            encoding_issues=[],
            language_distribution=[],
            findings=findings,
        )


#: Module-level singleton — import and add to REGISTRY in parsers/__init__.py.
pdf_scanned_parser = ScannedPDFParser()
