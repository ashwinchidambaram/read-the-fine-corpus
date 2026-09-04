"""Unit tests for the Phase 1 recursive character chunker.

Module: src/finecorpus/pipeline/build/chunker.py

Covers:
- Segment boundary invariant: a chunk never spans segments (chunker called per-segment).
- Overlap correctness.
- Determinism across calls.
- Tiny segments → single chunk.
- Chunk text is an exact substring of the input segment text (T-04 seed).
- Dense chunk_index (0, 1, 2, ...).
- Empty / whitespace-only input.
- Parameter validation.
"""

from __future__ import annotations

import pytest

from finecorpus.pipeline.build.chunker import (
    _count_tokens,
    chunk_segment,
    split_text,
)

# ---------------------------------------------------------------------------
# Token-count proxy
# ---------------------------------------------------------------------------


class TestCountTokens:
    def test_empty(self):
        assert _count_tokens("") == 0

    def test_single_word(self):
        assert _count_tokens("hello") == 1

    def test_multi_word(self):
        assert _count_tokens("hello world foo") == 3

    def test_whitespace_only(self):
        assert _count_tokens("   ") == 0

    def test_leading_trailing_whitespace(self):
        assert _count_tokens("  hello  ") == 1


# ---------------------------------------------------------------------------
# chunk_segment: parameter validation
# ---------------------------------------------------------------------------


class TestChunkSegmentValidation:
    def test_max_tokens_zero_raises(self):
        with pytest.raises(ValueError, match="max_tokens"):
            chunk_segment("hello", max_tokens=0, overlap_tokens=0)

    def test_negative_max_tokens_raises(self):
        with pytest.raises(ValueError, match="max_tokens"):
            chunk_segment("hello", max_tokens=-5, overlap_tokens=0)

    def test_negative_overlap_raises(self):
        with pytest.raises(ValueError, match="overlap_tokens"):
            chunk_segment("hello", max_tokens=10, overlap_tokens=-1)

    def test_overlap_eq_max_raises(self):
        with pytest.raises(ValueError, match="overlap_tokens"):
            chunk_segment("hello", max_tokens=10, overlap_tokens=10)

    def test_overlap_gt_max_raises(self):
        with pytest.raises(ValueError, match="overlap_tokens"):
            chunk_segment("hello", max_tokens=10, overlap_tokens=20)


# ---------------------------------------------------------------------------
# chunk_segment: tiny / short segments → single chunk
# ---------------------------------------------------------------------------


class TestTinySegments:
    def test_empty_text(self):
        """Empty text produces one chunk preserving the original."""
        chunks = chunk_segment("", max_tokens=512, overlap_tokens=50)
        assert len(chunks) == 1
        assert chunks[0].text == ""
        assert chunks[0].chunk_index == 0

    def test_whitespace_only(self):
        """Whitespace-only text produces one chunk."""
        chunks = chunk_segment("   ", max_tokens=512, overlap_tokens=50)
        assert len(chunks) == 1
        assert chunks[0].text == "   "

    def test_short_segment_single_chunk(self):
        """Segment with fewer than max_tokens produces exactly one chunk."""
        text = "This is a short paragraph."
        chunks = chunk_segment(text, max_tokens=512, overlap_tokens=50)
        assert len(chunks) == 1
        assert chunks[0].text == text
        assert chunks[0].chunk_index == 0

    def test_single_word(self):
        chunks = chunk_segment("hello", max_tokens=512, overlap_tokens=50)
        assert len(chunks) == 1
        assert chunks[0].text == "hello"

    def test_exactly_at_limit(self):
        """Text with exactly max_tokens words produces one chunk."""
        words = ["word"] * 10
        text = " ".join(words)
        chunks = chunk_segment(text, max_tokens=10, overlap_tokens=2)
        assert len(chunks) == 1
        assert chunks[0].text == text


# ---------------------------------------------------------------------------
# chunk_segment: multi-chunk behaviour
# ---------------------------------------------------------------------------


class TestMultipleChunks:
    def _make_text(self, n_words: int) -> str:
        """Make a text with n_words distinct words."""
        return " ".join(f"word{i}" for i in range(n_words))

    def test_produces_multiple_chunks_for_long_text(self):
        text = self._make_text(100)
        chunks = chunk_segment(text, max_tokens=20, overlap_tokens=4)
        assert len(chunks) > 1

    def test_dense_chunk_index(self):
        """chunk_index must be 0, 1, 2, ... with no gaps."""
        text = self._make_text(100)
        chunks = chunk_segment(text, max_tokens=20, overlap_tokens=4)
        for expected, chunk in enumerate(chunks):
            assert chunk.chunk_index == expected, (
                f"Expected chunk_index={expected}, got {chunk.chunk_index}"
            )

    def test_chunk_text_is_exact_substring_of_segment(self):
        """T-04 seed: every chunk text is an exact contiguous substring of the input."""
        text = self._make_text(100)
        chunks = chunk_segment(text, max_tokens=20, overlap_tokens=4)
        for chunk in chunks:
            assert chunk.text in text, f"Chunk text not a substring of segment: {chunk.text!r}"
            # Verify it's at the declared char offsets
            assert text[chunk.char_start : chunk.char_end] == chunk.text, (
                f"Chunk text does not match char offsets [{chunk.char_start}:{chunk.char_end}]"
            )

    def test_every_char_covered(self):
        """Every character of the segment appears in at least one chunk."""
        text = self._make_text(50)
        chunks = chunk_segment(text, max_tokens=10, overlap_tokens=2)
        covered = set()
        for chunk in chunks:
            for i in range(chunk.char_start, chunk.char_end):
                covered.add(i)
        for i in range(len(text)):
            assert i in covered, f"Character at position {i} not covered by any chunk"

    def test_chunk_token_count_field(self):
        """token_count field equals _count_tokens(chunk.text)."""
        text = self._make_text(100)
        chunks = chunk_segment(text, max_tokens=20, overlap_tokens=4)
        for chunk in chunks:
            assert chunk.token_count == _count_tokens(chunk.text), (
                f"token_count mismatch for chunk {chunk.chunk_index}"
            )

    def test_last_chunk_ends_at_segment_end(self):
        """The last chunk's char_end must equal len(segment_text)."""
        text = self._make_text(100)
        chunks = chunk_segment(text, max_tokens=20, overlap_tokens=4)
        assert chunks[-1].char_end == len(text), (
            f"Last chunk char_end={chunks[-1].char_end}, expected {len(text)}"
        )

    def test_first_chunk_starts_at_zero(self):
        """The first chunk's char_start must be 0."""
        text = self._make_text(100)
        chunks = chunk_segment(text, max_tokens=20, overlap_tokens=4)
        assert chunks[0].char_start == 0


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


class TestDeterminism:
    def test_same_text_same_output(self):
        """Same text + params always produces identical chunks."""
        text = " ".join(f"word{i}" for i in range(200))
        params = dict(max_tokens=30, overlap_tokens=5)
        chunks_a = chunk_segment(text, **params)
        chunks_b = chunk_segment(text, **params)
        assert len(chunks_a) == len(chunks_b)
        for a, b in zip(chunks_a, chunks_b, strict=True):
            assert a.text == b.text
            assert a.chunk_index == b.chunk_index
            assert a.char_start == b.char_start
            assert a.char_end == b.char_end

    def test_no_randomness_in_repeated_calls(self):
        """Multiple calls produce identical results (no global state)."""
        text = "alpha beta gamma delta epsilon " * 30
        params = dict(max_tokens=15, overlap_tokens=3)
        results = [chunk_segment(text, **params) for _ in range(5)]
        for i in range(1, 5):
            assert [c.text for c in results[i]] == [c.text for c in results[0]]


# ---------------------------------------------------------------------------
# Segment boundary invariant
# ---------------------------------------------------------------------------


class TestSegmentBoundary:
    """A chunk NEVER spans segments — chunker is called per-segment (§7.1).

    This is structural: we verify by calling chunk_segment independently
    for each segment and asserting that output chunks are substrings of
    THEIR OWN segment only.
    """

    def test_independent_segmentation_preserves_boundary(self):
        seg1 = "First segment " * 40
        seg2 = "Second segment " * 40

        chunks1 = chunk_segment(seg1, max_tokens=20, overlap_tokens=4)
        chunks2 = chunk_segment(seg2, max_tokens=20, overlap_tokens=4)

        # All chunks from seg1 must be substrings of seg1, not seg2
        for c in chunks1:
            assert c.text in seg1, "Chunk from seg1 is not a substring of seg1"
            assert c.text not in seg2 or c.text.strip() == "", (
                "Chunk from seg1 appears as substring of seg2 (harmless if very short)"
            )

        # All chunks from seg2 must be substrings of seg2
        for c in chunks2:
            assert c.text in seg2, "Chunk from seg2 is not a substring of seg2"

    def test_chunk_index_dense_per_segment(self):
        """chunk_index is dense per-segment (not global)."""
        seg1 = "word " * 50
        seg2 = "other " * 50

        chunks1 = chunk_segment(seg1, max_tokens=10, overlap_tokens=2)
        chunks2 = chunk_segment(seg2, max_tokens=10, overlap_tokens=2)

        # Each set starts at 0
        assert chunks1[0].chunk_index == 0
        assert chunks2[0].chunk_index == 0

        # Each set is dense
        for i, c in enumerate(chunks1):
            assert c.chunk_index == i
        for i, c in enumerate(chunks2):
            assert c.chunk_index == i


# ---------------------------------------------------------------------------
# Overlap verification
# ---------------------------------------------------------------------------


class TestOverlap:
    def test_consecutive_chunks_overlap(self):
        """Consecutive chunks share text in their overlap region."""
        # Use a text that guarantees splitting, with clear word boundaries
        text = "word" + str(0)
        for i in range(1, 200):
            text += f" word{i}"

        chunks = chunk_segment(text, max_tokens=20, overlap_tokens=5)
        assert len(chunks) >= 2, "Need at least 2 chunks to test overlap"

        # For consecutive pairs, the end of chunk[i] should overlap with the start of chunk[i+1]
        for i in range(len(chunks) - 1):
            c1 = chunks[i]
            c2 = chunks[i + 1]
            # c2 must start before c1 ends (char overlap)
            assert c2.char_start < c1.char_end, (
                f"No overlap between chunk {i} (end={c1.char_end}) "
                f"and chunk {i + 1} (start={c2.char_start})"
            )

    def test_no_overlap_gives_contiguous_non_overlapping(self):
        """overlap_tokens=0 produces non-overlapping chunks."""
        text = " ".join(f"word{i}" for i in range(100))
        chunks = chunk_segment(text, max_tokens=20, overlap_tokens=0)
        assert len(chunks) >= 2
        for i in range(len(chunks) - 1):
            c1 = chunks[i]
            c2 = chunks[i + 1]
            # No overlap: c2 starts where c1 ends (or after)
            assert c2.char_start >= c1.char_end, (
                f"Overlap detected with overlap_tokens=0: "
                f"chunk {i} ends at {c1.char_end}, chunk {i + 1} starts at {c2.char_start}"
            )

    def test_quantitative_overlap_within_tolerance(self):
        """Measured overlap tokens must fall within [0.8×, 1.2×+2] of overlap_tokens (F-03).

        The chunker uses a character-density estimate rather than an exact token budget,
        so the realised overlap is an approximation. The tolerance band [0.8×, 1.2×+2]
        accepts realistic estimation noise while catching large deviations.
        """
        overlap_tokens = 10
        max_tokens = 40
        text = " ".join(f"word{i}" for i in range(300))
        chunks = chunk_segment(text, max_tokens=max_tokens, overlap_tokens=overlap_tokens)
        assert len(chunks) >= 3, "Need at least 3 chunks to measure overlap for multiple pairs"

        lo = 0.8 * overlap_tokens
        hi = 1.2 * overlap_tokens + 2

        for i in range(len(chunks) - 1):
            c1 = chunks[i]
            c2 = chunks[i + 1]
            # The overlapping text is the portion of c1 that c2 also covers.
            overlap_text = text[c2.char_start : c1.char_end]
            measured = _count_tokens(overlap_text)
            assert lo <= measured <= hi, (
                f"Overlap between chunk {i} and {i + 1}: measured {measured} tokens, "
                f"expected [{lo:.1f}, {hi:.1f}] for overlap_tokens={overlap_tokens}. "
                f"Chunk {i}: [{c1.char_start}, {c1.char_end}), "
                f"chunk {i + 1}: [{c2.char_start}, {c2.char_end})"
            )


# ---------------------------------------------------------------------------
# split_text (internal)
# ---------------------------------------------------------------------------


class TestSplitText:
    def test_empty_returns_empty(self):
        assert split_text("", 10, 2) == []

    def test_short_text_single_span(self):
        text = "hello world"
        spans = split_text(text, 100, 10)
        assert len(spans) == 1
        assert spans[0] == (0, len(text))

    def test_all_chars_covered(self):
        text = " ".join(f"token{i}" for i in range(50))
        spans = split_text(text, 10, 2)
        # First span starts at 0
        assert spans[0][0] == 0
        # Last span ends at len(text)
        assert spans[-1][1] == len(text)

    def test_spans_are_valid(self):
        text = " ".join(f"token{i}" for i in range(50))
        spans = split_text(text, 10, 2)
        for start, end in spans:
            assert 0 <= start < end <= len(text)
