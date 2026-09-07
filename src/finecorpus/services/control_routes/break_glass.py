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
# Admin authentication (key-validated; identity is server-derived — a client-
# set header can NEVER establish admin identity on break-glass routes)
# ---------------------------------------------------------------------------


def _extract_raw_key(
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> str | None:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return x_api_key


def _require_admin_id(
    raw_key: Annotated[str | None, Depends(_extract_raw_key)] = None,
) -> str:
    """Authenticate an admin API key and return the server-derived identity.

    Security: break-glass is the most privileged operation in the system.
    Identity comes exclusively from a validated admin-role API key — never
    from a client-set header (spoofable-field bypass, fixed in PR #37).

    Returns the STABLE identity — ``principal.principal_id`` (= key_id) — NOT
    the human-readable ``principal.name``.  The retrieval read path validates a
    break-glass grant via ``active_grant_for(kb_id, principal.principal_id)``
    (retrieval/service.py), so the grant MUST be issued keyed on the same
    stable identity or every break-glass read fails closed (F-1).  ``name`` is
    a non-unique display label and is only surfaced in logs for readability.

    Raises:
        HTTPException 401/403: missing/invalid key or non-admin role.
    """
    if raw_key is None:
        raise HTTPException(
            status_code=401,
            detail="Admin API key required for break-glass routes.",
        ) from None
    try:
        from finecorpus.control.auth import ApiKeyRepository, Role

        session_factory = get_session_factory()
        with session_factory() as sess:
            repo = ApiKeyRepository(sess)
            principal = repo.validate(raw_key)
        if Role(principal.role) != Role.admin:
            raise HTTPException(
                status_code=403,
                detail="Admin role required for break-glass routes.",
            ) from None
        logger.info(
            "Break-glass admin authenticated: name=%s principal_id=%s",
            principal.name,
            principal.principal_id,
        )
        # F-1: stored grant identity and read-time lookup key must be the same
        # stable identity — principal_id (= key_id), never the human name.
        return principal.principal_id
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=401, detail="Invalid or expired API key.") from None


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
            grant_record, _audit_record = issue_grant(
                session=session,
                target_kb_id=body.target_kb_id,
                granting_admin_id=admin_id,
                reason=body.reason,
                window=window,
                notified_principals=body.notified_principals,
            )
            session.commit()
            # Read fields while the instance is still session-bound: the ORM
            # record detaches once the session closes (DetachedInstanceError).
            response = GrantResponse(
                grant_id=grant_record.grant_id,
                target_kb_id=grant_record.target_kb_id,
                granting_admin_id=grant_record.granting_admin_id,
                reason=grant_record.reason,
                granted_at=grant_record.granted_at.isoformat(),
                expires_at=grant_record.expires_at.isoformat(),
                audit_entry_id=grant_record.audit_entry_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    return response


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
        # Build the response while records are still session-bound.
        response = ActiveGrantsResponse(
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
    return response


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
            response = RevokeResponse(
                grant_id=record.grant_id,
                revoked=True,
                revoked_at=record.revoked_at.isoformat() if record.revoked_at else "",
            )
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

    return response


__all__ = ["router"]
