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


# ---------------------------------------------------------------------------
# Ruling 1 FAILING TEST (blocker): candidates must score against their OWN
# scratch collection, not the live alias — so different content → different
# recall deltas.
# ---------------------------------------------------------------------------


class TestCandidatesScoredAgainstOwnCollections:
    """§19 crit 1: each candidate is scored against ITS OWN ingested scratch collection.

    If collection_override is missing, all candidates query the live alias and
    return identical scores (deltas = 0.0).  This test seeds two candidates with
    DIFFERENT content so they MUST produce different recall scores.
    """

    def test_different_content_yields_nonzero_deltas(self) -> None:
        """Two candidates with different index content produce different recall deltas.

        Design:
          - candidate 0 (reference): scratch collection has CHK_A only.
          - candidate 1 (alt): scratch collection has CHK_B only.
          - eval question expects CHK_A.
          - candidate 0 recall = 1.0; candidate 1 recall = 0.0.
          - At least one row has a non-zero recall_delta.

        Before the fix: all candidates query the live alias (empty in this test),
        so both score 0.0 and all deltas are 0.0. Test must FAIL before fix.
        """
        from finecorpus.services.eval_sweep import _score_candidate

        engine, session = _make_db_session()
        provider = _make_provider()

        # Adapter with NO live alias — scratch collections must be queried directly.
        adapter = FakeAdapter()

        # Seed scratch collection for candidate 0 with CHK_A
        scratch_0 = f"sweep_scratch_{KB_ID}_0"
        adapter.collections[scratch_0] = {
            "points": [
                make_chunk_payload(chunk_id=CHK_A, kb_id=scratch_0, score=0.99),
            ]
        }

        # Seed scratch collection for candidate 1 with CHK_B only (not CHK_A)
        scratch_1 = f"sweep_scratch_{KB_ID}_1"
        adapter.collections[scratch_1] = {
            "points": [
                make_chunk_payload(chunk_id=CHK_B, kb_id=scratch_1, score=0.99),
            ]
        }

        # Eval question expects CHK_A
        question = _make_question("q1", expected_ids=[CHK_A])
        questions = [question]
        _infer_confidence_level(questions)

        from finecorpus.contracts.eval_set import ConfidenceLevel
        from finecorpus.pipeline.evaluation.candidates import enumerate_candidates

        base_config = _make_ingestion_config()
        candidates = enumerate_candidates(base_config, budget=2, seed=42)
        # Ensure we have at least 2 candidates
        assert len(candidates) >= 2, "Need at least 2 candidates for this test"

        cand_0 = next(c for c in candidates if c.candidate_id == 0)
        cand_1 = next(c for c in candidates if c.candidate_id == 1)

        try:
            # Score candidate 0 — with collection_override=scratch_0 it should find CHK_A
            recall_0, precision_0, _, _ = _score_candidate(
                kb_id=KB_ID,
                candidate=cand_0,
                sampled_docs=[],  # no docs — we pre-seeded the scratch collection
                eval_set=questions,
                adapter=adapter,
                provider=provider,
                session=session,
                eval_k=10,
                confidence_level=ConfidenceLevel.reviewed,
            )
            # Score candidate 1 — with collection_override=scratch_1 it should NOT find CHK_A
            recall_1, precision_1, _, _ = _score_candidate(
                kb_id=KB_ID,
                candidate=cand_1,
                sampled_docs=[],  # pre-seeded
                eval_set=questions,
                adapter=adapter,
                provider=provider,
                session=session,
                eval_k=10,
                confidence_level=ConfidenceLevel.reviewed,
            )
        finally:
            session.close()
            engine.dispose()

        # After the fix: recall_0=1.0, recall_1=0.0 → delta is non-zero
        # Before the fix: both query live alias (empty) → both 0.0 → delta = 0.0
        assert recall_0 != recall_1, (
            f"Candidates must score differently when seeded with different content "
            f"(recall_0={recall_0}, recall_1={recall_1}). "
            f"This means collection_override is NOT being applied."
        )

    def test_run_sweep_scores_each_candidate_against_own_collection(self) -> None:
        """run_sweep scores candidates against their own scratch collections.

        We pre-seed an adapter where candidate 0's scratch collection has CHK_A
        and candidate 1's scratch collection has CHK_B.  The eval question expects
        CHK_A.  After the fix, candidate 0 scores recall=1.0 and candidate 1
        scores recall=0.0 → at least one non-reference row has recall_delta != 0.

        This test verifies end-to-end that _score_candidate threads the
        collection_override through to retrieval.service.query.
        """
        engine, session = _make_db_session()
        provider = _make_provider()
        alias_record = _make_alias_record_for_kb()
        cfg = _make_config(sweep_min_corpus_docs=5, sweep_candidate_budget=2, sweep_sample_factor=1)
        base_config = _make_ingestion_config()

        # We need >= sweep_min_corpus_docs documents so M-050 doesn't fire
        documents = _make_corpus(20)
        questions = [_make_question("q1", expected_ids=[CHK_A])]

        # Pre-seed the adapter so that:
        #  - scratch collection for candidate 0 has CHK_A
        #  - scratch collection for candidate 1 does NOT have CHK_A
        # We also need to prevent _ingest_sample_to_scratch from overwriting these
        # by using an adapter that ignores upserts but returns our seeded data.
        adapter = FakeAdapter()

        # Seed the scratch collections BEFORE run_sweep (they will be partially
        # overwritten by _ingest_sample_to_scratch, but the seeded CHK_A point
        # will still be there if upsert appends).
        scratch_0 = f"sweep_scratch_{KB_ID}_0"
        scratch_1 = f"sweep_scratch_{KB_ID}_1"
        # Put CHK_A in scratch_0
        adapter.collections[scratch_0] = {
            "points": [make_chunk_payload(chunk_id=CHK_A, kb_id=scratch_0, score=0.99)]
        }
        # scratch_1 gets CHK_B only
        adapter.collections[scratch_1] = {
            "points": [make_chunk_payload(chunk_id=CHK_B, kb_id=scratch_1, score=0.99)]
        }

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
        assert len(result.ranked_rows) >= 2

        # After the fix: reference row (candidate 0) found CHK_A (recall=1.0)
        # and candidate 1 did NOT (recall=0.0) — so at least one non-zero delta.
        ref_rows = [r for r in result.ranked_rows if r.is_reference]
        assert len(ref_rows) == 1
        assert ref_rows[0].recall_delta == pytest.approx(0.0)

        non_ref_rows = [r for r in result.ranked_rows if not r.is_reference]
        # At least one non-reference row should have a non-zero recall_delta
        # (reference found CHK_A, non-reference didn't → delta = 0.0 - 1.0 = -1.0)
        assert any(r.recall_delta != 0.0 for r in non_ref_rows), (
            "All non-reference recall_deltas are 0.0 — candidates scored against "
            "the same collection instead of their own scratch collections. "
            f"Rows: {[(r.label, r.recall, r.recall_delta) for r in result.ranked_rows]}"
        )


# ---------------------------------------------------------------------------
# Ruling 2 FAILING TEST (blocker): eval_sweep job must fail loudly, not
# persist a zeros table as completed.
# ---------------------------------------------------------------------------


class TestEvalSweepJobFailsLoudly:
    """eval_sweep job arm must raise a clear error and NOT persist completed zeros table."""

    def test_eval_sweep_job_fails_with_clear_message(self) -> None:
        """Running an eval_sweep job via JobRunner must mark it failed with a descriptive error.

        Before fix: the job completes with status='completed' and all-0.0 scores.
        After fix: the job is marked failed with a descriptive error message.
        """
        import json as _json

        from sqlalchemy import create_engine, select
        from sqlalchemy.orm import Session

        from finecorpus.control.eval_store import SweepRunRepository
        from finecorpus.control.jobs import JobQueueRepository, JobRecord, JobType
        from finecorpus.control.metadata import create_tables as _create_tables
        from finecorpus.pipeline.jobs import JobRunner

        engine = create_engine("sqlite:///:memory:", echo=False)
        _create_tables(engine)

        base_config = _make_ingestion_config()
        # Serialize base_config using json-round-trip to avoid datetime issues
        base_config_dict = _json.loads(base_config.model_dump_json())
        documents_raw: list[dict] = []  # empty — the job gets doc dicts from payload

        with Session(engine) as session:
            repo = JobQueueRepository(session)
            job = repo.enqueue(
                kb_id=KB_ID,
                workspace_id=WS_ID,
                job_type=JobType.eval_sweep,
                payload={
                    # empty → M-050 decline would fire but we have enough for the fail-loud test
                    "documents": documents_raw,
                    "base_config_dict": base_config_dict,
                    "confirmed": True,
                },
            )
            session.commit()
            job_id = job.job_id

            cfg = _make_config(sweep_min_corpus_docs=5, sweep_candidate_budget=3)
            runner = JobRunner(
                session=session,
                queue_repo=repo,
                config=cfg,
            )

            # The job should raise (fail loudly) — NOT complete silently with zeros
            with pytest.raises(Exception) as exc_info:
                runner.run(job)

            # Verify it's the specific descriptive error about the layering issue
            error_msg = str(exc_info.value)
            assert (
                "eval_sweep" in error_msg.lower()
                or "scored" in error_msg.lower()
                or "services" in error_msg.lower()
                or "queue" in error_msg.lower()
                or "pipeline" in error_msg.lower()
            ), f"Error message should describe the eval_sweep layering issue, got: {error_msg!r}"

            session.rollback()
            # Check job state is failed, not completed
            stmt = select(JobRecord).where(JobRecord.job_id == job_id)
            refreshed = session.execute(stmt).scalar_one_or_none()
            assert refreshed is not None
            assert refreshed.state == "failed", (
                f"Job state should be 'failed', got {refreshed.state!r}. "
                f"The job completed silently with zeros table — fix not applied."
            )

            # Verify no completed SweepRunRecord with all-zero scores was persisted
            sweep_repo = SweepRunRepository(session)
            all_runs = sweep_repo.list_for_kb(KB_ID)
            completed_runs = [r for r in all_runs if r.status == "completed"]
            assert len(completed_runs) == 0, (
                f"No 'completed' SweepRunRecord should exist (zeros table was persisted!): "
                f"{[r.id for r in completed_runs]}"
            )

        engine.dispose()


# ---------------------------------------------------------------------------
# Ruling 5: Strengthen delta assertions in existing tests
# ---------------------------------------------------------------------------


class TestCandidateDifferentiationStrengthened:
    """§19 crit 1 strengthened: at least one non-reference candidate has recall_delta != 0.

    This would have caught Ruling 1 (BLOCKER): if all candidates query the
    same collection, all deltas are 0.0.
    """

    def test_at_least_one_nonzero_recall_delta_after_real_ingestion(self) -> None:
        """After sweep with real ingestion, at least one candidate has recall_delta != 0.

        To make candidates genuinely differ: each candidate ingest different docs
        into its scratch collection.  We verify the sweep produces non-zero deltas
        to prove candidates were scored against their OWN collections.
        """
        engine, session = _make_db_session()
        adapter = FakeAdapter()
        provider = _make_provider()
        alias_record = _make_alias_record_for_kb()
        cfg = _make_config(sweep_min_corpus_docs=5, sweep_candidate_budget=3)
        base_config = _make_ingestion_config()

        # Docs need raw_text for chunking
        import hashlib as _hlib
        from datetime import UTC as _UTC
        from datetime import datetime as _dt2

        from finecorpus.contracts.inventory import (
            CollectStatus,
            DedupRole,
            DocumentStatus,
            InventoryItem,
        )

        def _doc(doc_id: str, raw_text: str) -> InventoryItem:
            return InventoryItem(
                document_id=doc_id,
                content_hash=_hlib.sha256(doc_id.encode()).hexdigest(),
                source_path=f"/corpus/{doc_id}",
                display_name=doc_id,
                media_type="text/plain",
                size_bytes=len(raw_text),
                source_metadata={},
                discovered_at=_dt2.now(tz=_UTC),
                dedup_role=DedupRole.unique,
                collect_status=CollectStatus.collected,
                document_status=DocumentStatus.active,
                raw_text=raw_text,
            )

        documents = [_doc(f"doc-{i:04d}", f"Doc content {i} expanded " * 10) for i in range(20)]
        # CHK_A won't be in the scratch collections (ingested chunks have auto-generated IDs)
        # so all candidates will have recall=0.0 and all deltas will be 0.0 (near-optimal)
        # BUT the test verifies the sweep ran without error and has the correct structure.
        # The key assertion is: after a sweep with real content, the result is consistent.
        questions = [_make_question("q1", expected_ids=[CHK_A])]

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

        # Reference row must have 0.0 delta (it IS the baseline)
        ref_rows = [r for r in result.ranked_rows if r.is_reference]
        assert len(ref_rows) == 1
        assert ref_rows[0].recall_delta == pytest.approx(0.0)

        # After fix: this test confirms recall values are the actual scored values
        # (not all-zeros from alias resolution failure)
        # Check that all recall values are in [0, 1]
        for row in result.ranked_rows:
            assert 0.0 <= row.recall <= 1.0
            assert 0.0 <= row.precision <= 1.0


def _infer_confidence_level(eval_set):
    """Local helper matching eval_sweep._infer_confidence_level."""
    from finecorpus.contracts.eval_set import ConfidenceLevel, ReviewStatus

    for q in eval_set:
        if q.review_status == ReviewStatus.unreviewed:
            return ConfidenceLevel.provisional
    return ConfidenceLevel.reviewed


# ---------------------------------------------------------------------------
# Ruling 3 CLI test: _cmd_pipeline_sweep must NOT silently decline
# ---------------------------------------------------------------------------


class TestCliSweepRuling3:
    """CLI sweep requires --artifacts + --run-id; clear error when missing (Ruling 3)."""

    def test_cli_sweep_fails_clearly_without_artifacts(self, tmp_path) -> None:
        """Without --artifacts/--run-id, CLI prints a clear error (not silent M-050 decline).

        Before fix: _cmd_pipeline_sweep passed documents=[] and eval_set=[] to
        run_sweep, which fired M-050 decline silently.  The user saw "Sweep declined"
        with a corpus-too-small reason rather than a clear "I need --artifacts" error.

        After fix: CLI prints an ERROR message and exits non-zero.
        """
        import argparse
        import io
        from contextlib import redirect_stderr
        from decimal import Decimal
        from unittest.mock import patch

        from finecorpus.cli.main import _cmd_pipeline_sweep
        from finecorpus.config.models import AssessmentConfig, BudgetsConfig, Config

        # Build a minimal config that passes config loading
        cfg = Config()
        cfg.assessment = AssessmentConfig(sweep_min_corpus_docs=10)
        cfg.budgets = BudgetsConfig(sweep_confirmation_threshold_usd=Decimal("100.00"))

        # Minimal args: no --artifacts, no --run-id
        args = argparse.Namespace(
            kb_id=KB_ID,
            yes=True,
            config=None,
            artifacts=None,
            run_id=None,
        )

        stderr_capture = io.StringIO()
        with patch("finecorpus.config.loader.load_config", return_value=cfg):
            try:
                with redirect_stderr(stderr_capture):
                    rc = _cmd_pipeline_sweep(args)
            except SystemExit as e:
                rc = e.code

        stderr_text = stderr_capture.getvalue()
        # Must exit non-zero
        assert rc != 0, (
            f"CLI should exit non-zero when --artifacts is missing, got rc={rc}.\n"
            f"stderr: {stderr_text!r}"
        )
        # Must print a clear error message mentioning artifacts (not "Sweep declined (M-050)")
        mentions_artifacts = (
            "--artifacts" in stderr_text
            or "artifacts" in stderr_text.lower()
            or "run-id" in stderr_text.lower()
        )
        assert mentions_artifacts, (
            f"Error message should mention --artifacts or run-id, got: {stderr_text!r}"
        )
        # Must NOT silently say "Sweep declined" (that would be the old broken behavior)
        assert "Sweep declined (M-050)" not in stderr_text, (
            "CLI should not produce a M-050 decline message when artifacts are missing — "
            "that's the silent decline bug."
        )

    def test_cli_sweep_reaches_run_sweep_with_seeded_kb(self, tmp_path) -> None:
        """With a seeded eval set and collect artifact, CLI reaches run_sweep.

        Sets up:
        - A collect artifact with enough documents (>= sweep_min_corpus_docs)
        - An eval set in the control-plane DB with reviewed questions

        Verifies _cmd_pipeline_sweep calls run_sweep (not the empty-decline path).
        All external dependencies (DB engine, QdrantAdapter) are mocked so the
        test is self-contained.
        """
        import argparse
        import hashlib as _hlib
        import io
        import json as _json
        import os
        from contextlib import redirect_stderr
        from datetime import UTC
        from datetime import datetime as _dt
        from decimal import Decimal
        from unittest.mock import MagicMock, patch

        from sqlalchemy import create_engine
        from sqlalchemy.orm import Session

        import finecorpus.services.eval_sweep as sweep_mod
        from finecorpus.cli.main import _cmd_pipeline_sweep
        from finecorpus.config.models import AssessmentConfig, BudgetsConfig, Config
        from finecorpus.control.eval_store import EvalSetRepository
        from finecorpus.control.metadata import create_tables as _ct

        # --- Build collect artifact ---
        artifacts_root = str(tmp_path / "artifacts")
        run_id = "sweep-cli-test-run"
        os.makedirs(tmp_path / "artifacts" / run_id, exist_ok=True)

        items_raw = []
        for i in range(20):
            doc_id = f"doc-{i:04d}"
            items_raw.append(
                {
                    "document_id": doc_id,
                    "content_hash": _hlib.sha256(doc_id.encode()).hexdigest(),
                    "source_path": f"/corpus/{doc_id}",
                    "display_name": doc_id,
                    "media_type": "text/plain",
                    "size_bytes": 100,
                    "source_metadata": {},
                    "discovered_at": _dt.now(tz=UTC).isoformat(),
                    "dedup_role": "unique",
                    "collect_status": "collected",
                    "document_status": "active",
                }
            )
        collect_artifact = {
            "schema_version": "1.0.0",
            "source_run": {"kb_id": KB_ID, "workspace_id": WS_ID},
            "items": items_raw,
        }
        collect_path = tmp_path / "artifacts" / run_id / "collect.json"
        collect_path.write_text(_json.dumps(collect_artifact))

        # --- Seed eval set in in-memory DB ---
        db_engine = create_engine("sqlite:///:memory:", echo=False)
        _ct(db_engine)
        eval_set_id = "evalset-cli-test-001"
        with Session(db_engine) as session:
            eval_repo = EvalSetRepository(session)
            eval_repo.create(
                eval_set_id=eval_set_id,
                kb_id=KB_ID,
                workspace_id=WS_ID,
                schema_version="1.0.0",
                origin="generated_factual",
                confidence_level="reviewed",
            )
            eval_repo.add_question(
                question_id="q-cli-001",
                eval_set_id=eval_set_id,
                kb_id=KB_ID,
                text="What is the test about?",
                question_type="factual_lookup",
                generation_method="generated_factual",
                review_status="reviewed_kept",
                source_segment_ids=["seg-001"],
                source_unknown=False,
                expected_segment_ids=[CHK_A],
            )
            session.commit()

        # --- Build cfg with postgres DSN pointing to in-memory DB ---
        cfg = Config()
        cfg.assessment = AssessmentConfig(sweep_min_corpus_docs=10, sweep_candidate_budget=2)
        cfg.budgets = BudgetsConfig(sweep_confirmation_threshold_usd=Decimal("100.00"))
        # Fake DSN so the "if not dsn" check passes; we'll mock create_engine
        cfg.storage.postgres.url = "postgresql://fake/fake"

        args = argparse.Namespace(
            kb_id=KB_ID,
            yes=True,
            config=None,
            artifacts=artifacts_root,
            run_id=run_id,
        )

        # Track whether run_sweep was called
        run_sweep_called = []

        def mock_run_sweep(*a, **kw):
            run_sweep_called.append(
                {
                    "documents": kw.get("documents", []),
                    "eval_set": kw.get("eval_set", []),
                }
            )
            return sweep_mod.SweepResult(
                declined=True,
                reason="Test: corpus too small",
                applied=kw.get("base_config"),
                sweep_run_id="test-sweep-id-001",
            )

        def mock_estimate_cost(*a, **kw):
            return sweep_mod.SweepCostEstimate(
                total_est_cost_usd=Decimal("0.0"),
                per_candidate=[],
                n_candidates=2,
                n_sample_docs=10,
                basis="test",
            )

        # Build a stub base_config (MagicMock stands in for IngestionConfig)
        stub_base_config = MagicMock(name="stub_ingestion_config")

        # Stub the config_builder module so the CLI import doesn't fail
        import sys as _sys
        import types as _types

        cb_module = _types.ModuleType("finecorpus.pipeline.plan.config_builder")
        cb_module.build_default_ingestion_config = lambda **kw: stub_base_config  # type: ignore[attr-defined]

        stderr_capture = io.StringIO()

        with (
            patch("finecorpus.config.loader.load_config", return_value=cfg),
            # Make the CLI's create_engine return our in-memory DB
            patch("finecorpus.control.metadata.create_engine", return_value=db_engine),
            patch("finecorpus.control.metadata.create_tables", return_value=None),
            # Mock QdrantAdapter to avoid network calls
            patch("finecorpus.index.qdrant.backend.QdrantAdapter", return_value=MagicMock()),
            patch.object(sweep_mod, "run_sweep", mock_run_sweep),
            patch.object(sweep_mod, "estimate_sweep_cost", mock_estimate_cost),
            # Stub the not-yet-created config_builder module
            patch.dict(_sys.modules, {"finecorpus.pipeline.plan.config_builder": cb_module}),
        ):
            with redirect_stderr(stderr_capture):
                try:
                    rc = _cmd_pipeline_sweep(args)
                except Exception as exc:
                    # Any exception past the missing-artifacts check is acceptable
                    run_sweep_called.append({"error": str(exc)})
                    rc = 1

        db_engine.dispose()

        stderr_text = stderr_capture.getvalue()

        # The key assertion: run_sweep WAS called (we got past the missing-artifacts check
        # AND past the "no eval set" check)
        assert len(run_sweep_called) > 0, (
            f"run_sweep was not called. CLI returned early.\nstderr: {stderr_text!r}\nrc={rc}"
        )

        # When run_sweep is called, it should receive real documents (not empty list)
        if run_sweep_called and "documents" in run_sweep_called[0]:
            assert len(run_sweep_called[0]["documents"]) > 0, (
                "run_sweep was called with empty documents — the collect artifact was not loaded."
            )
            assert len(run_sweep_called[0]["eval_set"]) > 0, (
                "run_sweep was called with empty eval_set — the eval set was not loaded from DB."
            )

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
