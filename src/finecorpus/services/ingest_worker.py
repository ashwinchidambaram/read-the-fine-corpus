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


def scheduler_tick(
    session: Any = None,
    config: Any = None,
) -> None:
    """Evaluate reindex triggers and enqueue reindex jobs as needed.

    Called once per worker loop iteration, before job claim.  Failure-isolated:
    any exception is caught and logged; the worker loop continues.

    When session/config are not provided (legacy no-arg call from the loop),
    this function is a no-op — the loop passes them explicitly when available.

    Args:
        session: Optional SQLAlchemy Session for the control-plane DB.
        config: Optional platform config (CorpusConfig).
    """
    if session is None or config is None:
        # Called without args from the legacy loop path — no-op.
        return

    try:
        from finecorpus.pipeline.reindex import evaluate_triggers

        evaluate_triggers(
            session=session,
            config=config,
            adapter=None,  # adapter not wired here; change-detection uses None path
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("ingest-worker: scheduler_tick error: %s", exc, exc_info=True)


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
        embedding_provider=embedding_provider,
        index_adapter=index_adapter,
        worker_id=worker_id,
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

    while not _SHUTDOWN:
        _touch_heartbeat()

        try:
            with Session(engine) as session:
                queue_repo = JobQueueRepository(session)

                # scheduler_tick: evaluate reindex triggers (Phase 4-F)
                try:
                    scheduler_tick(session=session, config=config)
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
