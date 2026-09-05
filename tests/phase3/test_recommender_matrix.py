"""Tests for the heuristic recommender — §6.4 matrix encoding, determinism, provenance (M-025).

Every §6.4 matrix row is asserted against a constructed CorpusStats.
Same input twice → identical output (determinism).
Every set value has provenance with basis=heuristic (M-025).
Class-description influence flips basis to class_description.
"""

from __future__ import annotations

import pytest

from finecorpus.contracts.ingestion_config import (
    ChunkingStrategy,
    ClassDescription,
    RecommendationBasis,
    Tier2Operation,
)
from finecorpus.contracts.shared.blocks import SalienceTier, SegmentType
from finecorpus.pipeline.plan.corpus_stats import (
    ClassStats,
    CorpusStats,
    OcrStats,
    TokenLengthStats,
)
from finecorpus.pipeline.plan.recommender import recommend

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_token_stats(n: int = 50, p90: float = 320.0) -> TokenLengthStats:
    return TokenLengthStats(
        count=n,
        min=10,
        max=600,
        mean=200.0,
        median=180.0,
        p75=280.0,
        p90=p90,
        p95=400.0,
    )


def _make_class_stats(
    seg_type: str,
    *,
    n: int = 50,
    p90: float = 320.0,
    has_heading_structure: bool = False,
    ocr: OcrStats | None = None,
) -> ClassStats:
    return ClassStats(
        segment_type=seg_type,
        segment_count=n,
        token_lengths=_make_token_stats(n=n, p90=p90),
        has_heading_structure=has_heading_structure,
        table_shapes=[],
        ocr=ocr,
        language_counts={"en": n},
        salience_tier_counts={"primary": n},
    )


def _make_corpus(classes: dict[str, ClassStats]) -> CorpusStats:
    total = sum(cs.segment_count for cs in classes.values())
    langs: dict[str, int] = {}
    for cs in classes.values():
        for lang, count in cs.language_counts.items():
            langs[lang] = langs.get(lang, 0) + count
    return CorpusStats(by_class=classes, total_segments=total, detected_languages=langs)


def _desc(seg_type: str, text: str) -> ClassDescription:
    return ClassDescription(
        segment_class=SegmentType(seg_type),
        class_id=seg_type,
        description=text,
    )


# ---------------------------------------------------------------------------
# §6.4 matrix: Prose
# ---------------------------------------------------------------------------


class TestProseRow:
    """§6.4 Prose: recursive_char, respect_headings=True, breadcrumb when structure exists."""

    def test_prose_strategy_is_recursive_char(self):
        corpus = _make_corpus({"prose": _make_class_stats("prose")})
        result = recommend(corpus, [])
        rule = next(r for r in result.class_rules if r.segment_class == SegmentType.prose)
        assert rule.chunking.strategy == ChunkingStrategy.recursive_char

    def test_prose_respect_headings_is_true(self):
        corpus = _make_corpus({"prose": _make_class_stats("prose")})
        result = recommend(corpus, [])
        rule = next(r for r in result.class_rules if r.segment_class == SegmentType.prose)
        assert rule.chunking.respect_headings is True

    def test_prose_breadcrumb_on_with_heading_structure(self):
        corpus = _make_corpus({"prose": _make_class_stats("prose", has_heading_structure=True)})
        result = recommend(corpus, [])
        rule = next(r for r in result.class_rules if r.segment_class == SegmentType.prose)
        assert Tier2Operation.breadcrumb_augment in rule.transformation.tier2_operations

    def test_prose_breadcrumb_off_without_heading_structure(self):
        corpus = _make_corpus({"prose": _make_class_stats("prose", has_heading_structure=False)})
        result = recommend(corpus, [])
        rule = next(r for r in result.class_rules if r.segment_class == SegmentType.prose)
        assert Tier2Operation.breadcrumb_augment not in rule.transformation.tier2_operations

    def test_prose_max_tokens_derived_from_p90(self):
        # p90=320 → rounded up to 320 (already on 64 boundary)
        corpus = _make_corpus({"prose": _make_class_stats("prose", p90=320.0)})
        result = recommend(corpus, [])
        rule = next(r for r in result.class_rules if r.segment_class == SegmentType.prose)
        assert rule.chunking.max_tokens == 320

    def test_prose_max_tokens_rounds_to_64_boundary(self):
        # p90=350 → rounds up to 384 (next 64-boundary after 350)
        corpus = _make_corpus({"prose": _make_class_stats("prose", p90=350.0)})
        result = recommend(corpus, [])
        rule = next(r for r in result.class_rules if r.segment_class == SegmentType.prose)
        assert rule.chunking.max_tokens == 384

    def test_prose_class_context_when_description_present(self):
        corpus = _make_corpus({"prose": _make_class_stats("prose")})
        result = recommend(corpus, [_desc("prose", "Policy documents with rules.")])
        rule = next(r for r in result.class_rules if r.segment_class == SegmentType.prose)
        assert Tier2Operation.class_context in rule.transformation.tier2_operations


# ---------------------------------------------------------------------------
# §6.4 matrix: Tables
# ---------------------------------------------------------------------------


class TestTableRow:
    """§6.4 Tables: atomic_rows, repeat_headers_on_split, table_description augmentation."""

    def test_table_strategy_is_table_atomic(self):
        corpus = _make_corpus({"table": _make_class_stats("table")})
        result = recommend(corpus, [])
        rule = next(r for r in result.class_rules if r.segment_class == SegmentType.table)
        assert rule.chunking.strategy == ChunkingStrategy.table_atomic

    def test_table_atomic_rows_is_true(self):
        corpus = _make_corpus({"table": _make_class_stats("table")})
        result = recommend(corpus, [])
        rule = next(r for r in result.class_rules if r.segment_class == SegmentType.table)
        assert rule.chunking.atomic_rows is True

    def test_table_repeat_headers_on_split_is_true(self):
        corpus = _make_corpus({"table": _make_class_stats("table")})
        result = recommend(corpus, [])
        rule = next(r for r in result.class_rules if r.segment_class == SegmentType.table)
        assert rule.chunking.repeat_headers_on_split is True

    def test_table_description_augmentation_is_on(self):
        corpus = _make_corpus({"table": _make_class_stats("table")})
        result = recommend(corpus, [])
        rule = next(r for r in result.class_rules if r.segment_class == SegmentType.table)
        assert Tier2Operation.table_description in rule.transformation.tier2_operations

    def test_table_no_overlap(self):
        corpus = _make_corpus({"table": _make_class_stats("table")})
        result = recommend(corpus, [])
        rule = next(r for r in result.class_rules if r.segment_class == SegmentType.table)
        assert rule.chunking.overlap_tokens == 0

    def test_table_tier1_includes_table_to_markdown(self):
        corpus = _make_corpus({"table": _make_class_stats("table")})
        result = recommend(corpus, [])
        rule = next(r for r in result.class_rules if r.segment_class == SegmentType.table)
        from finecorpus.contracts.ingestion_config import Tier1Operation

        assert Tier1Operation.table_to_markdown in rule.transformation.tier1_operations


# ---------------------------------------------------------------------------
# §6.4 matrix: Code
# ---------------------------------------------------------------------------


class TestCodeRow:
    """§6.4 Code: code_syntax strategy, function/class split boundaries."""

    def test_code_strategy_is_code_syntax(self):
        corpus = _make_corpus({"code": _make_class_stats("code")})
        result = recommend(corpus, [])
        rule = next(r for r in result.class_rules if r.segment_class == SegmentType.code)
        assert rule.chunking.strategy == ChunkingStrategy.code_syntax

    def test_code_split_boundaries_are_function_class(self):
        corpus = _make_corpus({"code": _make_class_stats("code")})
        result = recommend(corpus, [])
        rule = next(r for r in result.class_rules if r.segment_class == SegmentType.code)
        assert rule.chunking.split_boundaries is not None
        assert "function" in rule.chunking.split_boundaries
        assert "class" in rule.chunking.split_boundaries

    def test_code_no_overlap(self):
        corpus = _make_corpus({"code": _make_class_stats("code")})
        result = recommend(corpus, [])
        rule = next(r for r in result.class_rules if r.segment_class == SegmentType.code)
        assert rule.chunking.overlap_tokens == 0


# ---------------------------------------------------------------------------
# §6.4 matrix: Scanned region
# ---------------------------------------------------------------------------


class TestScannedRegionRow:
    """§6.4 Scans: recursive_char, OCR confidence_floor from measured distribution."""

    def test_scanned_region_strategy_is_recursive_char(self):
        corpus = _make_corpus({"scanned_region": _make_class_stats("scanned_region")})
        result = recommend(corpus, [])
        rule = next(r for r in result.class_rules if r.segment_class == SegmentType.scanned_region)
        assert rule.chunking.strategy == ChunkingStrategy.recursive_char

    def test_scanned_region_confidence_floor_from_measured_distribution(self):
        ocr_stats = OcrStats(
            count=100, mean_confidence=0.82, p10_confidence=0.65, min_confidence=0.40
        )
        cs = _make_class_stats("scanned_region", ocr=ocr_stats)
        corpus = _make_corpus({"scanned_region": cs})
        result = recommend(corpus, [])
        rule = next(r for r in result.class_rules if r.segment_class == SegmentType.scanned_region)
        # p10=0.65 — should be within the clamped range
        assert rule.retrieval_treatment.confidence_floor is not None
        floor = rule.retrieval_treatment.confidence_floor
        assert 0.50 <= floor <= 0.85
        # Specifically should be at or near the p10 value (0.65)
        assert abs(floor - 0.65) < 0.01

    def test_scanned_region_default_floor_when_no_ocr_data(self):
        corpus = _make_corpus({"scanned_region": _make_class_stats("scanned_region", ocr=None)})
        result = recommend(corpus, [])
        rule = next(r for r in result.class_rules if r.segment_class == SegmentType.scanned_region)
        assert rule.retrieval_treatment.confidence_floor is not None
        assert 0.50 <= rule.retrieval_treatment.confidence_floor <= 0.85

    def test_scanned_region_ocr_confidence_floor_clamped_low(self):
        # Very low p10 (below floor minimum) → clamp to minimum
        ocr_stats = OcrStats(
            count=50, mean_confidence=0.40, p10_confidence=0.10, min_confidence=0.05
        )
        cs = _make_class_stats("scanned_region", ocr=ocr_stats)
        corpus = _make_corpus({"scanned_region": cs})
        result = recommend(corpus, [])
        rule = next(r for r in result.class_rules if r.segment_class == SegmentType.scanned_region)
        assert rule.retrieval_treatment.confidence_floor >= 0.50

    def test_scanned_region_tier1_includes_ocr_cleanup(self):
        corpus = _make_corpus({"scanned_region": _make_class_stats("scanned_region")})
        result = recommend(corpus, [])
        rule = next(r for r in result.class_rules if r.segment_class == SegmentType.scanned_region)
        from finecorpus.contracts.ingestion_config import Tier1Operation

        assert Tier1Operation.ocr_cleanup in rule.transformation.tier1_operations


# ---------------------------------------------------------------------------
# §6.4 matrix: Boilerplate / Front matter
# ---------------------------------------------------------------------------


class TestBoilerplateRow:
    """Boilerplate and front_matter: excluded from default_salience_filter."""

    @pytest.mark.parametrize("seg_type", ["boilerplate", "front_matter"])
    def test_excluded_from_default_salience_filter(self, seg_type: str):
        corpus = _make_corpus({seg_type: _make_class_stats(seg_type)})
        result = recommend(corpus, [])
        rule = next(r for r in result.class_rules if r.segment_class.value == seg_type)
        # Should be boilerplate tier, NOT primary/supporting
        filter_tiers = rule.retrieval_treatment.default_salience_filter
        assert SalienceTier.boilerplate in filter_tiers
        assert SalienceTier.primary not in filter_tiers
        assert SalienceTier.supporting not in filter_tiers


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


class TestDeterminism:
    """Same stats in → identical config out."""

    def test_same_stats_same_output(self):
        classes = {
            "prose": _make_class_stats("prose", has_heading_structure=True),
            "table": _make_class_stats("table"),
            "code": _make_class_stats("code"),
        }
        corpus = _make_corpus(classes)
        descs = [_desc("prose", "Policy text.")]
        result1 = recommend(corpus, descs)
        result2 = recommend(corpus, descs)

        # Class rules must be identical
        assert len(result1.class_rules) == len(result2.class_rules)
        for r1, r2 in zip(result1.class_rules, result2.class_rules, strict=True):
            assert r1.model_dump() == r2.model_dump()

        # Provenance must be identical
        assert len(result1.provenance) == len(result2.provenance)
        for p1, p2 in zip(result1.provenance, result2.provenance, strict=True):
            assert p1.model_dump() == p2.model_dump()


# ---------------------------------------------------------------------------
# M-025: every set value has provenance with basis=heuristic
# ---------------------------------------------------------------------------


class TestM025Provenance:
    """Every recommender-set value has provenance with basis=heuristic (M-025)."""

    def test_every_class_rule_has_provenance(self):
        classes = {
            "prose": _make_class_stats("prose"),
            "table": _make_class_stats("table"),
            "code": _make_class_stats("code"),
            "scanned_region": _make_class_stats("scanned_region"),
        }
        corpus = _make_corpus(classes)
        result = recommend(corpus, [])

        # Every class that got a rule must have at least one provenance entry
        classes_with_prov: set[str] = set()
        for p in result.provenance:
            target = p.target
            if target.startswith("/class_rules/"):
                parts = target.split("/")
                if len(parts) >= 3:
                    classes_with_prov.add(parts[2])

        for rule in result.class_rules:
            assert rule.segment_class.value in classes_with_prov, (
                f"No provenance for class {rule.segment_class.value}"
            )

    def test_all_provenance_entries_have_heuristic_or_class_description_basis(self):
        classes = {
            "prose": _make_class_stats("prose", has_heading_structure=True),
            "table": _make_class_stats("table"),
        }
        corpus = _make_corpus(classes)
        result = recommend(corpus, [])

        allowed_bases = {RecommendationBasis.heuristic, RecommendationBasis.class_description}
        for p in result.provenance:
            assert p.basis in allowed_bases, (
                f"Provenance for target={p.target} has unexpected basis {p.basis}"
            )

    def test_all_provenance_entries_have_non_empty_rationale(self):
        classes = {"prose": _make_class_stats("prose")}
        corpus = _make_corpus(classes)
        result = recommend(corpus, [])

        for p in result.provenance:
            assert p.rationale and len(p.rationale.strip()) > 10, (
                f"Provenance for target={p.target} has empty/trivial rationale"
            )


# ---------------------------------------------------------------------------
# Class-description influence flips basis
# ---------------------------------------------------------------------------


class TestClassDescriptionBasis:
    """Values influenced by a class description get basis=class_description."""

    def test_class_context_provenance_basis_is_class_description(self):
        corpus = _make_corpus({"prose": _make_class_stats("prose")})
        desc = _desc("prose", "Narrative policy text. Users ask about rules and requirements.")
        result = recommend(corpus, [desc])

        # Find provenance entries for class_context in prose
        class_context_prov = [
            p for p in result.provenance if "class_context" in p.target and "prose" in p.target
        ]
        assert len(class_context_prov) >= 1
        for p in class_context_prov:
            assert p.basis == RecommendationBasis.class_description, (
                f"class_context provenance should be class_description, got {p.basis}"
            )

    def test_without_class_description_no_class_description_basis(self):
        corpus = _make_corpus({"prose": _make_class_stats("prose")})
        result = recommend(corpus, [])

        class_desc_prov = [
            p for p in result.provenance if p.basis == RecommendationBasis.class_description
        ]
        assert len(class_desc_prov) == 0

    def test_class_context_op_present_only_with_description(self):
        corpus = _make_corpus({"table": _make_class_stats("table")})

        # Without description — no class_context op
        result_no_desc = recommend(corpus, [])
        rule_no_desc = next(
            r for r in result_no_desc.class_rules if r.segment_class == SegmentType.table
        )
        assert Tier2Operation.class_context not in rule_no_desc.transformation.tier2_operations

        # With description — class_context op present
        result_with_desc = recommend(corpus, [_desc("table", "Data tables with metrics.")])
        rule_with_desc = next(
            r for r in result_with_desc.class_rules if r.segment_class == SegmentType.table
        )
        assert Tier2Operation.class_context in rule_with_desc.transformation.tier2_operations
