"""Heuristic recommendation engine for the Plan stage (§6.4, M-025).

Pure function — no I/O, no LLM, no side effects.
``recommend`` maps CorpusStats + ClassDescriptions → RecommendationResult,
encoding the §6.4 content handling matrix per-class.

Every value the recommender sets gets a RecommendationProvenance entry with
basis=heuristic and a plain-language rationale (M-025). Values influenced by a
class description get basis=class_description.

Deterministic: same stats in → identical config out.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from finecorpus.contracts.ingestion_config import (
    ChunkingConfig,
    ChunkingStrategy,
    ClassDescription,
    ClassRule,
    RecommendationBasis,
    RecommendationProvenance,
    RetrievalStrategy,
    RetrievalTreatment,
    Tier1Operation,
    Tier2Operation,
    TransformationSettings,
)
from finecorpus.contracts.shared.blocks import SalienceTier, SegmentType
from finecorpus.pipeline.plan.corpus_stats import ClassStats, CorpusStats

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Minimum share of corpus segments a class must represent to be meaningful.
# Classes below this threshold in salience may still be excluded.
_MEANINGFUL_SHARE_FLOOR = 0.02  # 2%

# Max token sizing heuristics per class — used when the measured distribution
# is too thin (< 5 samples) to be reliable.
_DEFAULT_MAX_TOKENS: dict[str, int] = {
    "prose": 512,
    "heading": 128,
    "table": 1024,
    "list": 256,
    "code": 512,
    "figure_caption": 256,
    "figure_region": 512,
    "form_field": 128,
    "boilerplate": 256,
    "front_matter": 256,
    "revision_history": 256,
    "cross_reference": 128,
    "scanned_region": 512,
    "unknown": 512,
}

# OCR confidence floor — below this, segments are flagged for low-confidence
# retrieval down-weighting (§6.4 scans). Derived from measured distribution
# using the 10th percentile of observed OCR confidence values, clamped to
# a reasonable range.
_OCR_CONFIDENCE_FLOOR_MIN = 0.50
_OCR_CONFIDENCE_FLOOR_MAX = 0.85


# ---------------------------------------------------------------------------
# Output types
# ---------------------------------------------------------------------------


@dataclass
class RecommendationResult:
    """Result of the heuristic recommendation pass.

    Attributes:
        class_rules: Per-class ClassRule objects for every class observed in the corpus.
        provenance: All provenance records for every value the recommender set.
    """

    class_rules: list[ClassRule]
    provenance: list[RecommendationProvenance]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _prov(
    target: str,
    basis: RecommendationBasis,
    rationale: str,
) -> RecommendationProvenance:
    """Shorthand for building a RecommendationProvenance entry."""
    return RecommendationProvenance(
        target=target,
        basis=basis,
        sweep_run_id=None,
        rationale=rationale,
    )


def _max_tokens_from_stats(stats: ClassStats, default: int) -> int:
    """Derive max_tokens from the p90 of the measured token-length distribution.

    Uses p90 so that 90% of segments fit in one chunk, providing a data-driven
    sizing that avoids aggressive splitting of typical content.
    If fewer than 5 samples exist, return the class default.
    """
    if stats.token_lengths is None or stats.token_lengths.count < 5:
        return default
    # Round up to nearest 64-token boundary for clean numbers
    raw_p90 = stats.token_lengths.p90
    rounded = max(64, int(math.ceil(raw_p90 / 64) * 64))
    # Cap at 2048 to avoid pathological sizing
    return min(rounded, 2048)


def _ocr_confidence_floor(stats: ClassStats) -> float:
    """Derive OCR confidence floor from the measured distribution.

    Uses the 10th percentile of observed OCR confidence values, clamped to
    [0.50, 0.85]. This gives a data-driven floor (manifest-parity discipline:
    measured values, not invented).
    """
    if stats.ocr is None or stats.ocr.count == 0:
        return 0.60  # reasonable default for scanned content
    raw = stats.ocr.p10_confidence
    return round(max(_OCR_CONFIDENCE_FLOOR_MIN, min(_OCR_CONFIDENCE_FLOOR_MAX, raw)), 4)


def _desc_for_class(seg_type: str, descriptions: list[ClassDescription]) -> ClassDescription | None:
    """Return the class description for a given segment type, or None."""
    for cd in descriptions:
        if cd.segment_class.value == seg_type or cd.class_id == seg_type:
            return cd
    return None


# ---------------------------------------------------------------------------
# Per-class rule builders (§6.4 content matrix)
# ---------------------------------------------------------------------------


def _recommend_prose(
    stats: ClassStats,
    desc: ClassDescription | None,
    provenance: list[RecommendationProvenance],
    prefix: str,
) -> ClassRule:
    """§6.4 Prose: recursive_char, respect_headings, breadcrumb augmentation."""
    max_tokens = _max_tokens_from_stats(stats, _DEFAULT_MAX_TOKENS["prose"])
    respect_headings = True  # always on for prose (structure-aware)
    has_headings = stats.has_heading_structure

    tier2_ops = []
    if has_headings:
        tier2_ops.append(Tier2Operation.breadcrumb_augment)
        provenance.append(
            _prov(
                f"{prefix}/chunking/respect_headings",
                RecommendationBasis.heuristic,
                "Heuristic: heading segments detected in the corpus — respect_headings=True "
                "enables structure-aware chunking that avoids splitting prose mid-section.",
            )
        )
        provenance.append(
            _prov(
                f"{prefix}/transformation/tier2_operations/breadcrumb_augment",
                RecommendationBasis.heuristic,
                "Heuristic: real heading structure detected (heading segments present, "
                "structural_path depth >= 2). Parent breadcrumb augmentation is switched on "
                "to give every chunk the navigational context of its heading hierarchy.",
            )
        )
    else:
        provenance.append(
            _prov(
                f"{prefix}/chunking/respect_headings",
                RecommendationBasis.heuristic,
                "Heuristic: no heading structure detected in this class — respect_headings=True "
                "is still set (safe default; no heading boundaries to split at).",
            )
        )

    if desc is not None:
        tier2_ops.append(Tier2Operation.class_context)
        provenance.append(
            _prov(
                f"{prefix}/transformation/tier2_operations/class_context",
                RecommendationBasis.class_description,
                f"A class description is provided for '{stats.segment_type}': "
                "class_context augmentation is enabled so chunks carry the description "
                "text as retrieval context.",
            )
        )

    provenance.append(
        _prov(
            f"{prefix}/chunking/strategy",
            RecommendationBasis.heuristic,
            "Heuristic (§6.4 content matrix): prose → recursive_char chunking. "
            "Recursive character splitting respects natural paragraph and sentence boundaries "
            "while keeping chunks at the target token size.",
        )
    )
    provenance.append(
        _prov(
            f"{prefix}/chunking/max_tokens",
            RecommendationBasis.heuristic,
            f"Heuristic: max_tokens={max_tokens} derived from p90 of the measured prose "
            "token-length distribution (rounded up to nearest 64-token boundary). "
            "Sizes 90% of prose segments to fit in one chunk.",
        )
    )

    return ClassRule(
        segment_class=SegmentType(stats.segment_type),
        transformation=TransformationSettings(
            tier1_enabled=True,
            tier1_operations=[Tier1Operation.whitespace_repair],
            tier2_enabled=True,
            tier2_operations=tier2_ops,
            tier3_enabled=False,
            tier3_settings=None,
        ),
        chunking=ChunkingConfig(
            strategy=ChunkingStrategy.recursive_char,
            max_tokens=max_tokens,
            overlap_tokens=max(32, max_tokens // 8),
            respect_headings=respect_headings,
            atomic_rows=None,
            repeat_headers_on_split=None,
            split_boundaries=None,
        ),
        embedding_override=None,
        metadata_schema=[],
        retrieval_treatment=RetrievalTreatment(
            default_salience_filter=[SalienceTier.primary, SalienceTier.supporting],
            salience_weights=None,
            rerank_eligible=True,
            strategy=RetrievalStrategy.dense,
            confidence_floor=None,
        ),
    )


def _recommend_table(
    stats: ClassStats,
    desc: ClassDescription | None,
    provenance: list[RecommendationProvenance],
    prefix: str,
) -> ClassRule:
    """§6.4 Tables: atomic rows, repeat_headers_on_split, table_description augmentation."""
    max_tokens = _max_tokens_from_stats(stats, _DEFAULT_MAX_TOKENS["table"])

    tier2_ops = [Tier2Operation.table_description]
    provenance.append(
        _prov(
            f"{prefix}/transformation/tier2_operations/table_description",
            RecommendationBasis.heuristic,
            "Heuristic (§6.4 content matrix): tables → table_description augmentation. "
            "A natural-language description of the table's contents is generated and stored "
            "as an augmentation field; the verbatim table is returned to the caller.",
        )
    )

    if desc is not None:
        tier2_ops.append(Tier2Operation.class_context)
        provenance.append(
            _prov(
                f"{prefix}/transformation/tier2_operations/class_context",
                RecommendationBasis.class_description,
                f"A class description is provided for '{stats.segment_type}': "
                "class_context augmentation is enabled so table chunks carry the "
                "description as retrieval context.",
            )
        )

    provenance.append(
        _prov(
            f"{prefix}/chunking/strategy",
            RecommendationBasis.heuristic,
            "Heuristic (§6.4 content matrix): tables → table_atomic chunking. "
            "Tables MUST NOT be split in a way that separates rows from headers.",
        )
    )
    provenance.append(
        _prov(
            f"{prefix}/chunking/atomic_rows",
            RecommendationBasis.heuristic,
            "Heuristic (§6.4): atomic_rows=True prevents rows from being split from their headers. "
            "This is the primary correctness guarantee for table content.",
        )
    )
    provenance.append(
        _prov(
            f"{prefix}/chunking/repeat_headers_on_split",
            RecommendationBasis.heuristic,
            "Heuristic (§6.4): repeat_headers_on_split=True ensures that when a table "
            "is too large to keep atomic, each fragment still carries the header row "
            "for interpretability.",
        )
    )
    provenance.append(
        _prov(
            f"{prefix}/chunking/max_tokens",
            RecommendationBasis.heuristic,
            f"Heuristic: max_tokens={max_tokens} derived from p90 of measured table token lengths. "
            "Tables are kept larger than prose to preserve table structure.",
        )
    )

    return ClassRule(
        segment_class=SegmentType(stats.segment_type),
        transformation=TransformationSettings(
            tier1_enabled=True,
            tier1_operations=[Tier1Operation.table_to_markdown, Tier1Operation.whitespace_repair],
            tier2_enabled=True,
            tier2_operations=tier2_ops,
            tier3_enabled=False,
            tier3_settings=None,
        ),
        chunking=ChunkingConfig(
            strategy=ChunkingStrategy.table_atomic,
            max_tokens=max_tokens,
            overlap_tokens=0,  # tables: no overlap — atomic rows
            respect_headings=False,
            atomic_rows=True,
            repeat_headers_on_split=True,
            split_boundaries=None,
        ),
        embedding_override=None,
        metadata_schema=[],
        retrieval_treatment=RetrievalTreatment(
            default_salience_filter=[SalienceTier.primary, SalienceTier.supporting],
            salience_weights=None,
            rerank_eligible=True,
            strategy=RetrievalStrategy.dense,
            confidence_floor=None,
        ),
    )


def _recommend_code(
    stats: ClassStats,
    desc: ClassDescription | None,
    provenance: list[RecommendationProvenance],
    prefix: str,
) -> ClassRule:
    """§6.4 Code: code_syntax strategy with function/class boundaries."""
    max_tokens = _max_tokens_from_stats(stats, _DEFAULT_MAX_TOKENS["code"])

    tier2_ops = []
    if desc is not None:
        tier2_ops.append(Tier2Operation.class_context)
        provenance.append(
            _prov(
                f"{prefix}/transformation/tier2_operations/class_context",
                RecommendationBasis.class_description,
                f"A class description is provided for '{stats.segment_type}': "
                "class_context augmentation is enabled so code chunks carry the "
                "description as retrieval context.",
            )
        )

    provenance.append(
        _prov(
            f"{prefix}/chunking/strategy",
            RecommendationBasis.heuristic,
            "Heuristic (§6.4 content matrix): code → code_syntax chunking. "
            "Syntax-aware splitting at function/class boundaries preserves logical code units.",
        )
    )
    provenance.append(
        _prov(
            f"{prefix}/chunking/split_boundaries",
            RecommendationBasis.heuristic,
            "Heuristic: split_boundaries=[function, class] instructs the code_syntax splitter "
            "to prefer function and class definitions as chunk boundaries. This keeps related "
            "code together and avoids splitting mid-function.",
        )
    )

    return ClassRule(
        segment_class=SegmentType(stats.segment_type),
        transformation=TransformationSettings(
            tier1_enabled=True,
            tier1_operations=[Tier1Operation.whitespace_repair],
            tier2_enabled=bool(tier2_ops),
            tier2_operations=tier2_ops,
            tier3_enabled=False,
            tier3_settings=None,
        ),
        chunking=ChunkingConfig(
            strategy=ChunkingStrategy.code_syntax,
            max_tokens=max_tokens,
            overlap_tokens=0,  # code: no overlap — preserve logical boundaries
            respect_headings=False,
            atomic_rows=None,
            repeat_headers_on_split=None,
            split_boundaries=["function", "class"],
        ),
        embedding_override=None,
        metadata_schema=[],
        retrieval_treatment=RetrievalTreatment(
            default_salience_filter=[SalienceTier.primary, SalienceTier.supporting],
            salience_weights=None,
            rerank_eligible=False,
            strategy=RetrievalStrategy.dense,
            confidence_floor=None,
        ),
    )


def _recommend_scanned_region(
    stats: ClassStats,
    desc: ClassDescription | None,
    provenance: list[RecommendationProvenance],
    prefix: str,
) -> ClassRule:
    """§6.4 Scans: recursive_char with OCR confidence_floor from measured distribution."""
    max_tokens = _max_tokens_from_stats(stats, _DEFAULT_MAX_TOKENS["scanned_region"])
    conf_floor = _ocr_confidence_floor(stats)

    tier2_ops = []
    if desc is not None:
        tier2_ops.append(Tier2Operation.class_context)
        provenance.append(
            _prov(
                f"{prefix}/transformation/tier2_operations/class_context",
                RecommendationBasis.class_description,
                f"A class description is provided for '{stats.segment_type}': class_context "
                "augmentation is enabled so scanned chunks carry the description as context.",
            )
        )

    if stats.ocr is not None:
        ocr_basis = (
            f"Heuristic: confidence_floor={conf_floor:.4f} derived from the 10th percentile "
            f"of {stats.ocr.count} measured OCR confidence values in this corpus "
            f"(measured p10={stats.ocr.p10_confidence:.4f}, "
            f"mean={stats.ocr.mean_confidence:.4f}). "
            "Manifest-parity discipline: this value comes from measured data, not a preset. "
            "Segments below this floor are down-weighted at retrieval (§6.4)."
        )
    else:
        ocr_basis = (
            f"Heuristic: confidence_floor={conf_floor:.4f} (default — no OCR data measured for "
            "this class). Scanned regions without OCR confidence data use a conservative default "
            "to avoid surfacing low-quality OCR content at retrieval (§6.4)."
        )

    provenance.append(
        _prov(
            f"{prefix}/retrieval_treatment/confidence_floor",
            RecommendationBasis.heuristic,
            ocr_basis,
        )
    )
    provenance.append(
        _prov(
            f"{prefix}/chunking/strategy",
            RecommendationBasis.heuristic,
            "Heuristic (§6.4 content matrix): scanned regions → recursive_char chunking. "
            "OCR output is unstructured text; recursive splitting respects natural boundaries.",
        )
    )
    provenance.append(
        _prov(
            f"{prefix}/transformation/tier1_operations/ocr_cleanup",
            RecommendationBasis.heuristic,
            "Heuristic: scanned regions get ocr_cleanup as a Tier 1 operation to repair "
            "common OCR artefacts (ligature substitutions, hyphenation, whitespace) before "
            "chunking.",
        )
    )

    return ClassRule(
        segment_class=SegmentType(stats.segment_type),
        transformation=TransformationSettings(
            tier1_enabled=True,
            tier1_operations=[Tier1Operation.ocr_cleanup, Tier1Operation.whitespace_repair],
            tier2_enabled=bool(tier2_ops),
            tier2_operations=tier2_ops,
            tier3_enabled=False,
            tier3_settings=None,
        ),
        chunking=ChunkingConfig(
            strategy=ChunkingStrategy.recursive_char,
            max_tokens=max_tokens,
            overlap_tokens=max(32, max_tokens // 8),
            respect_headings=False,
            atomic_rows=None,
            repeat_headers_on_split=None,
            split_boundaries=None,
        ),
        embedding_override=None,
        metadata_schema=[],
        retrieval_treatment=RetrievalTreatment(
            default_salience_filter=[SalienceTier.primary, SalienceTier.supporting],
            salience_weights=None,
            rerank_eligible=False,
            strategy=RetrievalStrategy.dense,
            confidence_floor=conf_floor,
        ),
    )


def _recommend_boilerplate_or_front_matter(
    stats: ClassStats,
    desc: ClassDescription | None,
    provenance: list[RecommendationProvenance],
    prefix: str,
) -> ClassRule:
    """§6.4 Boilerplate/front_matter: excluded from default_salience_filter."""
    max_tokens = _max_tokens_from_stats(stats, _DEFAULT_MAX_TOKENS.get(stats.segment_type, 256))

    tier2_ops = []
    if desc is not None:
        tier2_ops.append(Tier2Operation.class_context)
        provenance.append(
            _prov(
                f"{prefix}/transformation/tier2_operations/class_context",
                RecommendationBasis.class_description,
                f"A class description is provided for '{stats.segment_type}': class_context "
                "augmentation is enabled.",
            )
        )

    provenance.append(
        _prov(
            f"{prefix}/retrieval_treatment/default_salience_filter",
            RecommendationBasis.heuristic,
            f"Heuristic (§6.4): {stats.segment_type} is excluded from the default salience filter. "
            "Boilerplate and front_matter segments are not surfaced in default retrieval — "
            "they are structurally noise in most retrieval contexts. They remain indexed and "
            "accessible via explicit salience_tier filter.",
        )
    )
    provenance.append(
        _prov(
            f"{prefix}/chunking/strategy",
            RecommendationBasis.heuristic,
            f"Heuristic: {stats.segment_type} → recursive_char chunking (standard fallback; "
            "the class is excluded from default retrieval regardless of chunking strategy).",
        )
    )

    return ClassRule(
        segment_class=SegmentType(stats.segment_type),
        transformation=TransformationSettings(
            tier1_enabled=True,
            tier1_operations=[Tier1Operation.whitespace_repair],
            tier2_enabled=bool(tier2_ops),
            tier2_operations=tier2_ops,
            tier3_enabled=False,
            tier3_settings=None,
        ),
        chunking=ChunkingConfig(
            strategy=ChunkingStrategy.recursive_char,
            max_tokens=max_tokens,
            overlap_tokens=max(32, max_tokens // 8),
            respect_headings=False,
            atomic_rows=None,
            repeat_headers_on_split=None,
            split_boundaries=None,
        ),
        embedding_override=None,
        metadata_schema=[],
        retrieval_treatment=RetrievalTreatment(
            # Excluded from default salience filter — not in primary/supporting by default
            default_salience_filter=[SalienceTier.boilerplate],
            salience_weights=None,
            rerank_eligible=False,
            strategy=RetrievalStrategy.dense,
            confidence_floor=None,
        ),
    )


def _recommend_generic(
    stats: ClassStats,
    desc: ClassDescription | None,
    provenance: list[RecommendationProvenance],
    prefix: str,
) -> ClassRule:
    """Generic fallback: recursive_char for list, heading, figure_caption, etc."""
    max_tokens = _max_tokens_from_stats(stats, _DEFAULT_MAX_TOKENS.get(stats.segment_type, 256))
    has_headings = stats.has_heading_structure

    tier2_ops = []
    if has_headings:
        tier2_ops.append(Tier2Operation.breadcrumb_augment)
        provenance.append(
            _prov(
                f"{prefix}/transformation/tier2_operations/breadcrumb_augment",
                RecommendationBasis.heuristic,
                f"Heuristic: heading structure detected in the corpus for '{stats.segment_type}' "
                "— breadcrumb augmentation is enabled to preserve navigational context.",
            )
        )

    if desc is not None:
        tier2_ops.append(Tier2Operation.class_context)
        provenance.append(
            _prov(
                f"{prefix}/transformation/tier2_operations/class_context",
                RecommendationBasis.class_description,
                f"A class description is provided for '{stats.segment_type}': class_context "
                "augmentation is enabled.",
            )
        )

    provenance.append(
        _prov(
            f"{prefix}/chunking/strategy",
            RecommendationBasis.heuristic,
            f"Heuristic: '{stats.segment_type}' → recursive_char chunking (generic fallback "
            "from the §6.4 content matrix; no class-specific rule applies).",
        )
    )
    provenance.append(
        _prov(
            f"{prefix}/chunking/max_tokens",
            RecommendationBasis.heuristic,
            f"Heuristic: max_tokens={max_tokens} derived from the measured distribution "
            f"for '{stats.segment_type}' (p90 rounded to 64-token boundary).",
        )
    )

    return ClassRule(
        segment_class=SegmentType(stats.segment_type),
        transformation=TransformationSettings(
            tier1_enabled=True,
            tier1_operations=[Tier1Operation.whitespace_repair],
            tier2_enabled=bool(tier2_ops),
            tier2_operations=tier2_ops,
            tier3_enabled=False,
            tier3_settings=None,
        ),
        chunking=ChunkingConfig(
            strategy=ChunkingStrategy.recursive_char,
            max_tokens=max_tokens,
            overlap_tokens=max(16, max_tokens // 8),
            respect_headings=False,
            atomic_rows=None,
            repeat_headers_on_split=None,
            split_boundaries=None,
        ),
        embedding_override=None,
        metadata_schema=[],
        retrieval_treatment=RetrievalTreatment(
            default_salience_filter=[SalienceTier.primary, SalienceTier.supporting],
            salience_weights=None,
            rerank_eligible=False,
            strategy=RetrievalStrategy.dense,
            confidence_floor=None,
        ),
    )


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


def _recommend_class(
    stats: ClassStats,
    desc: ClassDescription | None,
    provenance: list[RecommendationProvenance],
) -> ClassRule:
    """Dispatch to the correct per-class rule builder based on segment_type."""
    seg_type = stats.segment_type
    prefix = f"/class_rules/{seg_type}"

    if seg_type == SegmentType.prose.value:
        return _recommend_prose(stats, desc, provenance, prefix)
    elif seg_type == SegmentType.table.value:
        return _recommend_table(stats, desc, provenance, prefix)
    elif seg_type == SegmentType.code.value:
        return _recommend_code(stats, desc, provenance, prefix)
    elif seg_type == SegmentType.scanned_region.value:
        return _recommend_scanned_region(stats, desc, provenance, prefix)
    elif seg_type in (SegmentType.boilerplate.value, SegmentType.front_matter.value):
        return _recommend_boilerplate_or_front_matter(stats, desc, provenance, prefix)
    else:
        return _recommend_generic(stats, desc, provenance, prefix)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def recommend(
    stats: CorpusStats,
    class_descriptions: list[ClassDescription],
) -> RecommendationResult:
    """Produce per-class ClassRules encoding the §6.4 content matrix.

    Args:
        stats: Corpus statistics from compute_corpus_stats.
        class_descriptions: Optional class descriptions from a YAML file or the
            IngestionConfig. If a description exists for a class, the
            class_context augmentation is switched on and the provenance entry
            gets basis=class_description.

    Returns:
        RecommendationResult with class_rules (one per observed corpus class)
        and provenance (one entry per field the recommender set).

    Determinism guarantee: same stats + same class_descriptions →
    identical output. No random state, no timestamps.
    """
    provenance: list[RecommendationProvenance] = []
    class_rules: list[ClassRule] = []

    # Sort class names for stable output order
    sorted_classes = sorted(stats.by_class.keys())

    for seg_type in sorted_classes:
        class_stats = stats.by_class[seg_type]
        desc = _desc_for_class(seg_type, class_descriptions)
        rule = _recommend_class(class_stats, desc, provenance)
        class_rules.append(rule)

    return RecommendationResult(
        class_rules=class_rules,
        provenance=provenance,
    )


__all__ = [
    "RecommendationResult",
    "recommend",
]
