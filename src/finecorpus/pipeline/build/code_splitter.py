"""M-099 — Syntax-aware code splitter.

Splits code segments at function/class/top-level-declaration boundaries using
heuristic line-based detection.  No tree-sitter or external parse library is
used — the splitter depends only on the Python standard library.

Supported language hints
------------------------
- ``python`` / ``py``: Detects ``def``, ``class``, ``async def`` at column 0.
- ``javascript`` / ``typescript`` / ``js`` / ``ts``:
    Detects ``function``, ``class``, ``export function``, ``export class``,
    ``export default function``, ``const <name> = (...) =>``.
- ``go``: Detects ``func`` at column 0.
- Brace-depth languages (C, C++, Java, Rust, …): Detects lines that return
    to brace-depth 0 after having been inside a block.  Boundaries are placed
    at function/class-like preamble lines that start a depth-0 open block.
- ``unknown`` / ``""`` / any unrecognised hint: Falls back entirely to the
    recursive-char splitter (``split_text``).

Tiling invariant (§12)
-----------------------
The returned list of ChunkSpan objects tiles the input exactly:
  - spans[0].char_start == 0
  - spans[-1].char_end == len(text)
  - spans[i].char_end == spans[i+1].char_start  (no gaps, no overlaps)
  - "".join(span.text for span in spans) == text  (reassembly-exact)

Fallback rules
--------------
1. If no top-level boundaries are detected for the language: fall back to
   ``split_text`` (recursive-char).
2. If a single detected unit exceeds ``max_tokens``: fall back to ``split_text``
   applied WITHIN that unit, keeping other units intact.
3. Empty input: returns one zero-length ChunkSpan (consistent with chunker.py).

Known limitations (heuristic trade-offs; recorded decisions)
------------------------------------------------------------
a. **String-literal false splits** — a ``def`` or ``class`` keyword at column 0
   inside a multiline string literal (e.g. docstrings, raw string blocks) is
   indistinguishable from a real top-level definition using line-based heuristics.
   This causes false boundary splits in such files.  Correct detection requires a
   full parse tree (e.g. tree-sitter); the heuristic approach is the accepted
   Phase 3 trade-off.  See ``test_code_splitter.py::TestKnownLimitations`` for a
   test that captures this behaviour and marks it as a documented limitation.
b. **Column-0 decorators only** — the decorator back-extension in
   ``_python_boundaries`` recognises only column-0 ``@`` lines.  Indented
   decorators (rare in well-formed code) are not extended and remain in the
   preceding span.

See docs/pipeline/build.md for the full reference.
"""

from __future__ import annotations

import dataclasses
import re

from finecorpus.pipeline.build.chunker import ChunkSpan, _count_tokens, split_text

# ---------------------------------------------------------------------------
# Language boundary patterns
# ---------------------------------------------------------------------------

# Python: top-level defs/classes (column 0)
_PY_BOUNDARY_RE = re.compile(r"^(def |class |async def )")

# JavaScript / TypeScript top-level patterns
_JS_BOUNDARY_RE = re.compile(
    r"^("
    r"export\s+default\s+(async\s+)?function\b"
    r"|export\s+(async\s+)?function\b"
    r"|export\s+class\b"
    r"|export\s+const\s+\w+\s*="
    r"|(async\s+)?function\b"
    r"|class\b"
    r"|const\s+\w+\s*=\s*(async\s+)?\("
    r"|const\s+\w+\s*=\s*(async\s+)?function\b"
    r")"
)

# Go: top-level func declarations (column 0)
_GO_BOUNDARY_RE = re.compile(r"^func\b")

# Brace-depth languages: lines that look like a new top-level definition.
# We consider a line a boundary candidate if it matches these patterns AND
# the current brace depth returns to 0.
_BRACE_LANG_BOUNDARY_RE = re.compile(
    r"^("
    r"(public|private|protected|static|final|override|virtual|inline|extern)?\s*"
    r"[\w:<>*&]+\s+\w+\s*\("  # return-type funcname(  — C/C++/Java/Rust-ish
    r"|(pub\s+)?(async\s+)?fn\s+\w+"  # Rust fn
    r"|(pub|private|protected)?\s*(static\s+)?(void|int|bool|string|auto|var)\s+\w+"
    r")"
)

# Languages we handle with specific patterns
_PYTHON_LANGS = {"python", "py"}
_JS_LANGS = {"javascript", "typescript", "js", "ts", "jsx", "tsx"}
_GO_LANGS = {"go"}
_BRACE_LANGS = {"c", "cpp", "c++", "java", "rust", "rs", "csharp", "cs", "swift", "kotlin"}


# ---------------------------------------------------------------------------
# Boundary detection helpers
# ---------------------------------------------------------------------------


def _python_boundaries(lines: list[str]) -> list[int]:
    """Return 0-based line indices of top-level Python definitions.

    When a ``def``/``class``/``async def`` boundary is detected, the boundary
    index is extended backward over any immediately-preceding column-0 decorator
    lines (``@...``).  Only column-0 decorators are recognised; indented decorators
    are left as part of the preceding unit.  This ensures decorators travel with
    the definition they annotate rather than being stranded in the previous span.
    """
    boundaries: list[int] = []
    for i, line in enumerate(lines):
        if _PY_BOUNDARY_RE.match(line):
            # Walk backward over column-0 @decorator lines immediately above.
            start = i
            j = i - 1
            while j >= 0 and lines[j].startswith("@"):
                start = j
                j -= 1
            boundaries.append(start)
    return boundaries


def _js_boundaries(lines: list[str]) -> list[int]:
    """Return 0-based line indices of top-level JS/TS declarations."""
    boundaries: list[int] = []
    for i, line in enumerate(lines):
        if not line:
            # Empty lines cannot be top-level declarations.
            continue
        if _JS_BOUNDARY_RE.match(line.lstrip()):
            # Only accept column-0 (top-level) declarations.
            if not line[0].isspace():
                boundaries.append(i)
    return boundaries


def _go_boundaries(lines: list[str]) -> list[int]:
    """Return 0-based line indices of top-level Go func declarations."""
    boundaries: list[int] = []
    for i, line in enumerate(lines):
        if _GO_BOUNDARY_RE.match(line):
            boundaries.append(i)
    return boundaries


def _brace_depth_boundaries(lines: list[str]) -> list[int]:
    """Return 0-based line indices where a new top-level brace block begins."""
    boundaries: list[int] = []
    depth = 0
    for i, line in enumerate(lines):
        opens = line.count("{")
        closes = line.count("}")
        if depth == 0 and opens > closes and _BRACE_LANG_BOUNDARY_RE.match(line.lstrip()):
            # This line opens a new top-level block
            boundaries.append(i)
        depth = max(0, depth + opens - closes)
    return boundaries


# ---------------------------------------------------------------------------
# Core split logic
# ---------------------------------------------------------------------------


def _build_spans_from_boundaries(
    text: str,
    lines: list[str],
    boundary_line_indices: list[int],
    max_tokens: int,
    start_idx: int = 0,
) -> list[ChunkSpan]:
    """Build ChunkSpans from detected boundary line indices.

    Each consecutive pair of boundaries defines one "unit."  Units that exceed
    ``max_tokens`` are recursively split with ``split_text``.  The tiling invariant
    is maintained throughout.

    Args:
        text: The full text being split (only the substring from ``start_idx``).
        lines: Lines of ``text`` (pre-split for efficiency).
        boundary_line_indices: 0-based line indices where new units start.
        max_tokens: Target maximum tokens per span.
        start_idx: Character offset of ``text[0]`` within the original segment text.
            Used to produce correct ``char_start``/``char_end`` values.

    Returns:
        List of ChunkSpan objects; always non-empty; tiles ``text`` exactly.
    """
    # Convert line indices to char offsets within ``text``
    line_starts: list[int] = []
    pos = 0
    for line in lines:
        line_starts.append(pos)
        pos += len(line) + 1  # +1 for the '\n' stripped by split('\n')

    # Compute char boundaries for each detected unit
    # boundary_line_indices are lines where a new unit starts.
    # Unit i covers [boundary_line_indices[i], boundary_line_indices[i+1]).
    if not boundary_line_indices:
        # No boundaries — full fallback with overlap=0 for tiling invariant.
        raw = split_text(text, max_tokens, 0)
        return _raw_to_spans(text, raw, start_idx)

    # Build unit char ranges
    unit_ranges: list[tuple[int, int]] = []
    for k, line_idx in enumerate(boundary_line_indices):
        char_start_unit = line_starts[line_idx]
        if k + 1 < len(boundary_line_indices):
            next_line_idx = boundary_line_indices[k + 1]
            char_end_unit = line_starts[next_line_idx]
        else:
            char_end_unit = len(text)
        unit_ranges.append((char_start_unit, char_end_unit))

    # There may be a leading region before the first boundary (e.g. module docstring)
    if boundary_line_indices[0] > 0:
        # Prepend a leading "preamble" unit
        unit_ranges.insert(0, (0, line_starts[boundary_line_indices[0]]))

    spans: list[ChunkSpan] = []
    chunk_idx = 0
    for unit_char_start, unit_char_end in unit_ranges:
        unit_text = text[unit_char_start:unit_char_end]
        if not unit_text:
            continue
        unit_tokens = _count_tokens(unit_text)
        if unit_tokens <= max_tokens:
            spans.append(
                ChunkSpan(
                    text=unit_text,
                    chunk_index=chunk_idx,
                    char_start=start_idx + unit_char_start,
                    char_end=start_idx + unit_char_end,
                    token_count=unit_tokens,
                )
            )
            chunk_idx += 1
        else:
            # Unit exceeds max_tokens — fall back to recursive-char within this unit.
            # Use overlap=0 to preserve strict contiguous tiling within the unit.
            sub_raw = split_text(unit_text, max_tokens, 0)
            for sub_start, sub_end in sub_raw:
                sub_text = unit_text[sub_start:sub_end]
                if not sub_text:
                    continue
                spans.append(
                    ChunkSpan(
                        text=sub_text,
                        chunk_index=chunk_idx,
                        char_start=start_idx + unit_char_start + sub_start,
                        char_end=start_idx + unit_char_start + sub_end,
                        token_count=_count_tokens(sub_text),
                    )
                )
                chunk_idx += 1

    if not spans:
        # Last-resort: nothing was produced (e.g. all units were empty).
        # overlap=0 preserves tiling invariant.
        raw = split_text(text, max_tokens, 0)
        return _raw_to_spans(text, raw, start_idx)

    return spans


def _raw_to_spans(text: str, raw: list[tuple[int, int]], start_idx: int) -> list[ChunkSpan]:
    """Convert raw (char_start, char_end) pairs from ``split_text`` into ChunkSpan objects."""
    spans: list[ChunkSpan] = []
    for idx, (cs, ce) in enumerate(raw):
        chunk_text = text[cs:ce]
        spans.append(
            ChunkSpan(
                text=chunk_text,
                chunk_index=idx,
                char_start=start_idx + cs,
                char_end=start_idx + ce,
                token_count=_count_tokens(chunk_text),
            )
        )
    return spans


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def split_code(
    text: str,
    max_tokens: int,
    language_hint: str,
) -> list[ChunkSpan]:
    """Split code text into ChunkSpan objects at function/class/top-level boundaries.

    Heuristic line-based detection: no tree-sitter or external parse library.
    Falls back to ``split_text`` (recursive-char) when:
    - No boundaries are detected, OR
    - The language is unrecognised.
    Oversized single units fall back to ``split_text`` within that unit.

    Tiling invariant (§12):
    - ``spans[0].char_start == 0``
    - ``spans[-1].char_end == len(text)``
    - Consecutive spans are contiguous (no gaps, no overlaps).
    - Reassembly: ``"".join(s.text for s in spans) == text``.

    Args:
        text: The code segment's text.
        max_tokens: Target maximum tokens per span (whitespace-word proxy).
        language_hint: Language identifier string (case-insensitive).  Examples:
            ``"python"``, ``"javascript"``, ``"go"``, ``"c"``, ``"unknown"``.

    Returns:
        Non-empty list of ChunkSpan objects with dense 0-based ``chunk_index``
        values.  Always at least one element.
    """
    if not text:
        return [ChunkSpan(text=text, chunk_index=0, char_start=0, char_end=0, token_count=0)]

    lang = language_hint.lower().strip()
    lines = text.split("\n")

    # --- Detect boundaries ---
    boundaries: list[int] = []
    if lang in _PYTHON_LANGS:
        boundaries = _python_boundaries(lines)
    elif lang in _JS_LANGS:
        # For JS/TS, re-run with stripped lines to catch indented top-level
        # (some formatters indent; we only care about column-0 ones)
        boundaries = _js_boundaries(lines)
    elif lang in _GO_LANGS:
        boundaries = _go_boundaries(lines)
    elif lang in _BRACE_LANGS:
        boundaries = _brace_depth_boundaries(lines)
    # else: unknown lang → no boundaries → full fallback below

    if not boundaries:
        # Full fallback: recursive-char with overlap=0 for tiling invariant.
        raw = split_text(text, max_tokens, 0)
        return _raw_to_spans(text, raw, 0)

    spans = _build_spans_from_boundaries(text, lines, boundaries, max_tokens, start_idx=0)

    # Re-index chunk_index to be dense 0-based (it already is from _build_spans_from_boundaries,
    # but make explicit).  ChunkSpan is frozen; use dataclasses.replace to produce new instances.
    spans = [dataclasses.replace(span, chunk_index=new_idx) for new_idx, span in enumerate(spans)]

    # --- Verify tiling invariant ---
    # (This is a development-time assertion; it will be caught by tests too.)
    _assert_tiles(text, spans)

    return spans


def _assert_tiles(text: str, spans: list[ChunkSpan]) -> None:
    """Assert that spans tile ``text`` exactly (development-time invariant check)."""
    if not spans:
        return
    assert spans[0].char_start == 0, f"First span does not start at 0: {spans[0].char_start}"
    assert spans[-1].char_end == len(text), (
        f"Last span does not end at len(text)={len(text)}: {spans[-1].char_end}"
    )
    for i in range(len(spans) - 1):
        assert spans[i].char_end == spans[i + 1].char_start, (
            f"Gap/overlap between spans[{i}] and spans[{i + 1}]: "
            f"{spans[i].char_end} != {spans[i + 1].char_start}"
        )
    reassembled = "".join(s.text for s in spans)
    assert reassembled == text, "Reassembly does not match input text"


__all__ = [
    "split_code",
]
