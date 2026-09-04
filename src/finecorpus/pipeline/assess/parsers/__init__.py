"""Parser registry for the Assess stage.

``REGISTRY`` is an **ordered list** of ``FormatParser`` instances.  The Assess
stage iterates this list for each inventory item and routes to the first parser
whose ``can_parse`` returns ``True``.

Ordering rule
-------------
*  Most-specific parsers first.
*  ``FallbackUnsupportedParser`` **must** be last — it accepts everything.

Phase 2 extension
-----------------
To register a new parser (e.g. ``ocr_pdf``, ``html``, ``spreadsheet``):

1.  Create ``src/finecorpus/pipeline/assess/parsers/<name>.py`` and implement
    the ``FormatParser`` protocol (see ``base.py``).
2.  Import the module-level singleton here.
3.  Insert it into ``REGISTRY`` at the appropriate position (before more-general
    parsers that might also match).

Example — adding an OCR parser before the native PDF parser::

    from finecorpus.pipeline.assess.parsers.ocr_pdf import ocr_pdf_parser

    REGISTRY = [
        audio_parser,
        video_parser,
        cad_parser,
        html_parser,
        spreadsheet_parser,
        ocr_pdf_parser,    # ← new: image-only PDFs (checked before native)
        native_pdf_parser, # existing native-text PDF parser
        fallback_parser,   # ← always last
    ]
"""

from finecorpus.pipeline.assess.parsers.base import FormatParser, ParserContext
from finecorpus.pipeline.assess.parsers.pdf_native import native_pdf_parser
from finecorpus.pipeline.assess.parsers.unsupported import (
    audio_parser,
    cad_parser,
    fallback_parser,
    html_parser,
    spreadsheet_parser,
    video_parser,
)

#: Ordered parser registry.  AssessStage routes each item to the first match.
REGISTRY: list[FormatParser] = [
    audio_parser,
    video_parser,
    cad_parser,
    html_parser,
    spreadsheet_parser,
    native_pdf_parser,
    fallback_parser,
]

__all__ = [
    "REGISTRY",
    "FormatParser",
    "ParserContext",
]
