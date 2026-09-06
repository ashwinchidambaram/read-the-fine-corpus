"""Control-API routes: tombstone log (Phase 4, §17.1).

Routes:
    GET /v1/kb/{kb_id}/tombstones — list tombstone entries for a KB

All routes are THIN wrappers over ``finecorpus.control.tombstone.TombstoneRepository``.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query

logger = logging.getLogger(__name__)

router = APIRouter(tags=["tombstones"])

_session_factory: Any = None


def configure(*, session_factory: Any) -> None:
    global _session_factory
    _session_factory = session_factory


def _get_session() -> Any:
    if _session_factory is None:
        raise HTTPException(status_code=503, detail="Control-plane DB not configured.") from None
    return _session_factory


def _extract_raw_key(
    authorization: str | None = Header(default=None, alias="Authorization"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> str | None:
    if authorization is not None:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() == "bearer" and token:
            return token
    return x_api_key


def _record_to_dict(record: Any) -> dict[str, Any]:
    return {
        "entry_id": record.entry_id,
        "kb_id": record.kb_id,
        "document_id": record.document_id,
        "kind": record.kind,
        "deleted_at": record.deleted_at.isoformat() if record.deleted_at else None,
        "reason": record.reason,
        "deleted_by": record.deleted_by,
        "snapshots_destroyed": record.snapshots_destroyed,
    }


@router.get("/v1/kb/{kb_id}/tombstones", summary="List tombstone entries for a KB")
def list_tombstones(
    kb_id: Annotated[str, Path(description="Knowledge-base UUID")],
    kind: str | None = Query(default=None, description="Filter by kind: 'delete' or 'purge'"),
    raw_key: Annotated[str | None, Depends(_extract_raw_key)] = None,
) -> list[dict[str, Any]]:
    """Return all tombstone entries for a knowledge base.

    The tombstone log is APPEND-ONLY (§17.1). Entries are ordered by
    deletion timestamp ascending.
    """
    from finecorpus.control.tombstone import TOMBSTONE_KINDS, TombstoneRepository

    if kind is not None and kind not in TOMBSTONE_KINDS:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid kind '{kind}'. Must be one of {sorted(TOMBSTONE_KINDS)}.",
        ) from None

    session_factory = _get_session()
    with session_factory() as session:
        repo = TombstoneRepository(session)
        records = repo.list_for_kb(kb_id)

    if kind is not None:
        records = [r for r in records if r.kind == kind]

    return [_record_to_dict(r) for r in records]


__all__ = ["configure", "router"]
