"""Contract 2 — Parse result.

Stage boundary: Assess → Decompose.
See docs/contracts/parse-result.md for the authoritative spec (§6.2, §12, §14.1, §14.4, §7.6).

The Parse result answers "can we even read this". It carries a per-document quality score,
per-page/per-region confidence retained not thresholded (§6.2), and represents failures
rather than dropping them (§6 rule 6, §12). Every extraction traces to a SourceLocation (§12).
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from finecorpus.contracts.shared.blocks import (
    InvisibleContentKind,
    SensitivityFlag,
    SourceLocation,
    TenancyBlock,
)

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class ParseStatus(StrEnum):
    """Overall parse outcome.

    See docs/contracts/parse-result.md ParseResult.parse_status.
    """

    parsed = "parsed"
    partial = "partial"
    """Some regions failed but others succeeded; both represented."""
    failed = "failed"
    unreadable = "unreadable"
    excluded_pre_parse = "excluded_pre_parse"


class DocumentKind(StrEnum):
    """High-level parsed nature of the document.

    See docs/contracts/parse-result.md ParseResult.document_kind.
    Native vs scanned PDF detection is a MUST (§6.2).
    """

    native_pdf = "native_pdf"
    scanned_pdf = "scanned_pdf"
    mixed_pdf = "mixed_pdf"
    html = "html"
    spreadsheet = "spreadsheet"
    plaintext = "plaintext"
    office_doc = "office_doc"
    other = "other"


class TableStructureRetained(StrEnum):
    """Table structure preservation during parsing (§6.2 MUST detect).

    See docs/contracts/parse-result.md QualityScore.table_structure_retained.
    """

    n_a = "n_a"
    """No tables in the document."""
    full = "full"
    partial = "partial"
    lost = "lost"


class ExtractStatus(StrEnum):
    """Per-region extraction outcome.

    See docs/contracts/parse-result.md RegionResult.extract_status.
    Failures are represented, not dropped (§12).
    """

    ok = "ok"
    low_confidence = "low_confidence"
    failed = "failed"
    empty = "empty"


class RegionClassHint(StrEnum):
    """Provisional content-class hint to seed Decompose typing.

    See docs/contracts/parse-result.md RegionResult.detected_class_hint.
    """

    prose = "prose"
    table = "table"
    code = "code"
    list_ = "list"
    figure = "figure"
    form_field = "form_field"
    other = "other"


class BoilerplateKind(StrEnum):
    """Best-guess category for a boilerplate candidate (§6.2).

    See docs/contracts/parse-result.md BoilerplateCandidate.boilerplate_kind.
    """

    header = "header"
    footer = "footer"
    legal_preamble = "legal_preamble"
    revision_block = "revision_block"
    nav_chrome = "nav_chrome"
    other = "other"


class EncodingIssueKind(StrEnum):
    """Type of encoding corruption.

    See docs/contracts/parse-result.md EncodingIssue.kind.
    """

    mojibake = "mojibake"
    replacement_chars = "replacement_chars"
    control_chars = "control_chars"
    bidi_confusable = "bidi_confusable"
    other = "other"


class IssueSeverity(StrEnum):
    """Impact estimate for an encoding issue or finding.

    See docs/contracts/parse-result.md EncodingIssue.severity and Finding.severity.
    """

    low = "low"
    medium = "medium"
    high = "high"


class FindingSeverity(StrEnum):
    """Severity for structured findings (§6.2).

    See docs/contracts/parse-result.md Finding.severity.
    """

    info = "info"
    warning = "warning"
    error = "error"


# ---------------------------------------------------------------------------
# ParserRef
# ---------------------------------------------------------------------------


class ParserRef(BaseModel):
    """Which parser/engine and version produced this parse result.

    See docs/contracts/parse-result.md ParserRef.
    """

    name: str = Field(description="Parser engine (e.g. pdfium, tesseract, unstructured).")
    version: str = Field(description="Engine version, for reproducibility.")
    ocr_engine: str | None = Field(
        default=None,
        description="OCR engine when applicable.",
    )


# ---------------------------------------------------------------------------
# QualityScore
# ---------------------------------------------------------------------------


class QualityScore(BaseModel):
    """Document-level quality findings.

    See docs/contracts/parse-result.md QualityScore.
    Quality is retained for reporting and downstream weighting —
    not used as a threshold gate (§6.2).
    """

    overall: Annotated[float, Field(ge=0.0, le=1.0)] = Field(
        description="Composite document parse quality."
    )
    text_extraction_ratio: Annotated[float, Field(ge=0.0, le=1.0)] | None = Field(
        default=None,
        description=(
            "Fraction of the document from which text was extracted. "
            "Low values flag near-empty extraction (§6.2)."
        ),
    )
    table_structure_retained: TableStructureRetained = Field(
        description="Table structure loss during parsing (§6.2 MUST detect). n_a if no tables."
    )
    is_near_empty: bool = Field(description="Empty or near-empty extraction (§6.2 MUST detect).")
    mean_ocr_confidence: Annotated[float, Field(ge=0.0, le=1.0)] | None = Field(
        default=None,
        description=(
            "Mean OCR confidence across pages, null for native text. "
            "Detail lives per-page (not thresholded away, §6.2)."
        ),
    )


# ---------------------------------------------------------------------------
# InvisibleContentDetection
# ---------------------------------------------------------------------------


class InvisibleContentDetection(BaseModel):
    """Detected at parse time (§14.1 MUST — a known PDF injection vector).

    See docs/contracts/parse-result.md InvisibleContentDetection.
    Hidden text is RETAINED, not stripped (§14.1: labelled not sanitized).
    """

    kind: InvisibleContentKind = Field(description="Invisible-content mechanism (§14.1).")
    location: SourceLocation = Field(
        description="Where it was found (bbox / page used for off-page)."
    )
    text: str | None = Field(
        default=None,
        description="The hidden text (retained, not stripped — labelled not sanitized, §14.1).",
    )


# ---------------------------------------------------------------------------
# PageResult
# ---------------------------------------------------------------------------


class PageResult(BaseModel):
    """Per-page confidence and findings for paged sources.

    See docs/contracts/parse-result.md PageResult.
    OCR confidence is retained not thresholded (§6.2 MUST).
    """

    page_number: int = Field(description="1-based page number.")
    is_scanned: bool = Field(
        description="Native-text vs scanned distinction at page granularity (a mixed PDF has both)."
    )
    ocr_confidence: Annotated[float, Field(ge=0.0, le=1.0)] | None = Field(
        default=None,
        description=(
            "Per-page OCR confidence, retained not thresholded (§6.2 MUST). "
            "Null when the page is native text."
        ),
    )
    extraction_ratio: Annotated[float, Field(ge=0.0, le=1.0)] | None = Field(
        default=None,
        description="Text extracted from this page.",
    )
    invisible_content: list[InvisibleContentDetection] = Field(
        description="Per-page invisible-content detections (§14.1). Empty list = none detected."
    )


# ---------------------------------------------------------------------------
# RegionResult
# ---------------------------------------------------------------------------


class RegionResult(BaseModel):
    """The traceable extraction unit.

    Every extracted span of text is a region with a source location.

    See docs/contracts/parse-result.md RegionResult.

    Invariants:
    - Every RegionResult traces to a SourceLocation (§12). A region without a location is a defect.
    - Failures are represented via extract_status, not dropped (§12).
    """

    region_id: str = Field(description="Region identity (ULID), referenced by Decompose.")
    location: SourceLocation = Field(
        description=(
            "Where in the source this extraction came from "
            "(§12: every extraction traces to a source location)."
        )
    )
    text: str | None = Field(
        default=None,
        description=(
            "Extracted text for this region. "
            "Null when extraction failed (still represented via extract_status)."
        ),
    )
    extract_status: ExtractStatus = Field(
        description="Per-region outcome. Failures are represented, not dropped (§12)."
    )
    ocr_confidence: Annotated[float, Field(ge=0.0, le=1.0)] | None = Field(
        default=None,
        description=(
            "Region-level OCR confidence (finer than page; "
            "used for low-confidence down-weighting, §6.4)."
        ),
    )
    language: str | None = Field(
        default=None,
        description="Detected language for this region (§7.6) in BCP-47. 'und' if undetermined.",
    )
    detected_class_hint: RegionClassHint | None = Field(
        default=None,
        description=(
            "Provisional content-class hint (table, prose, code, …) to seed Decompose typing."
        ),
    )
    encoding_issue: bool = Field(description="Whether this region shows encoding corruption.")


# ---------------------------------------------------------------------------
# BoilerplateCandidate
# ---------------------------------------------------------------------------


class BoilerplateCandidate(BaseModel):
    """Repeated-text span flagged as boilerplate (§6.2, reuses dedup machinery).

    See docs/contracts/parse-result.md BoilerplateCandidate.
    """

    candidate_id: str = Field(description="Identity (ULID).")
    text_fingerprint: str = Field(
        description="Fingerprint of the repeated span (from dedup machinery, §6.2)."
    )
    occurrence_count: int = Field(
        description="How many documents/positions across the corpus carry it."
    )
    example_locations: list[SourceLocation] = Field(
        description="Sample locations in this document."
    )
    boilerplate_kind: BoilerplateKind | None = Field(
        default=None,
        description="Best-guess category (§6.2).",
    )


# ---------------------------------------------------------------------------
# EncodingIssue
# ---------------------------------------------------------------------------


class EncodingIssue(BaseModel):
    """Encoding corruption detection (§6.2).

    See docs/contracts/parse-result.md EncodingIssue.
    """

    location: SourceLocation = Field(description="Where corruption was detected.")
    kind: EncodingIssueKind = Field(description="Type of corruption.")
    severity: IssueSeverity = Field(description="Impact estimate.")


# ---------------------------------------------------------------------------
# LanguageShare
# ---------------------------------------------------------------------------


class LanguageShare(BaseModel):
    """Per-document language distribution entry (§7.6).

    See docs/contracts/parse-result.md LanguageShare.
    extra="forbid": unknown keys rejected on parse (M-071 secret-free by construction).
    """

    model_config = ConfigDict(extra="forbid")

    language: str = Field(description="Detected language in BCP-47.")
    fraction: Annotated[float, Field(ge=0.0, le=1.0)] = Field(
        description="Share of document content in this language (§7.6 distribution)."
    )


# ---------------------------------------------------------------------------
# Finding
# ---------------------------------------------------------------------------


class Finding(BaseModel):
    """Structured finding feeding the plain-language quality report (§6.2).

    See docs/contracts/parse-result.md Finding.
    """

    code: str = Field(
        description=(
            "Stable finding id, e.g. scanned_pdf, table_structure_lost, near_empty, "
            "encoding_corruption, password_protected, pii_detected."
        )
    )
    severity: FindingSeverity = Field(description="For report prioritization.")
    location: SourceLocation | None = Field(
        default=None,
        description="Where, when applicable.",
    )
    message: str = Field(
        description="Plain-language message for the client-presentable findings report (§6.2)."
    )
    sensitivity_flags: list[SensitivityFlag] | None = Field(
        default=None,
        description="PII/sensitive detections attached here (§14.4), advisory, no redaction.",
    )


# ---------------------------------------------------------------------------
# ParseResult (root)
# ---------------------------------------------------------------------------


class ParseResult(BaseModel):
    """Contract 2 root model — produced by Assess, consumed by Decompose.

    See docs/contracts/parse-result.md ParseResult (root).

    Invariants:
    - Every parse carries a QualityScore and per-page/per-region confidence (§12).
    - Confidence is retained, never thresholded away (§6.2).
    - Failures are represented, not dropped (§12, §6 rule 6).
    - Every RegionResult traces to a SourceLocation (§12).
    - Invisible-content detections are recorded with hidden text retained (§14.1).
    - Language is detected per region and aggregated to language_distribution (§7.6).
    - tenancy present (Phase 0 MUST).
    """

    schema_version: str = Field(description="Contract version (semver).")
    tenancy: TenancyBlock = Field(
        description="Inherited from the source document's inventory record."
    )
    document_id: str = Field(description="The parsed document (Inventory document_id).")
    content_hash: str = Field(
        description="Version of the document parsed (Inventory content_hash)."
    )
    parser: ParserRef = Field(
        description="Which parser/engine and version produced this (audit, reproducibility)."
    )
    parsed_at: datetime = Field(description="When parsing completed (UTC).")
    parse_status: ParseStatus = Field(
        description=(
            "Overall outcome. partial = some regions failed but others succeeded; "
            "both represented. failed/unreadable still emit a record (§6 rule 6)."
        )
    )
    document_kind: DocumentKind = Field(
        description="High-level parsed nature (native vs scanned PDF is a MUST detection, §6.2)."
    )
    quality: QualityScore = Field(description="Document-level quality findings.")
    pages: list[PageResult] = Field(
        description=(
            "Per-page confidence and findings for paged sources. "
            "Empty for non-paged (HTML, spreadsheet) which use regions."
        )
    )
    regions: list[RegionResult] = Field(
        description="Per-region extraction records; the traceable extraction units."
    )
    boilerplate_candidates: list[BoilerplateCandidate] = Field(
        description="Repeated-text spans flagged as boilerplate (§6.2, reuses dedup machinery)."
    )
    content_classes: list[str] = Field(
        description="Detected content classes requiring special handling/exclusion (§6.2, §7.5)."
    )
    encoding_issues: list[EncodingIssue] = Field(
        description="Encoding corruption detections (§6.2)."
    )
    language_distribution: list[LanguageShare] = Field(
        description="Per-document language mix (§7.6), aggregated from region-level detection."
    )
    findings: list[Finding] = Field(
        description="Structured findings feeding the plain-language quality report (§6.2)."
    )


__all__ = [
    "ParseStatus",
    "DocumentKind",
    "TableStructureRetained",
    "ExtractStatus",
    "RegionClassHint",
    "BoilerplateKind",
    "EncodingIssueKind",
    "IssueSeverity",
    "FindingSeverity",
    "ParserRef",
    "QualityScore",
    "InvisibleContentDetection",
    "PageResult",
    "RegionResult",
    "BoilerplateCandidate",
    "EncodingIssue",
    "LanguageShare",
    "Finding",
    "ParseResult",
]
