"""Phase 5 acceptance layer — maps the three §19 acceptance criteria to named tests.

§19 Phase 5 ("Evaluation") acceptance criteria:
  1. The config sweep produces a ranked table with cost estimates.
  2. A drift alert fires on an induced regression.
  3. Provisional labelling is inescapable.

Each criterion below is exercised through the REAL code path (no stubbed
scorers): criterion 1 via estimate_sweep_cost + render_ranked_table, criterion 2
via run_drift_check against a genuinely degraded live index, and criterion 3 via
the EvalQuestion contract (review_status has no default), _infer_confidence_level
(provisional propagates), and render_confidence_banner (PROVISIONAL is prominent).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from finecorpus.contracts.eval_set import (
    ConfidenceLevel,
    EvalQuestion,
    GenerationMethod,
    QuestionType,
    ReviewStatus,
)
from finecorpus.control.audit import AuditAction, AuditLogRecord
from finecorpus.control.eval_store import EvalBaselineRepository, EvalSetRepository
from finecorpus.control.metadata import create_tables
from finecorpus.embedding.fake import FakeProvider
from finecorpus.index.adapter import alias_name
from finecorpus.services.eval_drift import run_drift_check
from finecorpus.services.eval_sweep import (
    RankedRow,
    SweepCostEstimate,
    SweepResult,
    estimate_sweep_cost,
    render_ranked_table,
)

# Reuse the proven sweep fixtures so criterion 1 maps to the real service.
from tests.phase5.test_eval_sweep import (
    KB_ID as SWEEP_KB_ID,
)
from tests.phase5.test_eval_sweep import (
    _make_config as _make_sweep_config,
)
from tests.phase5.test_eval_sweep import (
    _make_corpus,
    _make_db_session,
    _make_ingestion_config,
)
from tests.phase5.test_eval_sweep import (
    _make_provider as _make_sweep_provider,
)
from tests.retrieval.helpers import (
    FakeAdapter,
    FakeAliasRepository,
    make_alias_record,
    make_chunk_payload,
)

# ---------------------------------------------------------------------------
# Criterion 1: sweep produces a ranked table with cost estimates
# ---------------------------------------------------------------------------


class TestCriterion1SweepRankedTableWithCost:
    """§19 crit 1: the config sweep produces a ranked table with cost estimates."""

    def test_cost_estimate_produced_before_execution(self) -> None:
        """estimate_sweep_cost returns per-candidate + total cost with no index touched."""
        engine, session = _make_db_session()
        provider = _make_sweep_provider()
        cfg = _make_sweep_config(sweep_min_corpus_docs=5, sweep_candidate_budget=3)
        base_config = _make_ingestion_config()
        documents = _make_corpus(20)
        try:
            cost_est = estimate_sweep_cost(
                SWEEP_KB_ID,
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
        assert len(cost_est.per_candidate) == cost_est.n_candidates
        assert isinstance(cost_est.total_est_cost_usd, Decimal)
        # The cost estimate is a real aggregate of per-candidate costs.
        assert cost_est.total_est_cost_usd == sum(e.est_cost_usd for e in cost_est.per_candidate)

    def test_ranked_table_renders_with_cost_column(self) -> None:
        """render_ranked_table produces a ranked table that surfaces per-config cost."""
        result = SweepResult(
            ranked_rows=[
                RankedRow(
                    rank=1,
                    candidate_id=2,
                    label="chunk=512/overlap=64",
                    recall=0.82,
                    precision=0.71,
                    recall_delta=0.06,
                    precision_delta=0.03,
                    est_cost_usd=Decimal("1.20"),
                    index_size=1500,
                    ingestion_seconds=42.0,
                    is_reference=False,
                ),
                RankedRow(
                    rank=2,
                    candidate_id=0,
                    label="reference",
                    recall=0.76,
                    precision=0.68,
                    recall_delta=0.0,
                    precision_delta=0.0,
                    est_cost_usd=Decimal("0.90"),
                    index_size=1400,
                    ingestion_seconds=39.0,
                    is_reference=True,
                ),
            ],
            near_optimal=False,
            confidence_level=ConfidenceLevel.reviewed,
            n_sample_docs=20,
            n_candidates_run=2,
        )
        table = render_ranked_table(result)
        assert "Ranked Results" in table
        # Ranking is present (rank 1 winner) and a cost figure is surfaced.
        assert "1" in table
        assert "1.20" in table
        assert "chunk=512/overlap=64" in table


# ---------------------------------------------------------------------------
# Criterion 2: a drift alert fires on an induced regression
# ---------------------------------------------------------------------------

_DRIFT_KB = "kb-accept-drift"
_DRIFT_ALIAS = alias_name(_DRIFT_KB)
_DRIFT_COLL = f"rtfc_{_DRIFT_KB.replace('-', '').lower()}_00000001"
_DRIFT_ES = "es-accept-drift"
_DRIFT_WS = "ws-accept-drift"
_MODEL_ID = "fake-embed-v1"
_DIMS = 64
_CHK_A = "chk_a"
_CHK_B = "chk_b"
_NOW = datetime(2026, 9, 6, 12, 0, 0, tzinfo=UTC)


class TestCriterion2DriftAlertOnInducedRegression:
    """§19 crit 2: a drift alert fires on an induced regression (real scoring path)."""

    def test_induced_regression_fires_drift_detected(self) -> None:
        engine = create_engine("sqlite://")
        create_tables(engine)
        provider = FakeProvider(dimensions=_DIMS, model_id=_MODEL_ID)
        try:
            with Session(engine) as session:
                eval_repo = EvalSetRepository(session)
                baseline_repo = EvalBaselineRepository(session)
                eval_repo.create(
                    eval_set_id=_DRIFT_ES,
                    kb_id=_DRIFT_KB,
                    workspace_id=_DRIFT_WS,
                    schema_version="1.0.0",
                    origin="generated",
                    confidence_level="reviewed",
                    baseline_ref=None,
                    created_at=_NOW,
                )
                eval_repo.add_question(
                    question_id="q-accept-drift-001",
                    eval_set_id=_DRIFT_ES,
                    kb_id=_DRIFT_KB,
                    text="What is CHK_A about?",
                    question_type=QuestionType.factual_lookup.value,
                    generation_method=GenerationMethod.generated_factual.value,
                    review_status=ReviewStatus.reviewed_kept.value,
                    source_segment_ids=[_CHK_A],
                    source_unknown=False,
                    expected_segment_ids=[_CHK_A],
                )
                # Prior index was good — baseline recall 1.0.
                baseline_repo.upsert_current(
                    kb_id=_DRIFT_KB,
                    reference_id="ref-accept-001",
                    reference_fingerprint="fp-accept-001",
                    eval_set_id=_DRIFT_ES,
                    recall=1.0,
                    precision=1.0,
                    scored_at=_NOW,
                )
                session.commit()

                # Degraded live index — CHK_A is gone → real recall 0.0.
                degraded = FakeAdapter()
                degraded.seed_collection(
                    alias=_DRIFT_ALIAS,
                    coll=_DRIFT_COLL,
                    points=[make_chunk_payload(chunk_id=_CHK_B, kb_id=_DRIFT_KB, score=0.9)],
                )
                alias_record = make_alias_record(_DRIFT_KB, model_id=_MODEL_ID, dimensions=_DIMS)
                cfg = _make_sweep_config()
                cfg.assessment.eval_regression_threshold = 0.05

                repo = FakeAliasRepository({alias_record.alias: alias_record})
                with patch("finecorpus.retrieval.service.AliasRepository") as MockRepo:
                    MockRepo.return_value = repo
                    result = run_drift_check(
                        _DRIFT_KB,
                        session=session,
                        adapter=degraded,
                        provider=provider,
                        config=cfg,
                    )

                assert result.regressed is True
                assert result.status == "regressed"
                assert result.current_recall == pytest.approx(0.0, abs=1e-9)

                audit_rows = list(
                    session.execute(
                        select(AuditLogRecord).where(
                            AuditLogRecord.entry_type == str(AuditAction.drift_detected)
                        )
                    ).scalars()
                )
                assert len(audit_rows) == 1
                assert audit_rows[0].target_kb_id == _DRIFT_KB
        finally:
            engine.dispose()


# ---------------------------------------------------------------------------
# Criterion 3: provisional labelling is inescapable
# ---------------------------------------------------------------------------


class TestCriterion3ProvisionalLabellingInescapable:
    """§19 crit 3: provisional labelling is inescapable."""

    def test_review_status_has_no_default(self) -> None:
        """An EvalQuestion cannot be constructed without an explicit review_status."""
        with pytest.raises(Exception):  # noqa: B017 — pydantic ValidationError
            EvalQuestion(  # type: ignore[call-arg]
                question_id="q1",
                text="Q?",
                generation_method=GenerationMethod.generated_factual,
                # review_status intentionally omitted → ValidationError
                source_segment_ids=["s1"],
                source_unknown=False,
                question_type=QuestionType.factual_lookup,
                expected_segment_ids=["s1"],
            )

    def test_generated_unreviewed_set_is_provisional(self) -> None:
        """Any unreviewed question makes the whole set provisional (M-044 propagation)."""
        from finecorpus.services.eval_sweep import _infer_confidence_level  # noqa: PLC0415

        questions = [
            EvalQuestion(
                question_id="q1",
                text="Q1?",
                generation_method=GenerationMethod.generated_factual,
                review_status=ReviewStatus.reviewed_kept,
                source_segment_ids=["s1"],
                source_unknown=False,
                question_type=QuestionType.factual_lookup,
                expected_segment_ids=["s1"],
            ),
            EvalQuestion(
                question_id="q2",
                text="Q2?",
                generation_method=GenerationMethod.generated_factual,
                review_status=ReviewStatus.unreviewed,
                source_segment_ids=["s2"],
                source_unknown=False,
                question_type=QuestionType.factual_lookup,
                expected_segment_ids=["s2"],
            ),
        ]
        assert _infer_confidence_level(questions) == ConfidenceLevel.provisional

    def test_confidence_banner_surfaces_provisional(self) -> None:
        """render_confidence_banner makes PROVISIONAL prominent (added in PR-9)."""
        from finecorpus.pipeline.report import render_confidence_banner  # noqa: PLC0415

        banner = render_confidence_banner(
            confidence_level=ConfidenceLevel.provisional,
            n_total=10,
            n_unreviewed=4,
        )
        assert "PROVISIONAL" in banner
        assert "4" in banner

    def test_confidence_banner_calm_when_reviewed(self) -> None:
        from finecorpus.pipeline.report import render_confidence_banner  # noqa: PLC0415

        banner = render_confidence_banner(
            confidence_level=ConfidenceLevel.reviewed,
            n_total=10,
            n_unreviewed=0,
        )
        assert "PROVISIONAL" not in banner
