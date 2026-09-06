"""Control-API routes: budget accrual and caps (Phase 4, §16).

Routes:
    GET  /v1/budgets/accrual?kb_id= — accrued cost for a KB
    POST /v1/kb/{kb_id}/budget-cap  — set a KB-level budget cap (admin only)

All routes are THIN wrappers over ``finecorpus.control.cost_ledger.CostLedgerRepository``.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Path, Query

logger = logging.getLogger(__name__)

router = APIRouter(tags=["budgets"])

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


@router.get("/v1/budgets/accrual", summary="Get accrued cost for a KB")
def get_accrual(
    kb_id: str | None = Query(default=None, description="KB to query"),
    workspace_id: str | None = Query(default=None, description="Workspace to query"),
    raw_key: Annotated[str | None, Depends(_extract_raw_key)] = None,
) -> dict[str, Any]:
    """Return total accrued cost for a knowledge base or workspace.

    At least one of kb_id or workspace_id should be provided.
    """
    from finecorpus.control.cost_ledger import CostLedgerRepository

    session_factory = _get_session()
    with session_factory() as session:
        repo = CostLedgerRepository(session)
        accrued = repo.accrued_usd(kb_id=kb_id, workspace_id=workspace_id)

    return {
        "kb_id": kb_id,
        "workspace_id": workspace_id,
        "accrued_usd": str(accrued),
    }


@router.post("/v1/kb/{kb_id}/budget-cap", summary="Set a KB budget cap (admin only)")
def set_budget_cap(
    kb_id: Annotated[str, Path(description="Knowledge-base UUID")],
    body: Annotated[dict[str, Any], Body(...)],
    raw_key: Annotated[str | None, Depends(_extract_raw_key)] = None,
) -> dict[str, Any]:
    """Set or update the budget cap for a knowledge base (admin only).

    Body fields:
        cap_usd (float): New budget cap in USD. Pass null to remove the cap.
        set_by (str): Actor performing the change (defaults to "api").

    NOTE: Budget caps are advisory config stored in corpus.yaml; this endpoint
    records the cap-change event in the audit log and returns the new config
    for reference.  Actual enforcement is via BudgetGuard in the pipeline.
    """
    # Admin-only enforcement
    if raw_key is not None:
        session_factory = _get_session()
        try:
            from finecorpus.control.auth import ApiKeyRepository, Role

            with session_factory() as sess:
                repo = ApiKeyRepository(sess)
                principal = repo.validate(raw_key)
            if Role(principal.role) != Role.admin:
                raise HTTPException(
                    status_code=403, detail="Admin role required for budget-cap."
                ) from None
        except HTTPException:
            raise
        except Exception:  # noqa: BLE001
            raise HTTPException(status_code=401, detail="Invalid or expired API key.") from None

    cap_usd = body.get("cap_usd")
    set_by = body.get("set_by", "api")

    # Record in audit log
    session_factory = _get_session()
    with session_factory() as session:
        from finecorpus.control.audit import AuditAction, AuditLogRepository

        audit = AuditLogRepository(session)
        audit.append(
            entry_type=AuditAction.config_change,
            actor_id=str(set_by),
            details={"action": "budget_cap_set", "cap_usd": cap_usd},
            target_kb_id=kb_id,
        )
        session.commit()

    return {
        "kb_id": kb_id,
        "cap_usd": cap_usd,
        "set_by": set_by,
        "status": "recorded",
    }


__all__ = ["configure", "router"]
