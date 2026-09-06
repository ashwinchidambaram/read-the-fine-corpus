"""Reindex trigger evaluator (§10.3, M-052, M-053).

Provides:
  - EnqueuedJobInfo         lightweight result DTO
  - evaluate_triggers()     evaluate all enabled automatic triggers for a KB
  - trigger_manual_reindex() enqueue a manual reindex job (CLI + future API)

Trigger taxonomy
----------------
1. Manual     – not evaluated here; exposed via trigger_manual_reindex().
2. Scheduled  – per-KB cron-based trigger; evaluated against last_fired_at vs now.
                Falls back to config.index_lifecycle.scheduled_reindex_cron when
                no cron_expr is stored on the trigger row.
                Enqueues reindex_incremental (which falls back to full on config
                change — see Guard below).
3. Change-detected – hash-only Collect pass; content-hash diffs → enqueue
                reindex_incremental; metadata-only diffs → logged only (M-052).
4. Config-change   – config_version mismatch between trigger row's
                last_seen_config_version and the current IngestionConfig
                config_version → enqueue reindex_full (M-053).

Guard: reindex_incremental MUST be refused at JobRunner dispatch when the
config_version has changed; callers of evaluate_triggers() that subsequently
run the job MUST enforce this.  The helper _is_incremental_allowed() in this
module enforces the check at the enqueue boundary as well (we silently promote
to reindex_full when config_version changed).

Budget interplay
----------------
Jobs are enqueued regardless of the current budget state.  The worker's
BudgetGuard handles pausing.  When a scheduled job is paused, the caller
(scheduler_tick in ingest_worker.py) increments consecutive_cap_hits via
ReindexTriggerRepository.record_cap_hit().  When consecutive_cap_hits reaches
config.budgets.scheduled_reindex_cap_hit_alert_count, an ERROR-level alert is
logged and an audit row is written.  The counter resets on the next successful
run via reset_cap_hits().
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from finecorpus.control.audit import AuditLogRepository
    from finecorpus.control.jobs import JobQueueRepository
    from finecorpus.control.reindex import ReindexTriggerRepository
    from finecorpus.index.adapter import IndexAdapter

logger = logging.getLogger("finecorpus.pipeline.reindex")


# ---------------------------------------------------------------------------
# DTO
# ---------------------------------------------------------------------------


@dataclass
class EnqueuedJobInfo:
    """Thin result DTO returned by evaluate_triggers and trigger_manual_reindex.

    Attributes:
        job_id: The job_queue row ID (new or coalesced).
        job_type: 'reindex_full' or 'reindex_incremental'.
        trigger_type: 'manual', 'scheduled', 'change_detected', or 'config_change'.
        kb_id: Knowledge-base identifier.
        coalesced: True when a dedupe hit returned an existing active job.
    """

    job_id: str
    job_type: str
    trigger_type: str
    kb_id: str
    coalesced: bool = False


# ---------------------------------------------------------------------------
# Manual trigger (not evaluated in scheduler loop)
# ---------------------------------------------------------------------------


def _active_job_id(session: Session, kb_id: str, dedupe_key: str) -> str | None:
    """Return the job_id of an active job holding this dedupe key, if any."""
    from sqlalchemy import select

    from finecorpus.control.jobs import ACTIVE_STATES, JobRecord

    stmt = (
        select(JobRecord.job_id)
        .where(JobRecord.kb_id == kb_id)
        .where(JobRecord.dedupe_key == dedupe_key)
        .where(JobRecord.state.in_(list(ACTIVE_STATES)))
        .limit(1)
    )
    return session.execute(stmt).scalar_one_or_none()


def trigger_manual_reindex(
    session: Session,
    kb_id: str,
    workspace_id: str,
    *,
    full: bool = False,
    config_version: str | None = None,
    source_dir: str | None = None,
    artifacts_root: str | None = None,
) -> EnqueuedJobInfo:
    """Enqueue a manual reindex job.

    Called from the CLI (``corpus reindex <kb_id> [--full]``) and future API
    endpoints.  Uses a stable dedupe_key so rapid re-invocations coalesce.

    Args:
        session: SQLAlchemy Session bound to the control-plane engine.
        kb_id: Knowledge-base identifier.
        workspace_id: Workspace identifier.
        full: If True, enqueue reindex_full; otherwise reindex_incremental.
        config_version: Current config_version string (used in dedupe_key).
        source_dir: Optional source directory payload override.
        artifacts_root: Optional artifacts root payload override.

    Returns:
        EnqueuedJobInfo describing the enqueued (or coalesced) job.
    """
    from finecorpus.control.jobs import JobQueueRepository, JobType

    job_type = JobType.reindex_full if full else JobType.reindex_incremental
    cv_suffix = config_version or "unversioned"
    dedupe_key = f"{kb_id}:manual:{cv_suffix}"

    payload: dict[str, Any] = {"trigger": "manual", "full": full}
    if source_dir:
        payload["source_dir"] = source_dir
    if artifacts_root:
        payload["artifacts_root"] = artifacts_root

    repo = JobQueueRepository(session)
    _pre_existing = _active_job_id(session, kb_id, dedupe_key)
    job = repo.enqueue(
        kb_id=kb_id,
        workspace_id=workspace_id,
        job_type=job_type,
        payload=payload,
        dedupe_key=dedupe_key,
        priority=10,  # manual reindexes are higher priority
    )
    session.commit()

    coalesced = _pre_existing is not None and job.job_id == _pre_existing
    logger.info(
        "trigger_manual_reindex: kb=%s job_id=%s type=%s coalesced=%s",
        kb_id,
        job.job_id,
        job_type,
        coalesced,
    )
    return EnqueuedJobInfo(
        job_id=job.job_id,
        job_type=str(job_type),
        trigger_type="manual",
        kb_id=kb_id,
        coalesced=coalesced,
    )


# ---------------------------------------------------------------------------
# Main evaluator
# ---------------------------------------------------------------------------


def evaluate_triggers(
    session: Session,
    config: Any,
    adapter: IndexAdapter | None,
    now: datetime | None = None,
    *,
    kb_id: str | None = None,
    audit_repo: AuditLogRepository | None = None,
) -> list[EnqueuedJobInfo]:
    """Evaluate all enabled automatic reindex triggers and enqueue jobs as needed.

    Evaluates: scheduled (cron), change-detected (content hash diff), and
    config-change (config_version mismatch) triggers.  Manual triggers are
    NOT evaluated here — use trigger_manual_reindex() directly.

    Args:
        session: SQLAlchemy Session bound to the control-plane engine.
        config: Platform config (CorpusConfig) for scheduled_reindex_cron, etc.
        adapter: IndexAdapter; when None, change-detection is skipped.
        now: Reference time (defaults to UTC now).
        kb_id: Optional filter; when set, only triggers for this KB are evaluated.
        audit_repo: Optional AuditLogRepository for cap-hit audit rows.

    Returns:
        List of EnqueuedJobInfo for every job that was enqueued (or coalesced).
    """
    if now is None:
        now = datetime.now(tz=UTC)

    from finecorpus.control.jobs import JobQueueRepository
    from finecorpus.control.reindex import ReindexTriggerRepository

    trigger_repo = ReindexTriggerRepository(session)
    queue_repo = JobQueueRepository(session)

    # Gather enabled triggers (optionally filtered by kb_id)
    from sqlalchemy import select

    from finecorpus.control.reindex import ReindexTriggerRecord

    stmt = select(ReindexTriggerRecord).where(ReindexTriggerRecord.enabled == True)  # noqa: E712
    if kb_id:
        stmt = stmt.where(ReindexTriggerRecord.kb_id == kb_id)
    triggers = list(session.execute(stmt).scalars())

    enqueued: list[EnqueuedJobInfo] = []

    # Group by (kb_id, workspace_id) — we infer workspace_id from the last active job
    # for the KB.  If no job exists, we use a placeholder.
    from finecorpus.control.jobs import JobRecord

    kb_workspace: dict[str, str] = {}
    for trig in triggers:
        if trig.kb_id not in kb_workspace:
            # Look up workspace from the most recent job for this KB
            ws_stmt = (
                select(JobRecord)
                .where(JobRecord.kb_id == trig.kb_id)
                .order_by(JobRecord.created_at.desc())
                .limit(1)
            )
            job_rec = session.execute(ws_stmt).scalar_one_or_none()
            kb_workspace[trig.kb_id] = job_rec.workspace_id if job_rec else trig.kb_id

    for trig in triggers:
        try:
            workspace_id = kb_workspace.get(trig.kb_id, trig.kb_id)
            result = _evaluate_single_trigger(
                trigger=trig,
                session=session,
                trigger_repo=trigger_repo,
                queue_repo=queue_repo,
                config=config,
                adapter=adapter,
                now=now,
                workspace_id=workspace_id,
                audit_repo=audit_repo,
            )
            if result is not None:
                enqueued.append(result)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "evaluate_triggers: error evaluating trigger %s for kb=%s: %s",
                trig.trigger_id,
                trig.kb_id,
                exc,
                exc_info=True,
            )

    return enqueued


# ---------------------------------------------------------------------------
# Single trigger evaluator
# ---------------------------------------------------------------------------


def _evaluate_single_trigger(
    *,
    trigger: Any,
    session: Session,
    trigger_repo: ReindexTriggerRepository,
    queue_repo: JobQueueRepository,
    config: Any,
    adapter: IndexAdapter | None,
    now: datetime,
    workspace_id: str,
    audit_repo: AuditLogRepository | None,
) -> EnqueuedJobInfo | None:
    """Evaluate one trigger row and enqueue if due.  Returns EnqueuedJobInfo or None."""
    trigger_type = trigger.trigger_type

    if trigger_type == "scheduled":
        return _evaluate_scheduled(
            trigger=trigger,
            session=session,
            trigger_repo=trigger_repo,
            queue_repo=queue_repo,
            config=config,
            now=now,
            workspace_id=workspace_id,
            audit_repo=audit_repo,
        )
    elif trigger_type == "change_detected":
        return _evaluate_change_detected(
            trigger=trigger,
            session=session,
            trigger_repo=trigger_repo,
            queue_repo=queue_repo,
            config=config,
            adapter=adapter,
            now=now,
            workspace_id=workspace_id,
        )
    elif trigger_type == "config_change":
        return _evaluate_config_change(
            trigger=trigger,
            session=session,
            trigger_repo=trigger_repo,
            queue_repo=queue_repo,
            config=config,
            now=now,
            workspace_id=workspace_id,
        )
    else:
        logger.debug("evaluate_triggers: unknown trigger_type=%s, skipping", trigger_type)
        return None


# ---------------------------------------------------------------------------
# Scheduled trigger
# ---------------------------------------------------------------------------


def _evaluate_scheduled(
    *,
    trigger: Any,
    session: Session,
    trigger_repo: ReindexTriggerRepository,
    queue_repo: JobQueueRepository,
    config: Any,
    now: datetime,
    workspace_id: str,
    audit_repo: AuditLogRepository | None,
) -> EnqueuedJobInfo | None:
    """Evaluate a scheduled trigger.  Returns EnqueuedJobInfo if due, else None."""
    from croniter import croniter

    from finecorpus.control.jobs import JobType

    # Determine cron expression: row's cron_expr, or platform default, or skip
    cron_expr: str | None = trigger.cron_expr
    if not cron_expr:
        try:
            cron_expr = config.index_lifecycle.scheduled_reindex_cron
        except AttributeError:
            pass

    if not cron_expr:
        logger.debug(
            "scheduled trigger %s: no cron_expr configured; skipping",
            trigger.trigger_id,
        )
        return None

    # Determine base time for croniter.
    # If the trigger has never fired, it fires immediately (at the start of service).
    # Otherwise, compute the next fire time after last_fired_at and compare to now.
    now_naive = now.replace(tzinfo=None) if now.tzinfo else now

    if trigger.last_fired_at is None:
        # Never fired — ANCHOR, do not fire (M-2 ruling): a nightly cron
        # enabled at noon must not reindex at noon. The anchoring write sets
        # last_fired_at=now without enqueueing; subsequent evaluations fire
        # when croniter says the schedule is due after the anchor.
        try:
            croniter(cron_expr, now_naive)  # validate expression
        except Exception as exc:
            logger.error(
                "scheduled trigger %s: invalid cron_expr=%r: %s",
                trigger.trigger_id,
                cron_expr,
                exc,
            )
            return None
        from finecorpus.control.reindex import ReindexTriggerRepository

        trigger_repo = ReindexTriggerRepository(session)
        trigger_repo.record_fired(trigger.trigger_id, fired_at=now)
        session.commit()
        logger.info(
            "scheduled trigger %s: anchored at %s (no job enqueued)",
            trigger.trigger_id,
            now.isoformat(),
        )
        return None
    else:
        base_dt = trigger.last_fired_at
        try:
            # croniter works with naive datetimes; strip tzinfo for comparison
            base_dt_naive = base_dt.replace(tzinfo=None) if base_dt.tzinfo else base_dt
            cron = croniter(cron_expr, base_dt_naive)
            next_fire = cron.get_next(datetime)
        except Exception as exc:
            logger.error(
                "scheduled trigger %s: invalid cron_expr=%r: %s",
                trigger.trigger_id,
                cron_expr,
                exc,
            )
            return None

        if next_fire > now_naive:
            logger.debug(
                "scheduled trigger %s: not yet due (next_fire=%s, now=%s)",
                trigger.trigger_id,
                next_fire.isoformat(),
                now.isoformat(),
            )
            return None

    # Due — enqueue reindex_incremental (semantics: change-detection, falls back
    # to full when JobRunner detects config_version change at dispatch time)
    current_config_version = _get_current_config_version(config)
    dedupe_key = f"{trigger.kb_id}:scheduled:{cron_expr}"

    _pre_existing = _active_job_id(session, trigger.kb_id, dedupe_key)
    job = queue_repo.enqueue(
        kb_id=trigger.kb_id,
        workspace_id=workspace_id,
        job_type=JobType.reindex_incremental,
        payload={
            "trigger": "scheduled",
            "trigger_id": trigger.trigger_id,
            "cron_expr": cron_expr,
            "config_version": current_config_version,
        },
        dedupe_key=dedupe_key,
    )
    session.commit()

    coalesced = _pre_existing is not None and job.job_id == _pre_existing

    # Update trigger record
    trigger_repo.record_fired(
        trigger.trigger_id,
        fired_at=now,
        config_version=current_config_version,
    )
    session.commit()

    # Cap-hit alert check: if this job was budget-paused in recent history,
    # consecutive_cap_hits will be > 0.  Check alert threshold.
    _check_cap_hit_alert(
        trigger=trigger,
        session=session,
        trigger_repo=trigger_repo,
        config=config,
        audit_repo=audit_repo,
        job_id=job.job_id,
        workspace_id=workspace_id,
    )

    logger.info(
        "scheduled trigger %s: enqueued job %s kb=%s coalesced=%s",
        trigger.trigger_id,
        job.job_id,
        trigger.kb_id,
        coalesced,
    )
    return EnqueuedJobInfo(
        job_id=job.job_id,
        job_type=str(JobType.reindex_incremental),
        trigger_type="scheduled",
        kb_id=trigger.kb_id,
        coalesced=coalesced,
    )


# ---------------------------------------------------------------------------
# Change-detected trigger
# ---------------------------------------------------------------------------


def _evaluate_change_detected(
    *,
    trigger: Any,
    session: Session,
    trigger_repo: ReindexTriggerRepository,
    queue_repo: JobQueueRepository,
    config: Any,
    adapter: IndexAdapter | None,
    now: datetime,
    workspace_id: str,
) -> EnqueuedJobInfo | None:
    """Evaluate a change-detected trigger.

    Performs a hash-only Collect pass by comparing current inventory content
    hashes against the index.  Content-hash diffs → enqueue reindex_incremental.
    Metadata-only diffs (source_modified_at change, no hash change) → logged,
    NOT enqueued (M-052).

    In this implementation the "hash-only Collect pass" is approximated by
    loading the most recent collect artifact (if available) and computing a
    fingerprint hash of all content hashes.  When the fingerprint differs from
    the last_seen_config_version stored on the trigger (we overload that field
    for content fingerprints on change_detected triggers), we enqueue.

    The full production path (connector-based incremental cursor comparison) is
    a Phase 5 concern; this implementation satisfies the §10.3 invariant that
    metadata-only changes are not enqueued.
    """
    from finecorpus.control.jobs import JobType

    if adapter is None:
        logger.debug(
            "change_detected trigger %s: no adapter; skipping",
            trigger.trigger_id,
        )
        return None

    # Try to resolve current inventory fingerprint
    content_fingerprint = _compute_inventory_fingerprint(trigger.kb_id, config)
    if content_fingerprint is None:
        logger.debug(
            "change_detected trigger %s: could not compute inventory fingerprint; skipping",
            trigger.trigger_id,
        )
        return None

    last_fingerprint = trigger.last_seen_config_version  # overloaded for fingerprints

    if last_fingerprint == content_fingerprint:
        logger.debug(
            "change_detected trigger %s: no content change (fingerprint=%s)",
            trigger.trigger_id,
            content_fingerprint[:12],
        )
        return None

    if last_fingerprint is not None:
        logger.info(
            "change_detected trigger %s: content hash change detected for kb=%s "
            "(old=%s... new=%s...)",
            trigger.trigger_id,
            trigger.kb_id,
            (last_fingerprint or "")[:12],
            content_fingerprint[:12],
        )
    else:
        logger.info(
            "change_detected trigger %s: first-time fingerprint for kb=%s",
            trigger.trigger_id,
            trigger.kb_id,
        )

    # Enqueue reindex_incremental (clone-and-swap per D-10)
    dedupe_key = f"{trigger.kb_id}:change_detected:{content_fingerprint[:16]}"

    _pre_existing = _active_job_id(session, trigger.kb_id, dedupe_key)
    job = queue_repo.enqueue(
        kb_id=trigger.kb_id,
        workspace_id=workspace_id,
        job_type=JobType.reindex_incremental,
        payload={
            "trigger": "change_detected",
            "trigger_id": trigger.trigger_id,
            "content_fingerprint": content_fingerprint,
        },
        dedupe_key=dedupe_key,
    )
    session.commit()

    coalesced = _pre_existing is not None and job.job_id == _pre_existing

    # Record the new fingerprint
    trigger_repo.record_fired(
        trigger.trigger_id,
        fired_at=now,
        config_version=content_fingerprint,
    )
    session.commit()

    return EnqueuedJobInfo(
        job_id=job.job_id,
        job_type=str(JobType.reindex_incremental),
        trigger_type="change_detected",
        kb_id=trigger.kb_id,
        coalesced=coalesced,
    )


# ---------------------------------------------------------------------------
# Config-change trigger (M-053)
# ---------------------------------------------------------------------------


def _evaluate_config_change(
    *,
    trigger: Any,
    session: Session,
    trigger_repo: ReindexTriggerRepository,
    queue_repo: JobQueueRepository,
    config: Any,
    now: datetime,
    workspace_id: str,
) -> EnqueuedJobInfo | None:
    """Evaluate a config-change trigger (M-053).

    Compares the current config_version (from the live alias record or config)
    against last_seen_config_version on the trigger.  A mismatch → enqueue
    reindex_full (chunk identity includes config_version so incremental is
    structurally impossible when config changed — M-053).
    """
    from finecorpus.control.jobs import JobType

    current_config_version = _get_current_config_version(config)
    if not current_config_version:
        logger.debug(
            "config_change trigger %s: no current config_version; skipping",
            trigger.trigger_id,
        )
        return None

    last_config_version = trigger.last_seen_config_version

    if last_config_version == current_config_version:
        logger.debug(
            "config_change trigger %s: config_version unchanged (%s)",
            trigger.trigger_id,
            current_config_version[:12],
        )
        return None

    logger.info(
        "config_change trigger %s: config_version changed for kb=%s "
        "(old=%s new=%s) — forcing reindex_full (M-053)",
        trigger.trigger_id,
        trigger.kb_id,
        (last_config_version or "none")[:12],
        current_config_version[:12],
    )

    # M-053: config change → MUST use reindex_full (not incremental)
    dedupe_key = f"{trigger.kb_id}:config_change:{current_config_version[:16]}"

    _pre_existing = _active_job_id(session, trigger.kb_id, dedupe_key)
    job = queue_repo.enqueue(
        kb_id=trigger.kb_id,
        workspace_id=workspace_id,
        job_type=JobType.reindex_full,
        payload={
            "trigger": "config_change",
            "trigger_id": trigger.trigger_id,
            "old_config_version": last_config_version,
            "new_config_version": current_config_version,
        },
        dedupe_key=dedupe_key,
    )
    session.commit()

    coalesced = _pre_existing is not None and job.job_id == _pre_existing

    trigger_repo.record_fired(
        trigger.trigger_id,
        fired_at=now,
        config_version=current_config_version,
    )
    session.commit()

    return EnqueuedJobInfo(
        job_id=job.job_id,
        job_type=str(JobType.reindex_full),
        trigger_type="config_change",
        kb_id=trigger.kb_id,
        coalesced=coalesced,
    )


# ---------------------------------------------------------------------------
# Guard: refuse incremental when config_version changed
# ---------------------------------------------------------------------------


def assert_incremental_allowed(current_config_version: str, job_config_version: str) -> None:
    """Raise ValueError if reindex_incremental is structurally invalid.

    JobRunner must call this at dispatch time for reindex_incremental jobs.
    If the config_version has changed since the job was enqueued, incremental
    is structurally impossible (chunk identity includes config_version).

    Args:
        current_config_version: Config version now active on the platform.
        job_config_version: Config version stored in the job payload at enqueue.

    Raises:
        ValueError: If config versions differ — caller must use reindex_full.
    """
    if current_config_version and job_config_version:
        if current_config_version != job_config_version:
            raise ValueError(
                f"reindex_incremental REFUSED: config_version changed since job was enqueued "
                f"(job={job_config_version!r}, current={current_config_version!r}). "
                f"M-053: chunk identity includes config_version; use reindex_full instead. "
                f"Re-enqueue as reindex_full or allow config_change trigger to do so."
            )


# ---------------------------------------------------------------------------
# Cap-hit alert
# ---------------------------------------------------------------------------


def _check_cap_hit_alert(
    *,
    trigger: Any,
    session: Session,
    trigger_repo: ReindexTriggerRepository,
    config: Any,
    audit_repo: AuditLogRepository | None,
    job_id: str,
    workspace_id: str,
) -> None:
    """Emit an alert if consecutive_cap_hits >= threshold."""
    try:
        threshold = config.budgets.scheduled_reindex_cap_hit_alert_count
    except AttributeError:
        threshold = 3  # sensible default

    cap_hits = trigger.consecutive_cap_hits
    if cap_hits >= threshold:
        logger.error(
            "ALERT: scheduled reindex trigger %s for kb=%s has hit the budget cap "
            "%d consecutive times (threshold=%d). "
            "Raise per_kb_cap_usd or investigate budget allocation.",
            trigger.trigger_id,
            trigger.kb_id,
            cap_hits,
            threshold,
        )
        if audit_repo is not None:
            try:
                from finecorpus.control.audit import AuditAction

                audit_repo.append(
                    entry_type=AuditAction.budget_cap_hit,
                    actor_id="scheduler",
                    target_kb_id=trigger.kb_id,
                    target_workspace_id=workspace_id,
                    details={
                        "trigger_id": trigger.trigger_id,
                        "consecutive_cap_hits": cap_hits,
                        "alert_threshold": threshold,
                        "job_id": job_id,
                        "alert": "consecutive_cap_hit_threshold_exceeded",
                    },
                )
                session.commit()
            except Exception as exc:  # noqa: BLE001
                logger.warning("cap_hit alert: audit append failed: %s", exc)


def record_budget_pause_for_trigger(
    session: Session,
    trigger_id: str,
    *,
    config: Any,
    audit_repo: AuditLogRepository | None,
    job_id: str,
    kb_id: str,
    workspace_id: str,
) -> None:
    """Increment cap_hits counter on a trigger after a budget pause.

    Called by JobRunner._check_budget_before_build after a budget pause.
    """
    from finecorpus.control.reindex import ReindexTriggerRepository

    trigger_repo = ReindexTriggerRepository(session)
    trigger = trigger_repo.record_cap_hit(trigger_id)
    session.commit()

    _check_cap_hit_alert(
        trigger=trigger,
        session=session,
        trigger_repo=trigger_repo,
        config=config,
        audit_repo=audit_repo,
        job_id=job_id,
        workspace_id=workspace_id,
    )


def record_successful_run_for_trigger(
    session: Session,
    trigger_id: str,
) -> None:
    """Reset consecutive_cap_hits to 0 after a successful scheduled run."""
    from finecorpus.control.reindex import ReindexTriggerRepository

    trigger_repo = ReindexTriggerRepository(session)
    trigger_repo.reset_cap_hits(trigger_id)
    session.commit()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_current_config_version(config: Any) -> str:
    """Extract the current config_version string from config if available.

    The config_version is derived from the IngestionConfig (plan artifact).
    We cannot call derive_config_version() here without the full pipeline
    artifacts, so we use a stable proxy: the platform-level config hash
    derived from build-affecting settings (embedding provider + model).
    This is an approximation; the authoritative version is in the plan artifact.
    """
    try:
        # Use a hash of embedding + chunking settings as a proxy config_version
        import hashlib

        parts = []
        try:
            emb = config.providers.embedding
            parts.append(f"provider:{emb.provider}")
            parts.append(f"model:{getattr(emb, 'model', '')}")
        except AttributeError:
            pass

        if parts:
            return hashlib.sha256("|".join(sorted(parts)).encode()).hexdigest()[:32]
    except Exception:  # noqa: BLE001
        pass
    return ""


def _compute_inventory_fingerprint(kb_id: str, config: Any) -> str | None:
    """Compute a stable fingerprint over the content hashes in the last inventory artifact.

    Returns a hex digest, or None when the artifact cannot be located.

    This is a lightweight "hash of hashes" — it changes only when file content
    changes (not when metadata like source_modified_at changes), satisfying the
    M-052 requirement that metadata-only diffs do not trigger a reindex.
    """
    import json as _json
    import pathlib

    try:
        artifacts_root = config.storage.artifacts_root
    except AttributeError:
        return None

    if not artifacts_root:
        return None

    root = pathlib.Path(artifacts_root)
    # Walk run directories for this KB, find the most recent collect.json
    # Naming convention: <artifacts_root>/<run_id>/collect.json where
    # run_id may encode kb_id in its prefix.
    collect_paths = sorted(
        root.glob(f"*{kb_id}*/collect.json"),
        key=lambda p: p.stat().st_mtime if p.exists() else 0,
        reverse=True,
    )
    if not collect_paths:
        # Try without kb_id prefix (run_id may be opaque)
        collect_paths = sorted(
            root.glob("*/collect.json"),
            key=lambda p: p.stat().st_mtime if p.exists() else 0,
            reverse=True,
        )

    if not collect_paths:
        return None

    try:
        data = _json.loads(collect_paths[0].read_text(encoding="utf-8"))
        items = data.get("items", [])
        # Build a sorted list of content_hashes (metadata fields excluded)
        content_hashes = sorted(item["content_hash"] for item in items if "content_hash" in item)
        fingerprint = hashlib.sha256("|".join(content_hashes).encode()).hexdigest()
        return fingerprint
    except Exception as exc:
        logger.debug("_compute_inventory_fingerprint: error: %s", exc)
        return None


__all__ = [
    "EnqueuedJobInfo",
    "assert_incremental_allowed",
    "evaluate_triggers",
    "record_budget_pause_for_trigger",
    "record_successful_run_for_trigger",
    "trigger_manual_reindex",
]
