"""Corpus statistics computation for the Plan stage recommender.

Pure function — no I/O, no LLM, no side effects.
``compute_corpus_stats`` aggregates per-segment-class statistics from a
SegmentSetBatch that drive the heuristic recommendation engine.

Token counting uses the whitespace-word proxy (``len(text.split())``) consistent
with ChunkingConfig.tokenizer == "whitespace_word" (§9.3 naive baseline, §10.5).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from statistics import mean, median, quantiles
from typing import Any

# ---------------------------------------------------------------------------
# Output types
# ---------------------------------------------------------------------------


@dataclass
class TokenLengthStats:
    """Distribution summary for token lengths within a segment class."""

    count: int
    min: int
    max: int
    mean: float
    median: float
    p75: float
    p90: float
    p95: float


@dataclass
class OcrStats:
    """OCR confidence distribution for segments with OCR data."""

    count: int  # segments that have ocr_confidence
    mean_confidence: float
    p10_confidence: float  # 10th percentile — typical floor for exclusion
    min_confidence: float


@dataclass
class ClassStats:
    """Per-segment-class aggregate statistics.

    Attributes:
        segment_type: The segment class this covers (string from SegmentType enum).
        segment_count: Total segments of this class.
        token_lengths: Token-length distribution (None if no text segments).
        has_heading_structure: True when heading segments are present AND mean
            structural_path depth >= 2 for the class (signals real heading hierarchy).
        table_shapes: Sampled (rows, columns) shapes for table segments; empty for
            non-table classes.
        ocr: OCR confidence distribution; None if no segments have ocr_confidence.
        language_counts: Detected language → segment count.
        salience_tier_counts: Salience tier string → segment count.
    """

    segment_type: str
    segment_count: int
    token_lengths: TokenLengthStats | None
    has_heading_structure: bool
    table_shapes: list[tuple[int, int]]  # (rows, cols) for table class
    ocr: OcrStats | None
    language_counts: dict[str, int]
    salience_tier_counts: dict[str, int]


@dataclass
class CorpusStats:
    """Corpus-wide statistics produced by compute_corpus_stats.

    Attributes:
        by_class: Per segment-type stats keyed by segment_type string.
        total_segments: Total segments across all segment sets.
        detected_languages: Language → total segment count across corpus.
    """

    by_class: dict[str, ClassStats]
    total_segments: int
    detected_languages: dict[str, int]


# ---------------------------------------------------------------------------
# Token counting
# ---------------------------------------------------------------------------


def _count_tokens(text: str) -> int:
    """Whitespace-word proxy: len(text.split()). Consistent with §9.3 baseline."""
    return len(text.split())


# ---------------------------------------------------------------------------
# Table shape estimation
# ---------------------------------------------------------------------------

_TABLE_MARKER_SEPARATORS = frozenset(["|", "+"])


def _estimate_table_shape(text: str | None) -> tuple[int, int] | None:
    """Estimate (rows, cols) from markdown-style table text.

    Returns None if we cannot determine shape.
    Only called for ``table`` class segments.
    """
    if not text:
        return None
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    # Filter to rows that look like markdown table rows (contain |)
    row_lines = [ln for ln in lines if "|" in ln]
    if not row_lines:
        return None
    # Count columns from the first row
    cols = max(1, len(row_lines[0].split("|")) - 1)
    # Exclude separator lines (rows that are all dashes/pipes)
    data_rows = [ln for ln in row_lines if not all(c in "-|+: " for c in ln)]
    rows = len(data_rows)
    return (rows, cols)


# ---------------------------------------------------------------------------
# Main computation
# ---------------------------------------------------------------------------


def compute_corpus_stats(batch: dict[str, Any]) -> CorpusStats:
    """Compute per-class statistics from a SegmentSetBatch dict.

    Args:
        batch: A SegmentSetBatch serialised as a dict (from ArtifactStore.load or
               model_dump). Expects ``batch["segment_sets"]`` to be a list of
               SegmentSet dicts.

    Returns:
        CorpusStats with per-class aggregates.
    """
    # Accumulate per-class raw data
    class_token_lists: dict[str, list[int]] = defaultdict(list)
    class_heading_segs: dict[str, int] = defaultdict(int)
    class_path_depths: dict[str, list[int]] = defaultdict(list)
    class_table_shapes: dict[str, list[tuple[int, int]]] = defaultdict(list)
    class_ocr_confs: dict[str, list[float]] = defaultdict(list)
    class_lang_counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    class_salience_counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    class_segment_counts: dict[str, int] = defaultdict(int)

    corpus_lang_counts: dict[str, int] = defaultdict(int)
    total_segments = 0

    segment_sets: list[dict[str, Any]] = batch.get("segment_sets", [])

    for ss in segment_sets:
        segments: list[dict[str, Any]] = ss.get("segments", [])
        for seg in segments:
            seg_type: str = seg.get("segment_type", "unknown")
            text: str | None = seg.get("text")
            structural_path: list[str] = seg.get("structural_path", [])
            ocr_confidence: float | None = seg.get("ocr_confidence")
            language: str = seg.get("language", "und")
            salience_tier: str = seg.get("salience_tier", "supporting")

            class_segment_counts[seg_type] += 1
            total_segments += 1

            # Token lengths (whitespace-word proxy)
            if text is not None:
                class_token_lists[seg_type].append(_count_tokens(text))

            # Heading structure — track if this type has heading-ish depth
            path_depth = len(structural_path)
            class_path_depths[seg_type].append(path_depth)

            # Heading segments signal real heading structure corpus-wide
            if seg_type == "heading":
                # Mark the heading class itself
                class_heading_segs[seg_type] += 1
                # Also propagate to prose (breadcrumb is relevant for prose)
                class_heading_segs["prose"] += 1

            # Table shapes
            if seg_type == "table":
                shape = _estimate_table_shape(text)
                if shape is not None:
                    class_table_shapes[seg_type].append(shape)

            # OCR confidence
            if ocr_confidence is not None:
                class_ocr_confs[seg_type].append(ocr_confidence)

            # Language distribution
            class_lang_counts[seg_type][language] += 1
            corpus_lang_counts[language] += 1

            # Salience tier distribution
            class_salience_counts[seg_type][salience_tier] += 1

    # Build ClassStats per observed class
    by_class: dict[str, ClassStats] = {}
    all_classes = set(class_segment_counts.keys())

    for seg_type in all_classes:
        seg_count = class_segment_counts[seg_type]

        # Token length stats
        token_list = class_token_lists.get(seg_type, [])
        if token_list:
            sorted_tokens = sorted(token_list)
            n = len(sorted_tokens)
            if n >= 4:
                qs = quantiles(sorted_tokens, n=100)
                p75 = qs[74]
                p90 = qs[89]
                p95 = qs[94]
            else:
                p75 = float(max(sorted_tokens))
                p90 = float(max(sorted_tokens))
                p95 = float(max(sorted_tokens))
            tl_stats = TokenLengthStats(
                count=n,
                min=min(sorted_tokens),
                max=max(sorted_tokens),
                mean=round(mean(sorted_tokens), 2),
                median=float(median(sorted_tokens)),
                p75=round(p75, 2),
                p90=round(p90, 2),
                p95=round(p95, 2),
            )
        else:
            tl_stats = None

        # Heading structure: the class has heading structure if:
        # 1. There are heading segments pointing at it, OR
        # 2. The class itself has mean structural_path depth >= 2
        path_depths = class_path_depths.get(seg_type, [])
        mean_depth = mean(path_depths) if path_depths else 0.0
        has_heading_structure = class_heading_segs.get(seg_type, 0) > 0 or mean_depth >= 2.0

        # OCR stats
        ocr_list = class_ocr_confs.get(seg_type, [])
        if ocr_list:
            sorted_ocr = sorted(ocr_list)
            n_ocr = len(sorted_ocr)
            if n_ocr >= 10:
                p10 = quantiles(sorted_ocr, n=100)[9]
            else:
                p10 = float(sorted_ocr[0])
            ocr_stats = OcrStats(
                count=n_ocr,
                mean_confidence=round(mean(sorted_ocr), 4),
                p10_confidence=round(p10, 4),
                min_confidence=round(sorted_ocr[0], 4),
            )
        else:
            ocr_stats = None

        by_class[seg_type] = ClassStats(
            segment_type=seg_type,
            segment_count=seg_count,
            token_lengths=tl_stats,
            has_heading_structure=has_heading_structure,
            table_shapes=list(class_table_shapes.get(seg_type, [])),
            ocr=ocr_stats,
            language_counts=dict(class_lang_counts.get(seg_type, {})),
            salience_tier_counts=dict(class_salience_counts.get(seg_type, {})),
        )

    return CorpusStats(
        by_class=by_class,
        total_segments=total_segments,
        detected_languages=dict(corpus_lang_counts),
    )


__all__ = [
    "TokenLengthStats",
    "OcrStats",
    "ClassStats",
    "CorpusStats",
    "compute_corpus_stats",
]
