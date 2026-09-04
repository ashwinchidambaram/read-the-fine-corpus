"""Parser protocol and context for the Assess stage.

Every format parser must implement the FormatParser protocol.  The Assess
stage routes each inventory item to the first parser whose ``can_parse``
returns True (ordered registry — see ``parsers/__init__.py``).

Extension points (Phase 2)
--------------------------
To add a new parser (e.g. OCR, HTML, spreadsheet):

1.  Create ``src/finecorpus/pipeline/assess/parsers/<name>.py`` and define a
    class or module-level instance that satisfies the ``FormatParser`` protocol.

2.  Implement ``can_parse(item)`` using extension, media-type, or any other
    signal available on the inventory item dict.

3.  Implement ``parse(item, tenancy, parsed_at, ctx)`` and return a ``ParseResult``
    (same contract as Phase 1 — see ``finecorpus.contracts.parse_result``).

4.  Import your parser and insert it at the correct position in ``REGISTRY``
    in ``parsers/__init__.py``.  The registry is an **ordered list**; the first
    parser whose ``can_parse`` returns True wins.  Unsupported parsers must
    remain at the end so they act as a catch-all for unrecognised formats.

Registry ordering rule
-----------------------
*  Most-specific parsers (e.g. ``ocr_pdf``) before less-specific ones
   (e.g. ``pdf_native``) if they handle overlapping ``can_parse`` conditions.
*  ``UnsupportedParser`` entries must be **last**; they return
   ``excluded_pre_parse`` for everything they claim.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from finecorpus.contracts.parse_result import ParseResult
from finecorpus.contracts.shared.blocks import TenancyBlock


@dataclass(frozen=True)
class ParserContext:
    """Configuration thresholds and knobs passed to every parser.

    All fields mirror the executor-defined constants documented in
    ``docs/pipeline/assess.md``.  Parsers must read thresholds from this
    context rather than hard-coding values so that future configuration
    injection (e.g. per-tenant overrides) does not require parser changes.

    Attributes:
        min_chars_per_page: Expected minimum characters per page for a
            well-extracted native-text page.  Used for extraction-density
            scoring.
        near_empty_threshold: Fraction of pages that must have more than
            ``page_nonempty_chars`` characters; below this the document is
            flagged ``is_near_empty=True``.
        page_nonempty_chars: Minimum characters on a page for it to count
            as non-empty in quality scoring.
    """

    min_chars_per_page: int = 200
    near_empty_threshold: float = 0.20
    page_nonempty_chars: int = 50


class FormatParser(Protocol):
    """Protocol every format parser must satisfy.

    A parser is a stateless object (class instance or module-level singleton)
    with two methods:

    ``can_parse(item)``
        Returns ``True`` if this parser claims the inventory item.  Must be
        fast and side-effect-free — the registry calls it for every item.

    ``parse(item, tenancy, parsed_at, ctx)``
        Performs the actual extraction and returns a ``ParseResult``.
        Must not raise; all errors must be recorded as ``parse_status`` and
        ``findings`` on the returned result.
    """

    def can_parse(self, item: dict[str, Any]) -> bool:
        """Return True if this parser can handle the inventory item."""
        ...

    def parse(
        self,
        item: dict[str, Any],
        tenancy: TenancyBlock,
        parsed_at: datetime,
        ctx: ParserContext,
    ) -> ParseResult:
        """Parse the item and return a ParseResult.

        Must never raise; failures are encoded in parse_status / findings.
        """
        ...
