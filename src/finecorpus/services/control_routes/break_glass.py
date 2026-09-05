"""Break-glass REST routes (§2.3, M-001..M-004).

Thin wiring over BreakGlassRepository and issue_grant().
All routes require admin role.

Routes:
  POST   /admin/break-glass/grant      — create a new grant (reason required)
  GET    /admin/break-glass/active     — list active grants
  DELETE /admin/break-glass/{grant_id} — revoke a grant

The router is included in control_api.py with prefix="" (routes are already
prefixed /admin/…).
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

router = APIRouter(tags=["break-glass"])

# ---------------------------------------------------------------------------
# Module-level session factory (set at startup by control_api.py lifespan)
# ---------------------------------------------------------------------------

_session_factory: Any = None  # () -> context manager yielding Session


def get_session_factory() -> Any:
    if _session_factory is None:
        raise HTTPException(status_code=503, detail="Control-plane DB not configured.")
    return _session_factory


# ---------------------------------------------------------------------------
# Admin principal extraction (simple header-based; control API uses X-Admin-Key
# for now — Phase 6 will wire the full auth stack)
# ---------------------------------------------------------------------------


def _require_admin_id(
    x_admin_id: Annotated[
        str | None,
        Header(
            alias="X-Admin-ID",
            description="Admin principal ID (Phase 4: trusted header; Phase 6 → full auth).",
        ),
    ] = None,
) -> str:
    """Require a non-empty X-Admin-ID header.

    Phase 4: the control API trusts this header directly.  Phase 6 will wire
    the full API-key authentication stack to replace this.

    Raises:
        HTTPException 401: If the header is absent.
    """
    if not x_admin_id:
        raise HTTPException(
            status_code=401,
            detail="Missing X-Admin-ID header.  Admin identity required for break-glass routes.",
        )
    return x_admin_id


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class GrantRequest(BaseModel):
    """POST /admin/break-glass/grant request body."""

    target_kb_id: str = Field(description="KB to unlock for admin content access.")
    reason: str = Field(
        min_length=1,
        description="Non-empty justification for the grant (M-001).",
    )
    window_hours: float | None = Field(
        default=None,
        gt=0,
        description=(
            "Grant duration in hours.  None → 4-hour default (D-04).  "
            "Zero or negative values are rejected."
        ),
    )
    notified_principals: list[str] = Field(
        default_factory=list,
        description="Principal IDs to notify of the grant (log/audit-based, M-004).",
    )


class GrantResponse(BaseModel):
    """Response body for a newly-issued break-glass grant."""

    grant_id: str = Field(description="Opaque grant identifier.")
    target_kb_id: str = Field(description="KB the grant unlocks.")
    granting_admin_id: str = Field(description="Admin who issued the grant.")
    reason: str = Field(description="Justification as stored.")
    granted_at: str = Field(description="ISO-8601 UTC grant timestamp.")
    expires_at: str = Field(description="ISO-8601 UTC expiry timestamp.")
    audit_entry_id: str = Field(description="Immutable audit log entry ID for this grant.")


class ActiveGrantInfo(BaseModel):
    """Summary of one active break-glass grant."""

    grant_id: str
    target_kb_id: str
    granting_admin_id: str
    reason: str
    granted_at: str
    expires_at: str
    audit_entry_id: str


class ActiveGrantsResponse(BaseModel):
    """Response body for GET /admin/break-glass/active."""

    grants: list[ActiveGrantInfo]


class RevokeResponse(BaseModel):
    """Response body for DELETE /admin/break-glass/{grant_id}."""

    grant_id: str
    revoked: bool
    revoked_at: str


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/admin/break-glass/grant",
    response_model=GrantResponse,
    summary="Issue a break-glass grant",
    description=(
        "Create a time-limited grant that allows an admin to read content from a KB "
        "without being in the chunk's permission_principals list.  "
        "Reason is mandatory (M-001).  "
        "The audit row is written BEFORE the grant (M-004 immutability guarantee).  "
        "Every read under the grant is written to the immutable audit log (T-05)."
    ),
)
def create_grant(
    body: GrantRequest,
    admin_id: Annotated[str, Depends(_require_admin_id)],
) -> GrantResponse:
    """Issue a break-glass grant (admin only; reason required)."""
    from finecorpus.control.break_glass import issue_grant

    session_factory = get_session_factory()
    window = timedelta(hours=body.window_hours) if body.window_hours is not None else None

    with session_factory() as session:
        try:
            grant_record, audit_record = issue_grant(
                session=session,
                target_kb_id=body.target_kb_id,
                granting_admin_id=admin_id,
                reason=body.reason,
                window=window,
                notified_principals=body.notified_principals,
            )
            session.commit()
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    return GrantResponse(
        grant_id=grant_record.grant_id,
        target_kb_id=grant_record.target_kb_id,
        granting_admin_id=grant_record.granting_admin_id,
        reason=grant_record.reason,
        granted_at=grant_record.granted_at.isoformat(),
        expires_at=grant_record.expires_at.isoformat(),
        audit_entry_id=grant_record.audit_entry_id,
    )


@router.get(
    "/admin/break-glass/active",
    response_model=ActiveGrantsResponse,
    summary="List active break-glass grants",
    description=(
        "Return all currently active (non-expired, non-revoked) break-glass grants.  Admin only."
    ),
)
def list_active_grants(
    admin_id: Annotated[str, Depends(_require_admin_id)],
) -> ActiveGrantsResponse:
    """List active break-glass grants."""
    from finecorpus.control.break_glass import BreakGlassRepository

    session_factory = get_session_factory()
    with session_factory() as session:
        repo = BreakGlassRepository(session)
        grants = repo.list_active()

    return ActiveGrantsResponse(
        grants=[
            ActiveGrantInfo(
                grant_id=g.grant_id,
                target_kb_id=g.target_kb_id,
                granting_admin_id=g.granting_admin_id,
                reason=g.reason,
                granted_at=g.granted_at.isoformat(),
                expires_at=g.expires_at.isoformat(),
                audit_entry_id=g.audit_entry_id,
            )
            for g in grants
        ]
    )


@router.delete(
    "/admin/break-glass/{grant_id}",
    response_model=RevokeResponse,
    summary="Revoke a break-glass grant",
    description=(
        "Revoke an active or expired break-glass grant.  "
        "Admin only.  Returns 404 if the grant does not exist.  "
        "Returns 409 if the grant is already revoked."
    ),
)
def revoke_grant(
    grant_id: str,
    admin_id: Annotated[str, Depends(_require_admin_id)],
) -> RevokeResponse:
    """Revoke a break-glass grant."""
    from finecorpus.control.break_glass import BreakGlassRepository

    session_factory = get_session_factory()
    with session_factory() as session:
        repo = BreakGlassRepository(session)
        try:
            record = repo.revoke(grant_id)
            session.commit()
        except KeyError:
            raise HTTPException(
                status_code=404,
                detail=f"Grant '{grant_id}' not found.",
            ) from None
        except ValueError as exc:
            raise HTTPException(
                status_code=409,
                detail=str(exc),
            ) from exc

    return RevokeResponse(
        grant_id=record.grant_id,
        revoked=True,
        revoked_at=record.revoked_at.isoformat() if record.revoked_at else "",
    )


__all__ = ["router"]
