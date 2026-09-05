"""Tier 1 — Structure normalization transforms (§7.2).

Implements the four Tier 1 operations named in the spec and the Tier1Operation enum:
  - ``whitespace_repair``: Normalise line endings, collapse runs of whitespace,
    strip trailing whitespace, ensure text ends with no trailing newline clutter.
  - ``ocr_cleanup``: Remove null bytes, soft-hyphens, and other OCR artefacts.
    Collapses repeated punctuation that commonly appears in low-quality scans.
  - ``table_to_markdown``: No-op in this module — table conversion is a parse-layer
    concern; the text reaching Build is already in its representation.  The record is
    emitted only if the caller explicitly requests the operation (allows callers to
    annotate provenance truthfully when conversion happened upstream).
  - ``header_inference``: Promote leading short lines that look like headings by
    adding a ``#`` prefix when the text has no existing Markdown headings.

All functions are deterministic, pure (no side-effects), and produce accurate
TransformationRecord entries reflecting whether text actually changed.

Tier 1 contract (§7.2)
-----------------------
- ``changed_text=True``  when any bytes of the canonical source were altered.
- ``changed_text=False`` when the operation was a no-op.
- ``applied_by=deterministic`` for all Tier 1 ops (no model involved).

See docs/pipeline/build.md for the full pipeline reference.
"""

from __future__ import annotations

import re
import unicodedata

from finecorpus.contracts.ingestion_config import Tier1Operation
from finecorpus.contracts.shared.blocks import AppliedBy, TransformationRecord, TransformationTier

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_SOFT_HYPHEN = "­"
_NULL_BYTE = "\x00"
# Control chars except \t, \n, \r
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
# Whitespace normalisation: collapse each run of spaces/tabs within a line
_INLINE_WS_RE = re.compile(r"[ \t]{2,}")
# Repeated punctuation (OCR artefact): collapses runs of 4+ identical chars to 1.
# Three-char runs (e.g. ``...``, ``!!!``) are intentionally preserved — they carry
# semantic meaning (ellipsis, emphasis).  Four or more are treated as OCR noise.
_REPEATED_PUNCT_RE = re.compile(r"([.,:;!?])\1{3,}")


def _make_record(
    operation: Tier1Operation,
    changed: bool,
    note: str | None = None,
) -> TransformationRecord:
    return TransformationRecord(
        tier=TransformationTier.tier_1,
        operation=operation.value,
        applied_by=AppliedBy.deterministic,
        model_ref=None,
        changed_text=changed,
        note=note,
    )


# ---------------------------------------------------------------------------
# Individual op implementations
# ---------------------------------------------------------------------------


def _apply_whitespace_repair(text: str) -> tuple[str, bool]:
    """Normalise whitespace without removing meaningful content.

    Steps:
    1. Normalise line endings: ``\\r\\n`` → ``\\n``, ``\\r`` → ``\\n``.
    2. Collapse runs of spaces/tabs within each line to a single space.
    3. Strip trailing whitespace from each line.
    4. Collapse runs of 3+ blank lines to 2 blank lines (paragraph double-space preserved).
    5. Strip leading/trailing blank lines from the whole text.
    """
    # Step 1: normalise line endings
    out = text.replace("\r\n", "\n").replace("\r", "\n")

    # Step 2+3: per-line normalise + strip
    lines = [_INLINE_WS_RE.sub(" ", line).rstrip() for line in out.split("\n")]

    # Step 4: collapse excessive blank lines (3+ → 2)
    result_lines: list[str] = []
    consecutive_blank = 0
    for line in lines:
        if line == "":
            consecutive_blank += 1
            if consecutive_blank <= 2:
                result_lines.append(line)
        else:
            consecutive_blank = 0
            result_lines.append(line)

    # Step 5: strip leading/trailing blank lines
    while result_lines and result_lines[0] == "":
        result_lines.pop(0)
    while result_lines and result_lines[-1] == "":
        result_lines.pop()

    out = "\n".join(result_lines)
    return out, out != text


def _apply_ocr_cleanup(text: str) -> tuple[str, bool]:
    """Remove OCR artefacts that do not carry semantic meaning.

    Operations (applied in order):
    1. Remove null bytes.
    2. Remove soft hyphens (U+00AD).
    3. Remove other C0 control characters (except \\t, \\n, \\r which are whitespace).
    4. Collapse repeated punctuation runs of 4+ identical chars to 1
       (e.g. ``...`` preserved; ``....`` → ``.``).
    5. Unicode NFC normalisation so decomposed characters become composed form.

    Note: We deliberately do NOT collapse all Unicode to ASCII — that would silently
    destroy multilingual content (§7.6).
    """
    out = text.replace(_NULL_BYTE, "").replace(_SOFT_HYPHEN, "")
    out = _CTRL_RE.sub("", out)
    out = _REPEATED_PUNCT_RE.sub(r"\1", out)
    out = unicodedata.normalize("NFC", out)
    return out, out != text


def _apply_table_to_markdown(text: str) -> tuple[str, bool]:
    """Record that table-to-markdown conversion has been applied (no-op transform).

    Table conversion happens at the parse layer; by the time text reaches Build,
    tables are already rendered.  This function exists so callers can emit an honest
    TransformationRecord when that upstream conversion occurred.  The text is returned
    unchanged; ``changed_text=False`` reflects that no bytes were altered HERE.

    Wave-2 wiring note: ``build/stage.py::_build_table_to_markdown_record`` currently
    emits a ``table_to_markdown`` TransformationRecord independently of this path.
    Wave-2 MUST remove that independent emission and drive provenance exclusively through
    ``apply_tier1``'s records to avoid double-recording.  The orchestrator is tracking
    this as a Wave-2 brief item; this comment is the in-code breadcrumb.
    """
    # No-op: conversion already happened upstream (parse layer).
    return text, False


def _apply_header_inference(text: str) -> tuple[str, bool]:
    """Promote bare heading candidates to Markdown headings.

    Heuristic: if the text has no existing Markdown headings (lines starting with ``#``),
    examine the first non-empty line.  If it is:
    - Shorter than 80 characters, AND
    - Does not end with a sentence-terminating character (``.``, ``?``, ``!``), AND
    - Does not start with a bullet (``-``, ``*``, ``1.``), AND
    - The second non-empty line (if present) is meaningfully longer than the first line,

    …prepend ``# `` to turn it into an H1.

    This is intentionally conservative — we only infer headings for the clear case.
    Ambiguous cases produce ``changed_text=False`` (no-op).
    """
    lines = text.split("\n")

    # Check for existing Markdown headings
    if any(line.startswith("#") for line in lines):
        return text, False

    # Find first two non-empty lines
    non_empty = [line for line in lines if line.strip()]
    if not non_empty:
        return text, False

    first = non_empty[0]

    # Must be short (not a prose sentence)
    if len(first) >= 80:
        return text, False

    # Must not end with sentence terminator
    if first.rstrip().endswith((".", "?", "!")):
        return text, False

    # Must not start with a bullet/list marker
    stripped = first.lstrip()
    if stripped and (
        stripped[0] in ("-", "*")
        or (stripped[:2].rstrip() and stripped[0].isdigit() and "." in stripped[:4])
    ):
        return text, False

    # Must have a longer body line after it to indicate it's truly a heading
    if len(non_empty) < 2:
        return text, False

    second = non_empty[1]
    if len(second) <= len(first):
        return text, False

    # Promote: find the first occurrence of first line (by content) and prefix with #
    new_lines = []
    promoted = False
    for line in lines:
        if not promoted and line == first:
            new_lines.append(f"# {line}")
            promoted = True
        else:
            new_lines.append(line)

    out = "\n".join(new_lines)
    return out, out != text


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def apply_tier1(
    text: str,
    ops: list[Tier1Operation],
) -> tuple[str, list[TransformationRecord]]:
    """Apply an ordered list of Tier 1 normalisation operations to ``text``.

    Operations are applied in the order given.  Each operation produces at most one
    TransformationRecord.  Records with ``changed_text=False`` are included only when
    the operation was explicitly requested — they document what was tried, which is
    useful for provenance even on no-ops.

    Spec contract (§7.2):
    - Deterministic: same ``(text, ops)`` always produces the same output.
    - Pure: no side-effects; does not write to disk or call any external service.
    - Honest: ``changed_text`` reflects whether THIS operation actually altered bytes.
    - Records list is in application order.
    - No records are emitted for operations not present in ``ops``.

    Args:
        text: The segment text to normalise.
        ops: Ordered list of Tier1Operation values to apply (subset of all known ops).
            Pass an empty list to apply nothing; returns ``(text, [])`` deterministically.

    Returns:
        ``(canonical_text, records)`` where ``canonical_text`` is the normalised text
        and ``records`` is the list of TransformationRecord objects in application order.
    """
    records: list[TransformationRecord] = []
    current = text

    for op in ops:
        if op == Tier1Operation.whitespace_repair:
            new_text, changed = _apply_whitespace_repair(current)
            record = _make_record(op, changed)
        elif op == Tier1Operation.ocr_cleanup:
            new_text, changed = _apply_ocr_cleanup(current)
            record = _make_record(op, changed)
        elif op == Tier1Operation.table_to_markdown:
            new_text, changed = _apply_table_to_markdown(current)
            record = _make_record(
                op,
                changed,
                note="table_to_markdown applied at parse layer; no bytes altered at Build",
            )
        elif op == Tier1Operation.header_inference:
            new_text, changed = _apply_header_inference(current)
            record = _make_record(op, changed)
        else:
            # Unknown operation — pass through and emit a no-op record.
            new_text = current
            record = _make_record(op, False, note=f"unknown operation {op!r}; no bytes altered")

        current = new_text
        records.append(record)

    return current, records


__all__ = [
    "apply_tier1",
]
