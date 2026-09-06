"""Ingest worker — queue-driven job execution loop.

Architecture:
  - Claims jobs from job_queue using SELECT FOR UPDATE SKIP LOCKED (JobQueueRepository).
  - Executes each job via JobRunner (pipeline/jobs.py).
  - Writes a heartbeat file every loop iteration for Docker healthcheck liveness.
  - Graceful SIGTERM shutdown (finishes the current job before exiting).
  - scheduler_tick() is a named seam for the reindex-trigger evaluator (sibling unit).

The healthcheck validates both existence and recency (< 90 s) so a hung loop
goes unhealthy automatically.  The file path is published for external callers.
"""

from __future__ import annotations

import logging
import os
import pathlib
import signal
import time
from typing import Any

logger = logging.getLogger("finecorpus.ingest_worker")

# Written once per loop iteration so that the Docker healthcheck can confirm
# the worker is alive and not stuck.  The healthcheck validates both existence
# and recency (< 90 s) so a hung loop goes unhealthy automatically.
HEARTBEAT_FILE = pathlib.Path("/tmp/worker-heartbeat")

# Default poll interval (seconds) when not set by config
_DEFAULT_POLL_INTERVAL_S = 10

# Global shutdown flag (set by SIGTERM handler)
_SHUTDOWN = False


def _handle_sigterm(signum: int, frame: Any) -> None:
    """SIGTERM handler — set shutdown flag; current job finishes before exit."""
    global _SHUTDOWN  # noqa: PLW0603
    logger.info("ingest-worker: SIGTERM received; will shut down after current job")
    _SHUTDOWN = True


def _is_drift_cron_due(cron_expr: str | None, now: Any = None) -> bool:
    """Return True when a drift-detection cron run is due right now.

    Uses a simple stub implementation: fires on every call when the cron_expr
    is set (non-None, non-empty).  In production, replace with croniter or
    similar to honour the actual cron schedule.

    Args:
        cron_expr: Cron expression string, or None/empty to disable.
        now: Current UTC datetime (unused by stub; reserved for testing).

    Returns:
        True when a drift run is due.
    """
    if not cron_expr:
        return False
    # Stub: fires whenever cron is set.  A real implementation would use
    # croniter to evaluate whether the expression is satisfied at ``now``.
    return True


def scheduler_tick(
    session: Any = None,
    config: Any = None,
    adapter: Any = None,
    provider: Any = None,
) -> None:
    """Evaluate reindex triggers and enqueue reindex jobs as needed.

    Also runs cron-driven drift detection (§9.4) when
    observability.drift_detection_cron is set.

    Called once per worker loop iteration, before job claim.  Failure-isolated:
    any exception is caught and logged; the worker loop continues.

    When session/config are not provided (legacy no-arg call from the loop),
    this function is a no-op — the loop passes them explicitly when available.

    Args:
        session: Optional SQLAlchemy Session for the control-plane DB.
        config: Optional platform config (CorpusConfig).
        adapter: Optional IndexAdapter for drift scoring.  When None, cron drift
            is skipped (no-op) — the adapter is only available in the full
            worker context.
        provider: Optional EmbeddingProvider for drift scoring.  When None,
            cron drift is skipped.
    """
    if session is None or config is None:
        # Called without args from the legacy loop path — no-op.
        return

    # ---- Reindex trigger evaluation ----
    try:
        from finecorpus.pipeline.reindex import evaluate_triggers

        evaluate_triggers(
            session=session,
            config=config,
            adapter=None,  # adapter not wired here; change-detection uses None path
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("ingest-worker: scheduler_tick error: %s", exc, exc_info=True)

    # ---- Cron drift detection (§9.4) ----
    try:
        drift_cron: str | None = None
        try:
            drift_cron = config.observability.drift_detection_cron
        except AttributeError:
            pass

        if drift_cron and adapter is not None and provider is not None:
            if _is_drift_cron_due(drift_cron):
                _run_cron_drift_checks(
                    session=session,
                    config=config,
                    adapter=adapter,
                    provider=provider,
                )
        elif drift_cron:
            logger.debug(
                "ingest-worker: drift_detection_cron is set but adapter/provider "
                "not available — cron drift check skipped this tick"
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("ingest-worker: cron drift check error: %s", exc, exc_info=True)


def _run_cron_drift_checks(
    *,
    session: Any,
    config: Any,
    adapter: Any,
    provider: Any,
) -> None:
    """Run drift checks for all active KBs (cron path).

    Iterates over all alias records and runs run_drift_check for each KB
    that has a retained current baseline.  Failure-isolated per KB.

    Args:
        session: SQLAlchemy Session.
        config: CorpusConfig.
        adapter: IndexAdapter.
        provider: EmbeddingProvider.
    """
    from sqlalchemy import select as _select  # noqa: PLC0415

    from finecorpus.control.eval_store import EvalBaselineRepository  # noqa: PLC0415
    from finecorpus.control.metadata import AliasRecord  # noqa: PLC0415
    from finecorpus.services.eval_drift import run_drift_check  # noqa: PLC0415

    # Enumerate all registered KBs via alias records.
    try:
        all_aliases = list(session.execute(_select(AliasRecord)).scalars())
    except Exception as exc:  # noqa: BLE001
        logger.debug("ingest-worker: could not enumerate alias records for cron drift: %s", exc)
        return

    baseline_repo = EvalBaselineRepository(session)
    for alias_record in all_aliases:
        kb_id = alias_record.kb_id
        try:
            baseline = baseline_repo.get_current(kb_id)
            if baseline is None:
                continue
            result = run_drift_check(
                kb_id=kb_id,
                session=session,
                adapter=adapter,
                provider=provider,
                config=config,
            )
            logger.info(
                "ingest-worker: cron drift check for kb=%r → status=%s regressed=%s",
                kb_id,
                result.status,
                result.regressed,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "ingest-worker: cron drift check failed for kb=%r: %s",
                kb_id,
                exc,
                exc_info=True,
            )


def _touch_heartbeat() -> None:
    """Touch the heartbeat file so Docker healthcheck can verify liveness."""
    try:
        HEARTBEAT_FILE.touch()
    except OSError as exc:
        logger.warning("ingest-worker: could not touch heartbeat file: %s", exc)


def _run_one_job_isolated(
    job: Any,
    runner: Any,
) -> None:
    """Execute a single job with per-job error isolation.

    Exceptions from JobRunner are caught, the job is marked failed, and the
    worker loop continues.  This function is separated so tests can exercise
    the isolation logic directly.

    Args:
        job: The claimed JobRecord.
        runner: The JobRunner instance.
    """
    try:
        runner.run(job)
    except Exception as exc:  # noqa: BLE001
        # JobRunner already marked the job failed; just log here
        logger.error(
            "ingest-worker: job %s failed with unhandled exception: %s",
            getattr(job, "job_id", "?"),
            exc,
            exc_info=True,
        )


def _build_runner(
    session: Any,
    queue_repo: Any,
    config: Any,
) -> Any:
    """Construct a JobRunner from config, session, and queue repo.

    Resolves embedding + index providers from config when available.
    Returns a JobRunner with the best-effort set of providers.
    """
    from finecorpus.control.audit import AuditLogRepository
    from finecorpus.control.cost_ledger import BudgetGuard, CostLedgerRepository
    from finecorpus.pipeline.jobs import JobRunner

    ledger_repo: CostLedgerRepository | None = None
    audit_repo: AuditLogRepository | None = None
    budget_guard: BudgetGuard | None = None

    ledger_repo = CostLedgerRepository(session)
    audit_repo = AuditLogRepository(session)

    # Build BudgetGuard from config.budgets if available
    try:
        budgets = config.budgets
        kb_cap = float(budgets.per_kb_cap_usd) if budgets.per_kb_cap_usd is not None else None
        ws_cap = (
            float(budgets.per_workspace_cap_usd)
            if budgets.per_workspace_cap_usd is not None
            else None
        )
        if kb_cap is not None or ws_cap is not None:
            budget_guard = BudgetGuard(
                kb_cap_usd=kb_cap,
                workspace_cap_usd=ws_cap,
                repo=ledger_repo,
            )
    except AttributeError:
        pass

    # Resolve embedding provider and index adapter from config.
    # Both are REQUIRED for queue-mode execution.  Any construction failure is
    # re-raised here so the caller (_run_one_job_isolated) marks the job failed
    # with a diagnostic error_msg — silent swallowing is intentionally removed.
    from finecorpus.embedding.registry import build_provider_from_config
    from finecorpus.index.qdrant import QdrantAdapter

    embedding_provider = build_provider_from_config(config)

    qdrant_url = config.storage.qdrant.url
    qdrant_api_key = config.storage.qdrant.api_key
    index_adapter = QdrantAdapter(url=qdrant_url, api_key=qdrant_api_key)

    worker_id = os.environ.get("RTFC_WORKER_ID", "ingest-worker")

    return JobRunner(
        session=session,
        queue_repo=queue_repo,
        ledger_repo=ledger_repo,
        audit_repo=audit_repo,
        budget_guard=budget_guard,
        config=config,
        embedding_provider=embedding_provider,
        index_adapter=index_adapter,
        worker_id=worker_id,
    )


def _build_adapter_provider(config: Any) -> tuple[Any, Any]:
    """Best-effort construction of (index_adapter, embedding_provider) from config.

    Returns ``(None, None)`` on any construction failure so the worker loop can
    still run reindex triggers and claim jobs; cron/serviced drift is then
    skipped rather than crashing the loop.
    """
    try:
        from finecorpus.embedding.registry import build_provider_from_config  # noqa: PLC0415
        from finecorpus.index.qdrant import QdrantAdapter  # noqa: PLC0415

        provider = build_provider_from_config(config)
        adapter = QdrantAdapter(
            url=config.storage.qdrant.url,
            api_key=config.storage.qdrant.api_key,
        )
        return adapter, provider
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "ingest-worker: could not build adapter/provider for drift scoring: %s "
            "(cron + serviced drift disabled this session)",
            exc,
        )
        return None, None


def _service_eval_drift_job(
    *,
    job: Any,
    session: Any,
    config: Any,
    adapter: Any,
    provider: Any,
    queue_repo: Any,
) -> None:
    """Service a claimed ``eval_drift`` job in the SERVICES layer (§9.4, M-100).

    The pipeline-layer JobRunner cannot import services (C-5), so the scored
    drift check runs here instead of being handed to the runner (whose
    eval_drift arm fails loud by design).  This is the real production executor
    for post-reindex drift intents: it runs ``run_drift_check`` (which returns
    ``no_baseline`` gracefully when a KB has no retained baseline) and completes
    the job.  On any scoring error it fails the job loudly — never fakes a result.
    """
    from finecorpus.services.eval_drift import run_drift_check  # noqa: PLC0415

    if adapter is None or provider is None:
        msg = (
            "eval_drift job cannot be serviced: index adapter/embedding provider "
            "unavailable (check storage.qdrant + embedding config)."
        )
        logger.error("ingest-worker: %s job=%s", msg, job.job_id)
        queue_repo.start(job.job_id)
        queue_repo.fail(job.job_id, error_msg=msg)
        session.commit()
        return

    try:
        queue_repo.start(job.job_id)
        session.commit()
        result = run_drift_check(
            kb_id=job.kb_id,
            session=session,
            adapter=adapter,
            provider=provider,
            config=config,
        )
        queue_repo.complete(job.job_id)
        session.commit()
        logger.info(
            "ingest-worker: serviced eval_drift job %s kb=%s status=%s regressed=%s "
            "delta_recall=%s",
            job.job_id,
            job.kb_id,
            result.status,
            result.regressed,
            result.delta_recall,
        )
    except Exception as exc:  # noqa: BLE001
        error_msg = f"{type(exc).__name__}: {exc}"
        logger.error(
            "ingest-worker: eval_drift job %s failed during scoring: %s",
            job.job_id,
            error_msg,
            exc_info=True,
        )
        try:
            queue_repo.fail(job.job_id, error_msg=error_msg)
            session.commit()
        except Exception as mark_exc:  # noqa: BLE001
            logger.error(
                "ingest-worker: could not mark eval_drift job %s failed: %s",
                job.job_id,
                mark_exc,
            )


def run() -> None:
    """Main worker loop: claim jobs from the queue and execute pipeline stages.

    Startup:
      1. Load config from corpus.yaml (or FINECORPUS_CONFIG env var).
      2. Create control-plane DB session.
      3. Resolve embedding provider + index adapter.

    Loop (every poll_interval_s):
      - Touch heartbeat file.
      - Run scheduler_tick() (reindex trigger seam).
      - Reap stale jobs (missed heartbeat).
      - Claim one job.
      - If job found: run via JobRunner with per-job error isolation.
      - Sleep for poll_interval_s (or less if job was found, to drain queue).

    Shutdown:
      SIGTERM sets _SHUTDOWN flag; the current job finishes before exit.
    """
    global _SHUTDOWN  # noqa: PLW0603
    _SHUTDOWN = False

    # Install SIGTERM handler
    signal.signal(signal.SIGTERM, _handle_sigterm)

    import finecorpus

    logger.info(
        "ingest-worker starting",
        extra={"service": "ingest-worker", "version": finecorpus.__version__},
    )

    # Load config
    config = None
    try:
        from finecorpus.config.loader import load_config

        config_path = os.environ.get("FINECORPUS_CONFIG", "corpus.yaml")
        config = load_config(config_path)
        logger.info("ingest-worker: config loaded from %s", config_path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("ingest-worker: could not load config (%s); operating in minimal mode", exc)

    # Determine poll interval and heartbeat timeout from config
    poll_interval_s = _DEFAULT_POLL_INTERVAL_S
    heartbeat_timeout_s: float = 60.0

    if config is not None:
        try:
            heartbeat_timeout_s = float(config.index_lifecycle.worker_heartbeat_timeout_seconds)
        except AttributeError:
            pass

    # Check if we have a control-plane DB configured
    control_dsn: str | None = None
    if config is not None:
        try:
            control_dsn = config.storage.postgres.url
        except AttributeError:
            pass

    if control_dsn is None:
        logger.warning(
            "ingest-worker: no control-plane DSN configured; "
            "job queue is disabled — running heartbeat-only mode"
        )
        _run_heartbeat_only(poll_interval_s)
        return

    # Set up control-plane DB
    from sqlalchemy.orm import Session

    from finecorpus.control.jobs import JobQueueRepository
    from finecorpus.control.metadata import create_engine, create_tables

    engine = create_engine(control_dsn)
    create_tables(engine)

    worker_id = os.environ.get("RTFC_WORKER_ID", "ingest-worker")
    logger.info("ingest-worker: starting job loop worker_id=%s", worker_id)

    # Build the index adapter + embedding provider once at loop scope so cron
    # drift (scheduler_tick) and serviced eval_drift jobs have a real scoring
    # path.  Best-effort: (None, None) on failure disables drift but keeps the
    # loop running (B-1/B-2 fix — the drift trigger was previously inert because
    # scheduler_tick was called without these and claimed eval_drift jobs were
    # routed to the pipeline runner's fail-loud arm).
    drift_adapter, drift_provider = _build_adapter_provider(config)

    while not _SHUTDOWN:
        _touch_heartbeat()

        try:
            with Session(engine) as session:
                queue_repo = JobQueueRepository(session)

                # scheduler_tick: evaluate reindex triggers (Phase 4-F) + cron drift (§9.4)
                try:
                    scheduler_tick(
                        session=session,
                        config=config,
                        adapter=drift_adapter,
                        provider=drift_provider,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("ingest-worker: scheduler_tick error: %s", exc)

                # Reap stale jobs
                try:
                    reaped = queue_repo.reap_stale(heartbeat_timeout_s)
                    if reaped:
                        session.commit()
                        logger.info("ingest-worker: reaped %d stale jobs", len(reaped))
                except Exception as exc:  # noqa: BLE001
                    logger.warning("ingest-worker: reap_stale error: %s", exc)

                # Claim a job
                job = None
                try:
                    job = queue_repo.claim(worker_id)
                    if job is not None:
                        session.commit()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("ingest-worker: claim error: %s", exc)
                    job = None

                if job is not None:
                    logger.info(
                        "ingest-worker: claimed job %s type=%s kb=%s",
                        job.job_id,
                        job.job_type,
                        job.kb_id,
                    )
                    # eval_drift jobs are serviced in the SERVICES layer (scored
                    # drift check) — the pipeline runner cannot import services
                    # (C-5) and its eval_drift arm fails loud by design.
                    from finecorpus.control.jobs import JobType  # noqa: PLC0415

                    if job.job_type == str(JobType.eval_drift):
                        _service_eval_drift_job(
                            job=job,
                            session=session,
                            config=config,
                            adapter=drift_adapter,
                            provider=drift_provider,
                            queue_repo=queue_repo,
                        )
                        continue
                    try:
                        runner = _build_runner(session, queue_repo, config)
                    except Exception as build_exc:  # noqa: BLE001
                        # Provider/adapter construction failure → fail the job
                        # immediately with a descriptive error so it is not lost
                        # silently.  The worker loop itself continues.
                        error_msg = f"{type(build_exc).__name__}: {build_exc}"
                        logger.error(
                            "ingest-worker: job %s failed — could not build runner: %s",
                            job.job_id,
                            error_msg,
                            exc_info=True,
                        )
                        try:
                            queue_repo.start(job.job_id)
                            queue_repo.fail(job.job_id, error_msg=error_msg)
                            session.commit()
                        except Exception as mark_exc:  # noqa: BLE001
                            logger.error(
                                "ingest-worker: could not mark job %s failed: %s",
                                job.job_id,
                                mark_exc,
                            )
                        continue
                    _run_one_job_isolated(job, runner)
                    # Don't sleep if we found a job — drain the queue
                    continue
                else:
                    logger.debug("ingest-worker: no jobs queued; sleeping %ds", poll_interval_s)

        except Exception as exc:  # noqa: BLE001
            logger.error("ingest-worker: loop iteration error: %s", exc, exc_info=True)

        time.sleep(poll_interval_s)

    logger.info("ingest-worker: shutdown complete")
    engine.dispose()


def _run_heartbeat_only(poll_interval_s: int) -> None:
    """Fallback loop when no control-plane DB is configured.

    Emits a heartbeat every poll_interval_s so the process stays alive
    and Docker healthchecks pass.  Jobs cannot be claimed in this mode.
    """
    logger.info("ingest-worker: running in heartbeat-only mode (no control-plane DB)")
    while not _SHUTDOWN:
        _touch_heartbeat()
        logger.info(
            "ingest-worker heartbeat — no control-plane DB configured",
            extra={"service": "ingest-worker", "status": "idle"},
        )
        time.sleep(poll_interval_s)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    run()
