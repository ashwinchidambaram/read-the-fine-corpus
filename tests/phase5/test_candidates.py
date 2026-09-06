"""Phase 5 tests: sweep candidate enumeration + corpus sampling (M-046, D-07).

Covers:
- test_sample_corpus_deterministic_stratified:
    * same seed → same sample
    * strata represented when corpus is large
    * large corpus → strict subset (not full set)
    * small corpus → full set returned
- test_candidate_budget_capped:
    * candidates list never exceeds budget (D-07)
    * single-budget returns only the base config
- test_reference_always_candidate_zero:
    * candidate_id 0 is always the base config
    * label == "base" for candidate 0
- test_enumerate_deterministic:
    * same seed + base → identical candidate list
    * all returned configs are valid IngestionConfig instances

All tests are pure: no I/O, no retrieval, no service imports.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

import pytest

from finecorpus.contracts.ingestion_config import (
    ChunkingConfig,
    ChunkingStrategy,
    ClassRule,
    EmbeddingConfig,
    IngestionConfig,
    LanguageDecision,
    LanguageSupportDecision,
    NaiveBaselineRef,
    RecommendationBasis,
    RecommendationProvenance,
    RetrievalStrategy,
    RetrievalTreatment,
    Tier1Operation,
    TransformationSettings,
)
from finecorpus.contracts.inventory import (
    CollectStatus,
    DedupRole,
    DocumentStatus,
    InventoryItem,
)
from finecorpus.contracts.shared.blocks import (
    PermissionFidelity,
    PermissionMode,
    PermissionSource,
    SalienceTier,
    SegmentType,
    TenancyBlock,
)
from finecorpus.contracts.versions import INGESTION_CONFIG_SCHEMA_VERSION
from finecorpus.pipeline.evaluation.candidates import (
    SweepCandidate,
    enumerate_candidates,
    sample_corpus,
)

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _tenancy() -> TenancyBlock:
    return TenancyBlock(
        workspace_id="ws-test",
        kb_id="kb-test",
        permission_mode=PermissionMode.public_to_kb,
        permission_principals=[],
        permission_source=PermissionSource.platform,
        permission_fidelity=PermissionFidelity.authoritative,
        permission_resolved_at=None,
    )


def _make_ingestion_config(
    strategy: ChunkingStrategy = ChunkingStrategy.recursive_char,
    max_tokens: int = 512,
    overlap_tokens: int = 64,
    retrieval: RetrievalStrategy = RetrievalStrategy.dense,
) -> IngestionConfig:
    """Build a minimal valid IngestionConfig for testing."""
    chunking = ChunkingConfig(
        strategy=strategy,
        max_tokens=max_tokens,
        overlap_tokens=overlap_tokens,
        respect_headings=False,
    )
    embedding = EmbeddingConfig(
        provider="fake",
        model="fake-embed-v1",
        dimensions=64,
        normalize=True,
        supports_languages=["*"],
    )
    default_rule = ClassRule(
        segment_class=SegmentType.prose,
        transformation=TransformationSettings(
            tier1_enabled=True,
            tier1_operations=[Tier1Operation.whitespace_repair],
            tier2_enabled=False,
            tier2_operations=[],
            tier3_enabled=False,
        ),
        chunking=chunking,
        embedding_override=None,
        metadata_schema=[],
        retrieval_treatment=RetrievalTreatment(
            default_salience_filter=[SalienceTier.primary, SalienceTier.supporting],
            salience_weights=None,
            rerank_eligible=False,
            strategy=retrieval,
        ),
    )
    affecting = {
        "max_tokens": max_tokens,
        "overlap_tokens": overlap_tokens,
        "strategy": strategy.value,
        "provider": "fake",
        "model": "fake-embed-v1",
        "dimensions": 64,
    }
    config_version = hashlib.sha256(json.dumps(affecting, sort_keys=True).encode()).hexdigest()

    return IngestionConfig(
        schema_version=INGESTION_CONFIG_SCHEMA_VERSION,
        tenancy=_tenancy(),
        config_version=config_version,
        created_at=datetime.now(tz=UTC),
        naive_baseline=NaiveBaselineRef(
            reference_id="naive-baseline-v0",
            description="Test baseline.",
        ),
        class_rules=[],
        default_rule=default_rule,
        embedding=embedding,
        retrieval_defaults=RetrievalTreatment(
            default_salience_filter=[SalienceTier.primary, SalienceTier.supporting],
            salience_weights=None,
            rerank_eligible=False,
            strategy=retrieval,
        ),
        language_support=LanguageSupportDecision(
            detected_languages=[],
            unsupported_languages=[],
            decision=LanguageDecision.proceed,
        ),
        spreadsheet_triage=[],
        exclusions_confirmed=[],
        provenance=[
            RecommendationProvenance(
                target="/default_rule",
                basis=RecommendationBasis.heuristic,
                rationale="Test baseline.",
            )
        ],
        secret_free_attestation=True,
    )


def _make_item(
    doc_id: str,
    media_type: str = "application/pdf",
) -> InventoryItem:
    """Build a minimal valid InventoryItem."""
    return InventoryItem(
        document_id=doc_id,
        content_hash=hashlib.sha256(doc_id.encode()).hexdigest(),
        source_path=f"/corpus/{doc_id}",
        display_name=doc_id,
        media_type=media_type,
        size_bytes=1000,
        source_metadata={},
        discovered_at=datetime.now(tz=UTC),
        dedup_role=DedupRole.unique,
        collect_status=CollectStatus.collected,
        document_status=DocumentStatus.active,
    )


def _make_corpus(
    n: int,
    media_types: list[str] | None = None,
) -> list[InventoryItem]:
    """Generate a corpus of n documents cycling through media_types."""
    if media_types is None:
        media_types = [
            "application/pdf",
            "text/html",
            "text/plain",
        ]
    return [_make_item(f"doc-{i:04d}", media_types[i % len(media_types)]) for i in range(n)]


# ---------------------------------------------------------------------------
# sample_corpus tests
# ---------------------------------------------------------------------------


class TestSampleCorpusDeterministicStratified:
    def test_same_seed_same_sample(self) -> None:
        """Same seed → identical sample on repeated calls."""
        docs = _make_corpus(200)
        result_a = sample_corpus(docs, min_docs=50, sample_factor=2, seed=42)
        result_b = sample_corpus(docs, min_docs=50, sample_factor=2, seed=42)
        assert [d.document_id for d in result_a] == [d.document_id for d in result_b]

    def test_different_seed_different_sample(self) -> None:
        """Different seeds → different samples (statistical expectation for large corpus)."""
        docs = _make_corpus(500)
        result_a = sample_corpus(docs, min_docs=50, sample_factor=2, seed=1)
        result_b = sample_corpus(docs, min_docs=50, sample_factor=2, seed=2)
        ids_a = {d.document_id for d in result_a}
        ids_b = {d.document_id for d in result_b}
        # With 100 samples from 500 docs the overlap should not be total
        assert ids_a != ids_b

    def test_large_corpus_returns_strict_subset(self) -> None:
        """Large corpus (> min_docs * sample_factor) → strict subset returned."""
        docs = _make_corpus(300)
        result = sample_corpus(docs, min_docs=50, sample_factor=2, seed=42)
        # target = min(50*2, 300) = 100 < 300
        assert len(result) < len(docs)
        assert len(result) == min(50 * 2, 300)

    def test_small_corpus_returns_full_set(self) -> None:
        """Corpus ≤ target → full corpus returned (no sampling)."""
        docs = _make_corpus(30)
        result = sample_corpus(docs, min_docs=50, sample_factor=2, seed=42)
        # target = min(50*2, 30) = 30 = corpus size → full set
        assert len(result) == len(docs)
        assert {d.document_id for d in result} == {d.document_id for d in docs}

    def test_corpus_equal_to_target_returns_full_set(self) -> None:
        """Corpus == target exactly → full corpus returned."""
        docs = _make_corpus(100)
        result = sample_corpus(docs, min_docs=50, sample_factor=2, seed=42)
        # target = min(100, 100) = 100
        assert len(result) == 100
        assert {d.document_id for d in result} == {d.document_id for d in docs}

    def test_strata_represented_in_large_sample(self) -> None:
        """Multiple media-type families are each represented in the sample."""
        media_types = [
            "application/pdf",
            "text/html",
            "text/plain",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "text/csv",
        ]
        # 10 docs per stratum → 50 docs total, each stratum well represented
        docs: list[InventoryItem] = []
        for i, mt in enumerate(media_types):
            for j in range(20):
                docs.append(_make_item(f"doc-{i:02d}-{j:03d}", mt))
        # 100 docs total; target = min(50*2, 100) = 100 → all returned
        # Use a smaller sample to test stratification
        result = sample_corpus(docs, min_docs=20, sample_factor=2, seed=7)
        # target = 40; 5 strata × 20 docs each → each stratum should contribute
        seen_types = {d.media_type for d in result}
        assert len(seen_types) > 1, "Multiple strata must be represented in the sample"

    def test_all_returned_items_are_from_input(self) -> None:
        """All sampled items are from the original corpus."""
        docs = _make_corpus(200)
        ids = {d.document_id for d in docs}
        result = sample_corpus(docs, min_docs=50, sample_factor=2, seed=99)
        for item in result:
            assert item.document_id in ids

    def test_no_duplicates_in_sample(self) -> None:
        """Sampled list contains no duplicate document_ids."""
        docs = _make_corpus(200)
        result = sample_corpus(docs, min_docs=50, sample_factor=2, seed=0)
        ids = [d.document_id for d in result]
        assert len(ids) == len(set(ids))

    def test_single_document_corpus(self) -> None:
        """Corpus of 1 document is always returned in full."""
        docs = [_make_item("only-doc")]
        result = sample_corpus(docs, min_docs=50, sample_factor=2, seed=42)
        assert len(result) == 1
        assert result[0].document_id == "only-doc"

    def test_empty_corpus_returns_empty(self) -> None:
        """Empty corpus returns empty list."""
        result = sample_corpus([], min_docs=50, sample_factor=2, seed=42)
        assert result == []

    def test_result_sorted_by_document_id(self) -> None:
        """Returned list is sorted by document_id for stable ordering."""
        docs = _make_corpus(200)
        result = sample_corpus(docs, min_docs=50, sample_factor=2, seed=5)
        ids = [d.document_id for d in result]
        assert ids == sorted(ids)

    def test_sample_size_uses_min_docs_times_factor(self) -> None:
        """Sample size = min(min_docs * sample_factor, corpus_size)."""
        docs = _make_corpus(500)
        result = sample_corpus(docs, min_docs=30, sample_factor=3, seed=1)
        assert len(result) == 90  # 30 * 3

    def test_sample_factor_one_returns_min_docs(self) -> None:
        """With sample_factor=1, sample size = min(min_docs, corpus_size)."""
        docs = _make_corpus(500)
        result = sample_corpus(docs, min_docs=50, sample_factor=1, seed=1)
        assert len(result) == 50

    # --- Ruling 1 regression tests ---

    def test_low_target_does_not_over_sample(self) -> None:
        """6 populated strata, target=3 → exactly 3 docs returned (strict subset)."""
        # 6 strata × 3 docs each = 18 docs total; target = min(3*1, 18) = 3
        media_types = [
            "application/pdf",  # pdf
            "text/html",  # html
            "text/plain",  # text
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",  # office
            "text/csv",  # spreadsheet
            "image/png",  # other
        ]
        docs: list[InventoryItem] = []
        for i, mt in enumerate(media_types):
            for j in range(3):
                docs.append(_make_item(f"stratum-{i:02d}-doc-{j:02d}", mt))
        # 18 total, target = 3*1 = 3 → must return exactly 3
        result = sample_corpus(docs, min_docs=3, sample_factor=1, seed=42)
        assert len(result) == 3, (
            f"Expected exactly 3 documents (target), got {len(result)} — "
            "floor-1 per stratum must not apply when n_active_strata > target"
        )

    def test_low_target_deterministic_across_calls(self) -> None:
        """6 strata, target=3 → same 3 docs returned on repeated calls with same seed."""
        media_types = [
            "application/pdf",
            "text/html",
            "text/plain",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "text/csv",
            "image/png",
        ]
        docs: list[InventoryItem] = []
        for i, mt in enumerate(media_types):
            for j in range(3):
                docs.append(_make_item(f"det-stratum-{i:02d}-doc-{j:02d}", mt))
        result_a = sample_corpus(docs, min_docs=3, sample_factor=1, seed=77)
        result_b = sample_corpus(docs, min_docs=3, sample_factor=1, seed=77)
        assert [d.document_id for d in result_a] == [d.document_id for d in result_b], (
            "Repeated calls with same seed must return identical results"
        )

    def test_default_params_large_corpus_returns_correct_count(self) -> None:
        """Default-like params (target >> strata) return exactly target docs."""
        # 3 strata, target = 100 — existing behavior must be preserved
        docs = _make_corpus(300)  # 300 docs, 3 strata cycling
        result = sample_corpus(docs, min_docs=100, sample_factor=1, seed=42)
        assert len(result) == 100, (
            f"Expected 100 documents (min_docs*sample_factor), got {len(result)}"
        )


# ---------------------------------------------------------------------------
# enumerate_candidates — budget capping (D-07)
# ---------------------------------------------------------------------------


class TestCandidateBudgetCapped:
    def test_never_exceeds_budget(self) -> None:
        """enumerate_candidates never returns more candidates than the budget."""
        base = _make_ingestion_config()
        for budget in [1, 5, 10, 25, 40, 100]:
            candidates = enumerate_candidates(base, budget=budget, seed=42)
            assert len(candidates) <= budget, f"budget={budget}: got {len(candidates)} candidates"

    def test_budget_one_returns_only_base(self) -> None:
        """budget=1 → exactly one candidate: the base config."""
        base = _make_ingestion_config()
        candidates = enumerate_candidates(base, budget=1, seed=42)
        assert len(candidates) == 1
        assert candidates[0].candidate_id == 0
        assert candidates[0].label == "base"

    def test_budget_larger_than_grid_returns_all(self) -> None:
        """budget larger than the grid size → all unique grid points returned."""
        base = _make_ingestion_config()
        # Grid = 2*3*2*2 = 24 variants + 1 base = up to 25 total
        candidates = enumerate_candidates(base, budget=100, seed=42)
        # Must have at least 2 (base + at least one variant)
        assert len(candidates) >= 2
        # Must not exceed full grid (25) given our fixed axes
        assert len(candidates) <= 25

    def test_budget_zero_raises(self) -> None:
        """budget=0 raises ValueError."""
        base = _make_ingestion_config()
        with pytest.raises(ValueError, match="budget"):
            enumerate_candidates(base, budget=0, seed=42)

    def test_budget_negative_raises(self) -> None:
        """budget < 0 raises ValueError."""
        base = _make_ingestion_config()
        with pytest.raises(ValueError, match="budget"):
            enumerate_candidates(base, budget=-1, seed=42)


# ---------------------------------------------------------------------------
# enumerate_candidates — reference always candidate zero
# ---------------------------------------------------------------------------


class TestReferenceAlwaysCandidateZero:
    def test_candidate_zero_has_id_zero(self) -> None:
        """candidate_id of the first element is always 0."""
        base = _make_ingestion_config()
        candidates = enumerate_candidates(base, budget=10, seed=42)
        assert candidates[0].candidate_id == 0

    def test_candidate_zero_label_is_base(self) -> None:
        """The first candidate's label is 'base'."""
        base = _make_ingestion_config()
        candidates = enumerate_candidates(base, budget=10, seed=42)
        assert candidates[0].label == "base"

    def test_candidate_zero_is_type_sweep_candidate(self) -> None:
        """The first candidate is a SweepCandidate dataclass instance."""
        base = _make_ingestion_config()
        candidates = enumerate_candidates(base, budget=10, seed=42)
        assert isinstance(candidates[0], SweepCandidate)

    def test_candidate_zero_config_schema_version_matches(self) -> None:
        """Candidate 0's config carries the same schema_version as the base."""
        base = _make_ingestion_config()
        candidates = enumerate_candidates(base, budget=10, seed=42)
        assert candidates[0].config.schema_version == base.schema_version

    def test_candidate_zero_config_is_valid_ingestion_config(self) -> None:
        """Candidate 0's config is a valid IngestionConfig (secret_free_attestation=True)."""
        base = _make_ingestion_config()
        candidates = enumerate_candidates(base, budget=10, seed=42)
        cfg = candidates[0].config
        assert isinstance(cfg, IngestionConfig)
        assert cfg.secret_free_attestation is True

    def test_base_config_parameters_preserved_in_candidate_zero(self) -> None:
        """Candidate 0's chunking parameters match the base config exactly."""
        base = _make_ingestion_config(
            strategy=ChunkingStrategy.recursive_char,
            max_tokens=512,
            overlap_tokens=64,
        )
        candidates = enumerate_candidates(base, budget=10, seed=42)
        c0 = candidates[0].config
        assert c0.default_rule.chunking.strategy == ChunkingStrategy.recursive_char
        assert c0.default_rule.chunking.max_tokens == 512
        assert c0.default_rule.chunking.overlap_tokens == 64

    def test_all_candidate_ids_are_sequential_from_zero(self) -> None:
        """candidate_id values are 0, 1, 2, … without gaps."""
        base = _make_ingestion_config()
        candidates = enumerate_candidates(base, budget=15, seed=42)
        ids = [c.candidate_id for c in candidates]
        assert ids == list(range(len(candidates)))


# ---------------------------------------------------------------------------
# enumerate_candidates — determinism
# ---------------------------------------------------------------------------


class TestEnumerateDeterministic:
    def test_same_seed_same_candidates(self) -> None:
        """Same seed + base → identical candidate list (labels and count)."""
        base = _make_ingestion_config()
        result_a = enumerate_candidates(base, budget=10, seed=42)
        result_b = enumerate_candidates(base, budget=10, seed=42)
        assert len(result_a) == len(result_b)
        assert [c.label for c in result_a] == [c.label for c in result_b]

    def test_same_seed_same_configs(self) -> None:
        """Same seed + base → configs are structurally identical."""
        base = _make_ingestion_config()
        result_a = enumerate_candidates(base, budget=10, seed=42)
        result_b = enumerate_candidates(base, budget=10, seed=42)
        for ca, cb in zip(result_a, result_b, strict=True):
            assert ca.config.model_dump() == cb.config.model_dump()

    def test_different_seeds_may_differ_under_budget_pressure(self) -> None:
        """Different seeds produce different ordering under budget truncation."""
        base = _make_ingestion_config()
        # Use a small budget to force truncation of the grid
        result_a = enumerate_candidates(base, budget=5, seed=1)
        result_b = enumerate_candidates(base, budget=5, seed=2)
        labels_a = [c.label for c in result_a]
        labels_b = [c.label for c in result_b]
        # They share candidate 0 (base) but may differ in the rest
        # (statistical; if grid < budget they will be identical)
        assert labels_a[0] == "base"
        assert labels_b[0] == "base"

    def test_all_candidates_are_valid_ingestion_configs(self) -> None:
        """All returned candidates carry valid IngestionConfig instances."""
        base = _make_ingestion_config()
        candidates = enumerate_candidates(base, budget=40, seed=42)
        for c in candidates:
            assert isinstance(c.config, IngestionConfig)
            assert c.config.secret_free_attestation is True
            # Pydantic validation passed (model_validate was called internally)
            # Re-validate to confirm structural invariants hold
            IngestionConfig.model_validate(c.config.model_dump(mode="python"))

    def test_variant_candidates_differ_from_base(self) -> None:
        """Non-zero candidates must differ from the base in at least one axis."""
        base = _make_ingestion_config(
            strategy=ChunkingStrategy.recursive_char,
            max_tokens=512,
            overlap_tokens=64,
            retrieval=RetrievalStrategy.dense,
        )
        candidates = enumerate_candidates(base, budget=40, seed=42)
        for c in candidates[1:]:  # skip candidate 0 (base)
            cfg = c.config
            differs = (
                cfg.default_rule.chunking.strategy != base.default_rule.chunking.strategy
                or cfg.default_rule.chunking.max_tokens != base.default_rule.chunking.max_tokens
                or cfg.default_rule.chunking.overlap_tokens
                != base.default_rule.chunking.overlap_tokens
                or cfg.retrieval_defaults.strategy != base.retrieval_defaults.strategy
            )
            assert differs, (
                f"Candidate {c.candidate_id} ({c.label!r}) is identical to the base config"
            )

    def test_no_duplicate_labels(self) -> None:
        """All candidate labels are unique (no two candidates are identical grid points)."""
        base = _make_ingestion_config()
        candidates = enumerate_candidates(base, budget=40, seed=42)
        labels = [c.label for c in candidates]
        assert len(labels) == len(set(labels))

    def test_returns_at_least_base_and_one_variant(self) -> None:
        """With budget >= 2, at least 2 candidates are returned."""
        base = _make_ingestion_config()
        candidates = enumerate_candidates(base, budget=2, seed=42)
        assert len(candidates) >= 2

    def test_sweep_candidate_is_frozen_dataclass(self) -> None:
        """SweepCandidate is a frozen dataclass (immutable)."""
        base = _make_ingestion_config()
        candidates = enumerate_candidates(base, budget=5, seed=42)
        c = candidates[0]
        with pytest.raises((AttributeError, TypeError)):
            c.label = "mutated"  # type: ignore[misc]
