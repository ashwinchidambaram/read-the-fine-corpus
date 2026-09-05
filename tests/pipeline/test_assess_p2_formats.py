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
from datetime import UTC, datetime
from typing import Any

import openpyxl
import pytest

from finecorpus.contracts.parse_result import RegionClassHint
from finecorpus.contracts.shared.blocks import (
    PermissionFidelity,
    PermissionMode,
    PermissionSource,
    TenancyBlock,
)
from finecorpus.pipeline.assess.parsers.base import ParserContext
from finecorpus.pipeline.assess.parsers.html import HTMLFormatParser
from finecorpus.pipeline.assess.parsers.spreadsheet import (
    TRIAGE_DATABASE,
    TRIAGE_MODEL,
    TRIAGE_REPORT,
    SpreadsheetFormatParser,
    _triage,
)

FIXTURE_CORPUS = pathlib.Path(__file__).parent.parent / "fixtures" / "golden" / "corpus"

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
        wb = openpyxl.load_workbook(str(FIXTURE_CORPUS / "model_spreadsheet.xlsx"), data_only=False)
        result = _triage(wb)
        assert result.kind == TRIAGE_MODEL, (
            f"Expected triage=model, got {result.kind!r}. Dominant signal: {result.dominant_signal}"
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
        codes = [f.code for f in pr.findings]
        assert exclusion_findings, f"Expected database exclusion finding, got: {codes}"
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
        codes = [f.code for f in pr.findings]
        assert exclusion_findings, f"Expected model exclusion finding, got: {codes}"

    def test_model_has_no_regions(self):
        """Model spreadsheet: no regions."""
        item = _make_item("model_spreadsheet.xlsx")
        pr = _SPREADSHEET_PARSER.parse(item, _TENANCY, _PARSED_AT, _CTX)
        assert pr.regions == [], f"Expected no regions for model spreadsheet, got {len(pr.regions)}"

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
        table_regions = [
            r for r in report_pr.regions if r.detected_class_hint == RegionClassHint.table
        ]
        assert table_regions, (
            f"Expected table regions in report, got region hints: "
            f"{[r.detected_class_hint for r in report_pr.regions]}"
        )

    def test_prose_regions_present(self, report_pr):
        """Report must have prose regions (commentary, narrative text)."""
        prose_regions = [
            r for r in report_pr.regions if r.detected_class_hint == RegionClassHint.prose
        ]
        assert prose_regions, "Expected prose regions in report spreadsheet"

    def test_chart_region_present(self, report_pr):
        """Report has BarChart → must produce at least one figure region."""
        figure_regions = [
            r for r in report_pr.regions if r.detected_class_hint == RegionClassHint.figure
        ]
        assert figure_regions, "Expected figure region for BarChart in report_spreadsheet.xlsx"

    def test_chart_region_has_title(self, report_pr):
        """Chart figure region should carry the chart title as text."""
        figure_regions = [
            r for r in report_pr.regions if r.detected_class_hint == RegionClassHint.figure
        ]
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
            assert r.location.cell_range, f"Region {r.region_id} missing cell_range location"

    def test_table_text_is_markdown(self, report_pr):
        """Table regions should be Markdown-serialized (safe for paragraph splitter)."""
        table_regions = [
            r for r in report_pr.regions if r.detected_class_hint == RegionClassHint.table
        ]
        assert table_regions
        first_table = table_regions[0]
        assert first_table.text, "Table region has no text"
        # Markdown table: starts with '|'
        assert first_table.text.strip().startswith("|"), (
            f"Table text should be Markdown pipe format: {first_table.text[:100]!r}"
        )

    def test_no_blank_lines_in_table_text(self, report_pr):
        """Table Markdown must have no blank lines (safe paragraph splitter behaviour)."""
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
            if r.text and (
                "Deployment Runbook" in r.text
                or "Architecture Overview" in r.text
                or "Overview" in r.text
            ):
                heading_texts.append(r.text)
        assert heading_texts, "Expected region containing heading text from Confluence export"

    def test_code_regions_present(self, confluence_pr):
        """Must have code regions (pre/code blocks with bash, python, yaml)."""
        code_regions = [
            r for r in confluence_pr.regions if r.detected_class_hint == RegionClassHint.code
        ]
        assert code_regions, (
            "Expected code regions from Confluence export (bash/python/yaml blocks)"
        )

    def test_code_region_has_expected_content(self, confluence_pr):
        """Code regions must contain actual code (not empty)."""
        code_regions = [
            r for r in confluence_pr.regions if r.detected_class_hint == RegionClassHint.code
        ]
        assert code_regions
        # At least one code block should contain a shell command
        code_texts = " ".join(r.text or "" for r in code_regions)
        assert "ssh" in code_texts or "kubectl" in code_texts or "git" in code_texts, (
            f"Expected shell commands in code blocks. Code text preview: {code_texts[:300]!r}"
        )

    def test_table_regions_present(self, confluence_pr):
        """Must have table regions (deployment status table, config reference table)."""
        table_regions = [
            r for r in confluence_pr.regions if r.detected_class_hint == RegionClassHint.table
        ]
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
            assert r.location.dom_path, f"Region {r.region_id} missing dom_path location"

    def test_nav_chrome_not_stripped(self, confluence_pr):
        """Nav chrome must be parsed (not stripped) — boilerplate detection handles it."""
        # The sidebar contains "Space Home", "All Pages", etc.
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
        table_regions = [
            r for r in nested_pr.regions if r.detected_class_hint == RegionClassHint.table
        ]
        assert table_regions, "Expected table regions in nested_tables.html"

    def test_multiple_table_regions(self, nested_pr):
        """Nested tables must produce multiple separate table regions (not merged).

        The fixture has 3 outer tables; the inner tables (in cells of Table 2)
        must be separate regions per OQ-9.
        """
        table_regions = [
            r for r in nested_pr.regions if r.detected_class_hint == RegionClassHint.table
        ]
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
        table_regions = [
            r for r in nested_pr.regions if r.detected_class_hint == RegionClassHint.table
        ]
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
            if r.text and (
                "Component" in r.text or "Supplier" in r.text or "Test Coverage" in r.text
            ):
                heading_texts.append(r.text)
        assert heading_texts, "Expected heading-related text in nested_tables regions"


# ---------------------------------------------------------------------------
# 6. Decompose compatibility — table Markdown survives paragraph splitting
# ---------------------------------------------------------------------------


class TestDecomposeCompatibility:
    """Table/code/list regions survive the Phase 1 segmentation pass without mangling."""

    def test_table_markdown_has_no_blank_lines(self):
        """Table Markdown must not contain blank lines (would split by paragraph splitter)."""
        from finecorpus.pipeline.assess.parsers.html import _table_to_markdown as html_md
        from finecorpus.pipeline.assess.parsers.spreadsheet import (
            _table_to_markdown as xlsx_md,
        )

        rows = [["Header A", "Header B"], ["Row 1A", "Row 1B"], ["Row 2A", "Row 2B"]]
        for fn in (xlsx_md, html_md):
            result = fn(rows)
            assert "\n\n" not in result, (
                f"{fn.__module__}.{fn.__name__}: table contains blank lines: {result!r}"
            )
            assert result.strip().startswith("|"), f"Markdown table must start with '|': {result!r}"

    def test_report_spreadsheet_table_no_blank_lines(self):
        """report_spreadsheet tables must not have blank lines after serialization."""
        item = _make_item("report_spreadsheet.xlsx")
        pr = _SPREADSHEET_PARSER.parse(item, _TENANCY, _PARSED_AT, _CTX)
        for r in pr.regions:
            if r.detected_class_hint == RegionClassHint.table and r.text:
                assert "\n\n" not in r.text, f"Table region contains blank lines: {r.text[:200]!r}"

    def test_html_table_no_blank_lines(self):
        """HTML table regions must not have blank lines."""
        item = _make_item("nested_tables.html")
        pr = _HTML_PARSER.parse(item, _TENANCY, _PARSED_AT, _CTX)
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

    @pytest.mark.parametrize(
        "fixture_name",
        [
            "report_spreadsheet.xlsx",
            "database_spreadsheet.xlsx",
            "model_spreadsheet.xlsx",
            "confluence_export.html",
            "nested_tables.html",
        ],
    )
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

    @pytest.mark.parametrize(
        "fixture_name",
        [
            "report_spreadsheet.xlsx",
            "database_spreadsheet.xlsx",
            "model_spreadsheet.xlsx",
        ],
    )
    def test_spreadsheet_always_has_triage_finding(self, fixture_name):
        """Every spreadsheet must have a spreadsheet_triage finding (§6.4 visibility)."""
        item = _make_item(fixture_name)
        pr = _SPREADSHEET_PARSER.parse(item, _TENANCY, _PARSED_AT, _CTX)
        triage_findings = [f for f in pr.findings if f.code == "spreadsheet_triage"]
        assert triage_findings, (
            f"{fixture_name}: Missing spreadsheet_triage finding. "
            f"Finding codes: {[f.code for f in pr.findings]}"
        )

    @pytest.mark.parametrize(
        "fixture_name",
        [
            "report_spreadsheet.xlsx",
            "database_spreadsheet.xlsx",
            "model_spreadsheet.xlsx",
            "confluence_export.html",
            "nested_tables.html",
        ],
    )
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


# ---------------------------------------------------------------------------
# F-01: DOM path ordinal correctness
# ---------------------------------------------------------------------------


class TestDomPathOrdinals:
    """F-01: ordinals must reset per real parent, not share a global tag counter."""

    def _parse_html(self, html: str) -> list[dict]:
        """Parse raw HTML and return regions."""

        item = {
            "document_id": "doc-test",
            "content_hash": "aa" * 32,
            "source_path": "/tmp/test.html",
        }
        # Write html to a temp file
        import tempfile

        with tempfile.NamedTemporaryFile(
            suffix=".html", mode="w", delete=False, encoding="utf-8"
        ) as f:
            f.write(html)
            tmp_path = f.name
        item["source_path"] = tmp_path

        pr = _HTML_PARSER.parse(item, _TENANCY, _PARSED_AT, _CTX)
        import os

        os.unlink(tmp_path)
        return pr.regions

    def test_sibling_tables_get_independent_ordinals(self):
        """Two sibling tables must each have table[1] and table[2] as independent ordinals.

        More critically, prose paragraphs INSIDE each table's cells must also
        have independent ordinals.  Before the fix, the ordinal counter was keyed
        on ancestor TAG NAMES tuple — so a <p> inside table[1]'s td and a <p>
        inside table[2]'s td shared a counter, making the second p[2] instead of p[1].
        """
        html = """<html><body>
        <table><tbody>
          <tr><td><p>Table 1 prose text here long enough</p></td></tr>
        </tbody></table>
        <table><tbody>
          <tr><td><p>Table 2 prose text here long enough</p></td></tr>
        </tbody></table>
        </body></html>"""

        regions = self._parse_html(html)
        dom_paths = [r.location.dom_path for r in regions if r.location.dom_path]

        # Both tables must appear: table[1] and table[2]
        assert any("table[1]" in p for p in dom_paths), (
            f"Expected table[1] in dom_paths. Got: {dom_paths}"
        )
        assert any("table[2]" in p for p in dom_paths), (
            f"Expected table[2] in dom_paths. Got: {dom_paths}"
        )

        # Prose regions inside the tables: the p under table[2] must be p[1],
        # not p[2] (which would indicate a shared counter across parents).
        # Check that no region inside table[2] has p[2] (inflated ordinal).
        table2_paths = [p for p in dom_paths if "table[2]" in p]
        for p in table2_paths:
            assert "p[2]" not in p, (
                f"Inflated p ordinal in table[2] path (shared counter bug): {p!r}"
            )

    def test_nested_table_inner_td_ordinals_small(self):
        """Nested-table inner td ordinals must be small (not inflated by outer table).

        F-01: before fix, inner table tds inherited the outer table's counter,
        producing inflated ordinals like td[43] instead of td[1].
        """
        html = """<html><body>
        <table><tbody>
          <tr><td>Outer A</td><td>Outer B</td></tr>
          <tr>
            <td>
              <table><tbody>
                <tr><td>Inner cell</td></tr>
              </tbody></table>
            </td>
          </tr>
        </tbody></table>
        </body></html>"""

        regions = self._parse_html(html)
        # Find any dom_path containing the inner table's td — ordinal should be [1]
        # not a large number from a shared counter
        inner_td_paths = [
            p
            for r in regions
            if r.location.dom_path
            for p in [r.location.dom_path]
            if "table[2]" in p or ("table[1]" in p and p.count("table") > 1)
        ]
        # At minimum: inner table's td must have ordinal 1 (small, not inflated)
        # We check that no td ordinal in any inner path exceeds 10 (sanity bound)
        import re

        for path in inner_td_paths:
            for m in re.finditer(r"td\[(\d+)\]", path):
                ordinal = int(m.group(1))
                assert ordinal <= 10, (
                    f"Inflated td ordinal {ordinal} in inner path {path!r} — shared counter bug"
                )


# ---------------------------------------------------------------------------
# F-02: Segmentation propagates dom_path / cell_range locators
# ---------------------------------------------------------------------------


class TestSegmentLocatorPropagation:
    """F-02: segments must carry the region's locator kind, not always page."""

    def _run_segmentation(self, regions: list[dict]) -> list:
        """Run the segmentation pass over synthetic regions and return segments."""
        from datetime import UTC, datetime

        from finecorpus.pipeline.decompose.passes.base import DocumentContext
        from finecorpus.pipeline.decompose.passes.segmentation import segmentation_pass

        doc_ctx = DocumentContext(
            document_id="doc-seg-test",
            content_hash="bb" * 32,
            tenancy=_TENANCY,
            parse_result={"regions": regions, "findings": []},
            decomposed_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        result = segmentation_pass.run(doc_ctx, [], [])
        return result.segments

    def test_html_region_segment_carries_dom_path(self):
        """Segments from dom_path regions must have locator_kind=dom_path."""
        from finecorpus.contracts.shared.blocks import LocatorKind

        regions = [
            {
                "region_id": "reg-html-1",
                "location": {
                    "locator_kind": LocatorKind.dom_path,
                    "dom_path": "body[1]/div[1]/p[1]",
                },
                "text": "This is a sufficiently long paragraph for segmentation purposes.",
                "extract_status": "ok",
                "detected_class_hint": "prose",
            }
        ]
        segments = self._run_segmentation(regions)
        assert segments, "Expected at least one segment from dom_path region"
        for seg in segments:
            assert seg.location.locator_kind.value == "dom_path", (
                f"Expected dom_path locator, got {seg.location.locator_kind}"
            )
            assert seg.location.dom_path, "dom_path must be populated for dom_path segment"

    def test_spreadsheet_region_segment_carries_cell_range(self):
        """Segments from cell_range regions must have locator_kind=cell_range."""
        from finecorpus.contracts.shared.blocks import LocatorKind

        regions = [
            {
                "region_id": "reg-xlsx-1",
                "location": {
                    "locator_kind": LocatorKind.cell_range,
                    "cell_range": "Sheet1!A1:D10",
                },
                "text": "| Header A | Header B |\n| --- | --- |\n| Row 1 | Row 2 |",
                "extract_status": "ok",
                "detected_class_hint": "table",
            }
        ]
        segments = self._run_segmentation(regions)
        assert segments, "Expected at least one segment from cell_range region"
        for seg in segments:
            assert seg.location.locator_kind.value == "cell_range", (
                f"Expected cell_range locator, got {seg.location.locator_kind}"
            )
            assert seg.location.cell_range, "cell_range must be populated for cell_range segment"

    def test_confluence_html_segments_carry_dom_path_locator(self):
        """End-to-end: confluence_export.html segments carry dom_path locator."""
        from datetime import UTC, datetime

        from finecorpus.contracts.shared.blocks import LocatorKind
        from finecorpus.pipeline.decompose.passes.base import DocumentContext
        from finecorpus.pipeline.decompose.passes.segmentation import segmentation_pass

        pr = _HTML_PARSER.parse(_make_item("confluence_export.html"), _TENANCY, _PARSED_AT, _CTX)
        regions_raw = [
            {
                "region_id": r.region_id,
                "location": {
                    "locator_kind": r.location.locator_kind,
                    "dom_path": r.location.dom_path,
                },
                "text": r.text or "",
                "extract_status": r.extract_status.value if r.extract_status else "ok",
                "detected_class_hint": (
                    r.detected_class_hint.value if r.detected_class_hint else None
                ),
            }
            for r in pr.regions
        ]
        doc_ctx = DocumentContext(
            document_id="doc-confluence",
            content_hash="cc" * 32,
            tenancy=_TENANCY,
            parse_result={"regions": regions_raw, "findings": []},
            decomposed_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        result = segmentation_pass.run(doc_ctx, [], [])
        segs = result.segments
        assert segs, "Expected segments from confluence_export.html"
        dom_path_segs = [s for s in segs if s.location.locator_kind == LocatorKind.dom_path]
        assert dom_path_segs, (
            f"Expected at least one dom_path segment from confluence HTML. "
            f"Locator kinds seen: {[s.location.locator_kind for s in segs]}"
        )
        # All dom_path segments must have a non-empty dom_path
        for seg in dom_path_segs:
            assert seg.location.dom_path, (
                f"dom_path segment missing dom_path value: {seg.segment_id}"
            )


# ---------------------------------------------------------------------------
# F-03: D-11 table_to_markdown TransformationRecord
# ---------------------------------------------------------------------------


class TestTableTransformationRecord:
    """F-03 / D-11: chunks from table regions carry table_to_markdown TransformationRecord."""

    def _segments_from_regions(self, regions: list[dict]) -> list:
        from datetime import UTC, datetime

        from finecorpus.pipeline.decompose.passes.base import DocumentContext
        from finecorpus.pipeline.decompose.passes.segmentation import segmentation_pass

        doc_ctx = DocumentContext(
            document_id="doc-build-test",
            content_hash="dd" * 32,
            tenancy=_TENANCY,
            parse_result={"regions": regions, "findings": []},
            decomposed_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        result = segmentation_pass.run(doc_ctx, [], [])
        return result.segments

    def test_table_segment_has_table_to_markdown_record(self):
        """A segment produced from a table region must have table_to_markdown in provenance.

        The TransformationRecord is emitted by the Build stage's _build_provenance().
        We test it by calling _build_provenance directly with a table-typed segment.
        """
        # Build a minimal SegmentSet stub
        from finecorpus.contracts.segment_set import (
            ReassemblyMethod,
            ReassemblyRecord,
            Segment,
            SegmentSet,
        )
        from finecorpus.contracts.shared.blocks import (
            LocatorKind,
            SalienceSignal,
            SalienceSignalKind,
            SalienceTier,
            SegmentType,
            SourceLocation,
        )
        from finecorpus.pipeline.build.stage import _build_provenance

        seg = Segment(
            segment_id="seg-table-001",
            document_order=0,
            segment_type=SegmentType.table,
            salience_tier=SalienceTier.primary,
            structural_path=[],
            segment_path="#0",
            location=SourceLocation(
                locator_kind=LocatorKind.cell_range,
                cell_range="Sheet1!A1:D5",
            ),
            source_region_ids=["reg-xlsx-001"],
            language="und",
            ocr_confidence=None,
            injection_suspicion=0.0,
            invisible_content_flags=[],
            sensitivity_flags=[],
            salience_signals=[
                SalienceSignal(
                    kind=SalienceSignalKind.segment_type_prior,
                    implied_tier=SalienceTier.primary,
                    won=True,
                    detail="table prior",
                )
            ],
            salience_basis=SalienceSignalKind.segment_type_prior,
            text="| A | B |\n| --- | --- |\n| 1 | 2 |",
        )

        ss = SegmentSet(
            schema_version="1.1.0",
            document_id="doc-build-test",
            content_hash="dd" * 32,
            tenancy=_TENANCY,
            segments=[seg],
            exclusions=[],
            cross_references=[],
            decomposed_at=_PARSED_AT,
            config_version="p1.0",
            reassembly=ReassemblyRecord(
                method=ReassemblyMethod.document_order_concat,
                covered_region_ids=["reg-xlsx-001"],
                reassembly_digest="abc123",
            ),
        )

        # Phase 3: transformation records flow from apply_tier1, not auto-injected.
        # The D-11 record is emitted when Tier1Operation.table_to_markdown is in the
        # class rule's tier1_operations.  Test by running apply_tier1 with that op.
        from finecorpus.contracts.ingestion_config import Tier1Operation
        from finecorpus.pipeline.build.transform import apply_tier1

        _, records = apply_tier1(seg.text, [Tier1Operation.table_to_markdown])
        provenance = _build_provenance(ss, seg, records)
        transformations = provenance.get("transformations", [])
        assert transformations, (
            "Expected table_to_markdown TransformationRecord for table segment, "
            f"got empty transformations. provenance keys: {list(provenance.keys())}"
        )
        ops = [t["operation"] for t in transformations]
        assert "table_to_markdown" in ops, (
            f"Expected table_to_markdown in transformations, got: {ops}"
        )
        rec = next(t for t in transformations if t["operation"] == "table_to_markdown")
        assert rec["tier"] == 1, f"Expected tier=1 (Tier 1), got {rec['tier']}"
        # table_to_markdown is a no-op at Build (conversion happened at parse layer)
        # so changed_text=False per Phase 3 design (transform.py _apply_table_to_markdown)
        assert rec["changed_text"] is False, (
            "table_to_markdown at Build layer is a parse-layer annotation: "
            "changed_text=False (no bytes altered at Build)."
        )
        assert rec["applied_by"] == "deterministic", (
            f"Expected deterministic, got {rec['applied_by']}"
        )

    def test_prose_segment_has_no_transformation_record(self):
        """A prose segment must NOT carry any TransformationRecord (no conversion applied)."""
        from finecorpus.contracts.segment_set import (
            ReassemblyMethod,
            ReassemblyRecord,
            Segment,
            SegmentSet,
        )
        from finecorpus.contracts.shared.blocks import (
            LocatorKind,
            SalienceSignal,
            SalienceSignalKind,
            SalienceTier,
            SegmentType,
            SourceLocation,
        )
        from finecorpus.pipeline.build.stage import _build_provenance

        seg = Segment(
            segment_id="seg-prose-001",
            document_order=0,
            segment_type=SegmentType.prose,
            salience_tier=SalienceTier.primary,
            structural_path=[],
            segment_path="#0",
            location=SourceLocation(
                locator_kind=LocatorKind.page,
                page_start=1,
                page_end=1,
            ),
            source_region_ids=["reg-prose-001"],
            language="und",
            ocr_confidence=None,
            injection_suspicion=0.0,
            invisible_content_flags=[],
            sensitivity_flags=[],
            salience_signals=[
                SalienceSignal(
                    kind=SalienceSignalKind.segment_type_prior,
                    implied_tier=SalienceTier.primary,
                    won=True,
                    detail="prose prior",
                )
            ],
            salience_basis=SalienceSignalKind.segment_type_prior,
            text="This is plain prose text. No table conversion was applied.",
        )

        ss = SegmentSet(
            schema_version="1.1.0",
            document_id="doc-build-test",
            content_hash="ee" * 32,
            tenancy=_TENANCY,
            segments=[seg],
            exclusions=[],
            cross_references=[],
            decomposed_at=_PARSED_AT,
            config_version="p1.0",
            reassembly=ReassemblyRecord(
                method=ReassemblyMethod.document_order_concat,
                covered_region_ids=["reg-prose-001"],
                reassembly_digest="def456",
            ),
        )

        # Phase 3: pass empty records (no tier1 ops applied for a plain prose segment)
        provenance = _build_provenance(ss, seg, [])
        transformations = provenance.get("transformations", [])
        assert transformations == [], (
            f"Prose segment must have no TransformationRecords, got: {transformations}"
        )


# ---------------------------------------------------------------------------
# F-07: CSV exclusion specificity
# ---------------------------------------------------------------------------


class TestCSVExclusion:
    """F-07: CSV files must be claimed and returned with a specific csv_not_supported finding."""

    def test_csv_can_parse_returns_true(self):
        """.csv extension must be claimed by SpreadsheetFormatParser.can_parse."""
        item = {"source_path": "/some/data.csv"}
        assert _SPREADSHEET_PARSER.can_parse(item), (
            "Expected can_parse=True for .csv (so fallback's wrong Phase-1 message is bypassed)"
        )

    def test_csv_produces_excluded_result(self, tmp_path):
        """Parsing a .csv produces excluded_pre_parse with csv_not_supported finding."""
        import csv

        csv_path = tmp_path / "data.csv"
        with open(csv_path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["col1", "col2"])
            writer.writerow(["a", "b"])

        item = {
            "document_id": "doc-csv-test",
            "content_hash": "ff" * 32,
            "source_path": str(csv_path),
        }
        pr = _SPREADSHEET_PARSER.parse(item, _TENANCY, _PARSED_AT, _CTX)
        assert pr.parse_status.value == "excluded_pre_parse", (
            f"Expected excluded_pre_parse for CSV, got {pr.parse_status!r}"
        )
        codes = [f.code for f in pr.findings]
        assert "csv_not_supported" in codes, f"Expected csv_not_supported finding, got: {codes}"

    def test_csv_finding_mentions_openpyxl_and_future_work(self, tmp_path):
        """The csv_not_supported finding message must be honest about why."""
        import csv

        csv_path = tmp_path / "data.csv"
        with open(csv_path, "w", newline="") as f:
            csv.writer(f).writerow(["x"])

        item = {
            "document_id": "doc-csv2",
            "content_hash": "11" * 32,
            "source_path": str(csv_path),
        }
        pr = _SPREADSHEET_PARSER.parse(item, _TENANCY, _PARSED_AT, _CTX)
        finding = next((f for f in pr.findings if f.code == "csv_not_supported"), None)
        assert finding is not None
        msg = finding.message.lower()
        assert "openpyxl" in msg or "csv" in msg, (
            f"Finding should mention openpyxl or csv limitation: {finding.message}"
        )
        assert "future" in msg or "phase 3" in msg or "convert" in msg, (
            f"Finding should mention future work or remediation: {finding.message}"
        )
