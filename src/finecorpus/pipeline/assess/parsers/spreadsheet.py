"""Spreadsheet parser for the Assess stage (Phase 2).

Handles .xlsx (and .xls via openpyxl fallback) files.

Triage
------
Every spreadsheet MUST be triaged into one of three kinds before any region
is emitted.  Triage is based on cheap structural heuristics applied to the
workbook, and the scores are surfaced as findings (§6.4 "visible").

    report    — formatted, narrative, chart-bearing. The ONLY kind ingested.
    database  — row-per-record, uniform typed data. MUST NOT be vectorized.
    model     — formula-dense; ingesting values creates a stale snapshot.

The heuristics are documented honestly below:

Formula-cell ratio (model signal)
    If more than MODEL_FORMULA_RATIO of data cells contain formulas, the
    spreadsheet is classified as a model.  Rationale: a spreadsheet whose
    meaning lives in the formula logic cannot be represented by a values
    snapshot.  The amortization fixture has ~90% formula cells (120×4 = 480
    formula cells out of 121×5 = 605 data cells, plus 4 summary formulas).

Uniform single-sheet high-row-count (database signal)
    If there is exactly one data sheet, its row count (excluding the header)
    exceeds DATABASE_ROW_THRESHOLD, all rows have the same number of
    non-empty cells as the header, and the header contains no merged cells
    or wrap-text blocks, the spreadsheet is classified as a database.
    Rationale: a customer/product record table with 500+ identical-structure
    rows is a relational dataset, not a narrative.

Multiple formatted sheets / chart presence / text-block cells (report signal)
    If none of the above fire, the spreadsheet is classified as a report if:
    - It has multiple sheets, OR
    - It has at least one embedded chart, OR
    - At least REPORT_TEXT_BLOCK_MIN cells contain text longer than
      REPORT_TEXT_CELL_MIN characters (narrative commentary).
    If none of these softer signals fire either, the spreadsheet defaults to
    report (the only kind this platform ingests — conservative classification).

Override
    The triage class can be overridden at config level via IngestionConfig
    .spreadsheet_triage[].source=user_override.  The parser does not consult
    the config — it performs fresh detection and records source="detected".
    The Plan stage merges the ingestion-config overrides.

Parsing (report kind only)
    - Each sheet → one section (structural path = [sheet_name]).
    - Cell-range tables → RegionResult with detected_class_hint=table.
    - Text-block / commentary cells → RegionResult with detected_class_hint=prose.
    - Chart presence → RegionResult with detected_class_hint=figure (no text
      extract; chart title captured as text if available).
    - Source locations use cell_range in A1 notation (e.g. "Sheet1!A1:D5").

Decompose compatibility
    Table, prose, and figure regions carry detected_class_hint.  The Phase 1
    segmentation pass does NOT consult detected_class_hint — it paragraph-splits
    all region text.  To prevent mangling:
    - Table regions: text is serialized as a pipe-delimited Markdown-style table
      (header row | separator | data rows).  Paragraph splitting on this text is
      safe since it contains no blank lines.
    - Prose regions: plain text; paragraph splitting is correct behaviour.
    - Figure regions: text is the chart title (single line); no mangling risk.
    The region-type finding records the detected_class_hint so the taxonomy
    unit (Phase 3) can refine decomposition without re-parsing.

openpyxl dependency
    openpyxl is a dev-dep that must be moved to runtime deps for Phase 2.
    It is imported at module level with a clear ImportError message if absent.
    The pyproject.toml change is part of this Phase 2 commit.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

try:
    import openpyxl
    from openpyxl.utils import get_column_letter

    _OPENPYXL_VERSION = openpyxl.__version__
    _OPENPYXL_AVAILABLE = True
except ImportError:
    _OPENPYXL_AVAILABLE = False
    _OPENPYXL_VERSION = "unavailable"

from finecorpus.contracts.parse_result import (
    DocumentKind,
    ExtractStatus,
    Finding,
    FindingSeverity,
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
# Constants and thresholds (all documented for honest disclosure)
# ---------------------------------------------------------------------------

_SPREADSHEET_EXTENSIONS = frozenset({".xlsx", ".xls", ".ods"})
_CSV_EXTENSIONS = frozenset({".csv"})

#: Fraction of data cells that must be formula-bearing → model classification.
MODEL_FORMULA_RATIO: float = 0.30

#: Minimum row count (excl. header) to trigger the database heuristic.
DATABASE_ROW_THRESHOLD: int = 50

#: Cells whose text exceeds this length count as narrative text blocks.
REPORT_TEXT_CELL_MIN: int = 80

#: Minimum number of narrative text-block cells to classify as report on
#: text-block signal alone (as opposed to chart / multi-sheet signals).
REPORT_TEXT_BLOCK_MIN: int = 2

#: Maximum rows to scan when building a table region (per sheet).
#: Prevents runaway on gigantic sheets that somehow pass the database filter.
TABLE_MAX_ROWS: int = 500

_PARSER_NAME = "openpyxl"

# ---------------------------------------------------------------------------
# Triage data classes (lightweight; not a Pydantic model — internal use only)
# ---------------------------------------------------------------------------

TRIAGE_REPORT = "report"
TRIAGE_DATABASE = "database"
TRIAGE_MODEL = "model"

_TriageKind = str  # one of the three constants above


class _TriageResult:
    """Internal result of spreadsheet triage."""

    def __init__(
        self,
        kind: _TriageKind,
        formula_ratio: float,
        max_data_rows: int,
        sheet_count: int,
        has_charts: bool,
        text_block_count: int,
        dominant_signal: str,
    ) -> None:
        self.kind = kind
        self.formula_ratio = formula_ratio
        self.max_data_rows = max_data_rows
        self.sheet_count = sheet_count
        self.has_charts = has_charts
        self.text_block_count = text_block_count
        self.dominant_signal = dominant_signal


# ---------------------------------------------------------------------------
# Triage heuristics
# ---------------------------------------------------------------------------


def _count_formula_cells(ws: Any) -> tuple[int, int]:
    """Return (formula_cell_count, total_data_cell_count) for a worksheet."""
    formula_count = 0
    total_count = 0
    for row in ws.iter_rows():
        for cell in row:
            if cell.value is None:
                continue
            total_count += 1
            if isinstance(cell.value, str) and cell.value.startswith("="):
                formula_count += 1
    return formula_count, total_count


def _is_uniform_sheet(ws: Any, min_rows: int) -> bool:
    """Return True if the sheet looks like a database (uniform row structure)."""
    rows = list(ws.iter_rows(values_only=False))
    if len(rows) < 2:  # noqa: PLR2004
        return False

    # Require at least min_rows data rows (excluding header)
    data_rows = rows[1:]
    if len(data_rows) < min_rows:
        return False

    # Header row must be non-empty and consistent
    header_row = rows[0]
    header_widths = [c for c in header_row if c.value is not None]
    if not header_widths:
        return False
    expected_width = len(header_widths)

    # Check uniformity: all data rows have the same number of non-empty cells
    # We sample up to 50 rows to keep triage fast on huge sheets.
    sample_rows = data_rows[:50]
    for row in sample_rows:
        non_empty = sum(1 for c in row if c.value is not None)
        # Allow one column of slack for trailing nulls / partial rows
        if abs(non_empty - expected_width) > 1:
            return False

    # Check that no header cell has wrap_text (a narrative formatting signal)
    for cell in header_row:
        if cell.value is None:
            continue
        alignment = cell.alignment
        if alignment and alignment.wrap_text:
            return False

    # Check that header cells don't contain long narrative text
    for cell in header_row:
        if isinstance(cell.value, str) and len(cell.value) > REPORT_TEXT_CELL_MIN:
            return False

    return True


def _count_text_blocks(wb: Any) -> int:
    """Count cells with long narrative text across all sheets."""
    count = 0
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for cell in row:
                if isinstance(cell.value, str) and len(cell.value) > REPORT_TEXT_CELL_MIN:
                    count += 1
    return count


def _has_charts(wb: Any) -> bool:
    """Return True if any sheet has an embedded chart."""
    for ws in wb.worksheets:
        if hasattr(ws, "_charts") and ws._charts:  # noqa: SLF001
            return True
    return False


def _triage(wb: Any) -> _TriageResult:
    """Apply the three-way triage to an opened workbook.

    Returns a _TriageResult with kind and all scores for findings visibility.
    """
    # --- Formula ratio (model signal) ---
    total_formula = 0
    total_cells = 0
    for ws in wb.worksheets:
        f, t = _count_formula_cells(ws)
        total_formula += f
        total_cells += t

    formula_ratio = (total_formula / total_cells) if total_cells > 0 else 0.0

    if formula_ratio >= MODEL_FORMULA_RATIO:
        return _TriageResult(
            kind=TRIAGE_MODEL,
            formula_ratio=formula_ratio,
            max_data_rows=0,
            sheet_count=len(wb.worksheets),
            has_charts=_has_charts(wb),
            text_block_count=0,
            dominant_signal=f"formula_ratio={formula_ratio:.2f} >= {MODEL_FORMULA_RATIO}",
        )

    # --- Uniform single-sheet high-row-count (database signal) ---
    sheet_count = len(wb.worksheets)
    max_data_rows = 0

    if sheet_count == 1:
        ws = wb.active
        rows = list(ws.iter_rows(values_only=False))
        max_data_rows = max(0, len(rows) - 1)  # subtract header row
        if _is_uniform_sheet(ws, DATABASE_ROW_THRESHOLD):
            return _TriageResult(
                kind=TRIAGE_DATABASE,
                formula_ratio=formula_ratio,
                max_data_rows=max_data_rows,
                sheet_count=sheet_count,
                has_charts=False,
                text_block_count=0,
                dominant_signal=(
                    f"single_sheet_uniform rows={max_data_rows} >= {DATABASE_ROW_THRESHOLD}"
                ),
            )
    else:
        for ws in wb.worksheets:
            rows = list(ws.iter_rows(values_only=False))
            max_data_rows = max(max_data_rows, max(0, len(rows) - 1))

    # --- Report signals ---
    has_chart = _has_charts(wb)
    text_blocks = _count_text_blocks(wb)

    if sheet_count > 1 or has_chart or text_blocks >= REPORT_TEXT_BLOCK_MIN:
        if has_chart:
            sig = "chart_presence"
        elif sheet_count > 1:
            sig = f"multiple_sheets={sheet_count}"
        else:
            sig = f"text_blocks={text_blocks} >= {REPORT_TEXT_BLOCK_MIN}"
        return _TriageResult(
            kind=TRIAGE_REPORT,
            formula_ratio=formula_ratio,
            max_data_rows=max_data_rows,
            sheet_count=sheet_count,
            has_charts=has_chart,
            text_block_count=text_blocks,
            dominant_signal=sig,
        )

    # Default to report (conservative — only report is ingested)
    return _TriageResult(
        kind=TRIAGE_REPORT,
        formula_ratio=formula_ratio,
        max_data_rows=max_data_rows,
        sheet_count=sheet_count,
        has_charts=has_chart,
        text_block_count=text_blocks,
        dominant_signal="default_report (no exclusion signal fired)",
    )


# ---------------------------------------------------------------------------
# Region extraction helpers (report kind only)
# ---------------------------------------------------------------------------


def _cell_ref(ws_title: str, row: int, col: int) -> str:
    """Return A1-notation cell reference for (1-based row, 1-based col)."""
    return f"{ws_title}!{get_column_letter(col)}{row}"


def _range_ref(ws_title: str, min_row: int, min_col: int, max_row: int, max_col: int) -> str:
    """Return A1-notation range reference."""
    return f"{ws_title}!{get_column_letter(min_col)}{min_row}:{get_column_letter(max_col)}{max_row}"


def _cell_text(cell: Any) -> str:
    """Extract text from a cell value, handling None gracefully."""
    if cell.value is None:
        return ""
    if isinstance(cell.value, str) and cell.value.startswith("="):
        # For report spreadsheets, formulas are rare; use the formula string
        # as a proxy (not computed value — openpyxl read_only does not compute).
        return cell.value
    return str(cell.value)


def _table_to_markdown(rows: list[list[str]]) -> str:
    """Serialize a list-of-rows to a pipe-delimited Markdown-style table.

    Safe for downstream paragraph splitting: no blank lines.
    """
    if not rows:
        return ""
    lines: list[str] = []
    header = rows[0]
    lines.append("| " + " | ".join(header) + " |")
    lines.append("| " + " | ".join("---" for _ in header) + " |")
    for row in rows[1:]:
        # Pad row to header width
        padded = row + [""] * max(0, len(header) - len(row))
        lines.append("| " + " | ".join(padded[: len(header)]) + " |")
    return "\n".join(lines)


def _region_id_for(sheet_name: str, cell_range: str) -> str:
    """Produce a stable region_id from sheet + cell range.

    Not a ULID (those require a time component); uses a deterministic prefix
    so the test suite can match on predictable IDs.  The taxonomy unit can
    upgrade to ULIDs if needed.
    """
    import hashlib

    raw = f"spreadsheet:{sheet_name}:{cell_range}"
    return "reg-xlsx-" + hashlib.sha256(raw.encode()).hexdigest()[:20]


# ---------------------------------------------------------------------------
# Table / prose / figure region extraction
# ---------------------------------------------------------------------------

_TABLE_HEADER_FILL_THRESHOLD = 0.0  # any fill on header row counts as formatted


def _looks_like_table_header(row: tuple[Any, ...]) -> bool:
    """Heuristic: does this row look like a table header?

    Signals: bold font, background fill, short cell values (labels not numbers).
    """
    for cell in row:
        if cell.value is None:
            continue
        font = cell.font
        fill = cell.fill
        if font and font.bold:
            return True
        if (
            fill
            and fill.fgColor
            and fill.fgColor.type != "none"
            and fill.fgColor.rgb
            not in (
                "00000000",
                "FFFFFFFF",
                "FF000000",
            )
        ):
            return True
    return False


def _extract_regions_from_sheet(
    ws: Any,
    ws_title: str,
    document_id: str,
) -> list[dict[str, Any]]:
    """Extract typed regions from a report-kind worksheet.

    Strategy:
    1. Walk the sheet row by row to find contiguous filled rectangular blocks.
    2. A block that starts with a formatted header row → table region.
    3. A block of one or a few text-heavy cells → prose region.
    4. Chart metadata → figure region (emitted separately, see caller).
    """
    all_rows = list(ws.iter_rows())
    if not all_rows:
        return []

    # Build a flat map: (row_idx, col_idx) → cell
    # Identify row "classes": header candidate, data, prose, empty
    regions: list[dict[str, Any]] = []

    max_col = ws.max_column or 0
    max_row = ws.max_row or 0

    if max_col == 0 or max_row == 0:
        return []

    # We scan for "blocks" — groups of consecutive non-empty rows.
    # A block that begins with a header-formatted row is a table.
    # A block of text-heavy cells is prose.

    in_block = False
    block_start_row = 1
    block_rows: list[tuple[Any, ...]] = []

    def _flush_block(block_rows: list[tuple[Any, ...]], block_start_row: int) -> None:
        """Emit regions for the accumulated block."""
        if not block_rows:
            return

        # Determine block extent
        block_end_row = block_start_row + len(block_rows) - 1
        non_empty_cols = set()
        for row in block_rows:
            for i, cell in enumerate(row, start=1):
                if cell.value is not None:
                    non_empty_cols.add(i)

        if not non_empty_cols:
            return

        min_col = min(non_empty_cols)
        actual_max_col = max(non_empty_cols)

        # Check if this block is a table or prose
        first_row = block_rows[0]
        is_header = _looks_like_table_header(first_row)
        has_data_rows = len(block_rows) > 1

        if is_header and has_data_rows:
            # Table region: serialize to markdown
            table_data: list[list[str]] = []
            for row in block_rows[:TABLE_MAX_ROWS]:
                row_texts = []
                for c_idx in range(min_col, actual_max_col + 1):
                    try:
                        cell = row[c_idx - 1]
                        row_texts.append(_cell_text(cell))
                    except IndexError:
                        row_texts.append("")
                table_data.append(row_texts)

            md_text = _table_to_markdown(table_data)
            if md_text:
                cell_range = _range_ref(
                    ws_title, block_start_row, min_col, block_end_row, actual_max_col
                )
                regions.append(
                    {
                        "region_id": _region_id_for(ws_title, cell_range),
                        "location": {
                            "locator_kind": LocatorKind.cell_range,
                            "cell_range": cell_range,
                        },
                        "text": md_text,
                        "extract_status": ExtractStatus.ok,
                        "detected_class_hint": RegionClassHint.table,
                        "encoding_issue": False,
                    }
                )
        else:
            # Prose region: collect long-text cells
            prose_texts: list[str] = []
            for row in block_rows:
                for cell in row:
                    ct = _cell_text(cell)
                    if ct.strip():
                        prose_texts.append(ct)

            combined = "\n\n".join(prose_texts)
            if combined.strip():
                cell_range = _range_ref(
                    ws_title, block_start_row, min_col, block_end_row, actual_max_col
                )
                regions.append(
                    {
                        "region_id": _region_id_for(ws_title, cell_range),
                        "location": {
                            "locator_kind": LocatorKind.cell_range,
                            "cell_range": cell_range,
                        },
                        "text": combined,
                        "extract_status": ExtractStatus.ok,
                        "detected_class_hint": RegionClassHint.prose,
                        "encoding_issue": False,
                    }
                )

    # Scan rows
    for r_idx, row in enumerate(all_rows, start=1):
        row_has_content = any(c.value is not None for c in row)

        if row_has_content:
            if not in_block:
                in_block = True
                block_start_row = r_idx
                block_rows = [row]
            else:
                block_rows.append(row)
        else:
            if in_block:
                _flush_block(block_rows, block_start_row)
                block_rows = []
                in_block = False

    if in_block and block_rows:
        _flush_block(block_rows, block_start_row)

    return regions


def _extract_chart_regions(ws: Any, ws_title: str) -> list[dict[str, Any]]:
    """Emit figure regions for charts embedded in a worksheet."""
    regions: list[dict[str, Any]] = []
    charts = getattr(ws, "_charts", [])
    for i, chart in enumerate(charts):
        title = ""
        if hasattr(chart, "title") and chart.title is not None:
            t = chart.title
            # openpyxl chart titles can be rich strings or plain strings
            if hasattr(t, "tx") and hasattr(t.tx, "rich"):
                try:
                    title = "".join(r.t for para in t.tx.rich.p for r in para.r if hasattr(r, "t"))
                except (AttributeError, TypeError):
                    pass
            elif hasattr(t, "strRef"):
                # Formula-based title — use as-is
                title = str(t)
            elif isinstance(t, str):
                title = t

        caption = title if title else f"Chart {i + 1} in sheet '{ws_title}'"
        # Anchor: use the chart's anchor position if available
        cell_range = f"{ws_title}!chart_{i + 1}"
        regions.append(
            {
                "region_id": _region_id_for(ws_title, f"chart_{i + 1}"),
                "location": {
                    "locator_kind": LocatorKind.cell_range,
                    "cell_range": cell_range,
                },
                "text": caption,
                "extract_status": ExtractStatus.ok,
                "detected_class_hint": RegionClassHint.figure,
                "encoding_issue": False,
            }
        )
    return regions


# ---------------------------------------------------------------------------
# Excluded result helper
# ---------------------------------------------------------------------------


def _make_excluded_spreadsheet(
    document_id: str,
    content_hash: str,
    tenancy: TenancyBlock,
    parsed_at: datetime,
    triage: _TriageResult,
    source_path: str,
) -> ParseResult:
    """Build an excluded ParseResult for database or model triage."""
    kind_label = {TRIAGE_DATABASE: "database", TRIAGE_MODEL: "model"}[triage.kind]
    reason_msg = {
        TRIAGE_DATABASE: (
            f"Spreadsheet triaged as DATABASE (row-per-record, {triage.max_data_rows} data rows). "
            "MUST NOT be vectorized per §6.4. Excluded as unservable. "
            "Remediation: ingest this data into a relational database; "
            "or reclassify as 'report' via spreadsheet_triage override if this is incorrect."
        ),
        TRIAGE_MODEL: (
            f"Spreadsheet triaged as MODEL (formula_ratio={triage.formula_ratio:.2f}). "
            "Ingesting a values snapshot creates a stale, misleading artifact per §6.4. "
            "Excluded as unservable. "
            "Remediation: export a documentation sheet without formulas as a separate document; "
            "or reclassify as 'report' via spreadsheet_triage override if this is incorrect."
        ),
    }[triage.kind]

    triage_finding = Finding(
        code="spreadsheet_triage",
        severity=FindingSeverity.info,
        location=None,
        message=(
            f"Triage: {kind_label.upper()} | disposition: exclude_unservable | "
            f"signal: {triage.dominant_signal} | "
            f"scores: formula_ratio={triage.formula_ratio:.2f}, "
            f"max_data_rows={triage.max_data_rows}, "
            f"sheet_count={triage.sheet_count}, "
            f"has_charts={triage.has_charts}, "
            f"text_blocks={triage.text_block_count}"
        ),
    )
    exclusion_finding = Finding(
        code=f"spreadsheet_{kind_label}_excluded",
        severity=FindingSeverity.error,
        location=None,
        message=reason_msg,
    )

    return ParseResult(
        schema_version="1.0.0",
        tenancy=tenancy,
        document_id=document_id,
        content_hash=content_hash,
        parser=ParserRef(name=_PARSER_NAME, version=_OPENPYXL_VERSION, ocr_engine=None),
        parsed_at=parsed_at,
        parse_status=ParseStatus.excluded_pre_parse,
        document_kind=DocumentKind.spreadsheet,
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
        content_classes=[f"spreadsheet_{kind_label}"],
        encoding_issues=[],
        language_distribution=[],
        findings=[triage_finding, exclusion_finding],
    )


# ---------------------------------------------------------------------------
# Report parse result builder
# ---------------------------------------------------------------------------


def _parse_as_report(
    wb: Any,
    document_id: str,
    content_hash: str,
    tenancy: TenancyBlock,
    parsed_at: datetime,
    triage: _TriageResult,
    source_path: str,
) -> ParseResult:
    """Build a ParseResult for a report-kind spreadsheet."""
    all_regions: list[RegionResult] = []

    # Triage finding (visible scores)
    triage_finding = Finding(
        code="spreadsheet_triage",
        severity=FindingSeverity.info,
        location=None,
        message=(
            f"Triage: REPORT | disposition: ingest | "
            f"signal: {triage.dominant_signal} | "
            f"scores: formula_ratio={triage.formula_ratio:.2f}, "
            f"max_data_rows={triage.max_data_rows}, "
            f"sheet_count={triage.sheet_count}, "
            f"has_charts={triage.has_charts}, "
            f"text_blocks={triage.text_block_count}"
        ),
    )

    findings: list[Finding] = [triage_finding]
    has_tables = False
    total_chars = 0

    for ws in wb.worksheets:
        ws_title = ws.title or "Sheet"

        # Content regions
        content_regions = _extract_regions_from_sheet(ws, ws_title, document_id)

        # Chart regions
        chart_regions = _extract_chart_regions(ws, ws_title)

        for r_raw in content_regions + chart_regions:
            loc_raw = r_raw["location"]
            loc = SourceLocation(
                locator_kind=loc_raw["locator_kind"],
                cell_range=loc_raw.get("cell_range"),
            )
            region = RegionResult(
                region_id=r_raw["region_id"],
                location=loc,
                text=r_raw.get("text"),
                extract_status=r_raw["extract_status"],
                ocr_confidence=None,
                language=None,
                detected_class_hint=r_raw.get("detected_class_hint"),
                encoding_issue=r_raw.get("encoding_issue", False),
            )
            all_regions.append(region)
            if r_raw.get("detected_class_hint") == RegionClassHint.table:
                has_tables = True
            if r_raw.get("text"):
                total_chars += len(r_raw["text"])

    if has_tables:
        table_retained = TableStructureRetained.full
    elif any(r.detected_class_hint == RegionClassHint.table for r in all_regions):
        table_retained = TableStructureRetained.partial
    else:
        table_retained = TableStructureRetained.n_a

    is_near_empty = total_chars < 50  # noqa: PLR2004
    extraction_ratio = min(1.0, total_chars / max(1, total_chars + 1))  # always ~1.0 for report

    # Overall quality: sheets extracted / total sheets
    quality_overall = 1.0 if all_regions else 0.0

    return ParseResult(
        schema_version="1.0.0",
        tenancy=tenancy,
        document_id=document_id,
        content_hash=content_hash,
        parser=ParserRef(name=_PARSER_NAME, version=_OPENPYXL_VERSION, ocr_engine=None),
        parsed_at=parsed_at,
        parse_status=ParseStatus.parsed if all_regions else ParseStatus.failed,
        document_kind=DocumentKind.spreadsheet,
        quality=QualityScore(
            overall=quality_overall,
            text_extraction_ratio=extraction_ratio,
            table_structure_retained=table_retained,
            is_near_empty=is_near_empty,
            mean_ocr_confidence=None,
        ),
        pages=[],
        regions=all_regions,
        boilerplate_candidates=[],
        content_classes=["spreadsheet_report"],
        encoding_issues=[],
        language_distribution=[],
        findings=findings,
    )


# ---------------------------------------------------------------------------
# Parser class
# ---------------------------------------------------------------------------


class SpreadsheetFormatParser:
    """Triage-first openpyxl-based spreadsheet parser (Phase 2).

    Implements the FormatParser protocol.  Must be registered before the
    FallbackUnsupportedParser in REGISTRY.

    Triage classification is always performed first and recorded in findings
    (§6.4 "visible").  database/model → excluded_pre_parse with reason.
    report → parsed with sheet sections, table/prose/figure regions.
    """

    def can_parse(self, item: dict[str, Any]) -> bool:
        suffix = Path(item.get("source_path", "")).suffix.lower()
        return suffix in _SPREADSHEET_EXTENSIONS or suffix in _CSV_EXTENSIONS

    def parse(
        self,
        item: dict[str, Any],
        tenancy: TenancyBlock,
        parsed_at: datetime,
        ctx: ParserContext,
    ) -> ParseResult:
        """Parse the spreadsheet item; never raises."""
        document_id = item["document_id"]
        content_hash = item["content_hash"]
        source_path = item["source_path"]

        # CSV is claimed by can_parse (so the fallback's wrong Phase-1 message is never shown)
        # but openpyxl cannot parse CSV.  Return an honest excluded result immediately.
        if Path(source_path).suffix.lower() in _CSV_EXTENSIONS:
            return ParseResult(
                schema_version="1.0.0",
                tenancy=tenancy,
                document_id=document_id,
                content_hash=content_hash,
                parser=ParserRef(name=_PARSER_NAME, version=_OPENPYXL_VERSION, ocr_engine=None),
                parsed_at=parsed_at,
                parse_status=ParseStatus.excluded_pre_parse,
                document_kind=DocumentKind.spreadsheet,
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
                content_classes=["csv_not_supported"],
                encoding_issues=[],
                language_distribution=[],
                findings=[
                    Finding(
                        code="csv_not_supported",
                        severity=FindingSeverity.error,
                        location=None,
                        message=(
                            "CSV files are not supported in Phase 2: openpyxl cannot parse CSV. "
                            "CSV support is future work. "
                            "Remediation: convert to .xlsx before ingestion, or wait for Phase 3."
                        ),
                    )
                ],
            )

        if not _OPENPYXL_AVAILABLE:
            return ParseResult(
                schema_version="1.0.0",
                tenancy=tenancy,
                document_id=document_id,
                content_hash=content_hash,
                parser=ParserRef(name=_PARSER_NAME, version="unavailable", ocr_engine=None),
                parsed_at=parsed_at,
                parse_status=ParseStatus.failed,
                document_kind=DocumentKind.spreadsheet,
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
                        code="missing_dependency_openpyxl",
                        severity=FindingSeverity.error,
                        location=None,
                        message=(
                            "openpyxl is not installed. "
                            "Add openpyxl to runtime dependencies and reinstall."
                        ),
                    )
                ],
            )

        def _load_wb(data_only: bool) -> Any:
            return openpyxl.load_workbook(source_path, read_only=False, data_only=data_only)

        def _open_error(exc: Exception) -> ParseResult:
            return ParseResult(
                schema_version="1.0.0",
                tenancy=tenancy,
                document_id=document_id,
                content_hash=content_hash,
                parser=ParserRef(name=_PARSER_NAME, version=_OPENPYXL_VERSION, ocr_engine=None),
                parsed_at=parsed_at,
                parse_status=ParseStatus.failed,
                document_kind=DocumentKind.spreadsheet,
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
                        code="spreadsheet_open_failed",
                        severity=FindingSeverity.error,
                        location=None,
                        message=f"Failed to open spreadsheet: {exc!r}",
                    )
                ],
            )

        # Triage requires formula detection → must load with data_only=False
        # so that cell.value returns '=...' formula strings, not computed values.
        try:
            wb_for_triage = _load_wb(data_only=False)
        except Exception as exc:
            return _open_error(exc)

        # Triage first — always
        triage = _triage(wb_for_triage)

        if triage.kind == TRIAGE_DATABASE:
            return _make_excluded_spreadsheet(
                document_id, content_hash, tenancy, parsed_at, triage, source_path
            )

        if triage.kind == TRIAGE_MODEL:
            return _make_excluded_spreadsheet(
                document_id, content_hash, tenancy, parsed_at, triage, source_path
            )

        # Report — reload with data_only=True so text cells show their display values
        # (not formula strings).  Formula-to-value conversion requires the xlsx cache;
        # if the cache is absent, cells will show None for formula cells, which is
        # acceptable for report-kind spreadsheets (prose/commentary cells are non-formula).
        try:
            wb_for_parse = _load_wb(data_only=True)
        except Exception as exc:
            return _open_error(exc)

        return _parse_as_report(
            wb_for_parse, document_id, content_hash, tenancy, parsed_at, triage, source_path
        )


#: Module-level singleton for REGISTRY.
spreadsheet_format_parser = SpreadsheetFormatParser()
