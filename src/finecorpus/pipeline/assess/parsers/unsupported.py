"""Honest-exclusion parsers for file types not handled by any other parser.

Each class handles one extension family and returns
``parse_status=excluded_pre_parse`` with an informative finding.

Phase 2 changes
---------------
The ``HTMLParser`` and ``SpreadsheetParser`` placeholder classes that existed
in Phase 1 have been removed.  Phase 2 provides real implementations in
``html.py`` and ``spreadsheet.py`` respectively.  Those parsers are now
registered before ``FallbackUnsupportedParser`` in ``REGISTRY``.

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
# Note: .html/.htm and .xlsx/.xls/.ods are now handled by real Phase 2 parsers
# in html.py and spreadsheet.py respectively; they are no longer listed here.
_AUDIO_EXTENSIONS = frozenset({".wav", ".mp3", ".aac", ".flac", ".ogg", ".m4a"})
_VIDEO_EXTENSIONS = frozenset({".mp4", ".mov", ".avi", ".mkv", ".webm"})
_CAD_EXTENSIONS = frozenset({".dwg", ".dxf", ".step", ".stp", ".iges", ".igs"})


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
# Note: html_parser and spreadsheet_parser are now in html.py / spreadsheet.py
audio_parser = AudioParser()
video_parser = VideoParser()
cad_parser = CADParser()
fallback_parser = FallbackUnsupportedParser()
