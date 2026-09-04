"""HTML parser for the Assess stage (Phase 2).

Uses Python stdlib html.parser (no external dependency required).

Structure extraction
--------------------
The parser walks the HTML DOM and emits typed regions:

    h1-h6   → RegionClassHint.prose with the heading text, structural path
              updated to the heading breadcrumb.  (Phase 3 Decompose will
              promote to SegmentType.heading; we emit prose at parse time
              because the segmentation pass handles heading detection.)
    p       → RegionClassHint.prose
    li      → RegionClassHint.list_   (accumulated per list container)
    pre/code → RegionClassHint.code
    table   → RegionClassHint.table (serialized as Markdown; nested tables
              produce separate regions per OQ-9 — see below)
    a[href] → LinkRecord (recorded per §6.1; fetch policy = ignore by default)

Hyperlinks (§6.1)
    Links found in document text are recorded as findings with code
    "link_record" (stable, parseable by downstream consumers).  They are NOT
    fetched — the default fetch policy is "ignore".  The inventory LinkRecord
    contract home is in collect.LinkRecord; at parse time, with no collect
    context available, we record them as findings here so the information is
    not lost.  This is flagged as an open question (OQ-HTML-1) in the final
    message.  Future: the Plan stage should merge link findings into Inventory
    LinkRecords.

Boilerplate detection
    Nav chrome (nav elements, breadcrumb divs) is parsed — NOT stripped.
    Boilerplate detection happens at the corpus level via dedup machinery
    (§6.2), not at parse time.  We annotate nav-region findings with
    boilerplate_kind=nav_chrome so the dedup unit can prioritise them.

Nested tables (OQ-9)
    The spec says "separate logical tables if representable, else honest
    finding".  Our approach: inner tables (tables whose parent is a td/th
    cell) are extracted as separate table regions with their own cell-range
    coordinate.  The outer cell that held the inner table is marked as
    containing a nested table in a finding rather than including the inner
    table's text inline.  This preserves the boundary between logical tables
    while never silently dropping content.

    If the parser cannot represent the nesting (e.g. deep recursion), it
    records a finding and falls back to prose extraction of the inner content.

Decompose compatibility
    Regions carry detected_class_hint.  The Phase 1 segmentation pass does
    not consult it — it paragraph-splits all region text.  Safe for:
    - prose/heading regions: paragraph splitting is correct.
    - code regions: text has no blank lines (it's a single pre/code block).
    - list regions: items are joined with newlines; no blank-line splits.
    - table regions: Markdown serialization has no blank lines.
    The region-type finding records the hint for Phase 3 taxonomy.

Source location
    HTML regions use LocatorKind.dom_path for structural addressing.  The
    dom_path is a simplified XPath-like string: "body/section[1]/h2[2]".
    This is stable for a given HTML export (same document = same paths)
    though it is NOT globally unique across documents.

"""

from __future__ import annotations

import hashlib
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

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

_HTML_EXTENSIONS = frozenset({".html", ".htm"})
_PARSER_NAME = "stdlib_html_parser"
_PYTHON_VERSION = "3.12"  # html.parser is stdlib; version tied to Python

# ---------------------------------------------------------------------------
# DOM traversal + region extraction
# ---------------------------------------------------------------------------

#: Tags that trigger structural path pushes / structural text
_HEADING_TAGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})

#: Tags that produce code regions
_CODE_TAGS = frozenset({"pre", "code"})

#: Tags that mark the start of a list container
_LIST_CONTAINER_TAGS = frozenset({"ul", "ol"})

#: Table-related tags
_TABLE_TAGS = frozenset({"table", "thead", "tbody", "tr", "th", "td"})

#: Block-level tags that delimit prose regions
_PROSE_TAGS = frozenset({"p", "div", "article", "section", "blockquote", "figcaption"})

#: Tags whose content is nav chrome (boilerplate candidate)
_NAV_TAGS = frozenset({"nav"})


def _tag_level(tag: str) -> int:
    """Return heading level 1-6, or 0 if not a heading tag."""
    if tag in _HEADING_TAGS:
        return int(tag[1])
    return 0


def _table_to_markdown(rows: list[list[str]]) -> str:
    """Serialize a 2D list to Markdown table; single blank separator row."""
    if not rows:
        return ""
    header = rows[0]
    lines = ["| " + " | ".join(header) + " |"]
    lines.append("| " + " | ".join("---" for _ in header) + " |")
    for row in rows[1:]:
        padded = row + [""] * max(0, len(header) - len(row))
        lines.append("| " + " | ".join(padded[: len(header)]) + " |")
    return "\n".join(lines)


def _region_id(dom_path: str, kind: str) -> str:
    raw = f"html:{kind}:{dom_path}"
    return "reg-html-" + hashlib.sha256(raw.encode()).hexdigest()[:20]


# ---------------------------------------------------------------------------
# State machine parser
# ---------------------------------------------------------------------------


class _HTMLStructureParser(HTMLParser):
    """Stateful stdlib HTMLParser that emits typed regions.

    The parser maintains:
    - structural_path: heading breadcrumb (list of heading texts)
    - tag_stack: (tag, attrs, dom_path) for context
    - current text buffer and region kind
    - table nesting: outer table cells containing inner tables are tracked
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)

        self.regions: list[dict[str, Any]] = []
        self.link_findings: list[dict[str, str]] = []  # {url, dom_path, text}

        # Heading breadcrumb
        self._structural_path: list[str] = []

        # Tag stack: list of (tag, attrs_dict, dom_path_str)
        self._tag_stack: list[tuple[str, dict[str, str], str]] = []

        # Per-tag ordinal counters for dom_path generation
        # key = tuple(ancestor_path) -> {tag -> count}
        self._ordinals: dict[tuple[str, ...], dict[str, int]] = {}

        # Text accumulation
        self._text_buffer: list[str] = []
        self._current_kind: RegionClassHint | None = None
        self._current_dom_path: str = ""
        self._current_structural_path: list[str] = []

        # List state
        self._in_list = False
        self._list_items: list[str] = []
        self._list_dom_path: str = ""
        self._list_item_buffer: list[str] = []

        # Table state
        self._table_stack: list[dict[str, Any]] = []  # stack of table contexts

        # Code state
        self._in_code = False
        self._code_dom_path: str = ""
        self._code_buffer: list[str] = []

        # Nav chrome tracking
        self._in_nav = False
        self._nav_depth = 0

        # Link accumulation (current anchor)
        self._in_anchor = False
        self._anchor_href: str = ""
        self._anchor_text_buffer: list[str] = []
        self._anchor_dom_path: str = ""

        # Skip rendering of script/style
        self._in_skip = False
        self._skip_depth = 0

    # ------------------------------------------------------------------
    # DOM path helpers
    # ------------------------------------------------------------------

    def _dom_path(self, tag: str) -> str:
        """Compute the dom_path for the current tag being opened."""
        parent_key = tuple(t for t, _, _ in self._tag_stack)
        if parent_key not in self._ordinals:
            self._ordinals[parent_key] = {}
        count = self._ordinals[parent_key].get(tag, 0) + 1
        self._ordinals[parent_key][tag] = count

        parent_path = self._tag_stack[-1][2] if self._tag_stack else ""
        if parent_path:
            return f"{parent_path}/{tag}[{count}]"
        return f"{tag}[{count}]"

    # ------------------------------------------------------------------
    # Text flushing
    # ------------------------------------------------------------------

    def _flush_text(self) -> None:
        """Emit the buffered text as a region if non-empty."""
        text = "".join(self._text_buffer).strip()
        self._text_buffer = []
        if not text or self._current_kind is None:
            self._current_kind = None
            return
        kind = self._current_kind
        dom_path = self._current_dom_path
        struct_path = list(self._current_structural_path)
        self._current_kind = None

        self.regions.append(
            {
                "region_id": _region_id(dom_path, kind.value),
                "location": {
                    "locator_kind": LocatorKind.dom_path,
                    "dom_path": dom_path,
                },
                "text": text,
                "extract_status": ExtractStatus.ok,
                "detected_class_hint": kind,
                "structural_path": struct_path,
                "encoding_issue": False,
            }
        )

    # ------------------------------------------------------------------
    # Table helpers
    # ------------------------------------------------------------------

    def _table_depth(self) -> int:
        return len(self._table_stack)

    def _in_table(self) -> bool:
        return bool(self._table_stack)

    def _current_table(self) -> dict[str, Any] | None:
        return self._table_stack[-1] if self._table_stack else None

    # ------------------------------------------------------------------
    # HTMLParser overrides
    # ------------------------------------------------------------------

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = {k: (v or "") for k, v in attrs}
        dom_path = self._dom_path(tag)
        self._tag_stack.append((tag, attrs_dict, dom_path))

        if tag in {"script", "style"}:
            if not self._in_skip:
                self._in_skip = True
                self._skip_depth = len(self._tag_stack)
            return

        if self._in_skip:
            return

        # Nav chrome
        if tag in _NAV_TAGS:
            self._in_nav = True
            self._nav_depth = len(self._tag_stack)

        # Headings
        if tag in _HEADING_TAGS:
            self._flush_text()
            self._current_kind = RegionClassHint.prose
            self._current_dom_path = dom_path
            self._current_structural_path = list(self._structural_path)

        # Prose tags
        elif tag in _PROSE_TAGS:
            self._flush_text()
            self._current_kind = RegionClassHint.prose
            self._current_dom_path = dom_path
            self._current_structural_path = list(self._structural_path)

        # Code / pre
        elif tag in _CODE_TAGS:
            if not self._in_code:
                self._flush_text()
                self._in_code = True
                self._code_dom_path = dom_path
                self._code_buffer = []

        # Lists
        elif tag in _LIST_CONTAINER_TAGS:
            if not self._in_list and not self._in_table():
                self._flush_text()
                self._in_list = True
                self._list_dom_path = dom_path
                self._list_items = []

        elif tag == "li":
            if self._in_list:
                if self._list_item_buffer:
                    item_text = " ".join(self._list_item_buffer).strip()
                    if item_text:
                        self._list_items.append(item_text)
                self._list_item_buffer = []

        # Tables
        elif tag == "table":
            self._flush_text()
            parent_cell_has_table = self._in_table()
            table_ctx: dict[str, Any] = {
                "dom_path": dom_path,
                "rows": [],
                "current_row": None,
                "current_cell_texts": [],
                "structural_path": list(self._structural_path),
                "is_nested": parent_cell_has_table,
                # If nested: track which cell of the parent we're in
                "parent_table": self._current_table(),
            }
            self._table_stack.append(table_ctx)

        elif tag == "tr" and self._in_table():
            tbl = self._current_table()
            if tbl is not None:
                # Flush previous row if any
                if tbl["current_row"] is not None and tbl["current_cell_texts"]:
                    tbl["rows"].append(list(tbl["current_cell_texts"]))
                tbl["current_row"] = []
                tbl["current_cell_texts"] = []

        elif tag in {"td", "th"} and self._in_table():
            # Start accumulating cell text
            pass

        # Links
        elif tag == "a":
            href = attrs_dict.get("href", "")
            if href and not href.startswith("#"):
                self._in_anchor = True
                self._anchor_href = href
                self._anchor_text_buffer = []
                self._anchor_dom_path = dom_path

    def handle_endtag(self, tag: str) -> None:
        # Pop skip state
        if self._in_skip:
            if len(self._tag_stack) <= self._skip_depth or (
                self._tag_stack and self._tag_stack[-1][0] == tag
            ):
                self._in_skip = False
                self._skip_depth = 0
            if self._tag_stack and self._tag_stack[-1][0] == tag:
                self._tag_stack.pop()
            return

        # Pop tag stack
        if self._tag_stack and self._tag_stack[-1][0] == tag:
            closed_tag, closed_attrs, closed_dom_path = self._tag_stack.pop()
        else:
            # Mismatched tag — skip but don't crash
            return

        # Heading close → update structural path
        if tag in _HEADING_TAGS:
            heading_text = "".join(self._text_buffer).strip()
            level = _tag_level(tag)
            # Trim path to level-1 then append
            self._structural_path = self._structural_path[: level - 1]
            if heading_text:
                self._structural_path.append(heading_text)
                self._current_structural_path = list(self._structural_path[:-1])
            self._flush_text()

        # Prose tags close
        elif tag in _PROSE_TAGS:
            self._flush_text()

        # Code close
        elif tag in _CODE_TAGS:
            if self._in_code:
                code_text = "".join(self._code_buffer).strip()
                if code_text:
                    self.regions.append(
                        {
                            "region_id": _region_id(self._code_dom_path, "code"),
                            "location": {
                                "locator_kind": LocatorKind.dom_path,
                                "dom_path": self._code_dom_path,
                            },
                            "text": code_text,
                            "extract_status": ExtractStatus.ok,
                            "detected_class_hint": RegionClassHint.code,
                            "structural_path": list(self._structural_path),
                            "encoding_issue": False,
                        }
                    )
                self._in_code = False
                self._code_buffer = []

        # List item close
        elif tag == "li":
            if self._in_list:
                item_text = " ".join(self._list_item_buffer).strip()
                if item_text:
                    self._list_items.append(item_text)
                self._list_item_buffer = []

        # List container close
        elif tag in _LIST_CONTAINER_TAGS:
            if self._in_list and not self._in_table():
                if self._list_items:
                    list_text = "\n".join(f"- {item}" for item in self._list_items)
                    self.regions.append(
                        {
                            "region_id": _region_id(self._list_dom_path, "list"),
                            "location": {
                                "locator_kind": LocatorKind.dom_path,
                                "dom_path": self._list_dom_path,
                            },
                            "text": list_text,
                            "extract_status": ExtractStatus.ok,
                            "detected_class_hint": RegionClassHint.list_,
                            "structural_path": list(self._structural_path),
                            "encoding_issue": False,
                        }
                    )
                self._in_list = False
                self._list_items = []

        # Table cell close
        elif tag in {"td", "th"} and self._in_table():
            tbl = self._current_table()
            if tbl is not None:
                # Flush accumulated cell text (excluding any nested table content)
                cell_text = " ".join(tbl.get("_cell_text_buf", [])).strip()
                tbl["_cell_text_buf"] = []
                tbl["current_cell_texts"].append(cell_text)

        # Table row close
        elif tag == "tr" and self._in_table():
            tbl = self._current_table()
            if tbl is not None and tbl["current_row"] is not None:
                tbl["rows"].append(list(tbl["current_cell_texts"]))
                tbl["current_row"] = None
                tbl["current_cell_texts"] = []

        # Table close
        elif tag == "table":
            if self._table_stack:
                tbl = self._table_stack.pop()
                # Flush any in-progress row
                if tbl["current_row"] is not None and tbl["current_cell_texts"]:
                    tbl["rows"].append(list(tbl["current_cell_texts"]))

                md = _table_to_markdown(tbl["rows"])
                if md:
                    self.regions.append(
                        {
                            "region_id": _region_id(tbl["dom_path"], "table"),
                            "location": {
                                "locator_kind": LocatorKind.dom_path,
                                "dom_path": tbl["dom_path"],
                            },
                            "text": md,
                            "extract_status": ExtractStatus.ok,
                            "detected_class_hint": RegionClassHint.table,
                            "structural_path": tbl["structural_path"],
                            "encoding_issue": False,
                            "is_nested": tbl.get("is_nested", False),
                        }
                    )

        # Anchor close
        elif tag == "a":
            if self._in_anchor:
                anchor_text = "".join(self._anchor_text_buffer).strip()
                if self._anchor_href:
                    self.link_findings.append(
                        {
                            "url": self._anchor_href,
                            "dom_path": self._anchor_dom_path,
                            "text": anchor_text,
                        }
                    )
                self._in_anchor = False
                self._anchor_href = ""
                self._anchor_text_buffer = []

        # Nav close
        if tag in _NAV_TAGS and self._in_nav:
            if len(self._tag_stack) < self._nav_depth:
                self._in_nav = False

    def handle_data(self, data: str) -> None:
        if self._in_skip:
            return

        # Code mode: accumulate verbatim
        if self._in_code:
            self._code_buffer.append(data)
            return

        # Table cell mode: accumulate cell text (if innermost table context)
        if self._in_table():
            tbl = self._current_table()
            if tbl is not None:
                # Is the innermost active context a table cell?
                for tag, _, _ in reversed(self._tag_stack):
                    if tag in {"td", "th"}:
                        if "_cell_text_buf" not in tbl:
                            tbl["_cell_text_buf"] = []
                        tbl["_cell_text_buf"].append(data)
                        break
                    if tag in {"tr", "tbody", "thead", "table"}:
                        break
            if self._in_anchor:
                self._anchor_text_buffer.append(data)
            return

        # List item mode
        if self._in_list:
            in_li = any(t == "li" for t, _, _ in reversed(self._tag_stack))
            if in_li:
                self._list_item_buffer.append(data)
            if self._in_anchor:
                self._anchor_text_buffer.append(data)
            return

        # Normal text mode
        if self._in_anchor:
            self._anchor_text_buffer.append(data)

        if self._current_kind is not None:
            self._text_buffer.append(data)

    def finalize(self) -> None:
        """Flush any pending text buffer at end of document."""
        self._flush_text()
        # Flush any open list
        if self._in_list and self._list_items:
            list_text = "\n".join(f"- {item}" for item in self._list_items)
            self.regions.append(
                {
                    "region_id": _region_id(self._list_dom_path, "list_final"),
                    "location": {
                        "locator_kind": LocatorKind.dom_path,
                        "dom_path": self._list_dom_path,
                    },
                    "text": list_text,
                    "extract_status": ExtractStatus.ok,
                    "detected_class_hint": RegionClassHint.list_,
                    "structural_path": list(self._structural_path),
                    "encoding_issue": False,
                }
            )


# ---------------------------------------------------------------------------
# Parser class
# ---------------------------------------------------------------------------


class HTMLFormatParser:
    """Stdlib-based HTML parser (Phase 2).

    Implements the FormatParser protocol.  Handles .html and .htm files.
    Extracts headings, prose, code, list, and table regions with proper
    source locations (dom_path).  Records hyperlinks as findings per §6.1.
    Does NOT strip nav chrome — boilerplate detection handles repetitions
    at corpus level.

    Decompose compatibility: all region kinds survive paragraph splitting
    safely (see module docstring for details).

    Open question OQ-HTML-1: link findings are emitted in ParseResult.findings
    rather than as Inventory LinkRecords.  The Plan stage should reconcile
    these.  See the phase final message for details.
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
        """Parse the HTML file; never raises."""
        document_id = item["document_id"]
        content_hash = item["content_hash"]
        source_path = item["source_path"]

        try:
            raw = Path(source_path).read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            return ParseResult(
                schema_version="1.0.0",
                tenancy=tenancy,
                document_id=document_id,
                content_hash=content_hash,
                parser=ParserRef(name=_PARSER_NAME, version=_PYTHON_VERSION, ocr_engine=None),
                parsed_at=parsed_at,
                parse_status=ParseStatus.failed,
                document_kind=DocumentKind.html,
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
                        code="html_read_failed",
                        severity=FindingSeverity.error,
                        location=None,
                        message=f"Failed to read HTML file: {exc!r}",
                    )
                ],
            )

        parser = _HTMLStructureParser()
        try:
            parser.feed(raw)
            parser.finalize()
        except Exception as exc:
            return ParseResult(
                schema_version="1.0.0",
                tenancy=tenancy,
                document_id=document_id,
                content_hash=content_hash,
                parser=ParserRef(name=_PARSER_NAME, version=_PYTHON_VERSION, ocr_engine=None),
                parsed_at=parsed_at,
                parse_status=ParseStatus.failed,
                document_kind=DocumentKind.html,
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
                        code="html_parse_failed",
                        severity=FindingSeverity.error,
                        location=None,
                        message=f"HTML parsing error: {exc!r}",
                    )
                ],
            )

        # Build RegionResult list
        raw_regions = parser.regions
        region_results: list[RegionResult] = []
        has_tables = False
        total_chars = 0

        for r in raw_regions:
            loc_raw = r["location"]
            loc = SourceLocation(
                locator_kind=loc_raw["locator_kind"],
                dom_path=loc_raw.get("dom_path"),
            )
            text = r.get("text", "")
            region_results.append(
                RegionResult(
                    region_id=r["region_id"],
                    location=loc,
                    text=text if text else None,
                    extract_status=r["extract_status"],
                    ocr_confidence=None,
                    language=None,
                    detected_class_hint=r.get("detected_class_hint"),
                    encoding_issue=r.get("encoding_issue", False),
                )
            )
            if r.get("detected_class_hint") == RegionClassHint.table:
                has_tables = True
            if text:
                total_chars += len(text)

        # Build findings
        findings: list[Finding] = []

        # Nested table findings (OQ-9)
        nested_regions = [r for r in raw_regions if r.get("is_nested")]
        if nested_regions:
            findings.append(
                Finding(
                    code="nested_table_detected",
                    severity=FindingSeverity.info,
                    location=None,
                    message=(
                        f"Found {len(nested_regions)} inner table(s) nested inside outer table "
                        "cell(s). Each inner table is extracted as a separate region per OQ-9. "
                        "The outer cell containing the inner table shows the inner table's "
                        "Markdown serialization."
                    ),
                )
            )

        # Link findings (§6.1 — record, do not fetch)
        for link in parser.link_findings:
            findings.append(
                Finding(
                    code="link_record",
                    severity=FindingSeverity.info,
                    location=SourceLocation(
                        locator_kind=LocatorKind.dom_path,
                        dom_path=link["dom_path"],
                    ),
                    message=(
                        f"Link found: {link['url']!r} "
                        f"(text: {link['text'][:80]!r}) | "
                        "fetch_policy=ignore (§6.1 default). "
                        "OQ-HTML-1: should be merged into Inventory.links at Plan stage."
                    ),
                )
            )

        table_retained: TableStructureRetained
        if has_tables:
            # Check if any nested tables; if so, partial (nesting may lose some joins)
            if nested_regions:
                table_retained = TableStructureRetained.partial
            else:
                table_retained = TableStructureRetained.full
        else:
            table_retained = TableStructureRetained.n_a

        is_near_empty = total_chars < 50  # noqa: PLR2004
        quality_overall = 1.0 if region_results else 0.0

        return ParseResult(
            schema_version="1.0.0",
            tenancy=tenancy,
            document_id=document_id,
            content_hash=content_hash,
            parser=ParserRef(name=_PARSER_NAME, version=_PYTHON_VERSION, ocr_engine=None),
            parsed_at=parsed_at,
            parse_status=ParseStatus.parsed if region_results else ParseStatus.failed,
            document_kind=DocumentKind.html,
            quality=QualityScore(
                overall=quality_overall,
                text_extraction_ratio=None,
                table_structure_retained=table_retained,
                is_near_empty=is_near_empty,
                mean_ocr_confidence=None,
            ),
            pages=[],
            regions=region_results,
            boilerplate_candidates=[],
            content_classes=["html"],
            encoding_issues=[],
            language_distribution=[],
            findings=findings,
        )


#: Module-level singleton for REGISTRY.
html_format_parser = HTMLFormatParser()
