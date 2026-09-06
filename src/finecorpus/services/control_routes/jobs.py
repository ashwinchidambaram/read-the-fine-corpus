"""Control-API routes: job management (Phase 4).

Routes:
    GET  /v1/jobs              — list jobs (admin: all; editor/viewer: their KB's jobs)
    GET  /v1/jobs/{id}         — get a single job
    POST /v1/jobs/{id}/resume  — resume a paused_budget job
    POST /v1/jobs/{id}/cancel  — cancel a queued/paused job
    POST /v1/kb/{kb_id}/reindex — enqueue a reindex job for a KB

All routes are THIN wrappers over ``finecorpus.control.jobs.JobQueueRepository``.
Auth: require_principal from services/deps.py.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query

logger = logging.getLogger(__name__)

router = APIRouter(tags=["jobs"])

# ---------------------------------------------------------------------------
# Dependency helpers (module-level factories injected at startup)
# ---------------------------------------------------------------------------

_session_factory: Any = None  # () -> context manager yielding Session


def configure(*, session_factory: Any) -> None:
    """Inject runtime dependencies from control_api lifespan."""
    global _session_factory
    _session_factory = session_factory


def _get_session() -> Any:
    if _session_factory is None:
        raise HTTPException(status_code=503, detail="Control-plane DB not configured.") from None
    return _session_factory


# ---------------------------------------------------------------------------
# Auth dependency (passthrough — control_api wires auth via include_router)
# ---------------------------------------------------------------------------


def _extract_raw_key(
    authorization: str | None = Header(default=None, alias="Authorization"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> str | None:
    if authorization is not None:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() == "bearer" and token:
            return token
    return x_api_key


# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------


def _job_to_dict(record: Any) -> dict[str, Any]:
    """Serialize a JobRecord to a plain dict for the API response."""
    return {
        "job_id": record.job_id,
        "kb_id": record.kb_id,
        "workspace_id": record.workspace_id,
        "job_type": record.job_type,
        "state": record.state,
        "priority": record.priority,
        "payload": record.payload,
        "progress": record.progress,
        "cost_accrued_usd": str(record.cost_accrued_usd),
        "claimed_by": record.claimed_by,
        "attempt": record.attempt,
        "dedupe_key": record.dedupe_key,
        "created_at": record.created_at.isoformat() if record.created_at else None,
        "started_at": record.started_at.isoformat() if record.started_at else None,
        "finished_at": record.finished_at.isoformat() if record.finished_at else None,
        "error_msg": record.error_msg,
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/v1/jobs", summary="List jobs")
def list_jobs(
    kb_id: str | None = Query(default=None, description="Filter by KB ID"),
    state: str | None = Query(default=None, description="Filter by job state"),
    raw_key: Annotated[str | None, Depends(_extract_raw_key)] = None,
) -> list[dict[str, Any]]:
    """List jobs.

    Admin sees all jobs; editor/viewer see jobs scoped to their KB.
    Optionally filter by kb_id and/or state.
    """
    from sqlalchemy import select

    from finecorpus.control.jobs import JobRecord

    session_factory = _get_session()
    with session_factory() as session:
        stmt = select(JobRecord)
        if kb_id:
            stmt = stmt.where(JobRecord.kb_id == kb_id)
        if state:
            stmt = stmt.where(JobRecord.state == state)
        stmt = stmt.order_by(JobRecord.created_at.desc()).limit(200)
        records = list(session.execute(stmt).scalars())

    return [_job_to_dict(r) for r in records]


@router.get("/v1/jobs/{job_id}", summary="Get a job")
def get_job(
    job_id: Annotated[str, Path(description="Job ID")],
    raw_key: Annotated[str | None, Depends(_extract_raw_key)] = None,
) -> dict[str, Any]:
    """Return a single job by ID."""
    from sqlalchemy import select

    from finecorpus.control.jobs import JobRecord

    session_factory = _get_session()
    with session_factory() as session:
        stmt = select(JobRecord).where(JobRecord.job_id == job_id)
        record = session.execute(stmt).scalar_one_or_none()

    if record is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.") from None
    return _job_to_dict(record)


@router.post("/v1/jobs/{job_id}/resume", summary="Resume a paused job")
def resume_job(
    job_id: Annotated[str, Path(description="Job ID")],
    raw_key: Annotated[str | None, Depends(_extract_raw_key)] = None,
) -> dict[str, Any]:
    """Resume a job in paused_budget state.

    Transitions the job back to ``queued`` so the worker picks it up again.
    """
    from finecorpus.control.jobs import JobQueueRepository

    session_factory = _get_session()
    with session_factory() as session:
        repo = JobQueueRepository(session)
        try:
            record = repo.resume(job_id)
            session.commit()
        except KeyError:
            raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.") from None
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _job_to_dict(record)


@router.post("/v1/jobs/{job_id}/cancel", summary="Cancel a job")
def cancel_job(
    job_id: Annotated[str, Path(description="Job ID")],
    raw_key: Annotated[str | None, Depends(_extract_raw_key)] = None,
) -> dict[str, Any]:
    """Cancel a queued or paused_budget job.

    Transitions the job to ``cancelled``. Cannot cancel running or completed jobs.
    """
    from sqlalchemy import select

    from finecorpus.control.jobs import JobRecord, JobState

    session_factory = _get_session()
    with session_factory() as session:
        stmt = select(JobRecord).where(JobRecord.job_id == job_id)
        record = session.execute(stmt).scalar_one_or_none()
        if record is None:
            raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.") from None
        if record.state not in (JobState.queued, JobState.paused_budget):
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Cannot cancel job in state '{record.state}'. "
                    "Only queued or paused_budget jobs can be cancelled."
                ),
            ) from None
        record.state = JobState.cancelled
        session.commit()
        return _job_to_dict(record)


@router.post("/v1/kb/{kb_id}/reindex", summary="Enqueue a reindex job")
def enqueue_reindex(
    kb_id: Annotated[str, Path(description="Knowledge-base UUID")],
    raw_key: Annotated[str | None, Depends(_extract_raw_key)] = None,
) -> dict[str, Any]:
    """Enqueue a full reindex job for a knowledge base.

    Returns the new (or existing deduplicated) JobRecord.
    """
    from finecorpus.control.jobs import JobQueueRepository, JobType
    from finecorpus.control.metadata import AliasRepository
    from finecorpus.index.adapter import alias_name

    session_factory = _get_session()
    with session_factory() as session:
        # Resolve workspace_id from alias record
        alias_repo = AliasRepository(session)
        alias = alias_repo.get(alias_name(kb_id))
        if alias is None:
            raise HTTPException(
                status_code=404, detail=f"Knowledge base '{kb_id}' is not registered."
            ) from None
        workspace_id = alias.workspace_id or "unknown"

        repo = JobQueueRepository(session)
        record = repo.enqueue(
            kb_id=kb_id,
            workspace_id=workspace_id,
            job_type=JobType.reindex_full,
            payload={"kb_id": kb_id, "triggered_by": "api"},
            dedupe_key=f"reindex_full:{kb_id}",
        )
        session.commit()
        return _job_to_dict(record)


__all__ = ["configure", "router"]
