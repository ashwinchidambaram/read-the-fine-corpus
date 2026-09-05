"""Phase 4 jobs CLI tests.

Coverage:
  - corpus jobs enqueue → job created with correct type/kb_id/state
  - corpus jobs list → tabular output, kb_id filter, state filter
  - corpus jobs status → detailed job info
  - corpus jobs resume → paused_budget → queued
  - corpus jobs cancel → queued → cancelled
  - Direct-mode budget: estimate above/below confirmation threshold behaviour
"""

from __future__ import annotations

import argparse
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from finecorpus.control.jobs import JobQueueRepository, JobState
from finecorpus.control.metadata import create_tables
from finecorpus.pipeline.jobs import cancel_job, enqueue_job, get_job, list_jobs, resume_job

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def engine():
    eng = create_engine("sqlite://")
    create_tables(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def session(engine):
    with Session(engine) as s:
        yield s


# ---------------------------------------------------------------------------
# Library helper tests (enqueue_job / list_jobs / get_job / resume_job / cancel_job)
# ---------------------------------------------------------------------------


class TestEnqueueJob:
    def test_enqueue_creates_job(self, session):
        job = enqueue_job(
            session=session,
            kb_id="kb-enq-1",
            workspace_id="ws-1",
            job_type="ingest",
            payload={"run_id": "r1"},
        )
        assert job.job_id is not None
        assert job.state == str(JobState.queued)
        assert job.kb_id == "kb-enq-1"
        assert job.job_type == "ingest"

    def test_enqueue_unknown_type_raises(self, session):
        with pytest.raises(ValueError, match="Unknown job_type"):
            enqueue_job(
                session=session,
                kb_id="kb-bad",
                workspace_id="ws-1",
                job_type="unknown_type",
                payload={},
            )

    def test_enqueue_dedupe_coalesces(self, session):
        j1 = enqueue_job(
            session=session,
            kb_id="kb-dedupe",
            workspace_id="ws-1",
            job_type="reindex_full",
            payload={},
            dedupe_key="dk-1",
        )
        j2 = enqueue_job(
            session=session,
            kb_id="kb-dedupe",
            workspace_id="ws-1",
            job_type="reindex_full",
            payload={},
            dedupe_key="dk-1",
        )
        assert j1.job_id == j2.job_id, "dedupe should coalesce to the same job"

    def test_enqueue_all_valid_types(self, session):
        for jt in ["ingest", "reindex_full", "reindex_incremental", "restore", "purge"]:
            job = enqueue_job(
                session=session,
                kb_id=f"kb-type-{jt}",
                workspace_id="ws-1",
                job_type=jt,
                payload={},
            )
            assert job.job_type == jt


class TestListJobs:
    def test_list_all(self, session):
        enqueue_job(
            session=session,
            kb_id="kb-list-1",
            workspace_id="ws-1",
            job_type="ingest",
            payload={},
        )
        jobs = list_jobs(session=session)
        assert len(jobs) >= 1

    def test_list_filter_kb(self, session):
        enqueue_job(
            session=session,
            kb_id="kb-list-filter",
            workspace_id="ws-1",
            job_type="ingest",
            payload={},
        )
        jobs = list_jobs(session=session, kb_id="kb-list-filter")
        assert all(j.kb_id == "kb-list-filter" for j in jobs)
        assert len(jobs) >= 1

    def test_list_filter_state(self, session):
        enqueue_job(
            session=session,
            kb_id="kb-list-state",
            workspace_id="ws-1",
            job_type="ingest",
            payload={},
        )
        jobs_queued = list_jobs(session=session, state="queued")
        assert all(j.state == "queued" for j in jobs_queued)

    def test_list_limit(self, session):
        for i in range(5):
            enqueue_job(
                session=session,
                kb_id=f"kb-limit-{i}",
                workspace_id="ws-1",
                job_type="ingest",
                payload={},
            )
        jobs = list_jobs(session=session, limit=2)
        assert len(jobs) <= 2


class TestGetJob:
    def test_get_existing(self, session):
        job = enqueue_job(
            session=session,
            kb_id="kb-get-1",
            workspace_id="ws-1",
            job_type="ingest",
            payload={},
        )
        found = get_job(session=session, job_id=job.job_id)
        assert found is not None
        assert found.job_id == job.job_id

    def test_get_missing_returns_none(self, session):
        found = get_job(session=session, job_id="nonexistent-job-id-xyz")
        assert found is None


class TestResumeJob:
    def test_resume_paused_job(self, session):
        enqueue_job(
            session=session,
            kb_id="kb-resume-1",
            workspace_id="ws-1",
            job_type="ingest",
            payload={},
        )
        repo = JobQueueRepository(session)
        claimed = repo.claim("w1")
        assert claimed is not None
        repo.start(claimed.job_id)
        repo.pause_budget(claimed.job_id)
        session.commit()

        resumed = resume_job(session=session, job_id=claimed.job_id)
        assert resumed.state == str(JobState.queued)

    def test_resume_non_paused_raises(self, session):
        job = enqueue_job(
            session=session,
            kb_id="kb-resume-2",
            workspace_id="ws-1",
            job_type="ingest",
            payload={},
        )
        with pytest.raises(ValueError, match="not paused_budget"):
            resume_job(session=session, job_id=job.job_id)


class TestCancelJob:
    def test_cancel_queued_job(self, session):
        job = enqueue_job(
            session=session,
            kb_id="kb-cancel-1",
            workspace_id="ws-1",
            job_type="ingest",
            payload={},
        )
        cancelled = cancel_job(session=session, job_id=job.job_id)
        assert cancelled.state == str(JobState.cancelled)

    def test_cancel_running_job_raises(self, session):
        enqueue_job(
            session=session,
            kb_id="kb-cancel-2",
            workspace_id="ws-1",
            job_type="ingest",
            payload={},
        )
        repo = JobQueueRepository(session)
        claimed = repo.claim("w-cancel")
        assert claimed is not None
        repo.start(claimed.job_id)
        session.commit()

        with pytest.raises(ValueError, match="only queued/paused_budget"):
            cancel_job(session=session, job_id=claimed.job_id)

    def test_cancel_nonexistent_raises(self, session):
        with pytest.raises(KeyError):
            cancel_job(session=session, job_id="does-not-exist")


# ---------------------------------------------------------------------------
# Direct-mode budget confirmation threshold tests
# ---------------------------------------------------------------------------


class TestConfirmationThreshold:
    """corpus pipeline run confirmation threshold (§16 budget config)."""

    def _make_estimate(self, total_cost_usd: Decimal) -> Any:
        """Minimal IngestionCostEstimate-like object."""
        est = MagicMock()
        est.total_cost_usd = total_cost_usd
        # Not a CostEstimateUnavailable
        from finecorpus.pipeline.costing import CostEstimateUnavailable

        assert not isinstance(est, CostEstimateUnavailable)
        return est

    def test_below_threshold_no_confirmation(self):
        """Estimate below threshold → _requires_confirmation returns False."""
        from finecorpus.cli.main import _requires_confirmation

        args = argparse.Namespace(config="corpus.yaml")
        estimate = self._make_estimate(Decimal("0.50"))

        # Mock load_config to return a config with threshold = $10
        mock_cfg = MagicMock()
        mock_cfg.budgets.ingestion_confirmation_threshold_usd = Decimal("10.00")

        with patch("finecorpus.cli.main._requires_confirmation.__wrapped__", None, create=True):
            with patch("finecorpus.config.loader.load_config", return_value=mock_cfg):
                result = _requires_confirmation(estimate, args)

        # $0.50 < $10.00 → no confirmation required
        assert result is False

    def test_above_threshold_requires_confirmation(self):
        """Estimate above threshold → _requires_confirmation returns True."""
        from finecorpus.cli.main import _requires_confirmation

        args = argparse.Namespace(config="corpus.yaml")
        estimate = self._make_estimate(Decimal("15.00"))

        mock_cfg = MagicMock()
        mock_cfg.budgets.ingestion_confirmation_threshold_usd = Decimal("10.00")

        with patch("finecorpus.config.loader.load_config", return_value=mock_cfg):
            result = _requires_confirmation(estimate, args)

        # $15.00 > $10.00 → confirmation required
        assert result is True

    def test_unavailable_always_requires_confirmation(self):
        """CostEstimateUnavailable → always requires confirmation."""
        from finecorpus.cli.main import _requires_confirmation
        from finecorpus.pipeline.costing import CostEstimateUnavailable

        args = argparse.Namespace(config="corpus.yaml")
        unavailable = CostEstimateUnavailable(provider_name="openai", reason="no key")

        result = _requires_confirmation(unavailable, args)
        assert result is True

    def test_config_load_failure_requires_confirmation(self):
        """Config load failure → fall back to always-confirm (safe default)."""
        from finecorpus.cli.main import _requires_confirmation

        args = argparse.Namespace(config="corpus.yaml")
        estimate = self._make_estimate(Decimal("0.01"))

        with patch(
            "finecorpus.config.loader.load_config", side_effect=FileNotFoundError("no config")
        ):
            result = _requires_confirmation(estimate, args)

        # Safe default: confirm when config unavailable
        assert result is True

    def test_exact_threshold_proceeds_without_prompt(self):
        """Estimate exactly at threshold → no confirmation needed (≤ threshold)."""
        from finecorpus.cli.main import _requires_confirmation

        args = argparse.Namespace(config="corpus.yaml")
        estimate = self._make_estimate(Decimal("10.00"))

        mock_cfg = MagicMock()
        mock_cfg.budgets.ingestion_confirmation_threshold_usd = Decimal("10.00")

        with patch("finecorpus.config.loader.load_config", return_value=mock_cfg):
            result = _requires_confirmation(estimate, args)

        # $10.00 ≤ $10.00 → no confirmation required
        assert result is False
