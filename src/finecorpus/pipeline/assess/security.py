"""Parse-time invisible-content detector for PDFs (§14.1).

This module implements the MUST requirements from §14.1:
  - White-on-white text detection (fill color ≈ white immediately before text ops)
  - Zero/tiny-size font detection (Tf < _TINY_FONT_PT threshold)
  - Off-page text positioning (Td/Tm places text outside MediaBox bounds)

Content-stream analysis
-----------------------
PDFs store page rendering instructions in *content streams*.  These streams are
typically FlateDecode-compressed.  We decompress each page's streams and run a
simple token-scan over the PostScript-like operators.

The scanner is deliberately heuristic:
  - It tracks graphics-state fields (fill color, font size) between text-op tokens.
  - Color operators: ``rg`` (RGB) and ``g`` (gray); we treat a component value ≥
    ``_WHITE_THRESHOLD`` as "effectively white" for the purposes of invisibility.
    Values are normalised to [0,1] range as per the PDF spec.
  - Font size operator: ``Tf <name> <size>`` — size below ``_TINY_FONT_PT`` is
    flagged as zero_size_font (the spec calls the category "zero-size fonts"; we
    extend this to any sub-threshold value because size-1 text is practically
    invisible).
  - Text matrix operator: ``Tm <a> <b> <c> <d> <e> <f>`` — translation (e, f) is
    the x/y position in PDF user space.  y < 0 or y > page_height is off-page
    (the A4 MediaBox bottom is y=0).
  - Text move operator: ``Td <tx> <ty>`` — relative move from current text position.
    We track absolute position and check against page bounds.

Limitations (honest, Phase 2 scope)
------------------------------------
  - The scanner does not maintain a full PDF graphics-state stack (gsave/grestore).
    A document that carefully uses graphics-state saves/restores to momentarily
    switch to white and then back will defeat us if the Tf and Td operators are
    separated by a gsave.  Phase 5 calibration may address this.
  - The off-page detector uses the MediaBox from pypdf, which returns the *page*
    MediaBox.  CropBox clipping is not considered.
  - Background color is assumed white unless the page sets an explicit background
    rect with a non-white fill; true contrast detection (white on a colored
    background) is not implemented.
  - Content streams produced by advanced PDF engines may use Form XObjects or
    patterns; those are not walked in Phase 2.

Wire-up
-------
Call ``detect_invisible_content(reader, page_num)`` from the parser **after**
text extraction.  It returns a list of ``InvisibleContentDetection`` objects to
store in ``PageResult.invisible_content`` and corresponding ``Finding`` objects.
Nothing is stripped — per §14.1, content is labelled, not sanitised.
"""

from __future__ import annotations

import re
import zlib
from typing import Any

import pypdf

from finecorpus.contracts.parse_result import (
    Finding,
    FindingSeverity,
    InvisibleContentDetection,
)
from finecorpus.contracts.shared.blocks import (
    InvisibleContentKind,
    LocatorKind,
    SourceLocation,
)

# ---------------------------------------------------------------------------
# Tuneable thresholds
# ---------------------------------------------------------------------------

#: RGB/gray component value above which we consider a color "effectively white".
#: PDF spec uses [0,1] normalised range where 1.0 = full intensity (white for RGB).
_WHITE_THRESHOLD = 0.9

#: Font size (in typographic points) below which text is considered invisible.
#: fpdf2 minimum is 1pt; the adversarial fixture uses 1pt and 2pt sizes.
_TINY_FONT_PT = 2.0

# ---------------------------------------------------------------------------
# Tokeniser
# ---------------------------------------------------------------------------

# Match a single PDF content-stream token: number, name, or operator string.
_TOKEN_RE = re.compile(
    rb"""
    (?P<number>  [-+]?\d+(?:\.\d+)? )      # integer or real
    |(?P<name>   /[A-Za-z0-9_.+\-#]+ )     # /FontName style name
    |(?P<string> \((?:[^\\()]|\\.)*\) )     # literal string (simple)
    |(?P<op>     [A-Za-z_][A-Za-z0-9_]* )  # operator / keyword
    """,
    re.VERBOSE,
)


def _tokenize(stream_bytes: bytes) -> list[bytes]:
    """Return a flat list of tokens from a PDF content stream."""
    return [m.group(0) for m in _TOKEN_RE.finditer(stream_bytes)]


# ---------------------------------------------------------------------------
# Color helpers
# ---------------------------------------------------------------------------


def _is_white_rgb(r: float, g: float, b: float) -> bool:
    """True when an RGB fill color is effectively white."""
    return r >= _WHITE_THRESHOLD and g >= _WHITE_THRESHOLD and b >= _WHITE_THRESHOLD


def _is_white_gray(gray: float) -> bool:
    """True when a gray fill color is effectively white."""
    return gray >= _WHITE_THRESHOLD


def _to_float(token: bytes) -> float:
    """Parse a number token to float; returns 0.0 on failure."""
    try:
        return float(token)
    except ValueError:
        return 0.0


# ---------------------------------------------------------------------------
# Stream decompressor
# ---------------------------------------------------------------------------


def _decompress_stream(raw: bytes) -> bytes:
    """Attempt zlib decompression; return raw bytes on failure."""
    try:
        return zlib.decompress(raw)
    except zlib.error:
        try:
            return zlib.decompress(raw, -15)  # raw deflate, no header
        except zlib.error:
            return raw


def _get_page_streams(page: Any) -> bytes:
    """Concatenate all content streams for a page into a single byte sequence.

    pypdf stores page content as either a single PdfStream or a list (array
    of streams, which the PDF spec says is to be treated as a single stream).
    We decompress each stream and concatenate with a space separator (safe
    because PDF content streams treat whitespace as a token separator).
    """
    content_obj = page.get("/Contents")
    if content_obj is None:
        return b""

    # Resolve indirect references
    try:
        content_obj = content_obj.get_object()
    except Exception:
        return b""

    streams: list[bytes] = []

    def _read_stream(obj: Any) -> bytes:
        try:
            resolved = obj.get_object() if hasattr(obj, "get_object") else obj
            if hasattr(resolved, "get_data"):
                return resolved.get_data()
            # Fallback: decompress the raw stream body ourselves
            if hasattr(resolved, "_raw_stream"):
                return _decompress_stream(resolved._raw_stream)  # type: ignore[attr-defined]
        except Exception:
            pass
        return b""

    if isinstance(content_obj, list):
        for item in content_obj:
            streams.append(_read_stream(item))
    else:
        streams.append(_read_stream(content_obj))

    return b" ".join(s for s in streams if s)


# ---------------------------------------------------------------------------
# MediaBox helper
# ---------------------------------------------------------------------------


def _get_mediabox(page: Any) -> tuple[float, float, float, float]:
    """Return (x0, y0, x1, y1) MediaBox for the page.

    PDF MediaBox is [x0 y0 x1 y1]; for A4 portrait this is [0 0 595.28 841.89].
    Returns (0, 0, 595.28, 841.89) as a fallback if the attribute is missing.
    """
    try:
        mb = page.mediabox
        return float(mb.left), float(mb.bottom), float(mb.right), float(mb.top)
    except Exception:
        return 0.0, 0.0, 595.28, 841.89


# ---------------------------------------------------------------------------
# Core scanner
# ---------------------------------------------------------------------------


def _scan_content_stream(
    tokens: list[bytes],
    page_num: int,
    mediabox: tuple[float, float, float, float],
) -> list[InvisibleContentDetection]:
    """Scan tokenised content stream for invisible-content patterns.

    Returns a list of ``InvisibleContentDetection`` instances.  Detected text
    is retained (labelled not sanitised — §14.1).

    Graphics-state tracked (partial — see module docstring for limitations):
      - fill_is_white: precomputed bool derived from fill color operators
      - font_size: current font size in points
      - cur_x, cur_y: current text position in user space
      - in_text: True when between BT / ET markers
    """
    detections: list[InvisibleContentDetection] = []

    # Graphics state
    fill_is_white = False
    font_size = 12.0  # PDF default
    cur_x, cur_y = 0.0, 0.0
    in_text = False

    mb_x0, mb_y0, mb_x1, mb_y1 = mediabox

    def _page_loc(text: str | None = None) -> SourceLocation:
        return SourceLocation(
            locator_kind=LocatorKind.page,
            page_start=page_num,
            page_end=page_num,
            bbox=[cur_x, cur_y, cur_x, cur_y] if in_text else None,
            coordinate_note="content-stream analysis; approximate position" if in_text else None,
        )

    n = len(tokens)
    i = 0
    while i < n:
        tok = tokens[i]

        # --- Color operators ---
        # ``r g b rg`` — non-stroking RGB color
        if tok == b"rg" and i >= 3:
            try:
                r = _to_float(tokens[i - 3])
                g = _to_float(tokens[i - 2])
                b = _to_float(tokens[i - 1])
                fill_is_white = _is_white_rgb(r, g, b)
            except (IndexError, ValueError):
                pass

        # ``gray g`` — non-stroking gray color
        elif tok == b"g" and i >= 1:
            try:
                gv = _to_float(tokens[i - 1])
                fill_is_white = _is_white_gray(gv)
            except (IndexError, ValueError):
                pass

        # --- Font size operator: /FontName size Tf ---
        elif tok == b"Tf" and i >= 2:
            try:
                size = _to_float(tokens[i - 1])
                font_size = size
            except (IndexError, ValueError):
                pass

        # --- Text block markers ---
        elif tok == b"BT":
            in_text = True
            # PDF spec: BT initialises the text state with the identity matrix.
            # The text position starts at (0,0) relative to the current CTM.
            # We reset cur_x, cur_y so that subsequent Td/Tm operators produce
            # correct absolute coordinates within this text block.
            cur_x, cur_y = 0.0, 0.0

        elif tok == b"ET":
            in_text = False

        # --- Text matrix: a b c d e f Tm  (e=x, f=y) ---
        elif tok == b"Tm" and i >= 6:
            try:
                cur_x = _to_float(tokens[i - 2])  # e
                cur_y = _to_float(tokens[i - 1])  # f
            except (IndexError, ValueError):
                pass

        # --- Text move: tx ty Td  (relative to current line origin) ---
        elif tok == b"Td" and i >= 2:
            try:
                cur_x += _to_float(tokens[i - 2])
                cur_y += _to_float(tokens[i - 1])
            except (IndexError, ValueError):
                pass

        # --- TD operator: same as Td but also sets leading ---
        elif tok == b"TD" and i >= 2:
            try:
                cur_x += _to_float(tokens[i - 2])
                cur_y += _to_float(tokens[i - 1])
            except (IndexError, ValueError):
                pass

        # --- Text show operators: Tj, TJ, ' (next-line-show), " ---
        elif tok in (b"Tj", b"TJ", b"'", b'"') and in_text:
            # Retrieve the string argument just before the operator.
            text_arg: str | None = None
            if i >= 1:
                raw_tok = tokens[i - 1]
                if raw_tok.startswith(b"(") and raw_tok.endswith(b")"):
                    try:
                        text_arg = raw_tok[1:-1].decode("latin-1", errors="replace")
                    except Exception:
                        text_arg = None

            # --- Check 1: white-on-white ---
            if fill_is_white:
                detections.append(
                    InvisibleContentDetection(
                        kind=InvisibleContentKind.white_on_white,
                        location=_page_loc(text_arg),
                        text=text_arg,
                    )
                )

            # --- Check 2: tiny font ---
            if font_size < _TINY_FONT_PT:
                detections.append(
                    InvisibleContentDetection(
                        kind=InvisibleContentKind.zero_size_font,
                        location=_page_loc(text_arg),
                        text=text_arg,
                    )
                )

            # --- Check 3: off-page positioning ---
            # y < mb_y0 means below the MediaBox bottom (y=0 for A4)
            # y > mb_y1 means above the MediaBox top
            # x < mb_x0 or x > mb_x1 means off left/right edge
            if cur_y < mb_y0 or cur_y > mb_y1 or cur_x < mb_x0 or cur_x > mb_x1:
                detections.append(
                    InvisibleContentDetection(
                        kind=InvisibleContentKind.off_page,
                        location=SourceLocation(
                            locator_kind=LocatorKind.page,
                            page_start=page_num,
                            page_end=page_num,
                            bbox=[cur_x, cur_y, cur_x, cur_y],
                            coordinate_note=(
                                f"text at y={cur_y:.2f} is outside MediaBox "
                                f"[{mb_x0},{mb_y0},{mb_x1},{mb_y1}]"
                            ),
                        ),
                        text=text_arg,
                    )
                )

        i += 1

    return detections


# ---------------------------------------------------------------------------
# Public API — called by parsers
# ---------------------------------------------------------------------------


def detect_invisible_content(
    reader: pypdf.PdfReader,
    page_num: int,
) -> tuple[list[InvisibleContentDetection], list[Finding]]:
    """Detect invisible-content patterns in a PDF page's content streams.

    Must be called **after** text extraction for the page.  This function reads
    the raw content streams from the pypdf reader object directly — it does NOT
    extract text again (no double-parse overhead).

    Args:
        reader: An open ``pypdf.PdfReader`` for the document.
        page_num: 1-based page number to analyse.

    Returns:
        A tuple ``(detections, findings)`` where:
        - ``detections``: list of ``InvisibleContentDetection`` objects to store
          in ``PageResult.invisible_content``.  May be empty.
        - ``findings``: list of ``Finding`` objects summarising detections at
          the document level (for the quality report).

    Nothing is stripped or rewritten — §14.1 "labelled, not sanitised".
    """
    page_idx = page_num - 1
    if page_idx < 0 or page_idx >= len(reader.pages):
        return [], []

    page = reader.pages[page_idx]
    mediabox = _get_mediabox(page)

    try:
        stream_bytes = _get_page_streams(page)
    except Exception:
        return [], []

    if not stream_bytes:
        return [], []

    tokens = _tokenize(stream_bytes)
    detections = _scan_content_stream(tokens, page_num, mediabox)

    # Deduplicate: one detection per (kind) per page to avoid flooding the output
    # with every Tj operator when a whole block is white-on-white.
    # We keep the first occurrence of each kind (which has the earliest text_arg).
    seen_kinds: set[InvisibleContentKind] = set()
    unique_detections: list[InvisibleContentDetection] = []
    for det in detections:
        if det.kind not in seen_kinds:
            seen_kinds.add(det.kind)
            unique_detections.append(det)

    findings: list[Finding] = []
    for det in unique_detections:
        page_loc = SourceLocation(
            locator_kind=LocatorKind.page,
            page_start=page_num,
            page_end=page_num,
        )
        findings.append(
            Finding(
                code="invisible_content_detected",
                severity=FindingSeverity.warning,
                location=page_loc,
                message=(
                    f"Page {page_num}: {det.kind.value} invisible content detected "
                    f"(§14.1). Hidden text retained per labelled-not-sanitised policy."
                ),
            )
        )

    return unique_detections, findings
