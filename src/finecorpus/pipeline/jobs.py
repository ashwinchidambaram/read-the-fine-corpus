"""Job runner — queue-driven pipeline execution with budget enforcement (§6.6, §16).

Provides:
  JobRunner — dispatches by job_type, enforces budget caps before costed stages,
              checkpoints after each stage, accrues cost to the ledger.

Design notes:
  - Budget guard is PAUSE-not-FAIL: a cap hit transitions the job to
    paused_budget and appends a budget_cap_hit audit row; no exception escapes.
  - Resume path: run() on a paused/requeued job skips completed stages by
    checking the job checkpoint's ``stages_completed`` list.  The pipeline
    stages are already resumable via their persisted artifact files.
  - restore/purge raise NotImplementedError — a sibling unit lands them;
    the dispatch seam is preserved.
  - jobs_cli helpers (enqueue_job, list_jobs, status_job, resume_job,
    cancel_job) live here so pipeline/jobs.py is the single jobs library.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from finecorpus.control.audit import AuditLogRepository
    from finecorpus.control.cost_ledger import BudgetGuard, CostLedgerRepository
    from finecorpus.control.jobs import JobQueueRepository, JobRecord
    from finecorpus.embedding.base import EmbeddingProvider
    from finecorpus.index.adapter import IndexAdapter

logger = logging.getLogger("finecorpus.pipeline.jobs")

# Stage order for ordering/skipping logic
_STAGE_ORDER = ["collect", "assess", "decompose", "plan", "build"]


class JobRunner:
    """Execute a single job record through the pipeline.

    Dispatch by job_type:
      - ingest, reindex_full, reindex_incremental → run_pipeline via orchestrator
      - restore, purge → NotImplementedError (sibling unit lands these)

    Args:
        session: SQLAlchemy Session bound to the control-plane engine.
        queue_repo: JobQueueRepository for heartbeat/checkpoint/complete/fail/pause_budget.
        ledger_repo: CostLedgerRepository for recording actual costs.
        audit_repo: AuditLogRepository for budget_cap_hit events.
        budget_guard: BudgetGuard; None means no cap enforcement.
        source_dir: Source directory (required for ingest jobs).
        artifacts_root: Root for pipeline artifacts.
        embedding_provider: EmbeddingProvider for Build stage.
        index_adapter: IndexAdapter for Build stage.
        worker_id: Worker identity string (for log context).
    """

    def __init__(
        self,
        *,
        session: Session,
        queue_repo: JobQueueRepository,
        ledger_repo: CostLedgerRepository | None = None,
        audit_repo: AuditLogRepository | None = None,
        budget_guard: BudgetGuard | None = None,
        source_dir: str | None = None,
        artifacts_root: str | None = None,
        embedding_provider: EmbeddingProvider | None = None,
        index_adapter: IndexAdapter | None = None,
        worker_id: str = "runner",
        config: Any = None,
    ) -> None:
        self._session = session
        self._queue = queue_repo
        self._ledger = ledger_repo
        self._audit = audit_repo
        self._guard = budget_guard
        self._source_dir = source_dir
        self._artifacts_root = artifacts_root
        self._embedding_provider = embedding_provider
        self._index_adapter = index_adapter
        self._worker_id = worker_id
        self._config = config

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(self, job: JobRecord) -> None:
        """Execute *job* through the pipeline.

        On budget pause: transitions job to paused_budget, appends audit row,
        returns normally (caller loop continues).  Never raises on budget cap.

        On unrecoverable stage error: re-raises after marking job failed.

        Args:
            job: The claimed JobRecord to execute.
        """
        from finecorpus.control.jobs import JobType

        job_type = job.job_type
        logger.info(
            "job_runner: starting job %s type=%s kb=%s",
            job.job_id,
            job_type,
            job.kb_id,
        )

        # Transition to running
        self._queue.start(job.job_id)
        self._session.commit()

        try:
            if job_type in (
                str(JobType.ingest),
                str(JobType.reindex_full),
                str(JobType.reindex_incremental),
            ):
                self._run_ingest(job)
            elif job_type == str(JobType.eval_sweep):
                self._run_eval_sweep(job)
            elif job_type == str(JobType.eval_drift):
                self._run_eval_drift(job)
            elif job_type in (str(JobType.restore), str(JobType.purge)):
                raise NotImplementedError(
                    f"job_type={job_type!r} is not yet implemented; a sibling unit will land it."
                )
            else:
                raise ValueError(f"Unknown job_type={job_type!r}")
        except _BudgetPausedSignal:
            # Already handled inside _run_ingest; job is paused_budget
            pass
        except Exception as exc:
            error_msg = f"{type(exc).__name__}: {exc}"
            logger.error("job_runner: job %s failed — %s", job.job_id, error_msg, exc_info=True)
            self._queue.fail(job.job_id, error_msg=error_msg)
            self._session.commit()
            raise

    # ------------------------------------------------------------------
    # Ingest / reindex execution
    # ------------------------------------------------------------------

    def _run_ingest(self, job: JobRecord) -> None:
        """Run all pipeline stages for an ingest/reindex job."""
        # Extract run parameters from payload
        payload = job.payload or {}
        run_id: str = payload.get("run_id", job.job_id)
        source_dir: str = payload.get("source_dir", self._source_dir or "")
        artifacts_root: str = payload.get("artifacts_root", self._artifacts_root or "")
        promote: bool = bool(payload.get("promote", False))

        # M-053 dispatch guard: incremental reindex is structurally impossible
        # when config_version changed since enqueue (chunk identity includes it).
        if str(job.job_type) == "reindex_incremental" and self._config is not None:
            from finecorpus.pipeline.reindex import (
                _get_current_config_version,
                assert_incremental_allowed,
            )

            assert_incremental_allowed(
                _get_current_config_version(self._config),
                str(payload.get("config_version", "")),
            )

        # Determine which stages have already completed (resume path)
        checkpoint_data = job.checkpoint or {}
        stages_completed: list[str] = list(checkpoint_data.get("stages_completed", []))

        logger.info(
            "job_runner: job %s run_id=%s stages_completed=%s",
            job.job_id,
            run_id,
            stages_completed,
        )

        # Check budget before Build (the only costed stage we can estimate upfront)
        # We check after Collect+Assess+Decompose+Plan have completed (or if resuming
        # past them) so we have a cost estimate available.
        # For simplicity: check before starting Build if we haven't run it yet.
        will_run_build = "build" not in stages_completed

        # Run stages that haven't been completed yet
        if not all(s in stages_completed for s in ["collect", "assess", "decompose", "plan"]):
            self._run_pre_build_stages(job, run_id, source_dir, artifacts_root, stages_completed)

        # Now check budget before Build
        if will_run_build:
            paused = self._check_budget_before_build(job, run_id, artifacts_root)
            if paused:
                raise _BudgetPausedSignal()

        # Run Build if not yet completed
        if "build" not in stages_completed:
            self._run_build_stage(job, run_id, source_dir, artifacts_root, promote)

        # All stages complete
        total_cost = float(job.cost_accrued_usd or 0)
        self._queue.complete(job.job_id, cost_accrued_usd=total_cost)
        self._session.commit()

        # B-1: successful scheduled run resets the cap-hit counter.
        trigger_id = (job.payload or {}).get("trigger_id")
        if trigger_id:
            try:
                from finecorpus.pipeline.reindex import record_successful_run_for_trigger

                record_successful_run_for_trigger(self._session, trigger_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("cap-hit reset failed for %s: %s", trigger_id, exc)
        logger.info("job_runner: job %s completed total_cost=%.6f", job.job_id, total_cost)

        # §9.4 post-reindex drift intent: enqueue a drift-check job to record
        # that a drift check should run after this reindex.  The pipeline layer
        # cannot score (C-5); the actual scored check runs from the services
        # layer (scheduler_tick or CLI).  Enqueue is best-effort (failure MUST
        # NOT fail the reindex — isolation).
        try:
            from finecorpus.control.jobs import JobQueueRepository, JobType  # noqa: PLC0415

            drift_repo = JobQueueRepository(self._session)
            drift_repo.enqueue(
                kb_id=job.kb_id,
                workspace_id=job.workspace_id,
                job_type=JobType.eval_drift,
                payload={
                    "trigger": "post_reindex",
                    "parent_job_id": job.job_id,
                    "run_id": run_id,
                },
                dedupe_key=f"drift_{job.kb_id}",
                priority=0,
            )
            self._session.commit()
            logger.info(
                "job_runner: enqueued eval_drift intent job for kb=%s after reindex %s",
                job.kb_id,
                job.job_id,
            )
        except Exception as exc:  # noqa: BLE001
            # Drift enqueue failures are strictly non-fatal.
            logger.warning(
                "job_runner: could not enqueue eval_drift intent for kb=%s: %s "
                "(non-fatal; drift check will run via cron if configured)",
                job.kb_id,
                exc,
            )

    def _run_pre_build_stages(
        self,
        job: JobRecord,
        run_id: str,
        source_dir: str,
        artifacts_root: str,
        stages_completed: list[str],
    ) -> None:
        """Run Collect → Assess → Decompose → Plan, skipping already-completed stages."""
        from datetime import UTC, datetime

        from finecorpus.pipeline.artifact_store import ArtifactStore
        from finecorpus.pipeline.assess import AssessStage
        from finecorpus.pipeline.collect import CollectStage
        from finecorpus.pipeline.decompose import DecomposeStage
        from finecorpus.pipeline.plan import PlanStage

        store = ArtifactStore(artifacts_root=artifacts_root, run_id=run_id)
        collected_at = datetime.now(tz=UTC)

        # Stage 1: Collect
        if "collect" not in stages_completed:
            collect = CollectStage(
                source_dir=source_dir,
                workspace_id=job.workspace_id,
                kb_id=job.kb_id,
                collected_at=collected_at,
            )
            collect.run(input_data=None, store=store)
            stages_completed.append("collect")
            self._checkpoint(job, stages_completed, {})
            self._heartbeat(job)

        # Stage 2: Assess
        if "assess" not in stages_completed:
            inventory_dict = store.load("collect")
            assess = AssessStage(run_id=run_id, run_started_at=collected_at)
            assess.run(input_data=inventory_dict, store=store)
            stages_completed.append("assess")
            self._checkpoint(job, stages_completed, {})
            self._heartbeat(job)

        # Stage 3: Decompose
        if "decompose" not in stages_completed:
            parse_result_batch = store.load("assess")
            decompose = DecomposeStage(
                run_started_at=collected_at,
                artifacts_root=artifacts_root,
                run_id=run_id,
            )
            decompose.run(input_data=parse_result_batch, store=store)
            stages_completed.append("decompose")
            self._checkpoint(job, stages_completed, {})
            self._heartbeat(job)

        # Stage 4: Plan
        if "plan" not in stages_completed:
            segment_set_batch = store.load("decompose")
            # D-16: thread the collect artifact's permission-gap acknowledgement
            # into the Plan stage (queue mode previously dropped it).
            ack = False
            try:
                collect_artifact = store.load("collect")
                ack = bool(
                    (collect_artifact.get("source_run") or {}).get(
                        "acknowledged_permission_gap", False
                    )
                )
            except Exception:  # noqa: BLE001
                ack = False
            plan = PlanStage(
                acknowledged_permission_gap=ack,
                audit_session=self._session,
            )
            plan.run(input_data=segment_set_batch, store=store)
            stages_completed.append("plan")
            self._checkpoint(job, stages_completed, {})
            self._heartbeat(job)

    def _run_build_stage(
        self,
        job: JobRecord,
        run_id: str,
        source_dir: str,
        artifacts_root: str,
        promote: bool,
    ) -> None:
        """Run the Build stage and record actual costs to the ledger."""
        from finecorpus.pipeline.artifact_store import ArtifactStore
        from finecorpus.pipeline.build import BuildStage

        store = ArtifactStore(artifacts_root=artifacts_root, run_id=run_id)
        ingestion_config = store.load("plan")

        build = BuildStage(
            artifacts_root=artifacts_root,
            run_id=run_id,
            workspace_id=job.workspace_id,
            kb_id=job.kb_id,
            embedding_provider=self._embedding_provider,
            index_adapter=self._index_adapter,
        )
        build_result_dict = build.run(input_data=ingestion_config, store=store)

        # Structural guard: queue-mode NEVER legitimately produces a skeleton result.
        # skeleton=True means the Build stage fell back to Phase 0 behaviour because
        # no real provider/adapter was injected.  Allowing a skeleton result to
        # complete the job silently would mask provider construction bugs (RULING 1c).
        if isinstance(build_result_dict, dict) and build_result_dict.get("skeleton") is True:
            raise RuntimeError(
                "Build stage returned skeleton=True (no-op result) in queue mode. "
                "This indicates the embedding provider or index adapter was not injected. "
                "Check _build_runner construction — provider and adapter are required."
            )

        # Extract actual costs from token_accounting
        token_accounting: dict[str, int] = {}
        if isinstance(build_result_dict, dict):
            token_accounting = build_result_dict.get("token_accounting", {})

        actual_cost = self._compute_actual_cost_from_accounting(token_accounting)

        # Record to ledger
        if self._ledger is not None and actual_cost > 0:
            self._ledger.record(
                kb_id=job.kb_id,
                workspace_id=job.workspace_id,
                operation_type="build_embedding",
                cost_usd=actual_cost,
                tokens_consumed=token_accounting.get("total_input_tokens", 0),
            )

        # Update job's accrued cost
        prior = Decimal(str(job.cost_accrued_usd or 0))
        job.cost_accrued_usd = prior + Decimal(str(actual_cost))

        # Checkpoint and heartbeat.
        # NOTE (same-object-reference assumption): we read job.checkpoint directly
        # from the in-memory JobRecord object.  This is safe because the worker
        # uses a single Session per job execution, so the ORM object is the same
        # instance that received each prior checkpoint() call.  In a hypothetical
        # session-per-stage architecture a DB re-read via self._queue.get(job.job_id)
        # would be necessary here to pick up checkpoint data written by prior stages.
        checkpoint_data = job.checkpoint or {}
        stages_completed = list(checkpoint_data.get("stages_completed", []))
        stages_completed.append("build")
        self._checkpoint(job, stages_completed, {"build_cost_usd": str(actual_cost)})
        self._heartbeat(job)
        self._session.commit()

        logger.info("job_runner: job %s build complete cost=%.6f", job.job_id, actual_cost)

    # ------------------------------------------------------------------
    # eval_drift execution (additive arm — PR-8 dispatch)
    # ------------------------------------------------------------------

    def _run_eval_drift(self, job: JobRecord) -> None:
        """Run an eval_drift job — FAIL LOUDLY (PR-7 fail-loud precedent).

        Layer constraint (C-5): pipeline jobs may NOT import from the services
        layer.  Retrieval-quality scoring (services.eval_drift.run_drift_check)
        cannot be performed here.  Persisting a "completed" drift result with
        all-zero scores would be actively misleading.

        This arm therefore FAILS LOUDLY with a clear descriptive error so no
        fake drift result is ever committed.

        Architecture: eval_drift jobs are enqueued by the post-reindex hook
        (this runner, after _run_ingest) AND are SERVICED in the services layer
        by the ingest-worker loop (_service_eval_drift_job), which CAN import
        services and runs the scored run_drift_check.  Cron-scheduled drift runs
        via scheduler_tick.  This pipeline-layer arm is only ever reached if a
        drift job is dispatched directly to the JobRunner (misconfiguration); it
        FAILS LOUD rather than committing a fake all-zero result.
        """
        _FAIL_MSG = (
            "eval_drift reached the pipeline JobRunner, which sits below the services "
            "layer and cannot score retrieval (C-5). Scored drift is serviced by the "
            "ingest-worker loop (_service_eval_drift_job) or scheduler_tick cron — a "
            "drift job should never be handed to JobRunner.run_job directly."
        )

        logger.error(
            "job_runner: eval_drift job %s cannot be scored via the queue worker — %s",
            job.job_id,
            _FAIL_MSG,
        )
        self._queue.fail(job.job_id, error_msg=_FAIL_MSG)
        self._session.commit()
        raise ValueError(_FAIL_MSG)

    # ------------------------------------------------------------------
    # eval_sweep execution (additive arm — PR-7 dispatch)
    # ------------------------------------------------------------------

    def _run_eval_sweep(self, job: JobRecord) -> None:
        """Run an eval_sweep job — FAIL LOUDLY (Ruling 2).

        Layer constraint (C-5): pipeline jobs may NOT import from the services
        layer.  Retrieval-quality scoring (services.eval_scoring.score_eval_set)
        cannot be performed here.  Persisting a "completed" SweepRunRecord with
        all-0.0 scores would be actively misleading — operators would see a zeros
        table that looks like a real sweep result.

        This arm therefore FAILS LOUDLY with a clear descriptive error so no
        zeros table is ever committed as if it were a real scored sweep.

        Deferral: queue-driven scored sweeps require a services-layer worker
        that can import retrieval.service and services.eval_scoring.  That
        architecture is out of scope for Phase 5.  Operators MUST use the CLI
        entrypoint (``corpus pipeline sweep <kb_id>``) which calls
        ``services.eval_sweep.run_sweep`` directly from the services layer.
        """
        _FAIL_MSG = (
            "eval_sweep must run via the services entrypoint "
            "(corpus pipeline sweep <kb_id>); "
            "the queue worker sits in the pipeline layer and cannot score. "
            "Queue-driven scored sweep is deferred."
        )

        logger.error(
            "job_runner: eval_sweep job %s cannot be scored via the queue worker — %s",
            job.job_id,
            _FAIL_MSG,
        )
        self._queue.fail(job.job_id, error_msg=_FAIL_MSG)
        self._session.commit()
        raise ValueError(_FAIL_MSG)

    # ------------------------------------------------------------------
    # Budget enforcement
    # ------------------------------------------------------------------

    def _check_budget_before_build(self, job: JobRecord, run_id: str, artifacts_root: str) -> bool:
        """Check budget guard before Build.  Returns True if the job was paused."""
        if self._guard is None:
            return False

        from finecorpus.control.audit import AuditAction
        from finecorpus.control.cost_ledger import BudgetDecision
        from finecorpus.pipeline.artifact_store import ArtifactStore

        # Try to load a cost estimate from plan + decompose artifacts
        projected_usd = 0.0
        try:
            store = ArtifactStore(artifacts_root=artifacts_root, run_id=run_id)
            plan_artifact = store.load("plan")
            decompose_artifact = store.load("decompose")
            projected_usd = _estimate_projected_cost(plan_artifact, decompose_artifact)
        except Exception as exc:  # noqa: BLE001
            logger.debug("job_runner: could not estimate projected cost: %s", exc)

        decision = self._guard.check(
            kb_id=job.kb_id,
            workspace_id=job.workspace_id,
            projected_usd=projected_usd,
        )

        if decision == BudgetDecision.allow:
            return False

        # Budget cap hit — pause the job
        logger.warning(
            "job_runner: budget cap hit for job %s decision=%s projected=%.4f",
            job.job_id,
            decision,
            projected_usd,
        )

        self._queue.pause_budget(job.job_id)

        # Append audit row
        if self._audit is not None:
            self._audit.append(
                entry_type=AuditAction.budget_cap_hit,
                actor_id=self._worker_id,
                target_kb_id=job.kb_id,
                target_workspace_id=job.workspace_id,
                details={
                    "job_id": job.job_id,
                    "decision": str(decision),
                    "projected_usd": str(projected_usd),
                },
            )

        self._session.commit()

        # B-1: live cap-hit governance — increment the trigger's counter so
        # the M-085 repeated-cap-hit alert can actually fire.
        trigger_id = (job.payload or {}).get("trigger_id")
        if trigger_id and self._config is not None:
            try:
                from finecorpus.pipeline.reindex import record_budget_pause_for_trigger

                record_budget_pause_for_trigger(
                    self._session,
                    trigger_id,
                    config=self._config,
                    audit_repo=self._audit,
                    job_id=job.job_id,
                    kb_id=job.kb_id,
                    workspace_id=job.workspace_id,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("cap-hit counter update failed for %s: %s", trigger_id, exc)
        return True

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _checkpoint(
        self,
        job: JobRecord,
        stages_completed: list[str],
        extra: dict[str, Any],
    ) -> None:
        """Save checkpoint data for the job."""
        data: dict[str, Any] = {"stages_completed": stages_completed}
        data.update(extra)
        self._queue.checkpoint(job.job_id, data=data)
        self._session.commit()

    def _heartbeat(self, job: JobRecord) -> None:
        """Touch the job heartbeat."""
        self._queue.heartbeat(job.job_id)

    def _compute_actual_cost_from_accounting(self, token_accounting: dict[str, int]) -> float:
        """Derive actual cost from token_accounting (Build stage output).

        For now: zero because we don't have real pricing wired at the job level.
        The cost estimate is the pre-build estimate; actual tokens are recorded so
        the ledger has the token count even when cost_usd is zero for local providers.
        """
        # If embedding provider has pricing info, compute it
        if self._embedding_provider is not None:
            try:
                caps = self._embedding_provider.capabilities
                cost_per_1k = getattr(caps, "cost_per_1k_tokens", None)
                if cost_per_1k is not None:
                    tokens = token_accounting.get("total_input_tokens", 0)
                    return float(Decimal(str(tokens)) / Decimal(1000) * Decimal(str(cost_per_1k)))
            except Exception:  # noqa: BLE001
                pass
        return 0.0


# ---------------------------------------------------------------------------
# Internal budget-pause signal (avoids exception-based flow for non-errors)
# ---------------------------------------------------------------------------


class _BudgetPausedSignal(Exception):
    """Internal signal: budget guard paused the job (not an error)."""


# ---------------------------------------------------------------------------
# Cost projection helper
# ---------------------------------------------------------------------------


def _estimate_projected_cost(plan_artifact: Any, decompose_artifact: Any) -> float:
    """Best-effort cost projection from plan + decompose artifacts.

    Returns 0.0 if the artifacts can't be parsed or the provider is local.
    """
    try:
        from finecorpus.contracts.ingestion_config import IngestionConfig
        from finecorpus.contracts.segment_set_batch import SegmentSetBatch
        from finecorpus.pipeline.costing import estimate_ingestion_cost, resolve_costing_providers

        if isinstance(plan_artifact, dict):
            ingestion_config = IngestionConfig.model_validate(plan_artifact)
        else:
            ingestion_config = plan_artifact

        if isinstance(decompose_artifact, dict):
            segment_batch = SegmentSetBatch.model_validate(decompose_artifact)
        else:
            segment_batch = decompose_artifact

        providers = resolve_costing_providers(ingestion_config)
        if providers.embedding_unavailable or providers.embedding_provider is None:
            return 0.0

        estimate = estimate_ingestion_cost(
            segment_set_batch=segment_batch,
            ingestion_config=ingestion_config,
            embedding_provider=providers.embedding_provider,
            llm_provider_or_none=providers.llm_provider,
        )
        return float(estimate.total_cost_usd)
    except Exception:  # noqa: BLE001
        return 0.0


# ---------------------------------------------------------------------------
# CLI helper functions (thin library layer — CLI calls these)
# ---------------------------------------------------------------------------


def enqueue_job(
    *,
    session: Session,
    kb_id: str,
    workspace_id: str,
    job_type: str,
    payload: dict[str, Any],
    dedupe_key: str | None = None,
    priority: int = 0,
) -> JobRecord:
    """Enqueue a new job via JobQueueRepository.

    Returns the new or existing (coalesced) JobRecord.
    """
    from finecorpus.control.jobs import JobQueueRepository, JobType

    try:
        jt = JobType(job_type)
    except ValueError:
        raise ValueError(
            f"Unknown job_type={job_type!r}. Valid types: {[t.value for t in JobType]}"
        ) from None

    repo = JobQueueRepository(session)
    job = repo.enqueue(
        kb_id=kb_id,
        workspace_id=workspace_id,
        job_type=jt,
        payload=payload,
        dedupe_key=dedupe_key,
        priority=priority,
    )
    session.commit()
    return job


def list_jobs(
    *,
    session: Session,
    kb_id: str | None = None,
    state: str | None = None,
    limit: int = 50,
) -> list[JobRecord]:
    """List jobs, optionally filtered by kb_id and/or state."""
    from sqlalchemy import select

    from finecorpus.control.jobs import JobRecord

    stmt = select(JobRecord).order_by(JobRecord.created_at.desc()).limit(limit)
    if kb_id is not None:
        stmt = stmt.where(JobRecord.kb_id == kb_id)
    if state is not None:
        stmt = stmt.where(JobRecord.state == state)
    return list(session.execute(stmt).scalars())


def get_job(*, session: Session, job_id: str) -> JobRecord | None:
    """Return a single job by ID, or None."""
    from sqlalchemy import select

    from finecorpus.control.jobs import JobRecord

    stmt = select(JobRecord).where(JobRecord.job_id == job_id)
    return session.execute(stmt).scalar_one_or_none()


def resume_job(*, session: Session, job_id: str) -> JobRecord:
    """Resume a paused_budget job (transitions back to queued).

    Raises:
        KeyError: If job_id not found.
        ValueError: If job is not in paused_budget state.
    """
    from finecorpus.control.jobs import JobQueueRepository

    repo = JobQueueRepository(session)
    job = repo.resume(job_id)
    session.commit()
    return job


def cancel_job(*, session: Session, job_id: str) -> JobRecord:
    """Cancel a queued or paused job.

    Raises:
        KeyError: If job_id not found.
        ValueError: If job is not in a cancellable state.
    """
    from sqlalchemy import select

    from finecorpus.control.jobs import JobRecord, JobState

    stmt = select(JobRecord).where(JobRecord.job_id == job_id)
    job = session.execute(stmt).scalar_one_or_none()
    if job is None:
        raise KeyError(f"job_id {job_id!r} not found")

    cancellable = {str(JobState.queued), str(JobState.paused_budget)}
    if job.state not in cancellable:
        raise ValueError(
            f"job {job_id!r} is in state {job.state!r}; "
            f"only queued/paused_budget jobs can be cancelled"
        )
    job.state = str(JobState.cancelled)
    session.commit()
    return job


__all__ = [
    "JobRunner",
    "cancel_job",
    "enqueue_job",
    "get_job",
    "list_jobs",
    "resume_job",
]
