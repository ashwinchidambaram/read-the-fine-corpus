"""Tier 3 diff preview (§7.2 "Diff preview shown before commit", §7.4).

Produces a human-readable unified diff between a chunk's canonical Tier-1
``original`` text and its Tier-3 ``rewritten`` text.  The preview is surfaced
whenever ``Tier3Settings.diff_preview_required`` is set — in the build/preview
path (dry-run inline chunks) and in explain output — so a KB owner can see
exactly what a rewrite changed before committing (§7.2 MUST).

Dependency-light by design: stdlib ``difflib`` only.  No third-party diff
library, no rendering framework — the output is plain text that any surface
(CLI, web preview, explain block) can display verbatim.
"""

from __future__ import annotations

import difflib

_ORIGINAL_LABEL = "original (canonical Tier-1)"
_REWRITTEN_LABEL = "rewritten (Tier-3)"


def render_tier3_diff(
    original: str,
    rewritten: str,
    *,
    context_lines: int = 3,
) -> str:
    """Return a unified-diff string between ``original`` and ``rewritten``.

    The diff is line-oriented (``difflib.unified_diff``).  When the two texts
    are identical the result is a single explanatory line rather than an empty
    string, so a caller can always show *something* meaningful.

    Parameters
    ----------
    original:
        The canonical Tier-1 text (what the chunk was rewritten from).
    rewritten:
        The Tier-3 rewritten text (what the chunk now serves).
    context_lines:
        Number of unchanged context lines around each change (``n`` in
        ``difflib.unified_diff``).

    Returns
    -------
    str
        A unified diff.  Terminates without a trailing newline.  If the two
        texts are byte-identical, returns a fixed "(no changes)" marker line.
    """
    if original == rewritten:
        return "(no changes: rewritten text is byte-identical to the original)"

    # keepends=True keeps line terminators so difflib renders them correctly;
    # splitlines() (no keepends) then rejoin gives clean per-line records.
    original_lines = original.splitlines()
    rewritten_lines = rewritten.splitlines()

    diff_lines = difflib.unified_diff(
        original_lines,
        rewritten_lines,
        fromfile=_ORIGINAL_LABEL,
        tofile=_REWRITTEN_LABEL,
        lineterm="",
        n=context_lines,
    )
    return "\n".join(diff_lines)


__all__ = [
    "render_tier3_diff",
]
