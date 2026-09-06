"""Regression tests for the PR #34 review findings.

B-1: cap-hit governance was dead code — the counter helpers were never called
from the production pause path, so the M-085 alert could never fire.
M-1: assert_incremental_allowed was never called at dispatch — a queued
reindex_incremental could run against a changed config_version (M-053 gap).
M-2: a never-fired cron trigger fired immediately on first evaluation.
m-1: the coalesced flag was false for the common queued-coalesce case.
m-3: queue mode ignored the collect artifact's acknowledged_permission_gap.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from finecorpus.control.audit import AuditLogRepository
from finecorpus.control.cost_ledger import BudgetDecision
from finecorpus.control.jobs import JobQueueRepository, JobState
from finecorpus.control.metadata import create_tables
from finecorpus.control.reindex import ReindexTriggerRepository
from finecorpus.pipeline.jobs import JobRunner
from finecorpus.pipeline.reindex import evaluate_triggers, trigger_manual_reindex

KB = "kb-pr34"
WS = "ws-pr34"


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite://")
    create_tables(engine)
    return Session(engine)


class _PauseGuard:
    """BudgetGuard stand-in that always pauses on the KB cap."""

    def check(self, **_: Any) -> BudgetDecision:
        return BudgetDecision.pause_kb_cap


class _StubConfig:
    """Minimal config carrying what reindex helpers read."""

    class budgets:
        scheduled_reindex_cap_hit_alert_count = 3

    class index_lifecycle:
        scheduled_reindex_cron = None

    class providers:
        class embedding:
            provider = "fake"
            model = "fake-model"


class TestCapHitGovernanceProductionPath:
    def test_budget_pause_increments_trigger_counter(self, session: Session) -> None:
        trig_repo = ReindexTriggerRepository(session)
        trig, _ = trig_repo.get_or_create(kb_id=KB, trigger_type="scheduled")
        session.flush()

        queue = JobQueueRepository(session)
        job = queue.enqueue(
            kb_id=KB,
            workspace_id=WS,
            job_type="reindex_incremental",
            payload={"trigger": "scheduled", "trigger_id": trig.trigger_id},
            dedupe_key=f"{KB}:scheduled:test",
        )
        session.flush()
        claimed = queue.claim("w-test")
        assert claimed is not None and claimed.job_id == job.job_id

        runner = JobRunner(
            session=session,
            queue_repo=queue,
            budget_guard=_PauseGuard(),  # type: ignore[arg-type]
            audit_repo=AuditLogRepository(session),
            config=_StubConfig(),
            worker_id="w-test",
        )
        runner.run(claimed)

        session.expire_all()
        assert claimed.state == JobState.paused_budget
        refreshed, _ = trig_repo.get_or_create(kb_id=KB, trigger_type="scheduled")
        assert refreshed.consecutive_cap_hits == 1, (
            "budget pause must increment consecutive_cap_hits via the "
            "production path (B-1: previously dead code)"
        )


class TestIncrementalRefusedOnConfigChange:
    def test_stale_config_version_fails_job(self, session: Session) -> None:
        queue = JobQueueRepository(session)
        queue.enqueue(
            kb_id=KB,
            workspace_id=WS,
            job_type="reindex_incremental",
            payload={"trigger": "scheduled", "config_version": "STALE-VERSION"},
            dedupe_key=f"{KB}:scheduled:stale",
        )
        session.flush()
        claimed = queue.claim("w-test")
        assert claimed is not None

        runner = JobRunner(
            session=session,
            queue_repo=queue,
            config=_StubConfig(),
            worker_id="w-test",
        )
        with pytest.raises(Exception, match="M-053|reindex_full"):
            runner.run(claimed)
        session.expire_all()
        assert claimed.state == JobState.failed
        assert claimed.error_msg and "full" in claimed.error_msg.lower()


class TestNeverFiredCronAnchors:
    def test_first_evaluation_anchors_without_firing(self, session: Session) -> None:
        trig_repo = ReindexTriggerRepository(session)
        trig, _ = trig_repo.get_or_create(kb_id=KB, trigger_type="scheduled")
        trig.cron_expr = "0 3 * * *"
        trig.enabled = True
        session.flush()

        noon = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)
        infos = evaluate_triggers(session, _StubConfig(), adapter=None, now=noon, kb_id=KB)
        scheduled = [i for i in infos if getattr(i, "trigger_type", "") == "scheduled"]
        assert not scheduled, "never-fired trigger must anchor, not fire immediately"
        session.expire_all()
        refreshed, _ = trig_repo.get_or_create(kb_id=KB, trigger_type="scheduled")
        assert refreshed.last_fired_at is not None, (
            "first evaluation must record the anchor timestamp"
        )

    def test_fires_when_due_after_anchor(self, session: Session) -> None:
        trig_repo = ReindexTriggerRepository(session)
        trig, _ = trig_repo.get_or_create(kb_id=KB, trigger_type="scheduled")
        trig.cron_expr = "0 3 * * *"
        trig.enabled = True
        session.flush()

        noon = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)
        evaluate_triggers(session, _StubConfig(), adapter=None, now=noon, kb_id=KB)

        next_day = datetime(2026, 9, 6, 3, 1, tzinfo=UTC)
        infos = evaluate_triggers(session, _StubConfig(), adapter=None, now=next_day, kb_id=KB)
        scheduled = [i for i in infos if getattr(i, "trigger_type", "") == "scheduled"]
        assert scheduled, "anchored trigger must fire once the cron time passes"


class TestCoalescedFlagTruthful:
    def test_second_manual_trigger_reports_coalesced(self, session: Session) -> None:
        info1 = trigger_manual_reindex(
            session, kb_id=KB, workspace_id=WS, full=True, config_version="cv1"
        )
        info2 = trigger_manual_reindex(
            session, kb_id=KB, workspace_id=WS, full=True, config_version="cv1"
        )
        assert info2.job_id == info1.job_id
        assert info2.coalesced is True, "queued-state coalesce must report coalesced=True (m-1)"
        assert info1.coalesced is False


class TestD16AckThreadedInQueueMode:
    def test_plan_stage_receives_ack_from_collect_artifact(
        self, session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, Any] = {}

        import finecorpus.pipeline.plan as plan_mod

        real_plan_stage = plan_mod.PlanStage

        def _capture(*args: Any, **kwargs: Any) -> Any:
            captured.update(kwargs)

            class _NoopStage:
                def run(self, **_: Any) -> None:
                    return None

            return _NoopStage()

        monkeypatch.setattr(plan_mod, "PlanStage", _capture)

        class _FakeStore:
            def __init__(self) -> None:
                self.artifacts = {
                    "collect": {
                        "source_run": {"acknowledged_permission_gap": True},
                        "items": [],
                    },
                    "assess": {},
                    "decompose": {},
                }

            def load(self, name: str) -> Any:
                return self.artifacts[name]

        queue = JobQueueRepository(session)
        job = queue.enqueue(
            kb_id=KB,
            workspace_id=WS,
            job_type="ingest",
            payload={},
            dedupe_key=None,
        )
        session.flush()

        runner = JobRunner(session=session, queue_repo=queue, config=_StubConfig(), worker_id="w")

        # Drive only the plan-construction slice of _run_pre_build_stages
        monkeypatch.setattr(
            "finecorpus.pipeline.artifact_store.ArtifactStore",
            lambda **_: _FakeStore(),
        )
        try:
            runner._run_pre_build_stages(  # noqa: SLF001
                job,
                "run-x",
                "/tmp/nonexistent-src",
                "/tmp/nonexistent-art",
                ["collect", "assess", "decompose"],
            )
        except Exception:
            pass  # later stages may fail on fake data; we only assert the capture

        assert captured.get("acknowledged_permission_gap") is True, (
            "queue mode must thread source_run.acknowledged_permission_gap "
            "from the collect artifact into PlanStage (m-3)"
        )
        assert real_plan_stage is not None
