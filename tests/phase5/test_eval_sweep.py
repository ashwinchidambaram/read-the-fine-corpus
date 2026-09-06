"""Phase 5 tests: configuration-sweep orchestration (M-046..051, §19 crit 1).

Tests:
  test_sweep_ranked_table_with_costs:
      §19 crit 1 — ranked rows carry recall/precision deltas + cost estimates;
      deterministic with FakeProvider + FakeAdapter.

  test_sweep_cost_shown_before_execution:
      M-047 — estimate_sweep_cost returns aggregate + per-candidate breakdown
      BEFORE any candidate ingests.

  test_sweep_confirmation_above_threshold:
      M-048 — estimate >= threshold + not confirmed → needs_confirmation=True,
      nothing ingested.

  test_sweep_declines_below_min_docs:
      M-050 — declined=True + reason + reference applied, no ranking rows,
      SweepRunRecord with status=declined persisted.

  test_sweep_near_optimal_no_manufactured_delta:
      M-051 — when all candidates score identically to the reference, winner
      is the reference, near_optimal=True, no fabricated delta.

  test_winner_sets_sweep_backed_provenance:
      Winner's IngestionConfig provenance list includes a sweep_backed entry.

  test_sweep_real_qdrant (integration-flagged, skip-marked):
      Real ingestion per candidate — phase-close overlay.
"""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import patch

import pytest

from finecorpus.config.models import AssessmentConfig, BudgetsConfig, Config
from finecorpus.contracts.eval_set import (
    EvalQuestion,
    GenerationMethod,
    QuestionType,
    ReviewStatus,
)
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
from finecorpus.control.eval_store import SweepRunRepository
from finecorpus.control.metadata import create_tables
from finecorpus.embedding.fake import FakeProvider
from finecorpus.index.adapter import alias_name
from finecorpus.services.eval_sweep import (
    SweepCostEstimate,
    estimate_sweep_cost,
    render_ranked_table,
    run_sweep,
)
from tests.retrieval.helpers import (
    FakeAdapter,
    FakeAliasRecord,
    FakeAliasRepository,
    make_alias_record,
    make_chunk_payload,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

KB_ID = "kb-sweep-test"
WS_ID = "ws-sweep-test"
ALIAS = alias_name(KB_ID)
COLL = f"rtfc_{KB_ID.replace('-', '').lower()}_00000001"
MODEL_ID = "fake-embed-v1"
DIMENSIONS = 64

CHK_A = "chk_a"
CHK_B = "chk_b"
CHK_C = "chk_c"

# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------


def _make_db_session():
    """Create an in-memory SQLite engine + session for control-plane tables."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    engine = create_engine("sqlite:///:memory:", echo=False)
    create_tables(engine)
    session = Session(engine)
    return engine, session


# ---------------------------------------------------------------------------
# Config/fixture helpers
# ---------------------------------------------------------------------------


def _make_config(
    *,
    sweep_min_corpus_docs: int = 10,
    sweep_candidate_budget: int = 4,
    sweep_sample_factor: int = 2,
    sweep_confirmation_threshold_usd: Decimal = Decimal("100.00"),  # high so tests don't gate
) -> Config:
    """Build a minimal Config with sweep settings."""
    cfg = Config()
    cfg.assessment = AssessmentConfig(
        sweep_min_corpus_docs=sweep_min_corpus_docs,
        sweep_candidate_budget=sweep_candidate_budget,
        sweep_sample_factor=sweep_sample_factor,
    )
    cfg.budgets = BudgetsConfig(
        sweep_confirmation_threshold_usd=sweep_confirmation_threshold_usd,
    )
    return cfg


def _tenancy() -> TenancyBlock:
    return TenancyBlock(
        workspace_id=WS_ID,
        kb_id=KB_ID,
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
        model=MODEL_ID,
        dimensions=DIMENSIONS,
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
        "model": MODEL_ID,
        "dimensions": DIMENSIONS,
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


def _make_item(doc_id: str, media_type: str = "application/pdf") -> InventoryItem:
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


def _make_corpus(n: int) -> list[InventoryItem]:
    return [_make_item(f"doc-{i:04d}") for i in range(n)]


def _make_question(
    qid: str,
    text: str = "What is X?",
    review_status: ReviewStatus = ReviewStatus.reviewed_kept,
    expected_ids: list[str] | None = None,
) -> EvalQuestion:
    return EvalQuestion(
        question_id=qid,
        text=text,
        generation_method=GenerationMethod.generated_factual,
        review_status=review_status,
        source_segment_ids=["seg-0"],
        source_unknown=False,
        question_type=QuestionType.factual_lookup,
        expected_segment_ids=expected_ids or [CHK_A],
    )


def _make_provider() -> FakeProvider:
    return FakeProvider(dimensions=DIMENSIONS, model_id=MODEL_ID)


def _make_alias_record_for_kb() -> FakeAliasRecord:
    return make_alias_record(KB_ID, model_id=MODEL_ID, dimensions=DIMENSIONS)


def _make_adapter_with_chunks() -> FakeAdapter:
    adapter = FakeAdapter()
    pts = [
        make_chunk_payload(chunk_id=CHK_A, kb_id=KB_ID, score=0.95),
        make_chunk_payload(chunk_id=CHK_B, kb_id=KB_ID, score=0.85),
        make_chunk_payload(chunk_id=CHK_C, kb_id=KB_ID, score=0.60),
    ]
    adapter.seed_collection(alias=ALIAS, coll=COLL, points=pts)
    return adapter


# ---------------------------------------------------------------------------
# Context manager for patching AliasRepository in scoring
# ---------------------------------------------------------------------------


@contextmanager
def _patch_alias_repo(alias_record: FakeAliasRecord):
    """Patch retrieval.service.AliasRepository to return alias_record."""
    from finecorpus.retrieval import service as _svc

    fake_repo = FakeAliasRepository({alias_record.alias: alias_record})
    with patch.object(_svc, "AliasRepository", return_value=fake_repo):
        yield


# ---------------------------------------------------------------------------
# test_sweep_cost_shown_before_execution (M-047)
# ---------------------------------------------------------------------------


class TestSweepCostShownBeforeExecution:
    """M-047 — estimate_sweep_cost is available and returns data BEFORE any execution."""

    def test_estimate_returns_before_any_ingestion(self) -> None:
        """estimate_sweep_cost must return without touching the index adapter."""
        engine, session = _make_db_session()
        provider = _make_provider()
        cfg = _make_config(sweep_min_corpus_docs=5, sweep_candidate_budget=3)
        base_config = _make_ingestion_config()
        documents = _make_corpus(20)

        # No adapter provided — cost estimation is purely computational
        try:
            cost_est = estimate_sweep_cost(
                KB_ID,
                base_config,
                documents,
                session=session,
                config=cfg,
                embed_caps=provider,
                llm_caps=None,
            )
        finally:
            session.close()
            engine.dispose()

        assert isinstance(cost_est, SweepCostEstimate)
        assert cost_est.n_candidates >= 1
        assert cost_est.n_sample_docs >= 1
        assert cost_est.n_sample_docs <= len(documents)
        assert isinstance(cost_est.total_est_cost_usd, Decimal)
        assert len(cost_est.per_candidate) == cost_est.n_candidates

    def test_estimate_per_candidate_entries(self) -> None:
        """Each candidate has a cost entry in per_candidate list."""
        engine, session = _make_db_session()
        provider = _make_provider()
        cfg = _make_config(sweep_min_corpus_docs=5, sweep_candidate_budget=3)
        base_config = _make_ingestion_config()
        documents = _make_corpus(20)

        try:
            cost_est = estimate_sweep_cost(
                KB_ID,
                base_config,
                documents,
                session=session,
                config=cfg,
                embed_caps=provider,
                llm_caps=None,
            )
        finally:
            session.close()
            engine.dispose()

        assert len(cost_est.per_candidate) > 0
        # Candidate 0 is always the base
        candidate_ids = [e.candidate_id for e in cost_est.per_candidate]
        assert 0 in candidate_ids
        # Labels exist
        for entry in cost_est.per_candidate:
            assert entry.label != ""

    def test_estimate_total_is_sum_of_per_candidate(self) -> None:
        """Total cost = sum of per-candidate costs."""
        engine, session = _make_db_session()
        provider = _make_provider()
        cfg = _make_config(sweep_min_corpus_docs=5, sweep_candidate_budget=3)
        base_config = _make_ingestion_config()
        documents = _make_corpus(20)

        try:
            cost_est = estimate_sweep_cost(
                KB_ID,
                base_config,
                documents,
                session=session,
                config=cfg,
                embed_caps=provider,
                llm_caps=None,
            )
        finally:
            session.close()
            engine.dispose()

        computed_total = sum(e.est_cost_usd for e in cost_est.per_candidate)
        assert cost_est.total_est_cost_usd == computed_total


# ---------------------------------------------------------------------------
# test_sweep_confirmation_above_threshold (M-048)
# ---------------------------------------------------------------------------


class TestSweepConfirmationAboveThreshold:
    """M-048 — when estimate >= threshold + not confirmed → needs_confirmation, nothing ingested."""

    def test_returns_needs_confirmation_when_cost_exceeds_threshold(self) -> None:
        """With threshold=$0.00 and not confirmed, run_sweep must need confirmation."""
        engine, session = _make_db_session()
        adapter = _make_adapter_with_chunks()
        provider = _make_provider()
        alias_record = _make_alias_record_for_kb()
        # Zero threshold so any cost triggers the gate
        cfg = _make_config(
            sweep_min_corpus_docs=5,
            sweep_candidate_budget=3,
            sweep_confirmation_threshold_usd=Decimal("0.00"),
        )
        base_config = _make_ingestion_config()
        documents = _make_corpus(20)
        questions = [_make_question("q1")]

        try:
            with _patch_alias_repo(alias_record):
                result = run_sweep(
                    KB_ID,
                    base_config,
                    documents,
                    questions,
                    session=session,
                    config=cfg,
                    adapter=adapter,
                    provider=provider,
                    confirmed=False,
                    workspace_id=WS_ID,
                )
        finally:
            session.close()
            engine.dispose()

        assert result.needs_confirmation is True
        assert result.declined is False
        assert result.ranked_rows == []
        assert result.estimate is not None

    def test_no_ingestion_when_needs_confirmation(self) -> None:
        """When needs_confirmation=True, the adapter was not called for ingestion."""
        engine, session = _make_db_session()
        adapter = FakeAdapter()  # empty — no pre-seeded chunks
        provider = _make_provider()
        cfg = _make_config(
            sweep_min_corpus_docs=5,
            sweep_candidate_budget=3,
            sweep_confirmation_threshold_usd=Decimal("0.00"),
        )
        base_config = _make_ingestion_config()
        documents = _make_corpus(20)
        questions = [_make_question("q1")]

        try:
            result = run_sweep(
                KB_ID,
                base_config,
                documents,
                questions,
                session=session,
                config=cfg,
                adapter=adapter,
                provider=provider,
                confirmed=False,
                workspace_id=WS_ID,
            )
        finally:
            session.close()
            engine.dispose()

        assert result.needs_confirmation is True
        # Adapter should not have been used for ingestion (only for scoring)
        assert adapter.search_call_count == 0

    def test_confirmed_bypasses_threshold_gate(self) -> None:
        """With confirmed=True, even a zero-threshold run proceeds past M-048."""
        engine, session = _make_db_session()
        adapter = _make_adapter_with_chunks()
        provider = _make_provider()
        alias_record = _make_alias_record_for_kb()
        cfg = _make_config(
            sweep_min_corpus_docs=5,
            sweep_candidate_budget=3,
            sweep_confirmation_threshold_usd=Decimal("0.00"),
        )
        base_config = _make_ingestion_config()
        documents = _make_corpus(20)
        questions = [_make_question("q1")]

        try:
            with _patch_alias_repo(alias_record):
                result = run_sweep(
                    KB_ID,
                    base_config,
                    documents,
                    questions,
                    session=session,
                    config=cfg,
                    adapter=adapter,
                    provider=provider,
                    confirmed=True,
                    workspace_id=WS_ID,
                )
        finally:
            session.close()
            engine.dispose()

        # Should not return needs_confirmation when confirmed=True
        assert result.needs_confirmation is False


# ---------------------------------------------------------------------------
# test_sweep_declines_below_min_docs (M-050)
# ---------------------------------------------------------------------------


class TestSweepDeclinesBelowMinDocs:
    """M-050 — corpus < sweep_min_corpus_docs → declined, reason, reference applied."""

    def test_declined_when_corpus_too_small(self) -> None:
        """With 3 documents and min=50, run_sweep declines."""
        engine, session = _make_db_session()
        adapter = FakeAdapter()
        provider = _make_provider()
        cfg = _make_config(sweep_min_corpus_docs=50)
        base_config = _make_ingestion_config()
        documents = _make_corpus(3)  # far below min
        questions = [_make_question("q1")]

        try:
            result = run_sweep(
                KB_ID,
                base_config,
                documents,
                questions,
                session=session,
                config=cfg,
                adapter=adapter,
                provider=provider,
                confirmed=True,
                workspace_id=WS_ID,
            )
        finally:
            session.close()
            engine.dispose()

        assert result.declined is True
        assert len(result.reason) > 0
        assert result.ranked_rows == []

    def test_decline_has_plain_language_reason(self) -> None:
        """Decline reason must mention doc count and minimum threshold."""
        engine, session = _make_db_session()
        adapter = FakeAdapter()
        provider = _make_provider()
        cfg = _make_config(sweep_min_corpus_docs=50)
        base_config = _make_ingestion_config()
        documents = _make_corpus(3)
        questions = [_make_question("q1")]

        try:
            result = run_sweep(
                KB_ID,
                base_config,
                documents,
                questions,
                session=session,
                config=cfg,
                adapter=adapter,
                provider=provider,
                confirmed=True,
                workspace_id=WS_ID,
            )
        finally:
            session.close()
            engine.dispose()

        assert "3" in result.reason  # doc count
        assert "50" in result.reason  # min threshold

    def test_decline_applies_reference_config(self) -> None:
        """applied is set to the base_config (reference) when declined."""
        engine, session = _make_db_session()
        adapter = FakeAdapter()
        provider = _make_provider()
        cfg = _make_config(sweep_min_corpus_docs=50)
        base_config = _make_ingestion_config()
        documents = _make_corpus(3)
        questions = []

        try:
            result = run_sweep(
                KB_ID,
                base_config,
                documents,
                questions,
                session=session,
                config=cfg,
                adapter=adapter,
                provider=provider,
                confirmed=True,
                workspace_id=WS_ID,
            )
        finally:
            session.close()
            engine.dispose()

        assert result.applied is not None
        assert isinstance(result.applied, IngestionConfig)

    def test_decline_persists_sweep_run_record(self) -> None:
        """A declined sweep still persists a SweepRunRecord with status=declined."""
        engine, session = _make_db_session()
        adapter = FakeAdapter()
        provider = _make_provider()
        cfg = _make_config(sweep_min_corpus_docs=50)
        base_config = _make_ingestion_config()
        documents = _make_corpus(3)
        questions = []

        try:
            result = run_sweep(
                KB_ID,
                base_config,
                documents,
                questions,
                session=session,
                config=cfg,
                adapter=adapter,
                provider=provider,
                confirmed=True,
                workspace_id=WS_ID,
            )
            # Verify the DB record
            sweep_repo = SweepRunRepository(session)
            runs = sweep_repo.list_for_kb(KB_ID)
        finally:
            session.close()
            engine.dispose()

        assert result.sweep_run_id != ""
        assert len(runs) == 1
        assert runs[0].status == "declined"
        assert runs[0].declined_reason is not None

    def test_no_ranking_rows_when_declined(self) -> None:
        """No ranking rows are generated when the sweep is declined."""
        engine, session = _make_db_session()
        adapter = FakeAdapter()
        provider = _make_provider()
        cfg = _make_config(sweep_min_corpus_docs=50)
        base_config = _make_ingestion_config()
        documents = _make_corpus(3)
        questions = []

        try:
            result = run_sweep(
                KB_ID,
                base_config,
                documents,
                questions,
                session=session,
                config=cfg,
                adapter=adapter,
                provider=provider,
                confirmed=True,
                workspace_id=WS_ID,
            )
            sweep_repo = SweepRunRepository(session)
            ranking = sweep_repo.get_ranking(result.sweep_run_id)
        finally:
            session.close()
            engine.dispose()

        assert result.ranked_rows == []
        assert ranking == []


# ---------------------------------------------------------------------------
# test_sweep_ranked_table_with_costs (§19 crit 1)
# ---------------------------------------------------------------------------


class TestSweepRankedTableWithCosts:
    """§19 acceptance criterion 1 — ranked table with recall/precision deltas + cost estimates."""

    def test_ranked_rows_present_after_sweep(self) -> None:
        """After a successful sweep, ranked_rows is non-empty."""
        engine, session = _make_db_session()
        adapter = _make_adapter_with_chunks()
        provider = _make_provider()
        alias_record = _make_alias_record_for_kb()
        cfg = _make_config(sweep_min_corpus_docs=5, sweep_candidate_budget=3)
        base_config = _make_ingestion_config()
        documents = _make_corpus(20)
        questions = [_make_question("q1")]

        try:
            with _patch_alias_repo(alias_record):
                result = run_sweep(
                    KB_ID,
                    base_config,
                    documents,
                    questions,
                    session=session,
                    config=cfg,
                    adapter=adapter,
                    provider=provider,
                    confirmed=True,
                    workspace_id=WS_ID,
                )
        finally:
            session.close()
            engine.dispose()

        assert result.declined is False
        assert result.needs_confirmation is False
        assert len(result.ranked_rows) > 0

    def test_ranked_rows_have_rank_1_through_n(self) -> None:
        """Ranked rows are 1-indexed and contiguous."""
        engine, session = _make_db_session()
        adapter = _make_adapter_with_chunks()
        provider = _make_provider()
        alias_record = _make_alias_record_for_kb()
        cfg = _make_config(sweep_min_corpus_docs=5, sweep_candidate_budget=3)
        base_config = _make_ingestion_config()
        documents = _make_corpus(20)
        questions = [_make_question("q1")]

        try:
            with _patch_alias_repo(alias_record):
                result = run_sweep(
                    KB_ID,
                    base_config,
                    documents,
                    questions,
                    session=session,
                    config=cfg,
                    adapter=adapter,
                    provider=provider,
                    confirmed=True,
                    workspace_id=WS_ID,
                )
        finally:
            session.close()
            engine.dispose()

        ranks = sorted(r.rank for r in result.ranked_rows)
        assert ranks == list(range(1, len(result.ranked_rows) + 1))

    def test_ranked_rows_carry_cost_estimates(self) -> None:
        """Each ranked row has an est_cost_usd field (Decimal)."""
        engine, session = _make_db_session()
        adapter = _make_adapter_with_chunks()
        provider = _make_provider()
        alias_record = _make_alias_record_for_kb()
        cfg = _make_config(sweep_min_corpus_docs=5, sweep_candidate_budget=3)
        base_config = _make_ingestion_config()
        documents = _make_corpus(20)
        questions = [_make_question("q1")]

        try:
            with _patch_alias_repo(alias_record):
                result = run_sweep(
                    KB_ID,
                    base_config,
                    documents,
                    questions,
                    session=session,
                    config=cfg,
                    adapter=adapter,
                    provider=provider,
                    confirmed=True,
                    workspace_id=WS_ID,
                )
        finally:
            session.close()
            engine.dispose()

        for row in result.ranked_rows:
            assert isinstance(row.est_cost_usd, Decimal)
            assert row.est_cost_usd >= Decimal("0")

    def test_ranked_rows_carry_recall_precision_deltas(self) -> None:
        """Each ranked row has recall_delta and precision_delta fields."""
        engine, session = _make_db_session()
        adapter = _make_adapter_with_chunks()
        provider = _make_provider()
        alias_record = _make_alias_record_for_kb()
        cfg = _make_config(sweep_min_corpus_docs=5, sweep_candidate_budget=3)
        base_config = _make_ingestion_config()
        documents = _make_corpus(20)
        questions = [_make_question("q1")]

        try:
            with _patch_alias_repo(alias_record):
                result = run_sweep(
                    KB_ID,
                    base_config,
                    documents,
                    questions,
                    session=session,
                    config=cfg,
                    adapter=adapter,
                    provider=provider,
                    confirmed=True,
                    workspace_id=WS_ID,
                )
        finally:
            session.close()
            engine.dispose()

        for row in result.ranked_rows:
            assert isinstance(row.recall_delta, float)
            assert isinstance(row.precision_delta, float)

    def test_reference_candidate_delta_is_zero(self) -> None:
        """The reference candidate (is_reference=True) has recall_delta=0 and precision_delta=0."""
        engine, session = _make_db_session()
        adapter = _make_adapter_with_chunks()
        provider = _make_provider()
        alias_record = _make_alias_record_for_kb()
        cfg = _make_config(sweep_min_corpus_docs=5, sweep_candidate_budget=3)
        base_config = _make_ingestion_config()
        documents = _make_corpus(20)
        questions = [_make_question("q1")]

        try:
            with _patch_alias_repo(alias_record):
                result = run_sweep(
                    KB_ID,
                    base_config,
                    documents,
                    questions,
                    session=session,
                    config=cfg,
                    adapter=adapter,
                    provider=provider,
                    confirmed=True,
                    workspace_id=WS_ID,
                )
        finally:
            session.close()
            engine.dispose()

        ref_rows = [r for r in result.ranked_rows if r.is_reference]
        assert len(ref_rows) == 1
        ref = ref_rows[0]
        assert ref.recall_delta == pytest.approx(0.0)
        assert ref.precision_delta == pytest.approx(0.0)

    def test_render_ranked_table_produces_text(self) -> None:
        """render_ranked_table returns a non-empty string."""
        engine, session = _make_db_session()
        adapter = _make_adapter_with_chunks()
        provider = _make_provider()
        alias_record = _make_alias_record_for_kb()
        cfg = _make_config(sweep_min_corpus_docs=5, sweep_candidate_budget=3)
        base_config = _make_ingestion_config()
        documents = _make_corpus(20)
        questions = [_make_question("q1")]

        try:
            with _patch_alias_repo(alias_record):
                result = run_sweep(
                    KB_ID,
                    base_config,
                    documents,
                    questions,
                    session=session,
                    config=cfg,
                    adapter=adapter,
                    provider=provider,
                    confirmed=True,
                    workspace_id=WS_ID,
                )
        finally:
            session.close()
            engine.dispose()

        table = render_ranked_table(result)
        assert len(table) > 0
        assert "Rank" in table or "rank" in table.lower()

    def test_sweep_persists_ranking_rows_in_db(self) -> None:
        """SweepRankingRow rows are persisted to the control-plane DB."""
        engine, session = _make_db_session()
        adapter = _make_adapter_with_chunks()
        provider = _make_provider()
        alias_record = _make_alias_record_for_kb()
        cfg = _make_config(sweep_min_corpus_docs=5, sweep_candidate_budget=3)
        base_config = _make_ingestion_config()
        documents = _make_corpus(20)
        questions = [_make_question("q1")]

        try:
            with _patch_alias_repo(alias_record):
                result = run_sweep(
                    KB_ID,
                    base_config,
                    documents,
                    questions,
                    session=session,
                    config=cfg,
                    adapter=adapter,
                    provider=provider,
                    confirmed=True,
                    workspace_id=WS_ID,
                )
            sweep_repo = SweepRunRepository(session)
            db_rows = sweep_repo.get_ranking(result.sweep_run_id)
        finally:
            session.close()
            engine.dispose()

        assert len(db_rows) == len(result.ranked_rows)
        for row in db_rows:
            assert row.recall >= 0.0
            assert row.precision >= 0.0


# ---------------------------------------------------------------------------
# test_sweep_near_optimal_no_manufactured_delta (M-051)
# ---------------------------------------------------------------------------


class TestSweepNearOptimalNoManufacturedDelta:
    """M-051 — when all candidates score like the reference, winner IS the reference."""

    def test_near_optimal_flag_set_when_no_improvement(self) -> None:
        """When all candidates score identically, near_optimal=True."""
        engine, session = _make_db_session()
        # Use an adapter with no pre-seeded chunks → all candidates score 0 recall
        adapter = FakeAdapter()
        provider = _make_provider()
        cfg = _make_config(
            sweep_min_corpus_docs=5,
            sweep_candidate_budget=3,
            sweep_confirmation_threshold_usd=Decimal("100.00"),
        )
        base_config = _make_ingestion_config()
        documents = _make_corpus(20)
        # Single question with expected_ids that won't match (all candidates recall = 0)
        questions = [_make_question("q1", expected_ids=[CHK_A, CHK_B])]

        try:
            result = run_sweep(
                KB_ID,
                base_config,
                documents,
                questions,
                session=session,
                config=cfg,
                adapter=adapter,
                provider=provider,
                confirmed=True,
                workspace_id=WS_ID,
                noise_margin=0.02,
            )
        finally:
            session.close()
            engine.dispose()

        # All candidates have recall=0, reference has recall=0, no one beats 0 + margin
        assert result.near_optimal is True

    def test_winner_is_reference_when_near_optimal(self) -> None:
        """When near_optimal=True, winner_config is set and recall_delta for rank-1 is 0."""
        engine, session = _make_db_session()
        adapter = FakeAdapter()
        provider = _make_provider()
        cfg = _make_config(
            sweep_min_corpus_docs=5,
            sweep_candidate_budget=3,
            sweep_confirmation_threshold_usd=Decimal("100.00"),
        )
        base_config = _make_ingestion_config()
        documents = _make_corpus(20)
        questions = [_make_question("q1", expected_ids=[CHK_A])]

        try:
            result = run_sweep(
                KB_ID,
                base_config,
                documents,
                questions,
                session=session,
                config=cfg,
                adapter=adapter,
                provider=provider,
                confirmed=True,
                workspace_id=WS_ID,
                noise_margin=0.02,
            )
        finally:
            session.close()
            engine.dispose()

        assert result.near_optimal is True
        assert result.winner_config is not None

    def test_no_positive_delta_manufactured_when_near_optimal(self) -> None:
        """When near_optimal=True, no ranked row shows a positive recall_delta > noise_margin."""
        engine, session = _make_db_session()
        adapter = FakeAdapter()  # empty — all candidates score 0
        provider = _make_provider()
        cfg = _make_config(
            sweep_min_corpus_docs=5,
            sweep_candidate_budget=3,
            sweep_confirmation_threshold_usd=Decimal("100.00"),
        )
        base_config = _make_ingestion_config()
        documents = _make_corpus(20)
        questions = [_make_question("q1", expected_ids=[CHK_A])]
        noise_margin = 0.02

        try:
            result = run_sweep(
                KB_ID,
                base_config,
                documents,
                questions,
                session=session,
                config=cfg,
                adapter=adapter,
                provider=provider,
                confirmed=True,
                workspace_id=WS_ID,
                noise_margin=noise_margin,
            )
        finally:
            session.close()
            engine.dispose()

        # No non-reference row should have recall_delta > noise_margin
        for row in result.ranked_rows:
            if not row.is_reference:
                assert row.recall_delta <= noise_margin

    def test_render_ranked_table_shows_near_optimal_note(self) -> None:
        """render_ranked_table includes an M-051 note when near_optimal=True."""
        engine, session = _make_db_session()
        adapter = FakeAdapter()
        provider = _make_provider()
        cfg = _make_config(
            sweep_min_corpus_docs=5,
            sweep_candidate_budget=3,
            sweep_confirmation_threshold_usd=Decimal("100.00"),
        )
        base_config = _make_ingestion_config()
        documents = _make_corpus(20)
        questions = [_make_question("q1", expected_ids=[CHK_A])]

        try:
            result = run_sweep(
                KB_ID,
                base_config,
                documents,
                questions,
                session=session,
                config=cfg,
                adapter=adapter,
                provider=provider,
                confirmed=True,
                workspace_id=WS_ID,
                noise_margin=0.02,
            )
        finally:
            session.close()
            engine.dispose()

        table = render_ranked_table(result)
        assert "M-051" in table or "near-optimal" in table.lower()


# ---------------------------------------------------------------------------
# test_winner_sets_sweep_backed_provenance
# ---------------------------------------------------------------------------


class TestWinnerSetsSweepBackedProvenance:
    """Winner's IngestionConfig has a sweep_backed provenance entry."""

    def test_winner_config_has_sweep_backed_provenance(self) -> None:
        """After a sweep, winner_config.provenance includes a sweep_backed entry."""
        engine, session = _make_db_session()
        adapter = _make_adapter_with_chunks()
        provider = _make_provider()
        alias_record = _make_alias_record_for_kb()
        cfg = _make_config(sweep_min_corpus_docs=5, sweep_candidate_budget=3)
        base_config = _make_ingestion_config()
        documents = _make_corpus(20)
        questions = [_make_question("q1")]

        try:
            with _patch_alias_repo(alias_record):
                result = run_sweep(
                    KB_ID,
                    base_config,
                    documents,
                    questions,
                    session=session,
                    config=cfg,
                    adapter=adapter,
                    provider=provider,
                    confirmed=True,
                    workspace_id=WS_ID,
                )
        finally:
            session.close()
            engine.dispose()

        assert result.winner_config is not None
        winner_prov = result.winner_config.provenance
        sweep_backed_entries = [
            p for p in winner_prov if p.basis == RecommendationBasis.sweep_backed
        ]
        assert len(sweep_backed_entries) >= 1

    def test_winner_provenance_links_to_sweep_run_id(self) -> None:
        """sweep_backed provenance entry references the sweep_run_id."""
        engine, session = _make_db_session()
        adapter = _make_adapter_with_chunks()
        provider = _make_provider()
        alias_record = _make_alias_record_for_kb()
        cfg = _make_config(sweep_min_corpus_docs=5, sweep_candidate_budget=3)
        base_config = _make_ingestion_config()
        documents = _make_corpus(20)
        questions = [_make_question("q1")]

        try:
            with _patch_alias_repo(alias_record):
                result = run_sweep(
                    KB_ID,
                    base_config,
                    documents,
                    questions,
                    session=session,
                    config=cfg,
                    adapter=adapter,
                    provider=provider,
                    confirmed=True,
                    workspace_id=WS_ID,
                )
        finally:
            session.close()
            engine.dispose()

        assert result.sweep_run_id != ""
        winner_prov = result.winner_config.provenance
        sweep_backed_entry = next(
            (p for p in winner_prov if p.basis == RecommendationBasis.sweep_backed),
            None,
        )
        assert sweep_backed_entry is not None
        assert sweep_backed_entry.sweep_run_id == result.sweep_run_id


# ---------------------------------------------------------------------------
# Integration-flagged test (skip-marked — phase-close overlay)
# ---------------------------------------------------------------------------


@pytest.mark.skip(reason="Integration test — requires real Qdrant; phase-close overlay")
class TestSweepRealQdrant:
    """Real ingestion per candidate — phase-close overlay (integration-flagged)."""

    def test_sweep_real_qdrant(self) -> None:
        """Run a full sweep against a real Qdrant instance.

        Requires:
        - QDRANT_URL env var or default http://localhost:6333
        - A running Qdrant instance with the given collection/alias
        """
        import os

        from finecorpus.index.qdrant.backend import QdrantAdapter

        qdrant_url = os.environ.get("QDRANT_URL", "http://localhost:6333")
        adapter = QdrantAdapter(url=qdrant_url)
        provider = _make_provider()
        engine, session = _make_db_session()
        cfg = _make_config(sweep_min_corpus_docs=5, sweep_candidate_budget=3)
        base_config = _make_ingestion_config()
        documents = _make_corpus(20)
        questions = [_make_question("q1")]

        try:
            result = run_sweep(
                KB_ID,
                base_config,
                documents,
                questions,
                session=session,
                config=cfg,
                adapter=adapter,
                provider=provider,
                confirmed=True,
                workspace_id=WS_ID,
            )
        finally:
            session.close()
            engine.dispose()

        assert not result.declined
        assert not result.needs_confirmation
        assert len(result.ranked_rows) > 0
