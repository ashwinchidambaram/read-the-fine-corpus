"""Parser registry for the Assess stage.

``REGISTRY`` is an **ordered list** of ``FormatParser`` instances.  The Assess
stage iterates this list for each inventory item and routes to the first parser
whose ``can_parse`` returns ``True``.

Ordering rule
-------------
*  Most-specific parsers first.
*  ``FallbackUnsupportedParser`` **must** be last — it accepts everything.

Phase 2 registry order
----------------------
``html_format_parser`` and ``spreadsheet_format_parser`` are real
implementations (they replace the Phase 1 placeholders that emitted
``excluded_pre_parse``).  ``pdf_scanned_parser`` is placed **before**
``native_pdf_parser`` so that it can intercept image-only PDFs before the
native parser marks them failed.

Claim logic:
- ``pdf_scanned_parser.can_parse`` opens the PDF and checks whether pypdf can
  extract any text.  If zero text on all pages AND no decompression errors, it
  returns True (image-only PDF → OCR path).  Otherwise False (falls through to
  ``native_pdf_parser``).
- ``native_pdf_parser.can_parse`` claims all remaining ``.pdf`` files — the
  scanned parser already filtered out image-only ones.  Mixed PDFs (native
  text + embedded scanned pages) are handled inside the native parser
  (per-page OCR, ``document_kind=mixed_pdf``).

This design keeps the claim logic explicit and co-located with each parser,
avoids sentinel values on the ParseResult contract, and pays only a cheap
pypdf-open cost for the native fast path.

Phase 3+ extension
------------------
To register a new parser:

1.  Create ``src/finecorpus/pipeline/assess/parsers/<name>.py`` and implement
    the ``FormatParser`` protocol (see ``base.py``).
2.  Import the module-level singleton here.
3.  Insert it into ``REGISTRY`` at the appropriate position (before more-general
    parsers that might also match).
"""

from finecorpus.pipeline.assess.parsers.base import FormatParser, ParserContext
from finecorpus.pipeline.assess.parsers.html import html_format_parser
from finecorpus.pipeline.assess.parsers.pdf_native import native_pdf_parser
from finecorpus.pipeline.assess.parsers.pdf_scanned import pdf_scanned_parser
from finecorpus.pipeline.assess.parsers.spreadsheet import spreadsheet_format_parser
from finecorpus.pipeline.assess.parsers.unsupported import (
    audio_parser,
    cad_parser,
    fallback_parser,
    video_parser,
)

#: Ordered parser registry.  AssessStage routes each item to the first match.
#:
#: Registry order (Phase 2):
#:   1. audio_parser              → .wav, .mp3, etc.   → excluded_pre_parse
#:   2. video_parser              → .mp4, .mov, etc.   → excluded_pre_parse
#:   3. cad_parser                → .dwg, .dxf, etc.   → excluded_pre_parse
#:   4. html_format_parser        → .html, .htm        → parsed (real, Phase 2)
#:   5. spreadsheet_format_parser → .xlsx/.xlsm/.csv   → parsed or triage-excluded
#:   6. pdf_scanned_parser        → .pdf (image-only)  → parsed via OCR
#:   7. native_pdf_parser         → .pdf (native/mixed)→ parsed / partial / failed
#:   8. fallback_parser           → everything else    → excluded_pre_parse
REGISTRY: list[FormatParser] = [
    audio_parser,
    video_parser,
    cad_parser,
    html_format_parser,  # Phase 2: real HTML parser
    spreadsheet_format_parser,  # Phase 2: real spreadsheet parser with triage
    pdf_scanned_parser,  # Phase 2: image-only PDFs (checked before native)
    native_pdf_parser,
    fallback_parser,
]

__all__ = [
    "REGISTRY",
    "FormatParser",
    "ParserContext",
    "html_format_parser",
    "pdf_scanned_parser",
    "spreadsheet_format_parser",
]
