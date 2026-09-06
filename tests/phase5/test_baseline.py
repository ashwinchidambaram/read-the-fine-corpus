"""Tests for finecorpus.pipeline.evaluation.baseline.

Covers:
- reference_ingestion_config: §9.3 params (recursive_char 512 / 75-overlap / dense /
  Tier 1 only / given embedding).
- reference_fingerprint: stable across calls; changes when embedding model changes.
- is_near_optimal: True when candidate ≈ reference, False when candidate clearly better.
- build_naive_baseline_ref: valid NaiveBaselineRef matching the contract.
"""

from __future__ import annotations

from finecorpus.contracts.ingestion_config import (
    ChunkingStrategy,
    EmbeddingConfig,
    NaiveBaselineRef,
    RetrievalStrategy,
)
from finecorpus.pipeline.evaluation.baseline import (
    NAIVE_BASELINE_REFERENCE_ID,
    build_naive_baseline_ref,
    is_near_optimal,
    reference_fingerprint,
    reference_ingestion_config,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_embedding(
    provider: str = "openai",
    model: str = "text-embedding-3-small",
    dimensions: int = 1536,
    normalize: bool = True,
) -> EmbeddingConfig:
    return EmbeddingConfig(
        provider=provider,
        model=model,
        dimensions=dimensions,
        normalize=normalize,
    )


# ---------------------------------------------------------------------------
# reference_ingestion_config — §9.3 parameter checks
# ---------------------------------------------------------------------------


class TestReferenceIngestionConfig:
    def test_chunking_strategy_is_recursive_char(self) -> None:
        cfg = reference_ingestion_config(_make_embedding())
        assert cfg["chunking"]["strategy"] == ChunkingStrategy.recursive_char.value

    def test_chunking_max_tokens_is_512(self) -> None:
        cfg = reference_ingestion_config(_make_embedding())
        assert cfg["chunking"]["max_tokens"] == 512

    def test_chunking_overlap_tokens_in_50_to_100(self) -> None:
        cfg = reference_ingestion_config(_make_embedding())
        overlap = cfg["chunking"]["overlap_tokens"]
        assert 50 <= overlap <= 100

    def test_retrieval_strategy_is_dense(self) -> None:
        cfg = reference_ingestion_config(_make_embedding())
        assert cfg["retrieval_strategy"] == RetrievalStrategy.dense.value

    def test_tier1_enabled(self) -> None:
        cfg = reference_ingestion_config(_make_embedding())
        assert cfg["transformation"]["tier1_enabled"] is True

    def test_tier2_disabled(self) -> None:
        """No augmentation beyond Tier 1 (§9.3)."""
        cfg = reference_ingestion_config(_make_embedding())
        assert cfg["transformation"]["tier2_enabled"] is False

    def test_tier3_disabled(self) -> None:
        """No augmentation beyond Tier 1 (§9.3)."""
        cfg = reference_ingestion_config(_make_embedding())
        assert cfg["transformation"]["tier3_enabled"] is False

    def test_embedding_matches_supplied(self) -> None:
        emb = _make_embedding(model="text-embedding-ada-002", dimensions=1536)
        cfg = reference_ingestion_config(emb)
        assert cfg["embedding"]["model"] == "text-embedding-ada-002"
        assert cfg["embedding"]["dimensions"] == 1536

    def test_different_embeddings_produce_different_configs(self) -> None:
        cfg_a = reference_ingestion_config(_make_embedding(model="model-a"))
        cfg_b = reference_ingestion_config(_make_embedding(model="model-b"))
        assert cfg_a["embedding"]["model"] != cfg_b["embedding"]["model"]


# ---------------------------------------------------------------------------
# reference_fingerprint — stability and sensitivity
# ---------------------------------------------------------------------------


class TestReferenceFingerprint:
    def test_returns_64_hex_chars(self) -> None:
        fp = reference_fingerprint(_make_embedding())
        assert len(fp) == 64
        assert all(c in "0123456789abcdef" for c in fp)

    def test_stable_across_calls_same_embedding(self) -> None:
        emb = _make_embedding()
        fp1 = reference_fingerprint(emb)
        fp2 = reference_fingerprint(emb)
        assert fp1 == fp2

    def test_stable_with_equivalent_embedding_objects(self) -> None:
        """Two EmbeddingConfig objects with same fields must produce the same fingerprint."""
        emb_a = _make_embedding(model="text-embedding-3-small", dimensions=1536)
        emb_b = _make_embedding(model="text-embedding-3-small", dimensions=1536)
        assert reference_fingerprint(emb_a) == reference_fingerprint(emb_b)

    def test_changes_when_embedding_model_changes(self) -> None:
        fp_a = reference_fingerprint(_make_embedding(model="model-alpha"))
        fp_b = reference_fingerprint(_make_embedding(model="model-beta"))
        assert fp_a != fp_b

    def test_changes_when_embedding_provider_changes(self) -> None:
        fp_a = reference_fingerprint(_make_embedding(provider="openai"))
        fp_b = reference_fingerprint(_make_embedding(provider="ollama"))
        assert fp_a != fp_b

    def test_changes_when_embedding_dimensions_changes(self) -> None:
        fp_a = reference_fingerprint(_make_embedding(dimensions=768))
        fp_b = reference_fingerprint(_make_embedding(dimensions=1536))
        assert fp_a != fp_b

    def test_deterministic_different_embedding_objects_same_content(self) -> None:
        """Regression: fingerprint must NOT depend on object identity."""
        emb1 = EmbeddingConfig(provider="openai", model="m", dimensions=100, normalize=True)
        emb2 = EmbeddingConfig(provider="openai", model="m", dimensions=100, normalize=True)
        assert reference_fingerprint(emb1) == reference_fingerprint(emb2)


# ---------------------------------------------------------------------------
# build_naive_baseline_ref — contract validation
# ---------------------------------------------------------------------------


class TestBuildNaiveBaselineRef:
    def test_returns_naive_baseline_ref_instance(self) -> None:
        ref = build_naive_baseline_ref(_make_embedding())
        assert isinstance(ref, NaiveBaselineRef)

    def test_reference_id_matches_constant(self) -> None:
        ref = build_naive_baseline_ref(_make_embedding())
        assert ref.reference_id == NAIVE_BASELINE_REFERENCE_ID

    def test_description_is_non_empty_string(self) -> None:
        ref = build_naive_baseline_ref(_make_embedding())
        assert isinstance(ref.description, str)
        assert len(ref.description) > 0

    def test_description_mentions_embedding_model(self) -> None:
        ref = build_naive_baseline_ref(_make_embedding(model="text-embedding-3-small"))
        assert "text-embedding-3-small" in ref.description

    def test_pydantic_validates_no_extra_fields(self) -> None:
        """NaiveBaselineRef has extra='forbid' — must not set undeclared fields."""
        ref = build_naive_baseline_ref(_make_embedding())
        # If extra fields were set, model_dump would include them; check field count.
        dumped = ref.model_dump()
        assert set(dumped.keys()) == {"reference_id", "description"}

    def test_naive_baseline_reference_id_constant_is_string(self) -> None:
        assert isinstance(NAIVE_BASELINE_REFERENCE_ID, str)
        assert len(NAIVE_BASELINE_REFERENCE_ID) > 0

    def test_description_varies_with_embedding(self) -> None:
        ref_a = build_naive_baseline_ref(_make_embedding(model="model-a"))
        ref_b = build_naive_baseline_ref(_make_embedding(model="model-b"))
        assert ref_a.description != ref_b.description


# ---------------------------------------------------------------------------
# is_near_optimal — M-051
# ---------------------------------------------------------------------------


class TestIsNearOptimal:
    # --- True (near-optimal) ---

    def test_candidate_equal_to_reference(self) -> None:
        assert is_near_optimal(candidate_score=0.80, reference_score=0.80, noise_margin=0.01)

    def test_candidate_slightly_above_reference_within_margin(self) -> None:
        assert is_near_optimal(candidate_score=0.82, reference_score=0.80, noise_margin=0.02)

    def test_candidate_slightly_above_exact_margin(self) -> None:
        # candidate - reference == noise_margin → still near-optimal (≤)
        assert is_near_optimal(candidate_score=0.83, reference_score=0.80, noise_margin=0.03)

    def test_candidate_below_reference(self) -> None:
        """Candidate worse than reference → still "near-optimal" (no beat)."""
        assert is_near_optimal(candidate_score=0.75, reference_score=0.80, noise_margin=0.01)

    def test_zero_noise_margin_candidate_equal(self) -> None:
        assert is_near_optimal(candidate_score=0.50, reference_score=0.50, noise_margin=0.0)

    # --- False (clearly better) ---

    def test_candidate_clearly_beats_reference(self) -> None:
        assert not is_near_optimal(candidate_score=0.95, reference_score=0.80, noise_margin=0.01)

    def test_candidate_just_above_margin(self) -> None:
        # 0.031 > 0.03 → beats reference
        assert not is_near_optimal(candidate_score=0.831, reference_score=0.80, noise_margin=0.03)

    def test_large_gap_returns_false(self) -> None:
        assert not is_near_optimal(candidate_score=1.0, reference_score=0.0, noise_margin=0.05)

    def test_zero_noise_margin_candidate_above(self) -> None:
        assert not is_near_optimal(candidate_score=0.51, reference_score=0.50, noise_margin=0.0)

    # --- Return type ---

    def test_returns_bool(self) -> None:
        result = is_near_optimal(0.8, 0.8, 0.01)
        assert isinstance(result, bool)

    # --- Pure / no side effects ---

    def test_multiple_calls_same_result(self) -> None:
        args = (0.82, 0.80, 0.01)
        assert is_near_optimal(*args) == is_near_optimal(*args)
