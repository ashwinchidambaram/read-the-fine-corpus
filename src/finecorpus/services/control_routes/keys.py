"""Control-API routes: API key management (Phase 4, §14.2).

Routes:
    POST   /v1/kb/{kb_id}/keys        — issue a new key (returns plaintext ONCE)
    DELETE /v1/kb/{kb_id}/keys/{key_id} — revoke a key
    POST   /v1/kb/{kb_id}/keys/{key_id}/rotate — rotate (revoke + issue new)

Security invariants:
    - Plaintext key is returned EXACTLY ONCE, at issuance.
    - key_hash is NEVER returned in any response.
    - Revoke/rotate operations are logged in audit_log.
    - All routes use X-API-Key / Authorization header auth (admin required).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Path

logger = logging.getLogger(__name__)

router = APIRouter(tags=["keys"])

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


def _require_admin(raw_key: str | None, session_factory: Any) -> Any:
    """Validate the key and require admin role. Returns principal or raises 401/403."""
    if raw_key is None:
        raise HTTPException(status_code=401, detail="Missing API key.") from None
    try:
        from finecorpus.control.auth import ApiKeyRepository, Role

        with session_factory() as sess:
            repo = ApiKeyRepository(sess)
            principal = repo.validate(raw_key)
        if Role(principal.role) != Role.admin:
            raise HTTPException(
                status_code=403, detail="Admin role required for key management."
            ) from None
        return principal
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=401, detail="Invalid or expired API key.") from None


def _record_to_safe_dict(record: Any) -> dict[str, Any]:
    """Serialize a ServicePrincipalKeyRecord without key material."""
    return {
        "key_id": record.key_id,
        "principal_name": record.principal_name,
        "role": record.role,
        "scope_kind": record.scope_kind,
        "workspace_id": record.workspace_id,
        "kb_id": record.kb_id,
        "created_at": record.created_at.isoformat() if record.created_at else None,
        "expires_at": record.expires_at.isoformat() if record.expires_at else None,
        "revoked_at": record.revoked_at.isoformat() if record.revoked_at else None,
        "last_used_at": record.last_used_at.isoformat() if record.last_used_at else None,
        "created_by": record.created_by,
    }


@router.post("/v1/kb/{kb_id}/keys", summary="Issue a new API key for a KB")
def issue_key(
    kb_id: Annotated[str, Path(description="Knowledge-base UUID")],
    body: Annotated[dict[str, Any], Body(...)],
    raw_key: Annotated[str | None, Depends(_extract_raw_key)] = None,
) -> dict[str, Any]:
    """Issue a new service-principal API key scoped to a KB.

    Returns:
        ``plaintext_key`` (str): The full key string — returned ONCE, never again.
        ``record`` (dict): Key metadata (no hash, no secret material).

    Body fields:
        principal_name (str): Human-readable name for the principal.
        role (str): One of "service", "viewer", "editor", "admin".
        expires_at (str | null): ISO-8601 expiry (optional).
        created_by (str): Actor performing issuance (defaults to caller's name).
    """
    session_factory = _get_session()
    principal = _require_admin(raw_key, session_factory)

    principal_name = body.get("principal_name", "")
    if not principal_name:
        raise HTTPException(status_code=422, detail="principal_name is required.") from None

    role_str = body.get("role", "service")
    created_by = body.get("created_by", principal.name if principal else "api")
    expires_at_str: str | None = body.get("expires_at")

    expires_at: datetime | None = None
    if expires_at_str:
        try:
            expires_at = datetime.fromisoformat(expires_at_str).replace(tzinfo=UTC)
        except ValueError:
            raise HTTPException(status_code=422, detail="expires_at must be ISO-8601.") from None

    from finecorpus.control.auth import ApiKeyRepository, AuthError, Role, ScopeKind

    try:
        role = Role(role_str)
    except ValueError:
        raise HTTPException(
            status_code=422, detail=f"Invalid role '{role_str}'. Valid: {[r.value for r in Role]}"
        ) from None

    record_dict: dict[str, Any] = {}
    with session_factory() as session:
        repo = ApiKeyRepository(session)
        try:
            plaintext_key, record = repo.issue(
                principal_name=principal_name,
                role=role,
                scope_kind=ScopeKind.kb,
                created_by=created_by,
                kb_id=kb_id,
                expires_at=expires_at,
            )
        except AuthError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        # Append audit entry
        from finecorpus.control.audit import AuditAction, AuditLogRepository

        audit = AuditLogRepository(session)
        audit.append(
            entry_type=AuditAction.key_issued,
            actor_id=created_by,
            details={
                "key_id": record.key_id,
                "principal_name": principal_name,
                "role": role_str,
            },
            target_kb_id=kb_id,
        )
        # Serialize inside the session (before expiry)
        record_dict = _record_to_safe_dict(record)
        session.commit()

    # Telemetry (outside session to avoid connection contention)
    try:
        from finecorpus.telemetry import incr_permission_change

        incr_permission_change(kb_id=kb_id, change_type="key_issued")
    except Exception:  # noqa: BLE001
        pass

    return {
        "plaintext_key": plaintext_key,
        "record": record_dict,
    }


@router.delete("/v1/kb/{kb_id}/keys/{key_id}", summary="Revoke an API key")
def revoke_key(
    kb_id: Annotated[str, Path(description="Knowledge-base UUID")],
    key_id: Annotated[str, Path(description="Key ID to revoke")],
    raw_key: Annotated[str | None, Depends(_extract_raw_key)] = None,
) -> dict[str, Any]:
    """Revoke a service-principal API key.

    The key is immediately invalidated. All subsequent requests using it will
    receive 401.
    """
    session_factory = _get_session()
    principal = _require_admin(raw_key, session_factory)

    from finecorpus.control.auth import ApiKeyRepository, AuthError

    record_dict: dict[str, Any] = {}
    with session_factory() as session:
        repo = ApiKeyRepository(session)
        try:
            record = repo.revoke(
                key_id,
                revoked_by=principal.name if principal else "api",
            )
        except KeyError:
            raise HTTPException(status_code=404, detail=f"Key '{key_id}' not found.") from None
        except AuthError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        from finecorpus.control.audit import AuditAction, AuditLogRepository

        audit = AuditLogRepository(session)
        audit.append(
            entry_type=AuditAction.key_revoked,
            actor_id=principal.name if principal else "api",
            details={"key_id": key_id},
            target_kb_id=kb_id,
        )
        record_dict = _record_to_safe_dict(record)
        session.commit()

    try:
        from finecorpus.telemetry import incr_permission_change

        incr_permission_change(kb_id=kb_id, change_type="key_revoked")
    except Exception:  # noqa: BLE001
        pass

    return {"status": "revoked", "key_id": key_id, "record": record_dict}


@router.post("/v1/kb/{kb_id}/keys/{key_id}/rotate", summary="Rotate an API key")
def rotate_key(
    kb_id: Annotated[str, Path(description="Knowledge-base UUID")],
    key_id: Annotated[str, Path(description="Key ID to rotate")],
    body: Annotated[dict[str, Any] | None, Body()] = None,
    raw_key: Annotated[str | None, Depends(_extract_raw_key)] = None,
) -> dict[str, Any]:
    """Rotate an API key: revoke the old one and issue a replacement.

    Returns the new plaintext key ONCE.
    """
    session_factory = _get_session()
    principal = _require_admin(raw_key, session_factory)

    expires_at_str: str | None = (body or {}).get("expires_at")
    expires_at: datetime | None = None
    if expires_at_str:
        try:
            expires_at = datetime.fromisoformat(expires_at_str).replace(tzinfo=UTC)
        except ValueError:
            raise HTTPException(status_code=422, detail="expires_at must be ISO-8601.") from None

    from finecorpus.control.auth import ApiKeyRepository, AuthError

    new_record_dict: dict[str, Any] = {}
    with session_factory() as session:
        repo = ApiKeyRepository(session)
        try:
            new_plaintext, new_record = repo.rotate(
                key_id,
                rotated_by=principal.name if principal else "api",
                expires_at=expires_at,
            )
        except KeyError:
            raise HTTPException(status_code=404, detail=f"Key '{key_id}' not found.") from None
        except AuthError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        from finecorpus.control.audit import AuditAction, AuditLogRepository

        audit = AuditLogRepository(session)
        audit.append(
            entry_type=AuditAction.key_issued,
            actor_id=principal.name if principal else "api",
            details={"old_key_id": key_id, "new_key_id": new_record.key_id, "action": "rotate"},
            target_kb_id=kb_id,
        )
        new_record_dict = _record_to_safe_dict(new_record)
        session.commit()

    try:
        from finecorpus.telemetry import incr_permission_change

        incr_permission_change(kb_id=kb_id, change_type="key_rotated")
    except Exception:  # noqa: BLE001
        pass

    return {
        "plaintext_key": new_plaintext,
        "old_key_id": key_id,
        "new_record": new_record_dict,
    }


__all__ = ["configure", "router"]
