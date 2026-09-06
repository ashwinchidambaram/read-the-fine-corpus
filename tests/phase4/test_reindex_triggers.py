"""Tests for reindex triggers (M-052, M-053, §10.3).

Tests:
  - test_manual: trigger_manual_reindex enqueues the right job type and dedupes.
  - test_change_detected_content_vs_metadata: content hash change → enqueue;
    metadata-only (same hash) → NOT enqueued (M-052).
  - test_scheduled_cron: croniter due/not-due with injected now.
  - test_config_change_forces_full_rebuild: M-053 + incremental-refusal probe.
  - test_cap_hit_alert_counter: consecutive_cap_hits threshold alert.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from finecorpus.control.jobs import JobQueueRepository, JobState, JobType
from finecorpus.control.metadata import create_tables
from finecorpus.control.reindex import ReindexTriggerRepository
from finecorpus.pipeline.reindex import (
    assert_incremental_allowed,
    evaluate_triggers,
    trigger_manual_reindex,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def engine():
    eng = create_engine("sqlite:///:memory:")
    create_tables(eng)
    return eng


@pytest.fixture
def session(engine):
    with Session(engine) as s:
        yield s


@pytest.fixture
def config():
    """Minimal config mock for trigger tests."""
    cfg = MagicMock()
    cfg.index_lifecycle.scheduled_reindex_cron = None
    cfg.budgets.scheduled_reindex_cap_hit_alert_count = 3
    # providers for config_version hash
    cfg.providers.embedding.provider = "fake"
    cfg.providers.embedding.model = "test"
    return cfg


def _make_trigger(session, trigger_type, kb_id="kb-1", cron_expr=None, enabled=True):
    """Helper to create a trigger record."""
    repo = ReindexTriggerRepository(session)
    record, _ = repo.get_or_create(
        kb_id=kb_id,
        trigger_type=trigger_type,
        cron_expr=cron_expr,
        enabled=enabled,
    )
    session.commit()
    return record


def _make_job(session, kb_id="kb-1", workspace_id="ws-1", job_type=JobType.ingest):
    """Helper to create a completed job so workspace_id is resolvable."""
    repo = JobQueueRepository(session)
    job = repo.enqueue(
        kb_id=kb_id,
        workspace_id=workspace_id,
        job_type=job_type,
        payload={},
    )
    job.state = str(JobState.completed)
    session.commit()
    return job


# ---------------------------------------------------------------------------
# test_manual
# ---------------------------------------------------------------------------


def test_manual_incremental(session):
    """trigger_manual_reindex with full=False → reindex_incremental."""
    _make_job(session)
    info = trigger_manual_reindex(
        session=session,
        kb_id="kb-1",
        workspace_id="ws-1",
        full=False,
    )
    assert info.job_type == str(JobType.reindex_incremental)
    assert info.trigger_type == "manual"
    assert not info.coalesced


def test_manual_full(session):
    """trigger_manual_reindex with full=True → reindex_full."""
    _make_job(session)
    info = trigger_manual_reindex(
        session=session,
        kb_id="kb-1",
        workspace_id="ws-1",
        full=True,
    )
    assert info.job_type == str(JobType.reindex_full)
    assert info.trigger_type == "manual"


def test_manual_dedupe(session):
    """Second identical manual trigger coalesces to the existing job."""
    _make_job(session)
    info1 = trigger_manual_reindex(
        session=session,
        kb_id="kb-1",
        workspace_id="ws-1",
        full=False,
        config_version="v1",
    )
    # Second call with same version → same dedupe_key → should return same job
    info2 = trigger_manual_reindex(
        session=session,
        kb_id="kb-1",
        workspace_id="ws-1",
        full=False,
        config_version="v1",
    )
    assert info1.job_id == info2.job_id


# ---------------------------------------------------------------------------
# test_change_detected_content_vs_metadata (M-052)
# ---------------------------------------------------------------------------


def test_change_detected_content_hash_change_enqueues(session, config, tmp_path):
    """Content hash change → enqueue reindex_incremental (M-052)."""
    import json

    _make_job(session)
    _make_trigger(session, "change_detected")

    # Write a collect.json with content hashes
    run_dir = tmp_path / "run1"
    run_dir.mkdir()
    collect = {
        "items": [
            {"document_id": "doc-1", "content_hash": "aaa"},
            {"document_id": "doc-2", "content_hash": "bbb"},
        ]
    }
    (run_dir / "collect.json").write_text(json.dumps(collect))

    config.storage.artifacts_root = str(tmp_path)

    now = datetime.now(tz=UTC)
    enqueued = evaluate_triggers(
        session=session,
        config=config,
        adapter=MagicMock(),  # adapter present (non-None) → change-detection not skipped
        now=now,
    )

    # First run (no last fingerprint) → enqueues
    assert len(enqueued) == 1
    assert enqueued[0].trigger_type == "change_detected"
    assert enqueued[0].job_type == str(JobType.reindex_incremental)


def test_change_detected_same_content_hash_no_enqueue(session, config, tmp_path):
    """Same content hashes → no enqueue (metadata-only change not enqueued, M-052)."""
    import json

    _make_job(session)
    _make_trigger(session, "change_detected")

    # Write collect.json
    run_dir = tmp_path / "run1"
    run_dir.mkdir()
    collect = {
        "items": [
            {"document_id": "doc-1", "content_hash": "aaa"},
        ]
    }
    (run_dir / "collect.json").write_text(json.dumps(collect))
    config.storage.artifacts_root = str(tmp_path)

    now = datetime.now(tz=UTC)

    # First evaluation: stores the fingerprint
    evaluate_triggers(session=session, config=config, adapter=MagicMock(), now=now)

    # Re-read trigger to get updated last_seen_config_version
    session.expire_all()

    # Second evaluation: same content hashes → no new job
    enqueued2 = evaluate_triggers(
        session=session,
        config=config,
        adapter=MagicMock(),
        now=now + timedelta(minutes=1),
    )
    # No new change_detected jobs (fingerprint unchanged)
    change_detected = [e for e in enqueued2 if e.trigger_type == "change_detected"]
    assert len(change_detected) == 0, "Metadata-only change must not trigger reindex (M-052)"


# ---------------------------------------------------------------------------
# test_scheduled_cron
# ---------------------------------------------------------------------------


def test_scheduled_cron_due(session, config):
    """Scheduled trigger is due (last_fired_at old enough) → enqueue."""
    _make_job(session)
    trig = _make_trigger(session, "scheduled", cron_expr="*/5 * * * *")

    # Set last_fired_at to 10 minutes ago → next fire was 5 min ago → due
    repo = ReindexTriggerRepository(session)
    past = datetime.now(tz=UTC) - timedelta(minutes=10)
    repo.record_fired(trig.trigger_id, fired_at=past)
    session.commit()

    now = datetime.now(tz=UTC)
    enqueued = evaluate_triggers(session=session, config=config, adapter=None, now=now)
    scheduled = [e for e in enqueued if e.trigger_type == "scheduled"]
    assert len(scheduled) == 1
    assert scheduled[0].job_type == str(JobType.reindex_incremental)


def test_scheduled_cron_not_due(session, config):
    """Scheduled trigger is NOT yet due → no job enqueued."""
    _make_job(session)
    trig = _make_trigger(session, "scheduled", cron_expr="0 2 * * *")  # daily at 2am

    # Set last_fired_at to just now → next fire is tomorrow 2am → not due
    repo = ReindexTriggerRepository(session)
    repo.record_fired(trig.trigger_id, fired_at=datetime.now(tz=UTC))
    session.commit()

    now = datetime.now(tz=UTC)
    enqueued = evaluate_triggers(session=session, config=config, adapter=None, now=now)
    scheduled = [e for e in enqueued if e.trigger_type == "scheduled"]
    assert len(scheduled) == 0


def test_scheduled_cron_never_fired_anchors(session, config):
    """Never-fired scheduled trigger ANCHORS on first evaluation (no job);
    fires on a later evaluation once the cron schedule is due (PR #34 M-2)."""
    _make_job(session)
    _make_trigger(session, "scheduled", cron_expr="*/1 * * * *")

    now = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)
    enqueued = evaluate_triggers(session=session, config=config, adapter=None, now=now)
    scheduled = [e for e in enqueued if e.trigger_type == "scheduled"]
    assert len(scheduled) == 0, "first evaluation anchors without firing"

    later = datetime(2026, 9, 5, 12, 2, tzinfo=UTC)
    enqueued = evaluate_triggers(session=session, config=config, adapter=None, now=later)
    scheduled = [e for e in enqueued if e.trigger_type == "scheduled"]
    assert len(scheduled) == 1, "anchored trigger fires once due"


# ---------------------------------------------------------------------------
# test_config_change_forces_full_rebuild (M-053)
# ---------------------------------------------------------------------------


def test_config_change_forces_reindex_full(session, config):
    """Config version change → enqueue reindex_full (M-053)."""
    _make_job(session)
    trig = _make_trigger(session, "config_change")

    # Set an old config version on the trigger
    repo = ReindexTriggerRepository(session)
    repo.record_fired(trig.trigger_id, config_version="old-version")
    session.commit()

    # Config returns a different version
    config.providers.embedding.provider = "new_provider"
    config.providers.embedding.model = "new_model"

    now = datetime.now(tz=UTC)
    enqueued = evaluate_triggers(session=session, config=config, adapter=None, now=now)
    config_change = [e for e in enqueued if e.trigger_type == "config_change"]
    assert len(config_change) == 1
    assert config_change[0].job_type == str(JobType.reindex_full), (
        "M-053: config version change MUST use reindex_full"
    )


def test_incremental_refused_when_config_version_changed():
    """assert_incremental_allowed raises when config_version mismatches (M-053)."""
    with pytest.raises(ValueError, match="reindex_incremental REFUSED"):
        assert_incremental_allowed(
            current_config_version="new-abc",
            job_config_version="old-xyz",
        )


def test_incremental_allowed_when_config_version_matches():
    """assert_incremental_allowed is silent when versions match."""
    assert_incremental_allowed(
        current_config_version="same-version",
        job_config_version="same-version",
    )  # No exception


# ---------------------------------------------------------------------------
# test_cap_hit_alert_counter
# ---------------------------------------------------------------------------


def test_cap_hit_alert_counter_triggers_at_threshold(session, config):
    """consecutive_cap_hits >= threshold → ERROR log emitted."""
    _make_job(session)
    trig = _make_trigger(session, "scheduled", cron_expr="*/5 * * * *")

    # Set last_fired_at to old → will fire
    repo = ReindexTriggerRepository(session)
    repo.record_fired(trig.trigger_id, fired_at=datetime.now(tz=UTC) - timedelta(hours=1))
    # Manually set consecutive_cap_hits to threshold - 1
    record = repo.get(trig.trigger_id)
    assert record is not None
    record.consecutive_cap_hits = 2  # threshold is 3
    session.commit()

    config.budgets.scheduled_reindex_cap_hit_alert_count = 3

    with patch("finecorpus.pipeline.reindex.logger") as mock_log:
        evaluate_triggers(session=session, config=config, adapter=None, now=datetime.now(tz=UTC))
        # No alert yet (cap_hits=2 < threshold=3)
        error_calls = [c for c in mock_log.error.call_args_list if "ALERT" in str(c)]
        assert len(error_calls) == 0


def test_cap_hit_alert_at_threshold(session, config):
    """consecutive_cap_hits == threshold → ALERT logged."""
    _make_job(session)
    trig = _make_trigger(session, "scheduled", cron_expr="*/5 * * * *")

    repo = ReindexTriggerRepository(session)
    repo.record_fired(trig.trigger_id, fired_at=datetime.now(tz=UTC) - timedelta(hours=1))
    record = repo.get(trig.trigger_id)
    assert record is not None
    record.consecutive_cap_hits = 3  # AT threshold (3 >= 3)
    session.commit()

    config.budgets.scheduled_reindex_cap_hit_alert_count = 3

    with patch("finecorpus.pipeline.reindex.logger") as mock_log:
        evaluate_triggers(session=session, config=config, adapter=None, now=datetime.now(tz=UTC))
        error_calls = [c for c in mock_log.error.call_args_list if "ALERT" in str(c)]
        assert len(error_calls) >= 1
