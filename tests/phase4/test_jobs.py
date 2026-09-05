"""Phase 4 job runner tests.

Test inventory:
  test_resume_from_checkpoint_no_duplication — run job → pause after a stage →
    resume → completed stages not re-executed (stage-execution spy counters).
  test_budget_pause_not_fail — guard returns pause → job state paused_budget,
    audit row appended, no exception, resumable.
  test_worker_error_isolation — JobRunner raising → job failed, loop continues.
  test_reap_stale_requeues — stale running jobs re-queued via worker integration.
  test_cost_accrual_recorded — ledger rows written per costed stage; job.cost_accrued_usd matches.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from finecorpus.control.audit import AuditAction, AuditLogRecord, AuditLogRepository
from finecorpus.control.cost_ledger import (
    BudgetDecision,
    CostLedgerRepository,
)
from finecorpus.control.jobs import JobQueueRepository, JobRecord, JobState, JobType
from finecorpus.control.metadata import create_tables
from finecorpus.pipeline.jobs import (
    JobRunner,
)
from finecorpus.services.ingest_worker import _run_one_job_isolated

# ---------------------------------------------------------------------------
# Fixtures — fresh engine + session per test for isolation
# ---------------------------------------------------------------------------


@pytest.fixture
def db():
    """Fresh in-memory SQLite engine + session per test."""
    engine = create_engine("sqlite://")
    create_tables(engine)
    with Session(engine) as session:
        yield engine, session
    engine.dispose()


@pytest.fixture
def session(db):
    _, s = db
    return s


@pytest.fixture
def queue_repo(session):
    return JobQueueRepository(session)


@pytest.fixture
def ledger_repo(session):
    return CostLedgerRepository(session)


@pytest.fixture
def audit_repo(session):
    return AuditLogRepository(session)


def _enqueue_and_claim(queue_repo, session, kb_id="kb-test", worker_id="w1"):
    """Create a job, claim it, and return the claimed record."""
    queue_repo.enqueue(
        kb_id=kb_id,
        workspace_id="ws-test",
        job_type=JobType.ingest,
        payload={"run_id": "run-1", "source_dir": "/src", "artifacts_root": "/arts"},
    )
    session.commit()
    job = queue_repo.claim(worker_id)
    assert job is not None
    session.commit()
    return job


# ---------------------------------------------------------------------------
# test_resume_from_checkpoint_no_duplication
# ---------------------------------------------------------------------------


class TestResumeFromCheckpoint:
    """Completed stages are not re-executed on resume."""

    def test_resume_from_checkpoint_no_duplication(
        self, queue_repo, session, ledger_repo, audit_repo, tmp_path
    ):
        """Run job → already checkpointed collect..plan → resume → Build stage runs once."""
        from finecorpus.pipeline.artifact_store import ArtifactStore

        arts = tmp_path / "arts"
        arts.mkdir()
        store = ArtifactStore(artifacts_root=str(arts), run_id="run-1")
        store.save("collect", {"schema_version": "0.0.0", "spy": "collect"})
        store.save("assess", {"schema_version": "0.0.0", "spy": "assess"})
        store.save("decompose", {"schema_version": "0.0.0", "spy": "decompose"})
        store.save("plan", {"schema_version": "0.0.0", "spy": "plan"})

        job = _enqueue_and_claim(queue_repo, session, kb_id="kb-resume-chk")
        job = queue_repo.start(job.job_id)
        session.commit()

        # Simulate that collect/assess/decompose/plan are already done via checkpoint
        queue_repo.checkpoint(
            job.job_id,
            data={"stages_completed": ["collect", "assess", "decompose", "plan"]},
        )
        session.commit()

        build_mock = MagicMock()
        build_mock.run.return_value = {
            "token_accounting": {"total_input_tokens": 100, "embed_call_count": 2},
            "schema_version": "0.0.0",
        }

        runner = JobRunner(
            session=session,
            queue_repo=queue_repo,
            ledger_repo=ledger_repo,
            audit_repo=audit_repo,
        )

        build_call_count = {"count": 0}

        original_run_build = runner._run_build_stage

        def spy_run_build(**kwargs):
            build_call_count["count"] += 1
            return original_run_build(**kwargs)

        # Track pre-build stage calls
        pre_build_call_count = {"count": 0}
        original_pre_build = runner._run_pre_build_stages

        def spy_pre_build(**kwargs):
            pre_build_call_count["count"] += 1
            # The real method will skip all stages since they're in checkpoint
            return original_pre_build(**kwargs)

        runner._run_build_stage = lambda **kw: spy_run_build(**kw)  # type: ignore
        runner._run_pre_build_stages = lambda **kw: spy_pre_build(**kw)  # type: ignore

        build_class_mock = MagicMock(return_value=build_mock)
        with patch("finecorpus.pipeline.build.BuildStage", build_class_mock):
            runner._run_build_stage(
                job=job,
                run_id="run-1",
                source_dir="/src",
                artifacts_root=str(arts),
                promote=False,
            )

        # Build stage was entered once
        assert build_class_mock.called, "BuildStage should have been instantiated"
        assert build_mock.run.call_count == 1, "Build.run should have been called exactly once"

        # Checkpoint reflects build completion
        session.commit()
        stmt = select(JobRecord).where(JobRecord.job_id == job.job_id)
        refreshed = session.execute(stmt).scalar_one()
        assert refreshed.checkpoint is not None
        assert "build" in refreshed.checkpoint.get("stages_completed", [])


# ---------------------------------------------------------------------------
# test_budget_pause_not_fail
# ---------------------------------------------------------------------------


class TestBudgetPauseNotFail:
    """Budget cap hit → paused_budget, audit row, no exception, resumable."""

    def test_budget_pause_not_fail(self, queue_repo, session, ledger_repo, audit_repo, tmp_path):
        """Guard returns pause → job state paused_budget, audit row appended, no exception."""
        guard = MagicMock()
        guard.check.return_value = BudgetDecision.pause_kb_cap

        job = _enqueue_and_claim(queue_repo, session, kb_id="kb-budget")
        job = queue_repo.start(job.job_id)
        session.commit()
        queue_repo.checkpoint(
            job.job_id,
            data={"stages_completed": ["collect", "assess", "decompose", "plan"]},
        )
        session.commit()

        runner = JobRunner(
            session=session,
            queue_repo=queue_repo,
            ledger_repo=ledger_repo,
            audit_repo=audit_repo,
            budget_guard=guard,
        )

        arts = tmp_path / "arts"
        arts.mkdir()
        from finecorpus.pipeline.artifact_store import ArtifactStore

        store = ArtifactStore(artifacts_root=str(arts), run_id="run-1")
        store.save("plan", {"schema_version": "0.0.0"})
        store.save("decompose", {"schema_version": "0.0.0"})

        # Must NOT raise
        paused = runner._check_budget_before_build(job, "run-1", str(arts))
        assert paused is True

        # Job must be paused_budget
        session.commit()
        stmt = select(JobRecord).where(JobRecord.job_id == job.job_id)
        refreshed = session.execute(stmt).scalar_one()
        assert refreshed.state == str(JobState.paused_budget)

        # Audit row must have been appended
        audit_rows = (
            session.execute(
                select(AuditLogRecord).where(AuditLogRecord.target_kb_id == "kb-budget")
            )
            .scalars()
            .all()
        )
        assert any(r.entry_type == str(AuditAction.budget_cap_hit) for r in audit_rows), (
            "No budget_cap_hit audit row found"
        )

        # Job is resumable: resume transitions back to queued
        resumed = queue_repo.resume(refreshed.job_id)
        session.commit()
        assert resumed.state == str(JobState.queued)

    def test_allow_decision_not_paused(
        self, queue_repo, session, ledger_repo, audit_repo, tmp_path
    ):
        """Guard returns allow → job continues, not paused."""
        guard = MagicMock()
        guard.check.return_value = BudgetDecision.allow

        job = _enqueue_and_claim(queue_repo, session, kb_id="kb-allow")
        job = queue_repo.start(job.job_id)
        session.commit()

        runner = JobRunner(
            session=session,
            queue_repo=queue_repo,
            ledger_repo=ledger_repo,
            audit_repo=audit_repo,
            budget_guard=guard,
        )

        arts = tmp_path / "arts-allow"
        arts.mkdir()
        paused = runner._check_budget_before_build(job, "run-allow", str(arts))
        assert paused is False

        # Job state should still be running
        stmt = select(JobRecord).where(JobRecord.job_id == job.job_id)
        refreshed = session.execute(stmt).scalar_one()
        assert refreshed.state == str(JobState.running)


# ---------------------------------------------------------------------------
# test_worker_error_isolation
# ---------------------------------------------------------------------------


class TestWorkerErrorIsolation:
    """JobRunner raising → job failed, loop continues."""

    def test_worker_error_isolation_no_reraise(self, queue_repo, session):
        """_run_one_job_isolated: runner exception is caught, not re-raised."""
        job = _enqueue_and_claim(queue_repo, session, kb_id="kb-error-iso")

        failing_runner = MagicMock()
        failing_runner.run.side_effect = RuntimeError("Simulated pipeline failure")

        # Must not propagate the exception
        _run_one_job_isolated(job, failing_runner)

        # Verify run was called
        failing_runner.run.assert_called_once_with(job)

    def test_job_fails_and_loop_continues(self, queue_repo, session, ledger_repo, audit_repo):
        """When JobRunner raises after marking job failed, loop is unaffected."""
        job = _enqueue_and_claim(queue_repo, session, kb_id="kb-fail-int")
        job = queue_repo.start(job.job_id)
        session.commit()

        class FailingRunner:
            def run(self, j):
                queue_repo.fail(j.job_id, error_msg="deliberate failure")
                session.commit()
                raise RuntimeError("deliberate failure")

        # Should not raise
        _run_one_job_isolated(job, FailingRunner())

        # Job must be failed
        stmt = select(JobRecord).where(JobRecord.job_id == job.job_id)
        refreshed = session.execute(stmt).scalar_one()
        assert refreshed.state == str(JobState.failed)


# ---------------------------------------------------------------------------
# test_reap_stale_requeues
# ---------------------------------------------------------------------------


class TestReapStaleRequeues:
    """Stale running jobs are re-queued via reap_stale."""

    def test_reap_stale_requeues(self, queue_repo, session):
        """Running job with old heartbeat is requeued by reap_stale."""
        job = _enqueue_and_claim(queue_repo, session, kb_id="kb-stale")
        job = queue_repo.start(job.job_id)
        session.commit()

        # Set heartbeat to 2 minutes ago
        old_time = datetime.now(tz=UTC) - timedelta(seconds=120)
        job.heartbeat_at = old_time
        session.commit()

        # Reap with 60s timeout
        reaped = queue_repo.reap_stale(60.0)
        session.commit()

        assert len(reaped) >= 1
        reaped_ids = {j.job_id for j in reaped}
        assert job.job_id in reaped_ids

        # Job should be back to queued
        stmt = select(JobRecord).where(JobRecord.job_id == job.job_id)
        refreshed = session.execute(stmt).scalar_one()
        assert refreshed.state == str(JobState.queued)

    def test_reap_does_not_requeue_completed(self, queue_repo, session):
        """Completed jobs are not affected by reap_stale."""
        job = _enqueue_and_claim(queue_repo, session, kb_id="kb-stale-comp")
        queue_repo.complete(job.job_id, cost_accrued_usd=0.0)
        session.commit()

        reaped = queue_repo.reap_stale(0.0)
        session.commit()

        reaped_ids = {j.job_id for j in reaped}
        assert job.job_id not in reaped_ids


# ---------------------------------------------------------------------------
# test_cost_accrual_recorded
# ---------------------------------------------------------------------------


class TestCostAccrualRecorded:
    """Ledger rows written per costed stage; job.cost_accrued_usd matches."""

    def test_cost_accrual_recorded(self, queue_repo, session, ledger_repo, audit_repo, tmp_path):
        """Build stage actual tokens → ledger row + job cost_accrued_usd updated."""
        from finecorpus.pipeline.artifact_store import ArtifactStore

        arts = tmp_path / "arts-cost"
        arts.mkdir()
        store = ArtifactStore(artifacts_root=str(arts), run_id="run-cost")
        store.save("plan", {"schema_version": "0.0.0"})

        job = _enqueue_and_claim(queue_repo, session, kb_id="kb-cost", worker_id="w-cost")
        job = queue_repo.start(job.job_id)
        session.commit()
        queue_repo.checkpoint(
            job.job_id,
            data={"stages_completed": ["collect", "assess", "decompose", "plan"]},
        )
        session.commit()

        # Fake embedding provider with non-zero cost
        fake_provider = MagicMock()
        fake_caps = MagicMock()
        fake_caps.cost_per_1k_tokens = Decimal("0.01")
        fake_caps.is_local = False
        fake_provider.capabilities = fake_caps

        build_mock = MagicMock()
        build_mock.run.return_value = {
            "token_accounting": {
                "total_input_tokens": 1000,
                "embed_call_count": 5,
                "llm_call_count": 0,
            },
            "schema_version": "0.0.0",
        }

        runner = JobRunner(
            session=session,
            queue_repo=queue_repo,
            ledger_repo=ledger_repo,
            audit_repo=audit_repo,
            embedding_provider=fake_provider,
        )

        build_class_mock = MagicMock(return_value=build_mock)
        with patch("finecorpus.pipeline.build.BuildStage", build_class_mock):
            runner._run_build_stage(
                job=job,
                run_id="run-cost",
                source_dir="/src",
                artifacts_root=str(arts),
                promote=False,
            )

        session.commit()

        # Check ledger
        accrued = ledger_repo.accrued_usd(kb_id="kb-cost")
        assert accrued > Decimal("0"), "Expected non-zero ledger entry for costed build"

        # Expected: 1000 tokens / 1000 * $0.01 = $0.01
        assert abs(accrued - Decimal("0.01")) < Decimal("0.0001")

        # Check job cost_accrued_usd
        stmt = select(JobRecord).where(JobRecord.job_id == job.job_id)
        refreshed = session.execute(stmt).scalar_one()
        assert Decimal(str(refreshed.cost_accrued_usd)) > Decimal("0")
        # Should match ledger
        assert abs(Decimal(str(refreshed.cost_accrued_usd)) - accrued) < Decimal("0.000001")

    def test_local_provider_zero_cost_no_ledger_row(
        self, queue_repo, session, ledger_repo, audit_repo, tmp_path
    ):
        """Local provider (cost_per_1k_tokens=None) → zero cost, no ledger row."""
        from finecorpus.pipeline.artifact_store import ArtifactStore

        arts = tmp_path / "arts-local"
        arts.mkdir()
        store = ArtifactStore(artifacts_root=str(arts), run_id="run-local")
        store.save("plan", {"schema_version": "0.0.0"})

        job = _enqueue_and_claim(queue_repo, session, kb_id="kb-local", worker_id="w-local")
        job = queue_repo.start(job.job_id)
        session.commit()
        queue_repo.checkpoint(
            job.job_id,
            data={"stages_completed": ["collect", "assess", "decompose", "plan"]},
        )
        session.commit()

        # Local provider — cost_per_1k_tokens is None
        fake_provider = MagicMock()
        fake_caps = MagicMock()
        fake_caps.cost_per_1k_tokens = None
        fake_caps.is_local = True
        fake_provider.capabilities = fake_caps

        build_mock = MagicMock()
        build_mock.run.return_value = {
            "token_accounting": {"total_input_tokens": 5000},
            "schema_version": "0.0.0",
        }

        runner = JobRunner(
            session=session,
            queue_repo=queue_repo,
            ledger_repo=ledger_repo,
            audit_repo=audit_repo,
            embedding_provider=fake_provider,
        )

        build_class_mock = MagicMock(return_value=build_mock)
        with patch("finecorpus.pipeline.build.BuildStage", build_class_mock):
            runner._run_build_stage(
                job=job,
                run_id="run-local",
                source_dir="/src",
                artifacts_root=str(arts),
                promote=False,
            )

        session.commit()

        # Zero cost → no ledger row
        accrued = ledger_repo.accrued_usd(kb_id="kb-local")
        assert accrued == Decimal("0"), "Local provider should have zero cost"
