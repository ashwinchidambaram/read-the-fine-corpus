"""Recursive character chunker — Phase 1 naive baseline (§9.3) + code dispatch (M-099).

Implements the **fixed reference configuration** from spec §9.3:
  - Recursive character splitting at 512 tokens with 50-token overlap.
  - A chunk NEVER spans segments — the segment is the routing unit (§7.1).
  - Splitting is deterministic (no randomness, no wall-clock).

Phase 3 addition: ``chunk_segment_dispatch`` dispatches on ``ChunkingStrategy`` and
routes ``code_syntax`` segments to the syntax-aware code splitter (M-099).

Token counting
--------------
tiktoken is NOT a project dependency (not in pyproject.toml). This module uses
a **whitespace-word proxy**: token count ≈ len(text.split()). This is an honest
approximation, not a semantic tokenizer. It is documented here and in build.md.
The approximation is conservative for English prose (GPT-4 BPE typically
produces 1.2–1.4 tokens/word; whitespace-word count underestimates tokens,
meaning chunks may be slightly larger than the target in BPE terms). This is
the accepted Phase 1 trade-off; Phase 3 may introduce a real tokenizer if the
evaluation sweep shows material impact.

If tiktoken is detected at runtime (optional import), it WILL NOT be used here
to keep the implementation simple and deterministic without network calls. The
module is tiktoken-free by design.

Invariants
----------
- ``chunk_text_is_substring`` — every chunk's text is an exact contiguous
  substring of the segment text it came from (spec §12, T-04 seed).
- ``no_span_across_segments`` — chunker is called per-segment; the output
  list for a segment contains only substrings of that segment's text.
- ``dense_chunk_index`` — chunk_index values are 0, 1, 2, … (no gaps) within
  each segment.
- ``single_chunk_for_tiny_segments`` — segments shorter than max_tokens
  produce exactly one chunk (the full segment text).
- ``deterministic`` — same (text, max_tokens, overlap_tokens) always produces
  the same splits.

See docs/pipeline/build.md for full parameter and semantics reference.
"""

from __future__ import annotations

from dataclasses import dataclass

from finecorpus.contracts.ingestion_config import ChunkingStrategy

# ---------------------------------------------------------------------------
# Token-count proxy
# ---------------------------------------------------------------------------

# Documented approximation: whitespace word count.
# 1 token ≈ 1 whitespace-delimited word (underestimates BPE tokens by ~20-40%
# for English prose; conservative in the sense that chunks may be slightly
# larger than the target in BPE token count).
_WORDS_PER_TOKEN = 1


def _count_tokens(text: str) -> int:
    """Return the whitespace-word-count approximation of token count.

    Honest approximation: documented in this module's docstring and in
    docs/pipeline/build.md. Not a BPE tokenizer. The proxy produces
    consistent, reproducible counts across runs.

    Args:
        text: The text to measure.

    Returns:
        Non-negative integer word count (0 for empty/whitespace-only text).
    """
    return len(text.split())


# ---------------------------------------------------------------------------
# Chunk data record
# ---------------------------------------------------------------------------


@dataclass
class ChunkSpan:
    """A single chunk produced from one segment.

    Attributes:
        text: The exact text of this chunk (a substring of the segment text).
        chunk_index: 0-based position within this segment's chunk list.
        char_start: 0-based character offset into the segment text where this
            chunk begins (inclusive). Used to derive SourceLocation.
        char_end: Exclusive character offset into the segment text where this
            chunk ends.
        token_count: Whitespace-word-count approximation of len(text.split()).
    """

    text: str
    chunk_index: int
    char_start: int
    char_end: int
    token_count: int


# ---------------------------------------------------------------------------
# Splitter
# ---------------------------------------------------------------------------

# Candidate split characters, tried in preference order.
# Higher-priority characters preserve more structure on the split.
_SEPARATORS: list[str] = [
    "\n\n",  # paragraph / block boundary
    "\n",  # line boundary
    ". ",  # sentence boundary (trailing space avoids splitting "U.S.")
    "! ",  # sentence boundary
    "? ",  # sentence boundary
    "; ",  # clause boundary
    ", ",  # clause boundary
    " ",  # word boundary (fallback)
]


def _find_split_point(text: str, max_chars: int) -> int:
    """Find the best split point at or before ``max_chars`` in ``text``.

    Tries each separator in ``_SEPARATORS`` order. Returns the position
    AFTER the separator (so the separator ends up at the end of the left
    chunk, keeping it attached to its preceding content).

    If no separator is found, returns ``max_chars`` exactly (hard truncation
    at the character boundary — no text is silently lost).

    Args:
        text: The full remaining text to split.
        max_chars: The maximum number of characters in the left chunk.

    Returns:
        Split position (0 < pos <= len(text)); 0 is never returned.
    """
    # Search window: look for a separator in the target range.
    # We search from the right so we use as many chars as possible.
    window = text[:max_chars]
    for sep in _SEPARATORS:
        idx = window.rfind(sep)
        if idx != -1:
            # Include the separator in the left chunk (split after it).
            return idx + len(sep)

    # Hard truncation — no separator found. This can happen for dense text
    # (e.g. code with no spaces). The boundary is char-clean (no bytes lost).
    return max_chars


def split_text(
    text: str,
    max_tokens: int,
    overlap_tokens: int,
) -> list[tuple[int, int]]:
    """Split ``text`` into overlapping windows using recursive character splitting.

    Returns a list of (char_start, char_end) pairs representing the raw
    character offsets of each chunk within ``text``.

    The algorithm is a one-pass greedy forward scan:
    1. Start at position 0.
    2. Estimate the character window for ``max_tokens`` tokens using the
       words-per-char density of the remaining text (lazy approximation
       to avoid O(n) tokenization per step).
    3. Find the best split point at or before that window using ``_find_split_point``.
    4. Record the (start, end) span.
    5. Advance the cursor backward by ``overlap_tokens`` worth of characters
       (estimated), so the next chunk overlaps the previous one.
    6. Repeat until the remaining text fits within ``max_tokens``.

    Invariants:
    - Every pair is a valid (char_start, char_end) with char_start < char_end.
    - Every character in ``text`` appears in at least one chunk.
    - Overlap: consecutive chunks share ``overlap_tokens`` worth of text.
    - Deterministic: no randomness in the algorithm.

    Args:
        text: Source text to split (must be non-empty; caller's guard).
        max_tokens: Target maximum tokens per chunk (whitespace-word proxy).
        overlap_tokens: Overlap tokens between consecutive chunks.

    Returns:
        List of (char_start, char_end) pairs, at least one element.
    """
    if not text:
        return []

    total_tokens = _count_tokens(text)
    if total_tokens <= max_tokens:
        # Whole text fits in one chunk — common case for short segments.
        return [(0, len(text))]

    spans: list[tuple[int, int]] = []
    cursor = 0
    text_len = len(text)

    while cursor < text_len:
        remaining = text[cursor:]
        remaining_tokens = _count_tokens(remaining)

        if remaining_tokens <= max_tokens:
            # Remaining text fits — final chunk.
            spans.append((cursor, text_len))
            break

        # Estimate character count for max_tokens.
        # density = chars per token for the remaining text.
        # Avoid division by zero (remaining_tokens guaranteed > 0 here).
        density = len(remaining) / remaining_tokens
        estimated_end = min(cursor + int(density * max_tokens) + 1, text_len)

        # Find the best split point at or before estimated_end.
        split_len = _find_split_point(remaining, estimated_end - cursor)
        chunk_end = cursor + split_len

        # Ensure progress — never get stuck if split_len came back 0.
        # (split_len is guaranteed > 0 by _find_split_point, but be defensive.)
        if chunk_end <= cursor:
            chunk_end = min(cursor + 1, text_len)

        spans.append((cursor, chunk_end))

        if chunk_end >= text_len:
            break

        # Compute overlap: step back overlap_tokens worth of chars.
        chunk_text = text[cursor:chunk_end]
        chunk_tokens = _count_tokens(chunk_text)
        # Proportion of the chunk that is overlap.
        if chunk_tokens > 0 and overlap_tokens > 0:
            actual_overlap = min(overlap_tokens, chunk_tokens - 1)
            overlap_chars = int(len(chunk_text) * actual_overlap / chunk_tokens)
        else:
            overlap_chars = 0

        next_cursor = chunk_end - overlap_chars
        # Ensure forward progress — cursor must always advance.
        if next_cursor <= cursor:
            next_cursor = cursor + 1
        cursor = min(next_cursor, text_len)

    return spans


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def chunk_segment(
    segment_text: str,
    max_tokens: int = 512,
    overlap_tokens: int = 50,
) -> list[ChunkSpan]:
    """Split a single segment's text into overlapping chunks.

    This is the ONLY public function of this module. It is called once per
    segment by the Build stage. A chunk NEVER spans segments (§7.1) because
    this function receives exactly one segment's text.

    Args:
        segment_text: The segment's text content. If empty or whitespace-only,
            returns a single chunk with the original text so no content is
            silently discarded (§6 rule 6; §1.4 principle 1: nothing destroyed).
        max_tokens: Target chunk size in whitespace-word-count tokens.
            Default 512 per spec §9.3 naive baseline.
        overlap_tokens: Overlap between consecutive chunks in whitespace-word-count
            tokens. Default 50 per spec §9.3 naive baseline (within the 50–100
            range specified).
        # Note: overlap_tokens=50 is the lower bound of the §9.3 range [50–100].

    Returns:
        Non-empty list of ChunkSpan objects with dense 0-based chunk_index.
        The list always has at least one element.

    Raises:
        ValueError: If max_tokens < 1 or overlap_tokens < 0 or
            overlap_tokens >= max_tokens.
    """
    if max_tokens < 1:
        raise ValueError(f"max_tokens must be >= 1, got {max_tokens}")
    if overlap_tokens < 0:
        raise ValueError(f"overlap_tokens must be >= 0, got {overlap_tokens}")
    if overlap_tokens >= max_tokens:
        raise ValueError(f"overlap_tokens ({overlap_tokens}) must be < max_tokens ({max_tokens})")

    # Empty/whitespace-only segment: produce one chunk preserving the text.
    # Caller should have pre-filtered, but we handle it defensively.
    if not segment_text.strip():
        return [
            ChunkSpan(
                text=segment_text,
                chunk_index=0,
                char_start=0,
                char_end=len(segment_text),
                token_count=_count_tokens(segment_text),
            )
        ]

    raw_spans = split_text(segment_text, max_tokens, overlap_tokens)

    chunks: list[ChunkSpan] = []
    for idx, (char_start, char_end) in enumerate(raw_spans):
        text = segment_text[char_start:char_end]
        chunks.append(
            ChunkSpan(
                text=text,
                chunk_index=idx,
                char_start=char_start,
                char_end=char_end,
                token_count=_count_tokens(text),
            )
        )

    return chunks


def chunk_segment_dispatch(
    segment_text: str,
    strategy: ChunkingStrategy,
    max_tokens: int = 512,
    overlap_tokens: int = 50,
    split_boundaries: list[str] | None = None,
    language_hint: str = "unknown",
) -> list[ChunkSpan]:
    """Route to the appropriate splitter based on ``strategy`` (M-099).

    Dispatches:
    - ``code_syntax``: routes to ``split_code`` in ``code_splitter.py``.
      ``language_hint`` selects the boundary-detection heuristic; ``split_boundaries``
      is accepted but ignored (boundaries are detected automatically).
    - All other strategies: fall through to ``chunk_segment`` (recursive-char).
      This includes ``recursive_char``, ``structure_aware``, ``table_atomic``, and
      ``semantic``, all of which use the existing recursive-char logic in Phase 3
      (Phase 4 will wire the remaining strategies).

    Finding (for the orchestrator): ``ChunkingStrategy.code_syntax`` EXISTS in the
    contracts enum (ingestion_config.py line 38).  No contracts edit was required.

    Args:
        segment_text: The segment's text content.
        strategy: The chunking strategy from the ingestion config.
        max_tokens: Target chunk size (whitespace-word proxy).
        overlap_tokens: Overlap between consecutive chunks (whitespace-word proxy).
            Ignored for ``code_syntax`` (overlap is internal to the code splitter).
        split_boundaries: Accepted but unused — code boundaries are auto-detected.
        language_hint: Language identifier passed to ``split_code`` when strategy
            is ``code_syntax``.  E.g. ``"python"``, ``"javascript"``, ``"go"``.
            Default is ``"unknown"``, which triggers full recursive-char fallback
            inside ``split_code``.

    Returns:
        Non-empty list of ChunkSpan objects.

    Raises:
        ValueError: Same conditions as ``chunk_segment``.
    """
    _ = split_boundaries  # accepted; auto-detection makes this unnecessary

    if strategy == ChunkingStrategy.code_syntax:
        # Lazy import to avoid circular dependency at module load time
        from finecorpus.pipeline.build.code_splitter import split_code  # noqa: PLC0415

        if not segment_text:
            return [
                ChunkSpan(
                    text=segment_text,
                    chunk_index=0,
                    char_start=0,
                    char_end=0,
                    token_count=0,
                )
            ]
        return split_code(segment_text, max_tokens, language_hint)

    # All other strategies: recursive-char baseline.
    return chunk_segment(segment_text, max_tokens, overlap_tokens)


__all__ = [
    "ChunkSpan",
    "chunk_segment",
    "chunk_segment_dispatch",
    "split_text",
    "_count_tokens",
]
