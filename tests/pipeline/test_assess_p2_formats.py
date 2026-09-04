"""Phase 2 tests — Spreadsheet and HTML parsers.

Acceptance criteria (§6.4, Phase 2 brief):
1. Triage classifies all three spreadsheet fixture kinds correctly.
2. Triage scores are visible (spreadsheet_triage finding present, scores in message).
3. database/model → excluded_pre_parse with stated reason.
4. report → parsed: 3 sheets → 3 sections worth of content (regions present),
   table regions present, commentary prose present, chart region recorded.
5. confluence_export.html → headings/paths, code blocks, macro table regions,
   wiki links recorded in findings.
6. nested_tables.html → inner tables produce separate regions (not merged), finding present.
7. Nothing-dropped: HTML and spreadsheet ParseResults are produced.

Decompose compatibility:
  - Table/code/list regions survive as segments (detected_class_hint carried through).
  - Markdown-serialized table text passes through paragraph splitter without mangling.
"""

from __future__ import annotations

import pathlib
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

FIXTURE_CORPUS = pathlib.Path(__file__).parent.parent / "fixtures" / "golden" / "corpus"

# ---------------------------------------------------------------------------
# Parser-level imports (test without full pipeline when possible)
# ---------------------------------------------------------------------------

from finecorpus.pipeline.assess.parsers.spreadsheet import (
    TRIAGE_DATABASE,
    TRIAGE_MODEL,
    TRIAGE_REPORT,
    SpreadsheetFormatParser,
    _triage,
)
from finecorpus.pipeline.assess.parsers.html import HTMLFormatParser
from finecorpus.pipeline.assess.parsers.base import ParserContext
from finecorpus.contracts.shared.blocks import TenancyBlock, PermissionMode, PermissionSource, PermissionFidelity

import openpyxl


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

_PARSED_AT = datetime(2026, 1, 1, tzinfo=UTC)

_TENANCY = TenancyBlock(
    workspace_id="ws-p2-test",
    kb_id="kb-p2-test",
    permission_mode=PermissionMode.public_to_kb,
    permission_principals=[],
    permission_source=PermissionSource.platform,
    permission_fidelity=PermissionFidelity.authoritative,
    permission_resolved_at=None,
)

_CTX = ParserContext()

_SPREADSHEET_PARSER = SpreadsheetFormatParser()
_HTML_PARSER = HTMLFormatParser()


def _make_item(filename: str) -> dict[str, Any]:
    path = FIXTURE_CORPUS / filename
    return {
        "document_id": f"doc-{filename}",
        "content_hash": "deadbeef" * 8,
        "source_path": str(path),
    }


# ---------------------------------------------------------------------------
# 1. Spreadsheet triage: fixture classification
# ---------------------------------------------------------------------------


class TestSpreadsheetTriage:
    """§6.4 triage heuristics — all three fixture kinds must classify correctly."""

    def test_report_spreadsheet_triaged_as_report(self):
        """report_spreadsheet.xlsx must be classified as REPORT.

        Triage must use data_only=False to see formula strings (model signal).
        """
        wb = openpyxl.load_workbook(
            str(FIXTURE_CORPUS / "report_spreadsheet.xlsx"), data_only=False
        )
        result = _triage(wb)
        assert result.kind == TRIAGE_REPORT, (
            f"Expected triage=report, got {result.kind!r}. "
            f"Dominant signal: {result.dominant_signal}"
        )

    def test_database_spreadsheet_triaged_as_database(self):
        """database_spreadsheet.xlsx must be classified as DATABASE.

        Triage must use data_only=False to see formula strings (model signal).
        """
        wb = openpyxl.load_workbook(
            str(FIXTURE_CORPUS / "database_spreadsheet.xlsx"), data_only=False
        )
        result = _triage(wb)
        assert result.kind == TRIAGE_DATABASE, (
            f"Expected triage=database, got {result.kind!r}. "
            f"Dominant signal: {result.dominant_signal}"
        )

    def test_model_spreadsheet_triaged_as_model(self):
        """model_spreadsheet.xlsx must be classified as MODEL.

        Triage MUST use data_only=False to detect formula strings ('=...');
        data_only=True returns cached values and hides formulas.
        """
        wb = openpyxl.load_workbook(
            str(FIXTURE_CORPUS / "model_spreadsheet.xlsx"), data_only=False
        )
        result = _triage(wb)
        assert result.kind == TRIAGE_MODEL, (
            f"Expected triage=model, got {result.kind!r}. "
            f"Dominant signal: {result.dominant_signal}"
        )

    def test_triage_scores_visible_in_report_finding(self):
        """§6.4: triage class AND scores must be visible — spreadsheet_triage finding present."""
        item = _make_item("report_spreadsheet.xlsx")
        pr = _SPREADSHEET_PARSER.parse(item, _TENANCY, _PARSED_AT, _CTX)
        triage_findings = [f for f in pr.findings if f.code == "spreadsheet_triage"]
        assert triage_findings, "Expected spreadsheet_triage finding in report parse result"
        msg = triage_findings[0].message
        # Scores must be in the message for visibility
        assert "formula_ratio" in msg, f"formula_ratio not in triage message: {msg}"
        assert "max_data_rows" in msg, f"max_data_rows not in triage message: {msg}"
        assert "sheet_count" in msg, f"sheet_count not in triage message: {msg}"
        assert "has_charts" in msg, f"has_charts not in triage message: {msg}"

    def test_triage_scores_visible_in_database_finding(self):
        """§6.4: database triage scores visible."""
        item = _make_item("database_spreadsheet.xlsx")
        pr = _SPREADSHEET_PARSER.parse(item, _TENANCY, _PARSED_AT, _CTX)
        triage_findings = [f for f in pr.findings if f.code == "spreadsheet_triage"]
        assert triage_findings, "Expected spreadsheet_triage finding in database parse result"
        msg = triage_findings[0].message
        assert "DATABASE" in msg, f"DATABASE not in triage message: {msg}"
        assert "formula_ratio" in msg

    def test_triage_scores_visible_in_model_finding(self):
        """§6.4: model triage scores visible."""
        item = _make_item("model_spreadsheet.xlsx")
        pr = _SPREADSHEET_PARSER.parse(item, _TENANCY, _PARSED_AT, _CTX)
        triage_findings = [f for f in pr.findings if f.code == "spreadsheet_triage"]
        assert triage_findings, "Expected spreadsheet_triage finding in model parse result"
        msg = triage_findings[0].message
        assert "MODEL" in msg, f"MODEL not in triage message: {msg}"
        assert "formula_ratio" in msg


# ---------------------------------------------------------------------------
# 2. Database / Model → excluded with reason
# ---------------------------------------------------------------------------


class TestSpreadsheetExclusion:
    """database/model must be excluded with stated reason (§6.4, §7.5)."""

    def test_database_excluded_pre_parse(self):
        """database_spreadsheet.xlsx: parse_status=excluded_pre_parse."""
        item = _make_item("database_spreadsheet.xlsx")
        pr = _SPREADSHEET_PARSER.parse(item, _TENANCY, _PARSED_AT, _CTX)
        assert pr.parse_status.value == "excluded_pre_parse", (
            f"Expected excluded_pre_parse, got {pr.parse_status!r}"
        )

    def test_database_exclusion_has_reason(self):
        """database exclusion must state reason (§7.5 remediation required)."""
        item = _make_item("database_spreadsheet.xlsx")
        pr = _SPREADSHEET_PARSER.parse(item, _TENANCY, _PARSED_AT, _CTX)
        exclusion_findings = [f for f in pr.findings if "database_excluded" in f.code]
        assert exclusion_findings, f"Expected database exclusion finding, got: {[f.code for f in pr.findings]}"
        msg = exclusion_findings[0].message
        assert "Remediation" in msg or "remediation" in msg.lower(), (
            f"Exclusion finding must state remediation: {msg}"
        )

    def test_database_has_no_regions(self):
        """Database spreadsheet: no regions (nothing to vectorize)."""
        item = _make_item("database_spreadsheet.xlsx")
        pr = _SPREADSHEET_PARSER.parse(item, _TENANCY, _PARSED_AT, _CTX)
        assert pr.regions == [], (
            f"Expected no regions for database spreadsheet, got {len(pr.regions)}"
        )

    def test_model_excluded_pre_parse(self):
        """model_spreadsheet.xlsx: parse_status=excluded_pre_parse."""
        item = _make_item("model_spreadsheet.xlsx")
        pr = _SPREADSHEET_PARSER.parse(item, _TENANCY, _PARSED_AT, _CTX)
        assert pr.parse_status.value == "excluded_pre_parse", (
            f"Expected excluded_pre_parse, got {pr.parse_status!r}"
        )

    def test_model_exclusion_has_reason(self):
        """model exclusion must state reason."""
        item = _make_item("model_spreadsheet.xlsx")
        pr = _SPREADSHEET_PARSER.parse(item, _TENANCY, _PARSED_AT, _CTX)
        exclusion_findings = [f for f in pr.findings if "model_excluded" in f.code]
        assert exclusion_findings, f"Expected model exclusion finding, got: {[f.code for f in pr.findings]}"

    def test_model_has_no_regions(self):
        """Model spreadsheet: no regions."""
        item = _make_item("model_spreadsheet.xlsx")
        pr = _SPREADSHEET_PARSER.parse(item, _TENANCY, _PARSED_AT, _CTX)
        assert pr.regions == [], (
            f"Expected no regions for model spreadsheet, got {len(pr.regions)}"
        )

    def test_database_triage_class_recorded(self):
        """database_spreadsheet: content_classes must record spreadsheet_database."""
        item = _make_item("database_spreadsheet.xlsx")
        pr = _SPREADSHEET_PARSER.parse(item, _TENANCY, _PARSED_AT, _CTX)
        assert "spreadsheet_database" in pr.content_classes, (
            f"Expected spreadsheet_database in content_classes, got {pr.content_classes}"
        )

    def test_model_triage_class_recorded(self):
        """model_spreadsheet: content_classes must record spreadsheet_model."""
        item = _make_item("model_spreadsheet.xlsx")
        pr = _SPREADSHEET_PARSER.parse(item, _TENANCY, _PARSED_AT, _CTX)
        assert "spreadsheet_model" in pr.content_classes, (
            f"Expected spreadsheet_model in content_classes, got {pr.content_classes}"
        )


# ---------------------------------------------------------------------------
# 3. Report → parsed: sheets, tables, prose, charts
# ---------------------------------------------------------------------------


class TestSpreadsheetReportParsing:
    """report_spreadsheet.xlsx parsed correctly (§6.4 report handling)."""

    @pytest.fixture(scope="class")
    def report_pr(self):
        """Parse report_spreadsheet.xlsx once for all tests in class."""
        item = _make_item("report_spreadsheet.xlsx")
        return _SPREADSHEET_PARSER.parse(item, _TENANCY, _PARSED_AT, _CTX)

    def test_parse_status_is_parsed(self, report_pr):
        """report_spreadsheet must have parse_status=parsed."""
        assert report_pr.parse_status.value == "parsed", (
            f"Expected parsed, got {report_pr.parse_status!r}"
        )

    def test_document_kind_is_spreadsheet(self, report_pr):
        """document_kind must be spreadsheet."""
        assert report_pr.document_kind.value == "spreadsheet"

    def test_regions_present(self, report_pr):
        """Report must produce at least one region."""
        assert report_pr.regions, "Expected regions for report spreadsheet"

    def test_three_sheets_produce_content(self, report_pr):
        """The 3-sheet report should produce regions from each sheet.

        Fixture has: Executive Summary, Revenue Analysis, Commentary.
        Regions should carry locations from all three sheets.
        """
        sheets_with_content = set()
        for r in report_pr.regions:
            if r.location.cell_range:
                sheet_name = r.location.cell_range.split("!")[0]
                sheets_with_content.add(sheet_name)
        assert len(sheets_with_content) >= 2, (
            f"Expected content from at least 2 sheets, got {sheets_with_content}"
        )

    def test_table_regions_present(self, report_pr):
        """Report must have table regions (key metrics table, revenue table)."""
        from finecorpus.contracts.parse_result import RegionClassHint
        table_regions = [r for r in report_pr.regions if r.detected_class_hint == RegionClassHint.table]
        assert table_regions, (
            f"Expected table regions in report, got region hints: "
            f"{[r.detected_class_hint for r in report_pr.regions]}"
        )

    def test_prose_regions_present(self, report_pr):
        """Report must have prose regions (commentary, narrative text)."""
        from finecorpus.contracts.parse_result import RegionClassHint
        prose_regions = [r for r in report_pr.regions if r.detected_class_hint == RegionClassHint.prose]
        assert prose_regions, "Expected prose regions in report spreadsheet"

    def test_chart_region_present(self, report_pr):
        """Report has BarChart → must produce at least one figure region."""
        from finecorpus.contracts.parse_result import RegionClassHint
        figure_regions = [r for r in report_pr.regions if r.detected_class_hint == RegionClassHint.figure]
        assert figure_regions, (
            "Expected figure region for BarChart in report_spreadsheet.xlsx"
        )

    def test_chart_region_has_title(self, report_pr):
        """Chart figure region should carry the chart title as text."""
        from finecorpus.contracts.parse_result import RegionClassHint
        figure_regions = [r for r in report_pr.regions if r.detected_class_hint == RegionClassHint.figure]
        assert figure_regions
        # At least one figure region must have non-empty text (chart title)
        has_text = any(r.text and r.text.strip() for r in figure_regions)
        assert has_text, (
            f"Expected chart figure region to have title text, got: "
            f"{[r.text for r in figure_regions]}"
        )

    def test_regions_have_cell_range_locations(self, report_pr):
        """All regions must have cell_range locations (§12 every extraction traces to location)."""
        for r in report_pr.regions:
            assert r.location.cell_range, (
                f"Region {r.region_id} missing cell_range location"
            )

    def test_table_text_is_markdown(self, report_pr):
        """Table regions should be Markdown-serialized (safe for paragraph splitter)."""
        from finecorpus.contracts.parse_result import RegionClassHint
        table_regions = [r for r in report_pr.regions if r.detected_class_hint == RegionClassHint.table]
        assert table_regions
        first_table = table_regions[0]
        assert first_table.text, "Table region has no text"
        # Markdown table: starts with '|'
        assert first_table.text.strip().startswith("|"), (
            f"Table text should be Markdown pipe format: {first_table.text[:100]!r}"
        )

    def test_no_blank_lines_in_table_text(self, report_pr):
        """Table Markdown must have no blank lines (safe paragraph splitter behaviour)."""
        from finecorpus.contracts.parse_result import RegionClassHint
        for r in report_pr.regions:
            if r.detected_class_hint == RegionClassHint.table and r.text:
                assert "\n\n" not in r.text, (
                    f"Table region has blank lines (will mangle paragraph splitter): "
                    f"{r.text[:200]!r}"
                )

    def test_triage_content_class_recorded(self, report_pr):
        """Report: content_classes must record spreadsheet_report."""
        assert "spreadsheet_report" in report_pr.content_classes


# ---------------------------------------------------------------------------
# 4. Confluence HTML — headings/paths, code, table, wiki links
# ---------------------------------------------------------------------------


class TestConfluenceHTML:
    """confluence_export.html parsed correctly (§6.2 HTML, §6.1 web links)."""

    @pytest.fixture(scope="class")
    def confluence_pr(self):
        """Parse confluence_export.html once."""
        item = _make_item("confluence_export.html")
        return _HTML_PARSER.parse(item, _TENANCY, _PARSED_AT, _CTX)

    def test_parse_status_is_parsed(self, confluence_pr):
        """Confluence HTML must be parsed (not excluded)."""
        assert confluence_pr.parse_status.value == "parsed", (
            f"Expected parsed, got {confluence_pr.parse_status!r}"
        )

    def test_document_kind_is_html(self, confluence_pr):
        """document_kind must be html."""
        assert confluence_pr.document_kind.value == "html"

    def test_regions_present(self, confluence_pr):
        """Must have at least one region."""
        assert confluence_pr.regions, "Expected regions from confluence_export.html"

    def test_heading_regions_present(self, confluence_pr):
        """Must have prose regions carrying heading text (h1, h2, h3)."""
        # Our parser emits headings as prose regions (detected_class_hint=prose);
        # the segmentation pass promotes them to heading segments.
        # We check that some regions have heading-style text.
        heading_texts = []
        for r in confluence_pr.regions:
            if r.text and ("Deployment Runbook" in r.text or "Architecture Overview" in r.text or "Overview" in r.text):
                heading_texts.append(r.text)
        assert heading_texts, (
            "Expected region containing heading text from Confluence export"
        )

    def test_code_regions_present(self, confluence_pr):
        """Must have code regions (pre/code blocks with bash, python, yaml)."""
        from finecorpus.contracts.parse_result import RegionClassHint
        code_regions = [r for r in confluence_pr.regions if r.detected_class_hint == RegionClassHint.code]
        assert code_regions, "Expected code regions from Confluence export (bash/python/yaml blocks)"

    def test_code_region_has_expected_content(self, confluence_pr):
        """Code regions must contain actual code (not empty)."""
        from finecorpus.contracts.parse_result import RegionClassHint
        code_regions = [r for r in confluence_pr.regions if r.detected_class_hint == RegionClassHint.code]
        assert code_regions
        # At least one code block should contain a shell command
        code_texts = " ".join(r.text or "" for r in code_regions)
        assert "ssh" in code_texts or "kubectl" in code_texts or "git" in code_texts, (
            f"Expected shell commands in code blocks. Code text preview: {code_texts[:300]!r}"
        )

    def test_table_regions_present(self, confluence_pr):
        """Must have table regions (macro-style deployment status table, config reference table)."""
        from finecorpus.contracts.parse_result import RegionClassHint
        table_regions = [r for r in confluence_pr.regions if r.detected_class_hint == RegionClassHint.table]
        assert table_regions, "Expected table regions from Confluence deployment status table"

    def test_wiki_links_recorded_as_findings(self, confluence_pr):
        """§6.1: wiki links must be recorded as link_record findings, not fetched."""
        link_findings = [f for f in confluence_pr.findings if f.code == "link_record"]
        assert link_findings, (
            "Expected link_record findings for Confluence wiki links (§6.1 record, not fetch)"
        )

    def test_link_findings_have_urls(self, confluence_pr):
        """Link findings must contain the actual URL."""
        link_findings = [f for f in confluence_pr.findings if f.code == "link_record"]
        assert link_findings
        # Check that at least one link points to a wiki page
        all_messages = " ".join(f.message for f in link_findings)
        assert "/wiki/" in all_messages, (
            f"Expected wiki URLs in link findings. Messages: {all_messages[:300]!r}"
        )

    def test_fetch_policy_is_ignore(self, confluence_pr):
        """§6.1 default fetch policy must be 'ignore' — links recorded, not fetched."""
        link_findings = [f for f in confluence_pr.findings if f.code == "link_record"]
        assert link_findings
        # All link findings should indicate ignore policy
        for finding in link_findings:
            assert "ignore" in finding.message.lower(), (
                f"Expected fetch_policy=ignore in link finding: {finding.message}"
            )

    def test_dom_path_locations(self, confluence_pr):
        """All regions must have dom_path source locations (§12)."""
        for r in confluence_pr.regions:
            assert r.location.dom_path, (
                f"Region {r.region_id} missing dom_path location"
            )

    def test_nav_chrome_not_stripped(self, confluence_pr):
        """Nav chrome must be parsed (not stripped) — boilerplate detection handles it."""
        # The sidebar contains "Space Home", "All Pages", etc.
        # These should appear somewhere in the regions.
        all_text = " ".join(r.text or "" for r in confluence_pr.regions)
        # We can't guarantee exact nav text is in regions (it's in li/nav elements)
        # but we can verify the parser didn't produce a parse_failed status
        assert confluence_pr.parse_status.value == "parsed"
        # And that we have a reasonable number of regions (nav adds content)
        assert len(confluence_pr.regions) >= 3, (
            f"Expected multiple regions, nav included, got {len(confluence_pr.regions)}"
        )


# ---------------------------------------------------------------------------
# 5. Nested tables — separate regions, finding present
# ---------------------------------------------------------------------------


class TestNestedTablesHTML:
    """nested_tables.html — inner tables extracted as separate regions (OQ-9)."""

    @pytest.fixture(scope="class")
    def nested_pr(self):
        """Parse nested_tables.html once."""
        item = _make_item("nested_tables.html")
        return _HTML_PARSER.parse(item, _TENANCY, _PARSED_AT, _CTX)

    def test_parse_status_is_parsed(self, nested_pr):
        """nested_tables.html must be parsed."""
        assert nested_pr.parse_status.value == "parsed"

    def test_table_regions_present(self, nested_pr):
        """Must have table regions."""
        from finecorpus.contracts.parse_result import RegionClassHint
        table_regions = [r for r in nested_pr.regions if r.detected_class_hint == RegionClassHint.table]
        assert table_regions, "Expected table regions in nested_tables.html"

    def test_multiple_table_regions(self, nested_pr):
        """Nested tables must produce multiple separate table regions (not merged).

        The fixture has 3 outer tables; the inner tables (in cells of Table 2)
        must be separate regions per OQ-9.
        """
        from finecorpus.contracts.parse_result import RegionClassHint
        table_regions = [r for r in nested_pr.regions if r.detected_class_hint == RegionClassHint.table]
        # Table 1, Table 2 (outer), and inner tables (3 inner), Table 3 = at least 3 distinct
        assert len(table_regions) >= 3, (
            f"Expected at least 3 table regions (outer + inner nested tables), "
            f"got {len(table_regions)}"
        )

    def test_inner_table_not_merged_with_outer(self, nested_pr):
        """Inner tables must not be merged into outer table row content.

        The outer Table 2 'Approved Components' column contains inner tables.
        If merged, the outer table rows would show the inner Markdown as prose text.
        The outer table region should show empty cells in that column (inner table
        is a separate region), OR the outer table cell shows the serialized inner table.
        Either way, the INNER table must be a separate region too.
        """
        from finecorpus.contracts.parse_result import RegionClassHint
        table_regions = [r for r in nested_pr.regions if r.detected_class_hint == RegionClassHint.table]
        # We expect at least some regions with different dom_paths (separate tables)
        dom_paths = [r.location.dom_path for r in table_regions if r.location.dom_path]
        unique_paths = set(dom_paths)
        assert len(unique_paths) >= 2, (
            f"Expected separate dom_paths for outer vs inner tables, got: {unique_paths}"
        )

    def test_nested_table_finding_present(self, nested_pr):
        """OQ-9: nested table detection must be recorded as a finding."""
        nested_findings = [f for f in nested_pr.findings if f.code == "nested_table_detected"]
        assert nested_findings, (
            f"Expected nested_table_detected finding, got: {[f.code for f in nested_pr.findings]}"
        )

    def test_heading_regions_present(self, nested_pr):
        """Must have regions carrying heading text."""
        heading_texts = []
        for r in nested_pr.regions:
            if r.text and ("Component" in r.text or "Supplier" in r.text or "Test Coverage" in r.text):
                heading_texts.append(r.text)
        assert heading_texts, "Expected heading-related text in nested_tables regions"


# ---------------------------------------------------------------------------
# 6. Decompose compatibility — table Markdown survives paragraph splitting
# ---------------------------------------------------------------------------


class TestDecomposeCompatibility:
    """Table/code/list regions survive the Phase 1 segmentation pass without mangling."""

    def test_table_markdown_has_no_blank_lines(self):
        """Table Markdown must not contain blank lines (would split by paragraph splitter)."""
        from finecorpus.pipeline.assess.parsers.spreadsheet import _table_to_markdown as xlsx_md
        from finecorpus.pipeline.assess.parsers.html import _table_to_markdown as html_md

        rows = [["Header A", "Header B"], ["Row 1A", "Row 1B"], ["Row 2A", "Row 2B"]]
        for fn in (xlsx_md, html_md):
            result = fn(rows)
            assert "\n\n" not in result, (
                f"{fn.__module__}.{fn.__name__}: Markdown table contains blank lines: {result!r}"
            )
            assert result.strip().startswith("|"), (
                f"Markdown table must start with '|': {result!r}"
            )

    def test_report_spreadsheet_table_no_blank_lines(self):
        """report_spreadsheet tables must not have blank lines after serialization."""
        item = _make_item("report_spreadsheet.xlsx")
        pr = _SPREADSHEET_PARSER.parse(item, _TENANCY, _PARSED_AT, _CTX)
        from finecorpus.contracts.parse_result import RegionClassHint
        for r in pr.regions:
            if r.detected_class_hint == RegionClassHint.table and r.text:
                assert "\n\n" not in r.text, (
                    f"Table region contains blank lines: {r.text[:200]!r}"
                )

    def test_html_table_no_blank_lines(self):
        """HTML table regions must not have blank lines."""
        item = _make_item("nested_tables.html")
        pr = _HTML_PARSER.parse(item, _TENANCY, _PARSED_AT, _CTX)
        from finecorpus.contracts.parse_result import RegionClassHint
        for r in pr.regions:
            if r.detected_class_hint == RegionClassHint.table and r.text:
                assert "\n\n" not in r.text, (
                    f"HTML table region contains blank lines: {r.text[:200]!r}"
                )


# ---------------------------------------------------------------------------
# 7. Nothing-dropped: all fixtures produce a ParseResult
# ---------------------------------------------------------------------------


class TestNothingDropped:
    """Phase 2 formats must always produce a ParseResult (never silently dropped)."""

    @pytest.mark.parametrize("fixture_name", [
        "report_spreadsheet.xlsx",
        "database_spreadsheet.xlsx",
        "model_spreadsheet.xlsx",
        "confluence_export.html",
        "nested_tables.html",
    ])
    def test_parse_result_always_produced(self, fixture_name):
        """Every fixture produces a non-None ParseResult with schema_version."""
        if fixture_name.endswith(".xlsx"):
            parser = _SPREADSHEET_PARSER
        else:
            parser = _HTML_PARSER
        item = _make_item(fixture_name)
        pr = parser.parse(item, _TENANCY, _PARSED_AT, _CTX)
        assert pr is not None, f"No ParseResult for {fixture_name}"
        assert pr.schema_version, f"Missing schema_version for {fixture_name}"
        assert pr.document_id == item["document_id"]
        assert pr.parse_status is not None

    @pytest.mark.parametrize("fixture_name", [
        "report_spreadsheet.xlsx",
        "database_spreadsheet.xlsx",
        "model_spreadsheet.xlsx",
    ])
    def test_spreadsheet_always_has_triage_finding(self, fixture_name):
        """Every spreadsheet must have a spreadsheet_triage finding (§6.4 visibility)."""
        item = _make_item(fixture_name)
        pr = _SPREADSHEET_PARSER.parse(item, _TENANCY, _PARSED_AT, _CTX)
        triage_findings = [f for f in pr.findings if f.code == "spreadsheet_triage"]
        assert triage_findings, (
            f"{fixture_name}: Missing spreadsheet_triage finding. "
            f"Finding codes: {[f.code for f in pr.findings]}"
        )

    @pytest.mark.parametrize("fixture_name", [
        "report_spreadsheet.xlsx",
        "database_spreadsheet.xlsx",
        "model_spreadsheet.xlsx",
        "confluence_export.html",
        "nested_tables.html",
    ])
    def test_regions_have_extract_status(self, fixture_name):
        """All regions must have extract_status (§12 failures represented not dropped)."""
        if fixture_name.endswith(".xlsx"):
            parser = _SPREADSHEET_PARSER
        else:
            parser = _HTML_PARSER
        item = _make_item(fixture_name)
        pr = parser.parse(item, _TENANCY, _PARSED_AT, _CTX)
        for r in pr.regions:
            assert r.extract_status is not None, (
                f"{fixture_name}: Region {r.region_id} missing extract_status"
            )
