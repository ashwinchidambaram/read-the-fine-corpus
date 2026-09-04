"""Honest-exclusion parsers for file types not supported in Phase 1.

Each class handles one extension family and returns
``parse_status=excluded_pre_parse`` with an informative finding.
Behaviour is identical to the original monolithic stage.py exclusion
branches — same statuses, same finding codes, same finding message strings.

Extension points (Phase 2)
--------------------------
When Phase 2 adds HTML or spreadsheet parsing, *remove the corresponding
``can_parse`` extension set from the relevant unsupported parser* (or delete
it entirely) and insert the real parser earlier in the registry.  The
unsupported parsers serve as honest placeholders until the real implementation
lands.

The ``FallbackUnsupportedParser`` always returns True from ``can_parse`` and
must therefore remain the **last** entry in the registry.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pypdf  # noqa: E402  (pypdf is a project dependency)

from finecorpus.contracts.parse_result import (
    DocumentKind,
    Finding,
    FindingSeverity,
    ParseResult,
    ParserRef,
    ParseStatus,
    QualityScore,
    TableStructureRetained,
)
from finecorpus.contracts.shared.blocks import TenancyBlock
from finecorpus.pipeline.assess.parsers.base import ParserContext

_PYPDF_VERSION = pypdf.__version__

_PARSER_REF = ParserRef(
    name="pypdf",
    version=_PYPDF_VERSION,
    ocr_engine=None,
)

# Extension sets (mirrors the constants in the original stage.py)
_AUDIO_EXTENSIONS = frozenset({".wav", ".mp3", ".aac", ".flac", ".ogg", ".m4a"})
_VIDEO_EXTENSIONS = frozenset({".mp4", ".mov", ".avi", ".mkv", ".webm"})
_CAD_EXTENSIONS = frozenset({".dwg", ".dxf", ".step", ".stp", ".iges", ".igs"})
_HTML_EXTENSIONS = frozenset({".html", ".htm"})
_SPREADSHEET_EXTENSIONS = frozenset({".xlsx", ".xls", ".csv", ".ods"})


def _make_excluded(
    document_id: str,
    content_hash: str,
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


class AudioParser:
    """Honest exclusion for audio files (.wav, .mp3, etc.)."""

    def can_parse(self, item: dict[str, Any]) -> bool:
        return Path(item.get("source_path", "")).suffix.lower() in _AUDIO_EXTENSIONS

    def parse(
        self,
        item: dict[str, Any],
        tenancy: TenancyBlock,
        parsed_at: datetime,
        ctx: ParserContext,
    ) -> ParseResult:
        source_path = item["source_path"]
        return _make_excluded(
            document_id=item["document_id"],
            content_hash=item["content_hash"],
            tenancy=tenancy,
            parsed_at=parsed_at,
            document_kind=DocumentKind.other,
            finding_code="unservable_audio",
            finding_msg=(
                f"Audio file ({Path(source_path).suffix}) cannot be parsed as text. "
                "Excluded — no text extraction possible."
            ),
        )


class VideoParser:
    """Honest exclusion for video files (.mp4, .mov, etc.)."""

    def can_parse(self, item: dict[str, Any]) -> bool:
        return Path(item.get("source_path", "")).suffix.lower() in _VIDEO_EXTENSIONS

    def parse(
        self,
        item: dict[str, Any],
        tenancy: TenancyBlock,
        parsed_at: datetime,
        ctx: ParserContext,
    ) -> ParseResult:
        source_path = item["source_path"]
        return _make_excluded(
            document_id=item["document_id"],
            content_hash=item["content_hash"],
            tenancy=tenancy,
            parsed_at=parsed_at,
            document_kind=DocumentKind.other,
            finding_code="unservable_video",
            finding_msg=(
                f"Video file ({Path(source_path).suffix}) cannot be parsed as text. "
                "Excluded — no text extraction possible."
            ),
        )


class CADParser:
    """Honest exclusion for CAD/binary files (.dwg, .dxf, etc.)."""

    def can_parse(self, item: dict[str, Any]) -> bool:
        return Path(item.get("source_path", "")).suffix.lower() in _CAD_EXTENSIONS

    def parse(
        self,
        item: dict[str, Any],
        tenancy: TenancyBlock,
        parsed_at: datetime,
        ctx: ParserContext,
    ) -> ParseResult:
        source_path = item["source_path"]
        return _make_excluded(
            document_id=item["document_id"],
            content_hash=item["content_hash"],
            tenancy=tenancy,
            parsed_at=parsed_at,
            document_kind=DocumentKind.other,
            finding_code="unservable_cad",
            finding_msg=(
                f"CAD/binary file ({Path(source_path).suffix}) cannot be parsed as text. "
                "Excluded — no text extraction possible."
            ),
        )


class HTMLParser:
    """Honest exclusion for HTML files (.html, .htm) — Phase 1 scope only.

    Phase 2 replaces this with a real HTML parser.  When that happens,
    remove this class from the registry (or remove it entirely from this module).
    """

    def can_parse(self, item: dict[str, Any]) -> bool:
        return Path(item.get("source_path", "")).suffix.lower() in _HTML_EXTENSIONS

    def parse(
        self,
        item: dict[str, Any],
        tenancy: TenancyBlock,
        parsed_at: datetime,
        ctx: ParserContext,
    ) -> ParseResult:
        return _make_excluded(
            document_id=item["document_id"],
            content_hash=item["content_hash"],
            tenancy=tenancy,
            parsed_at=parsed_at,
            document_kind=DocumentKind.html,
            finding_code="excluded_content_type_html",
            finding_msg=(
                "HTML file excluded in Phase 1 (native-text PDF scope only). "
                "Phase 2 adds HTML parsing support."
            ),
        )


class SpreadsheetParser:
    """Honest exclusion for spreadsheets (.xlsx, .xls, .csv, .ods) — Phase 1 scope only.

    Phase 2 replaces this with a real spreadsheet parser.  When that happens,
    remove this class from the registry (or remove it entirely from this module).
    """

    def can_parse(self, item: dict[str, Any]) -> bool:
        return Path(item.get("source_path", "")).suffix.lower() in _SPREADSHEET_EXTENSIONS

    def parse(
        self,
        item: dict[str, Any],
        tenancy: TenancyBlock,
        parsed_at: datetime,
        ctx: ParserContext,
    ) -> ParseResult:
        return _make_excluded(
            document_id=item["document_id"],
            content_hash=item["content_hash"],
            tenancy=tenancy,
            parsed_at=parsed_at,
            document_kind=DocumentKind.spreadsheet,
            finding_code="excluded_content_type_spreadsheet",
            finding_msg=(
                "Spreadsheet excluded in Phase 1 (native-text PDF scope only). "
                "Phase 2 adds spreadsheet triage and parsing support."
            ),
        )


class FallbackUnsupportedParser:
    """Catch-all exclusion for any file type not claimed by an earlier parser.

    Always returns ``True`` from ``can_parse``; must be **last** in REGISTRY.
    """

    def can_parse(self, item: dict[str, Any]) -> bool:
        return True

    def parse(
        self,
        item: dict[str, Any],
        tenancy: TenancyBlock,
        parsed_at: datetime,
        ctx: ParserContext,
    ) -> ParseResult:
        source_path = item["source_path"]
        return _make_excluded(
            document_id=item["document_id"],
            content_hash=item["content_hash"],
            tenancy=tenancy,
            parsed_at=parsed_at,
            document_kind=DocumentKind.other,
            finding_code="excluded_content_type_other",
            finding_msg=(
                f"File type ({Path(source_path).suffix!r}) not supported in Phase 1. "
                "Phase 2 adds broader content-type support."
            ),
        )


# Module-level singletons
audio_parser = AudioParser()
video_parser = VideoParser()
cad_parser = CADParser()
html_parser = HTMLParser()
spreadsheet_parser = SpreadsheetParser()
fallback_parser = FallbackUnsupportedParser()
