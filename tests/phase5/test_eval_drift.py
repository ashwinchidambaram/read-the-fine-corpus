"""Phase 5 tests for drift detection (M-100, §9.4, §19 criterion 2).

Test inventory:
  test_drift_alert_on_induced_regression — §19 crit 2: baseline retained,
    current index scores lower beyond threshold → DriftResult.regressed True
    + AuditAction.drift_detected row written + telemetry gauge set.
  test_no_regression_no_alert — delta <= threshold → regressed=False, no audit.
  test_no_baseline_returns_clear_status — no baseline → "no_baseline" status,
    not an error.
  test_drift_reruns_after_reindex — post-reindex enqueue fires: assert an
    eval_drift job is enqueued after _run_ingest completes (intent recording).
  test_drift_cron_disabled_when_unset — cron=None → drift check not run.
  test_eval_drift_job_arm_fails_loud — queue-mode eval_drift fails loud
    (no fake zeros), mirroring PR-7's eval_sweep arm.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from finecorpus.config.models import Config
from finecorpus.contracts.eval_set import (
    ConfidenceLevel,
    GenerationMethod,
    QuestionType,
    ReviewStatus,
)
from finecorpus.control.audit import AuditAction, AuditLogRecord
from finecorpus.control.eval_store import (
    EvalBaselineRepository,
    EvalSetRepository,
)
from finecorpus.control.jobs import JobQueueRepository, JobType
from finecorpus.control.metadata import create_tables
from finecorpus.embedding.fake import FakeProvider
from finecorpus.index.adapter import alias_name
from finecorpus.services.eval_drift import DriftResult, run_drift_check
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

KB_ID = "kb-drift-test"
ALIAS = alias_name(KB_ID)
COLL = f"rtfc_{KB_ID.replace('-', '').lower()}_00000001"
MODEL_ID = "fake-embed-v1"
DIMENSIONS = 64

CHK_A = "chk_a"
CHK_B = "chk_b"
CHK_C = "chk_c"

EVAL_SET_ID = "es-drift-001"
WS_ID = "ws-drift-test"

_NOW = datetime(2026, 9, 6, 12, 0, 0, tzinfo=UTC)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def engine():
    eng = create_engine("sqlite://")
    create_tables(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def session(engine):
    with Session(engine) as s:
        yield s


@pytest.fixture
def provider():
    return FakeProvider(dimensions=DIMENSIONS, model_id=MODEL_ID)


@pytest.fixture
def adapter():
    """FakeAdapter seeded with CHK_A, CHK_B, CHK_C for KB_ID."""
    a = FakeAdapter()
    pts = [
        make_chunk_payload(chunk_id=CHK_A, kb_id=KB_ID, score=0.95),
        make_chunk_payload(chunk_id=CHK_B, kb_id=KB_ID, score=0.85),
        make_chunk_payload(chunk_id=CHK_C, kb_id=KB_ID, score=0.60),
    ]
    a.seed_collection(alias=ALIAS, coll=COLL, points=pts)
    return a


@pytest.fixture
def config_with_threshold():
    """Config with eval_regression_threshold=0.05 (default)."""
    cfg = Config()
    return cfg


def _make_config(threshold: float = 0.05, cron: str | None = None) -> Config:
    cfg = Config()
    cfg.assessment.eval_regression_threshold = threshold
    cfg.observability.drift_detection_cron = cron
    return cfg


def _seed_eval_set(session: Session, *, recall: float = 0.90, precision: float = 0.80):
    """Seed an eval set, eval questions, and a baseline into the session."""
    eval_repo = EvalSetRepository(session)
    baseline_repo = EvalBaselineRepository(session)

    # Create eval set
    eval_repo.create(
        eval_set_id=EVAL_SET_ID,
        kb_id=KB_ID,
        workspace_id=WS_ID,
        schema_version="1.0.0",
        origin="generated",
        confidence_level="reviewed",
        baseline_ref=None,
        created_at=_NOW,
    )

    # Add a question pointing at CHK_A (so scoring is deterministic)
    eval_repo.add_question(
        question_id="q-drift-001",
        eval_set_id=EVAL_SET_ID,
        kb_id=KB_ID,
        text="What is CHK_A about?",
        question_type=QuestionType.factual_lookup.value,
        generation_method=GenerationMethod.generated_factual.value,
        review_status=ReviewStatus.reviewed_kept.value,
        source_segment_ids=[CHK_A],
        source_unknown=False,
        expected_segment_ids=[CHK_A],
    )

    # Seed the baseline (high recall = 0.90 means the prior index was good)
    baseline_repo.upsert_current(
        kb_id=KB_ID,
        reference_id="ref-001",
        reference_fingerprint="fp-001",
        eval_set_id=EVAL_SET_ID,
        recall=recall,
        precision=precision,
        scored_at=_NOW,
    )
    session.commit()


@contextmanager
def _patch_alias_repo(alias_record: FakeAliasRecord):
    """Patch AliasRepository.get for the duration of the context."""
    repo = FakeAliasRepository({alias_record.alias: alias_record})
    with patch("finecorpus.retrieval.service.AliasRepository") as MockRepo:
        MockRepo.return_value = repo
        yield


# ---------------------------------------------------------------------------
# test_drift_alert_on_induced_regression (§19 criterion 2)
# ---------------------------------------------------------------------------


class TestDriftAlertOnInducedRegression:
    """§19 crit 2: baseline high (0.90), current lower by >threshold → regressed=True."""

    def test_drift_alert_on_induced_regression(self, engine, provider, adapter):
        """Baseline recall=0.90, threshold=0.05.

        score_eval_set returns ~0.0 recall (FakeAdapter returns CHK_A which IS
        in expected, so recall=1.0... we need to fake a low score).
        We induce a regression by setting a very HIGH baseline (1.0) and scoring
        the current index at a lower value, but since FakeAdapter always returns
        CHK_A (which is the expected chunk), we must set baseline to 1.0 and use
        a threshold < delta.

        Strategy: baseline.recall = 1.0, score_eval_set returns recall=1.0 (CHK_A
        found). To induce regression, we mock score_eval_set to return a low score.
        """
        with Session(engine) as session:
            eval_repo = EvalSetRepository(session)
            baseline_repo = EvalBaselineRepository(session)

            # Seed eval set
            eval_repo.create(
                eval_set_id=EVAL_SET_ID,
                kb_id=KB_ID,
                workspace_id=WS_ID,
                schema_version="1.0.0",
                origin="generated",
                confidence_level="reviewed",
                baseline_ref=None,
                created_at=_NOW,
            )
            eval_repo.add_question(
                question_id="q-drift-001",
                eval_set_id=EVAL_SET_ID,
                kb_id=KB_ID,
                text="What is the content?",
                question_type=QuestionType.factual_lookup.value,
                generation_method=GenerationMethod.generated_factual.value,
                review_status=ReviewStatus.reviewed_kept.value,
                source_segment_ids=[CHK_A],
                source_unknown=False,
                expected_segment_ids=[CHK_A],
            )

            # High baseline (0.90 recall)
            baseline_repo.upsert_current(
                kb_id=KB_ID,
                reference_id="ref-001",
                reference_fingerprint="fp-001",
                eval_set_id=EVAL_SET_ID,
                recall=0.90,
                precision=0.80,
                scored_at=_NOW,
            )
            session.commit()

            alias_record = make_alias_record(KB_ID, model_id=MODEL_ID, dimensions=DIMENSIONS)
            cfg = _make_config(threshold=0.05)

            # Patch score_eval_set to return a low score (0.50 recall) — induced regression
            from finecorpus.pipeline.evaluation.metrics import ScoreSummary  # noqa: PLC0415
            from finecorpus.services.eval_scoring import ScoreResult  # noqa: PLC0415

            low_score = ScoreResult(
                summary=ScoreSummary(
                    mean_recall=0.50,
                    mean_precision=0.50,
                    recall_by_type={},
                    precision_by_type={},
                ),
                confidence_level=ConfidenceLevel.reviewed,
                k=10,
                n_scored=1,
                n_total=1,
            )

            with _patch_alias_repo(alias_record):
                with patch(
                    "finecorpus.services.eval_drift.score_eval_set",
                    return_value=low_score,
                ):
                    result = run_drift_check(
                        KB_ID,
                        session=session,
                        adapter=adapter,
                        provider=provider,
                        config=cfg,
                    )

        assert isinstance(result, DriftResult)
        assert result.regressed is True
        assert result.status == "regressed"
        assert result.delta_recall == pytest.approx(0.40, abs=1e-6)  # 0.90 - 0.50
        assert result.baseline_recall == pytest.approx(0.90)
        assert result.current_recall == pytest.approx(0.50)
        assert result.threshold == pytest.approx(0.05)

    def test_audit_row_written_on_regression(self, engine, provider, adapter):
        """AuditAction.drift_detected row must be written when regressed=True."""
        with Session(engine) as session:
            _seed_eval_set(session, recall=0.90, precision=0.80)

            alias_record = make_alias_record(KB_ID, model_id=MODEL_ID, dimensions=DIMENSIONS)
            cfg = _make_config(threshold=0.05)

            from finecorpus.pipeline.evaluation.metrics import ScoreSummary  # noqa: PLC0415
            from finecorpus.services.eval_scoring import ScoreResult  # noqa: PLC0415

            low_score = ScoreResult(
                summary=ScoreSummary(
                    mean_recall=0.20,
                    mean_precision=0.20,
                    recall_by_type={},
                    precision_by_type={},
                ),
                confidence_level=ConfidenceLevel.reviewed,
                k=10,
                n_scored=1,
                n_total=1,
            )

            with _patch_alias_repo(alias_record):
                with patch(
                    "finecorpus.services.eval_drift.score_eval_set",
                    return_value=low_score,
                ):
                    result = run_drift_check(
                        KB_ID,
                        session=session,
                        adapter=adapter,
                        provider=provider,
                        config=cfg,
                    )

            assert result.regressed is True

            # Verify audit row was written
            audit_rows = list(
                session.execute(
                    select(AuditLogRecord).where(
                        AuditLogRecord.entry_type == str(AuditAction.drift_detected)
                    )
                ).scalars()
            )
            assert len(audit_rows) == 1
            audit = audit_rows[0]
            assert audit.target_kb_id == KB_ID
            assert audit.details["delta_recall"] == pytest.approx(0.70, abs=1e-6)
            assert audit.details["threshold"] == pytest.approx(0.05)
            assert audit.details["baseline_recall"] == pytest.approx(0.90)
            assert audit.details["current_recall"] == pytest.approx(0.20)

    def test_telemetry_gauge_set_on_regression(self, engine, provider, adapter):
        """QUALITY_EVAL_DRIFT_SCORE_VS_BASELINE gauge is set on regression."""
        with Session(engine) as session:
            _seed_eval_set(session, recall=0.90, precision=0.80)

            alias_record = make_alias_record(KB_ID, model_id=MODEL_ID, dimensions=DIMENSIONS)
            cfg = _make_config(threshold=0.05)

            from finecorpus.pipeline.evaluation.metrics import ScoreSummary  # noqa: PLC0415
            from finecorpus.services.eval_scoring import ScoreResult  # noqa: PLC0415

            low_score = ScoreResult(
                summary=ScoreSummary(
                    mean_recall=0.50,
                    mean_precision=0.50,
                    recall_by_type={},
                    precision_by_type={},
                ),
                confidence_level=ConfidenceLevel.reviewed,
                k=10,
                n_scored=1,
                n_total=1,
            )

            gauge_calls: list[tuple[str, float]] = []

            class _FakeGauge:
                def labels(self, **kwargs: Any) -> _FakeGauge:
                    self._kb = kwargs.get("kb_id", "")
                    return self

                def set(self, value: float) -> None:
                    gauge_calls.append((self._kb, value))

            fake_gauge = _FakeGauge()

            with _patch_alias_repo(alias_record):
                with patch(
                    "finecorpus.services.eval_drift.score_eval_set",
                    return_value=low_score,
                ):
                    with patch(
                        "finecorpus.services.eval_drift._get_drift_gauge",
                        return_value=fake_gauge,
                    ):
                        result = run_drift_check(
                            KB_ID,
                            session=session,
                            adapter=adapter,
                            provider=provider,
                            config=cfg,
                        )

        assert result.regressed is True
        # gauge should have been called with the delta_recall value
        assert len(gauge_calls) >= 1
        kb_ids = [k for k, _ in gauge_calls]
        assert KB_ID in kb_ids


# ---------------------------------------------------------------------------
# test_no_regression_no_alert
# ---------------------------------------------------------------------------


class TestNoRegressionNoAlert:
    """Delta <= threshold → regressed=False, no drift_detected audit row."""

    def test_no_regression_no_alert(self, engine, provider, adapter):
        with Session(engine) as session:
            # Baseline recall = 0.90; current = 0.88 → delta = 0.02 < threshold 0.05
            _seed_eval_set(session, recall=0.90, precision=0.80)

            alias_record = make_alias_record(KB_ID, model_id=MODEL_ID, dimensions=DIMENSIONS)
            cfg = _make_config(threshold=0.05)

            from finecorpus.pipeline.evaluation.metrics import ScoreSummary  # noqa: PLC0415
            from finecorpus.services.eval_scoring import ScoreResult  # noqa: PLC0415

            near_baseline_score = ScoreResult(
                summary=ScoreSummary(
                    mean_recall=0.88,
                    mean_precision=0.78,
                    recall_by_type={},
                    precision_by_type={},
                ),
                confidence_level=ConfidenceLevel.reviewed,
                k=10,
                n_scored=1,
                n_total=1,
            )

            with _patch_alias_repo(alias_record):
                with patch(
                    "finecorpus.services.eval_drift.score_eval_set",
                    return_value=near_baseline_score,
                ):
                    result = run_drift_check(
                        KB_ID,
                        session=session,
                        adapter=adapter,
                        provider=provider,
                        config=cfg,
                    )

            assert result.regressed is False
            assert result.status == "ok"
            assert result.delta_recall == pytest.approx(0.02, abs=1e-6)

            # No drift_detected audit row should exist
            audit_rows = list(
                session.execute(
                    select(AuditLogRecord).where(
                        AuditLogRecord.entry_type == str(AuditAction.drift_detected)
                    )
                ).scalars()
            )
            assert len(audit_rows) == 0


# ---------------------------------------------------------------------------
# test_no_baseline_returns_clear_status
# ---------------------------------------------------------------------------


class TestNoBaselineReturnsClearStatus:
    """No baseline → DriftResult with status='no_baseline', not an error."""

    def test_no_baseline_returns_clear_status(self, engine, provider, adapter):
        with Session(engine) as session:
            # Do NOT seed any baseline
            cfg = _make_config(threshold=0.05)
            alias_record = make_alias_record(KB_ID, model_id=MODEL_ID, dimensions=DIMENSIONS)

            with _patch_alias_repo(alias_record):
                result = run_drift_check(
                    KB_ID,
                    session=session,
                    adapter=adapter,
                    provider=provider,
                    config=cfg,
                )

        assert isinstance(result, DriftResult)
        assert result.status == "no_baseline"
        assert result.regressed is False
        assert result.baseline_recall is None
        assert result.current_recall is None

    def test_no_baseline_no_audit_row(self, engine, provider, adapter):
        with Session(engine) as session:
            cfg = _make_config(threshold=0.05)
            alias_record = make_alias_record(KB_ID, model_id=MODEL_ID, dimensions=DIMENSIONS)

            with _patch_alias_repo(alias_record):
                run_drift_check(
                    KB_ID,
                    session=session,
                    adapter=adapter,
                    provider=provider,
                    config=cfg,
                )

            audit_rows = list(
                session.execute(
                    select(AuditLogRecord).where(
                        AuditLogRecord.entry_type == str(AuditAction.drift_detected)
                    )
                ).scalars()
            )
            assert len(audit_rows) == 0


# ---------------------------------------------------------------------------
# test_drift_reruns_after_reindex — post-reindex enqueue fires
# ---------------------------------------------------------------------------


class TestDriftRerunsAfterReindex:
    """Post-reindex enqueue: assert a drift job is enqueued after _run_ingest."""

    def test_drift_intent_enqueued_after_ingest(self, engine):
        """After _run_ingest completes, a JobType.eval_drift job should be enqueued."""
        from finecorpus.control.jobs import JobRecord  # noqa: PLC0415
        from finecorpus.pipeline.jobs import JobRunner  # noqa: PLC0415

        with Session(engine) as session:
            queue_repo = JobQueueRepository(session)

            # Enqueue a minimal ingest job
            job = queue_repo.enqueue(
                kb_id=KB_ID,
                workspace_id=WS_ID,
                job_type=JobType.ingest,
                payload={
                    "run_id": "run-drift-test-001",
                    "source_dir": "/tmp/fake-source",
                    "artifacts_root": "/tmp/fake-artifacts",
                    "promote": False,
                },
                dedupe_key=None,
                priority=0,
            )
            session.commit()

            runner = JobRunner(
                session=session,
                queue_repo=queue_repo,
            )

            # Patch _run_pre_build_stages, _check_budget_before_build, _run_build_stage
            # to avoid actual pipeline execution
            with patch.object(runner, "_run_pre_build_stages"):
                with patch.object(runner, "_check_budget_before_build", return_value=False):
                    with patch.object(runner, "_run_build_stage"):
                        # Start the job (needed before calling _run_ingest)
                        queue_repo.start(job.job_id)
                        session.commit()
                        # Simulate stages_completed = all stages
                        queue_repo.checkpoint(
                            job.job_id,
                            data={
                                "stages_completed": [
                                    "collect",
                                    "assess",
                                    "decompose",
                                    "plan",
                                    "build",
                                ]
                            },
                        )
                        session.commit()

                        # Reload job with checkpoint
                        from sqlalchemy import select as sa_select  # noqa: PLC0415

                        fresh_job = session.execute(
                            sa_select(JobRecord).where(JobRecord.job_id == job.job_id)
                        ).scalar_one()

                        # Run _run_ingest directly (post-reindex path)
                        runner._run_ingest(fresh_job)

            session.commit()

            # Assert a drift job was enqueued
            from sqlalchemy import select as sa_select2  # noqa: PLC0415

            drift_jobs = list(
                session.execute(
                    sa_select2(JobRecord).where(
                        JobRecord.kb_id == KB_ID,
                        JobRecord.job_type == str(JobType.eval_drift),
                    )
                ).scalars()
            )
            assert len(drift_jobs) >= 1
            assert drift_jobs[0].job_type == str(JobType.eval_drift)


# ---------------------------------------------------------------------------
# test_drift_cron_disabled_when_unset
# ---------------------------------------------------------------------------


class TestDriftCronDisabledWhenUnset:
    """cron=None → drift check not run from scheduler_tick."""

    def test_drift_not_run_when_cron_unset(self):
        """scheduler_tick with cron=None must not invoke run_drift_check."""
        from finecorpus.services.ingest_worker import scheduler_tick  # noqa: PLC0415

        cfg = _make_config(cron=None)  # cron disabled

        mock_session = MagicMock()
        mock_adapter = MagicMock()
        mock_provider = MagicMock()

        with patch("finecorpus.services.ingest_worker._run_cron_drift_checks") as mock_drift:
            with patch("finecorpus.pipeline.reindex.evaluate_triggers"):
                scheduler_tick(
                    session=mock_session,
                    config=cfg,
                    adapter=mock_adapter,
                    provider=mock_provider,
                )
        # Must not have been called when cron is None
        mock_drift.assert_not_called()

    def test_drift_not_run_when_no_args(self):
        """scheduler_tick with no args is a no-op (legacy path)."""
        from finecorpus.services.ingest_worker import scheduler_tick  # noqa: PLC0415

        with patch("finecorpus.services.ingest_worker._run_cron_drift_checks") as mock_drift:
            scheduler_tick()  # no-arg call
        mock_drift.assert_not_called()

    def test_is_drift_cron_due_false_when_none(self):
        """_is_drift_cron_due returns False for None/empty cron."""
        from finecorpus.services.ingest_worker import _is_drift_cron_due  # noqa: PLC0415

        assert _is_drift_cron_due(None) is False
        assert _is_drift_cron_due("") is False

    def test_is_drift_cron_due_true_when_set(self):
        """_is_drift_cron_due returns True when cron expression is set."""
        from finecorpus.services.ingest_worker import _is_drift_cron_due  # noqa: PLC0415

        assert _is_drift_cron_due("0 * * * *") is True


# ---------------------------------------------------------------------------
# test_eval_drift_job_arm_fails_loud (PR-7 fail-loud precedent)
# ---------------------------------------------------------------------------


class TestEvalDriftJobArmFailsLoud:
    """Queue-mode eval_drift must fail loudly — no fake zeros."""

    def test_eval_drift_job_fails_loud(self, engine):
        """JobRunner._run_eval_drift raises ValueError (no scored result produced)."""
        from finecorpus.pipeline.jobs import JobRunner  # noqa: PLC0415

        with Session(engine) as session:
            queue_repo = JobQueueRepository(session)

            job = queue_repo.enqueue(
                kb_id=KB_ID,
                workspace_id=WS_ID,
                job_type=JobType.eval_drift,
                payload={"trigger": "post_reindex"},
                dedupe_key=None,
            )
            session.commit()

            runner = JobRunner(session=session, queue_repo=queue_repo)
            queue_repo.start(job.job_id)
            session.commit()

            with pytest.raises(ValueError, match="eval_drift must run via the services entrypoint"):
                runner._run_eval_drift(job)

            # Job should be in failed state
            from sqlalchemy import select as sa_select  # noqa: PLC0415

            from finecorpus.control.jobs import JobRecord  # noqa: PLC0415

            failed_job = session.execute(
                sa_select(JobRecord).where(JobRecord.job_id == job.job_id)
            ).scalar_one()
            assert failed_job.state == "failed"
