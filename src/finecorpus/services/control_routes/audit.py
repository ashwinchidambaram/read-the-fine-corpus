"""Control-API routes: audit log (Phase 4, §13.2).

Routes:
    GET /v1/audit?kb_id=&workspace_id= — role-scoped audit log access

Role scoping:
    - admin     → sees all entries (no filter required)
    - editor    → their workspace's entries
    - viewer    → their KB's entries

All routes are THIN wrappers over ``finecorpus.control.audit.AuditLogRepository``.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query

logger = logging.getLogger(__name__)

router = APIRouter(tags=["audit"])

_session_factory: Any = None


def configure(*, session_factory: Any) -> None:
    """Inject runtime dependencies from control_api lifespan."""
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


def _entry_to_dict(record: Any) -> dict[str, Any]:
    return {
        "entry_id": record.entry_id,
        "entry_type": record.entry_type,
        "actor_id": record.actor_id,
        "target_kb_id": record.target_kb_id,
        "target_workspace_id": record.target_workspace_id,
        "details": record.details,
        "created_at": record.created_at.isoformat() if record.created_at else None,
    }


@router.get("/v1/audit", summary="Query the audit log (role-scoped)")
def list_audit(
    kb_id: str | None = Query(default=None, description="Filter by KB ID"),
    workspace_id: str | None = Query(default=None, description="Filter by workspace ID"),
    limit: int = Query(default=100, ge=1, le=1000),
    raw_key: Annotated[str | None, Depends(_extract_raw_key)] = None,
) -> list[dict[str, Any]]:
    """Return audit log entries filtered by role scope.

    - Admin: sees all entries; kb_id/workspace_id are optional filters.
    - Editor/workspace-scoped: must match their workspace_id.
    - Viewer/KB-scoped: must match their kb_id.
    - No auth (auth disabled): returns entries matching supplied filter params.
    """
    from finecorpus.control.audit import AuditLogRepository

    session_factory = _get_session()

    # Resolve principal if auth is enabled (best-effort; no auth → pass-through)
    principal = None
    if raw_key and _session_factory is not None:
        try:
            from finecorpus.control.auth import ApiKeyRepository

            with session_factory() as sess:
                repo = ApiKeyRepository(sess)
                principal = repo.validate(raw_key)
        except Exception:  # noqa: BLE001
            raise HTTPException(status_code=401, detail="Invalid or expired API key.") from None

    # Apply role-based scope enforcement
    if principal is not None:
        from finecorpus.control.auth import Role, ScopeKind

        role = Role(principal.role)
        scope = principal.scope_kind

        if role == Role.admin:
            # Admin sees everything; params are optional filters.
            pass
        elif scope == ScopeKind.workspace:
            # Editor: workspace-scoped. Force filter to their workspace.
            workspace_id = principal.workspace_id
        elif scope == ScopeKind.kb:
            # Viewer/service: KB-scoped. Force filter to their KB.
            kb_id = principal.kb_id
        else:
            raise HTTPException(
                status_code=403, detail="Insufficient scope for audit access."
            ) from None

    with session_factory() as session:
        audit_repo = AuditLogRepository(session)
        if kb_id:
            records = audit_repo.list_for_kb(kb_id, limit=limit)
        elif workspace_id:
            records = audit_repo.list_for_workspace(workspace_id, limit=limit)
        else:
            # No filter — return recent entries (only admin reaches here unfiltered)
            from sqlalchemy import select

            from finecorpus.control.audit import AuditLogRecord

            stmt = select(AuditLogRecord).order_by(AuditLogRecord.created_at.desc()).limit(limit)
            records = list(session.execute(stmt).scalars())

    return [_entry_to_dict(r) for r in records]


__all__ = ["configure", "router"]
