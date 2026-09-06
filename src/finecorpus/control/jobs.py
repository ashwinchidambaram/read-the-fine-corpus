"""Job queue repository (§6.6).

Provides:
  - JobRecord           ORM model
  - JobState            StrEnum (valid state machine)
  - JobType             StrEnum
  - JobQueueRepository  enqueue / claim / heartbeat / checkpoint /
                        complete / fail / pause_budget / resume / reap_stale

State machine (JobState):
  queued → claimed → running → checkpointed (intermediate) → completed
                             → paused_budget → queued (resumed)
                             → failed
                             → cancelled

  ``checkpointed`` is an internal sub-state of ``running`` represented in the
  DB as ``running`` with a non-null ``checkpoint`` field.  We do NOT expose it
  as a separate enum value because SELECT ... FOR UPDATE / heartbeat logic treats
  it identically to running; only the checkpoint column distinguishes progress.

  Simplified set stored in the DB:
    queued | claimed | running | paused_budget | completed | failed | cancelled
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, DateTime, Index, Integer, Numeric, String, Text, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, Session, mapped_column

from finecorpus.control.metadata import Base

# ---------------------------------------------------------------------------
# ULID-lite: timestamp prefix + random suffix (26 chars total)
# ---------------------------------------------------------------------------


def _new_job_id() -> str:
    """Generate a sortable 32-char ULID-style job ID (hex)."""
    # 8-char hex timestamp (seconds) + 24-char random hex
    ts = format(int(datetime.now(tz=UTC).timestamp()), "08x")
    rand = secrets.token_hex(12)
    return ts + rand


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class JobState(StrEnum):
    """Valid states for a job_queue row."""

    queued = "queued"
    claimed = "claimed"
    running = "running"
    paused_budget = "paused_budget"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"


# States in which a job is considered "active" for dedupe purposes
ACTIVE_STATES: frozenset[str] = frozenset(
    {JobState.queued, JobState.claimed, JobState.running, JobState.paused_budget}
)


class JobType(StrEnum):
    """Valid job types."""

    eval_drift = "eval_drift"
    eval_sweep = "eval_sweep"
    ingest = "ingest"
    purge = "purge"
    reindex_full = "reindex_full"
    reindex_incremental = "reindex_incremental"
    restore = "restore"


# ---------------------------------------------------------------------------
# ORM model
# ---------------------------------------------------------------------------


class JobRecord(Base):
    """ORM model for the ``job_queue`` table (§6.6)."""

    __tablename__ = "job_queue"
    # Index parity with migration 0002: create_all must emit the same
    # enforcement Alembic does, or dedupe silently vanishes on the
    # create_tables() path (PR #30 review finding 1). The dedupe index is
    # partial (active states only) on both PG and SQLite so completed jobs
    # never block re-enqueue.
    __table_args__ = (
        Index("ix_job_queue_kb_id", "kb_id"),
        Index(
            "ix_job_queue_dedupe",
            "kb_id",
            "dedupe_key",
            unique=True,
            postgresql_where=text("state IN ('queued','claimed','running','paused_budget')"),
            sqlite_where=text("state IN ('queued','claimed','running','paused_budget')"),
        ),
    )

    job_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    kb_id: Mapped[str] = mapped_column(String(64), nullable=False, index=False)
    workspace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    job_type: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    priority: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    payload: Mapped[Any] = mapped_column(JSON, nullable=False)
    checkpoint: Mapped[Any] = mapped_column(JSON, nullable=True)
    progress: Mapped[Any] = mapped_column(JSON, nullable=True)
    cost_accrued_usd: Mapped[Any] = mapped_column(
        Numeric(precision=18, scale=8), nullable=False, default=0
    )
    claimed_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempt: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    dedupe_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_msg: Mapped[str | None] = mapped_column(Text(), nullable=True)

    def __repr__(self) -> str:
        return (
            f"JobRecord("
            f"job_id={self.job_id!r}, "
            f"job_type={self.job_type!r}, "
            f"state={self.state!r}, "
            f"kb_id={self.kb_id!r})"
        )


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------


class JobQueueRepository:
    """Queue operations for ``job_queue``.

    Args:
        session: SQLAlchemy Session bound to the control-plane engine.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def enqueue(
        self,
        *,
        kb_id: str,
        workspace_id: str,
        job_type: JobType,
        payload: dict[str, Any],
        dedupe_key: str | None = None,
        priority: int = 0,
        created_at: datetime | None = None,
    ) -> JobRecord:
        """Enqueue a new job.

        If a job with the same (kb_id, dedupe_key) is already in an active
        state (queued/claimed/running/paused_budget), returns the EXISTING job
        (coalesce on IntegrityError from the partial unique index).

        Args:
            kb_id: KB this job belongs to.
            workspace_id: Workspace.
            job_type: Type of work.
            payload: Arbitrary JSON payload.
            dedupe_key: Optional deduplication key.  When provided, only one
                active job per (kb_id, dedupe_key) may exist.
            priority: Higher = higher priority (default 0).
            created_at: Creation timestamp (defaults to UTC now).

        Returns:
            New or existing JobRecord.
        """
        if created_at is None:
            created_at = datetime.now(tz=UTC)

        # App-level dedupe pre-check (works on every backend); the partial
        # unique index remains the backstop for concurrent racers on PG.
        if dedupe_key is not None:
            existing_stmt = (
                select(JobRecord)
                .where(JobRecord.kb_id == kb_id)
                .where(JobRecord.dedupe_key == dedupe_key)
                .where(JobRecord.state.in_(list(ACTIVE_STATES)))
                .limit(1)
            )
            existing = self._session.execute(existing_stmt).scalar_one_or_none()
            if existing is not None:
                return existing

        job_id = _new_job_id()
        record = JobRecord(
            job_id=job_id,
            kb_id=kb_id,
            workspace_id=workspace_id,
            job_type=str(job_type),
            state=JobState.queued,
            priority=priority,
            payload=payload,
            checkpoint=None,
            progress=None,
            cost_accrued_usd=0,
            claimed_by=None,
            heartbeat_at=None,
            attempt=0,
            dedupe_key=dedupe_key,
            created_at=created_at,
            started_at=None,
            finished_at=None,
            error_msg=None,
        )
        self._session.add(record)
        try:
            self._session.flush()
        except IntegrityError:
            self._session.rollback()
            # Coalesce: find the existing active job
            stmt = (
                select(JobRecord)
                .where(JobRecord.kb_id == kb_id)
                .where(JobRecord.dedupe_key == dedupe_key)
                .where(JobRecord.state.in_(list(ACTIVE_STATES)))
                .limit(1)
            )
            existing = self._session.execute(stmt).scalar_one_or_none()
            if existing is not None:
                return existing
            # If no active job found (race condition resolved), retry insert
            record = JobRecord(
                job_id=_new_job_id(),
                kb_id=kb_id,
                workspace_id=workspace_id,
                job_type=str(job_type),
                state=JobState.queued,
                priority=priority,
                payload=payload,
                checkpoint=None,
                progress=None,
                cost_accrued_usd=0,
                claimed_by=None,
                heartbeat_at=None,
                attempt=0,
                dedupe_key=dedupe_key,
                created_at=created_at,
                started_at=None,
                finished_at=None,
                error_msg=None,
            )
            self._session.add(record)
            self._session.flush()
        return record

    def claim(self, worker_id: str) -> JobRecord | None:
        """Claim the highest-priority queued job using SELECT FOR UPDATE SKIP LOCKED.

        Safe for concurrent workers: each worker gets a distinct job.

        Args:
            worker_id: Identifier of the claiming worker.

        Returns:
            Claimed JobRecord or None if queue is empty.
        """
        now = datetime.now(tz=UTC)

        # SELECT ... FOR UPDATE SKIP LOCKED is PG-specific but SQLAlchemy
        # emits it on PG and falls back gracefully on SQLite (no row locking
        # needed in single-process tests).
        try:
            stmt = (
                select(JobRecord)
                .where(JobRecord.state == JobState.queued)
                .order_by(JobRecord.priority.desc(), JobRecord.created_at.asc())
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            record = self._session.execute(stmt).scalar_one_or_none()
        except Exception:
            # SQLite does not support FOR UPDATE; fall back to plain SELECT
            stmt = (
                select(JobRecord)
                .where(JobRecord.state == JobState.queued)
                .order_by(JobRecord.priority.desc(), JobRecord.created_at.asc())
                .limit(1)
            )
            record = self._session.execute(stmt).scalar_one_or_none()

        if record is None:
            return None

        record.state = JobState.claimed
        record.claimed_by = worker_id
        record.heartbeat_at = now
        record.attempt = record.attempt + 1
        return record

    def start(self, job_id: str) -> JobRecord:
        """Transition a claimed job to running.

        Args:
            job_id: Job identifier.

        Returns:
            Updated JobRecord.
        """
        record = self._get_or_raise(job_id)
        now = datetime.now(tz=UTC)
        record.state = JobState.running
        record.started_at = record.started_at or now
        record.heartbeat_at = now
        return record

    def heartbeat(self, job_id: str) -> JobRecord:
        """Update the heartbeat timestamp for a running job.

        Args:
            job_id: Job identifier.

        Returns:
            Updated JobRecord.
        """
        record = self._get_or_raise(job_id)
        record.heartbeat_at = datetime.now(tz=UTC)
        return record

    def checkpoint(self, job_id: str, *, data: dict[str, Any]) -> JobRecord:
        """Save a checkpoint for a running job.

        Args:
            job_id: Job identifier.
            data: Checkpoint data (arbitrary JSON).

        Returns:
            Updated JobRecord.
        """
        record = self._get_or_raise(job_id)
        record.checkpoint = data
        record.heartbeat_at = datetime.now(tz=UTC)
        return record

    def complete(
        self,
        job_id: str,
        *,
        cost_accrued_usd: float = 0.0,
        finished_at: datetime | None = None,
    ) -> JobRecord:
        """Mark a job as completed.

        Args:
            job_id: Job identifier.
            cost_accrued_usd: Final cost accrued.
            finished_at: Completion timestamp (defaults to UTC now).

        Returns:
            Updated JobRecord.
        """
        record = self._get_or_raise(job_id)
        if finished_at is None:
            finished_at = datetime.now(tz=UTC)
        record.state = JobState.completed
        record.finished_at = finished_at
        record.cost_accrued_usd = cost_accrued_usd
        return record

    def fail(
        self,
        job_id: str,
        *,
        error_msg: str,
        finished_at: datetime | None = None,
    ) -> JobRecord:
        """Mark a job as failed.

        Args:
            job_id: Job identifier.
            error_msg: Human-readable error description.
            finished_at: Failure timestamp (defaults to UTC now).

        Returns:
            Updated JobRecord.
        """
        record = self._get_or_raise(job_id)
        if finished_at is None:
            finished_at = datetime.now(tz=UTC)
        record.state = JobState.failed
        record.finished_at = finished_at
        record.error_msg = error_msg
        return record

    def pause_budget(self, job_id: str) -> JobRecord:
        """Pause a job due to a budget cap (paused_budget state).

        Args:
            job_id: Job identifier.

        Returns:
            Updated JobRecord.
        """
        record = self._get_or_raise(job_id)
        record.state = JobState.paused_budget
        return record

    def resume(self, job_id: str) -> JobRecord:
        """Resume a paused_budget job (transitions back to queued).

        Args:
            job_id: Job identifier.

        Returns:
            Updated JobRecord.

        Raises:
            ValueError: If the job is not in paused_budget state.
        """
        record = self._get_or_raise(job_id)
        if record.state != JobState.paused_budget:
            raise ValueError(f"job {job_id!r} is in state {record.state!r}, not paused_budget")
        record.state = JobState.queued
        record.claimed_by = None
        record.heartbeat_at = None
        return record

    def reap_stale(
        self,
        timeout_s: float,
        *,
        now: datetime | None = None,
        requeue: bool = True,
    ) -> list[JobRecord]:
        """Reap stale claimed/running jobs that have missed heartbeats.

        A job is stale if heartbeat_at < now - timeout_s.

        Args:
            timeout_s: Seconds since last heartbeat before a job is considered stale.
            now: Reference time (defaults to UTC now).
            requeue: If True, re-queue stale jobs; if False, mark them failed.

        Returns:
            List of reaped JobRecords.
        """
        if now is None:
            now = datetime.now(tz=UTC)

        cutoff = now - timedelta(seconds=timeout_s)
        stmt = (
            select(JobRecord)
            .where(JobRecord.state.in_([JobState.claimed, JobState.running]))
            .where(JobRecord.heartbeat_at < cutoff)
        )
        stale = list(self._session.execute(stmt).scalars())

        for job in stale:
            if requeue:
                job.state = JobState.queued
                job.claimed_by = None
                job.heartbeat_at = None
            else:
                job.state = JobState.failed
                job.finished_at = now
                job.error_msg = f"reaped: missed heartbeat (timeout={timeout_s}s)"

        return stale

    def _get_or_raise(self, job_id: str) -> JobRecord:
        stmt = select(JobRecord).where(JobRecord.job_id == job_id)
        record = self._session.execute(stmt).scalar_one_or_none()
        if record is None:
            raise KeyError(f"job_id {job_id!r} not found")
        return record


__all__ = [
    "ACTIVE_STATES",
    "JobQueueRepository",
    "JobRecord",
    "JobState",
    "JobType",
]
