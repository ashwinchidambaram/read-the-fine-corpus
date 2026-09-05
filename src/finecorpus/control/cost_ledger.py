"""Cost ledger and budget guard (§16, M-088).

Provides:
  - CostLedgerRecord       ORM model
  - CostLedgerRepository   record() / accrued_usd()
  - BudgetDecision         StrEnum (allow | pause_kb_cap | pause_workspace_cap)
  - BudgetGuard            check() → BudgetDecision

Design: BudgetGuard receives cap values at construction time via plain arguments.
It does NOT import finecorpus.config — control is the bottom layer and must not
have upward dependencies.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import DateTime, Integer, Numeric, String, func, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from finecorpus.control.metadata import Base

# ---------------------------------------------------------------------------
# ORM model
# ---------------------------------------------------------------------------


class CostLedgerRecord(Base):
    """ORM model for the ``cost_ledger`` table (§16, M-088)."""

    __tablename__ = "cost_ledger"

    ledger_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    kb_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    workspace_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    operation_type: Mapped[str] = mapped_column(String(64), nullable=False)
    cost_usd: Mapped[Any] = mapped_column(Numeric(precision=18, scale=8), nullable=False)
    tokens_consumed: Mapped[int] = mapped_column(Integer(), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )

    def __repr__(self) -> str:
        return (
            f"CostLedgerRecord("
            f"ledger_id={self.ledger_id!r}, "
            f"kb_id={self.kb_id!r}, "
            f"operation_type={self.operation_type!r}, "
            f"cost_usd={self.cost_usd!r})"
        )


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------


class CostLedgerRepository:
    """Write and aggregate cost ledger entries (§16, M-088).

    Args:
        session: SQLAlchemy Session bound to the control-plane engine.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def record(
        self,
        *,
        kb_id: str,
        workspace_id: str,
        operation_type: str,
        cost_usd: float,
        tokens_consumed: int,
        recorded_at: datetime | None = None,
    ) -> CostLedgerRecord:
        """Append a cost ledger entry.

        Args:
            kb_id: KB that incurred the cost.
            workspace_id: Owning workspace.
            operation_type: Type of operation (e.g. 'embedding', 'llm_classify').
            cost_usd: Cost in USD (non-negative).
            tokens_consumed: Number of tokens consumed (non-negative).
            recorded_at: Timestamp (defaults to UTC now).

        Returns:
            New CostLedgerRecord (added to session, not committed).

        Raises:
            ValueError: If cost_usd or tokens_consumed is negative.
        """
        if cost_usd < 0:
            raise ValueError(f"cost_usd must be non-negative; got {cost_usd!r}")
        if tokens_consumed < 0:
            raise ValueError(f"tokens_consumed must be non-negative; got {tokens_consumed!r}")

        if recorded_at is None:
            recorded_at = datetime.now(tz=UTC)

        ledger_id = secrets.token_hex(16)
        record = CostLedgerRecord(
            ledger_id=ledger_id,
            kb_id=kb_id,
            workspace_id=workspace_id,
            operation_type=operation_type,
            cost_usd=Decimal(str(cost_usd)),
            tokens_consumed=tokens_consumed,
            recorded_at=recorded_at,
        )
        self._session.add(record)
        return record

    def accrued_usd(
        self,
        *,
        kb_id: str | None = None,
        workspace_id: str | None = None,
        since: datetime | None = None,
    ) -> Decimal:
        """Return total cost accrued, optionally filtered.

        At least one of ``kb_id`` or ``workspace_id`` should be provided for
        meaningful results; calling with no filters returns the global total.

        Args:
            kb_id: Filter by KB (nullable).
            workspace_id: Filter by workspace (nullable).
            since: If provided, only include entries recorded after this time.

        Returns:
            Total cost as Decimal (zero if no rows match).
        """
        stmt = select(func.sum(CostLedgerRecord.cost_usd))

        if kb_id is not None:
            stmt = stmt.where(CostLedgerRecord.kb_id == kb_id)
        if workspace_id is not None:
            stmt = stmt.where(CostLedgerRecord.workspace_id == workspace_id)
        if since is not None:
            stmt = stmt.where(CostLedgerRecord.recorded_at > since)

        result = self._session.execute(stmt).scalar_one_or_none()
        if result is None:
            return Decimal("0")
        return Decimal(str(result))


# ---------------------------------------------------------------------------
# BudgetGuard
# ---------------------------------------------------------------------------


class BudgetDecision(StrEnum):
    """Decision returned by BudgetGuard.check()."""

    allow = "allow"
    pause_kb_cap = "pause_kb_cap"
    pause_workspace_cap = "pause_workspace_cap"


class BudgetGuard:
    """Check projected spend against configured caps.

    The guard is constructed with cap values passed in directly — it does NOT
    import finecorpus.config.  The caller (service or job runner layer) is
    responsible for loading the caps from config and passing them here.

    Args:
        kb_cap_usd: Per-KB cap in USD (None → no cap).
        workspace_cap_usd: Per-workspace cap in USD (None → no cap).
        repo: CostLedgerRepository for current-spend queries.
    """

    def __init__(
        self,
        *,
        kb_cap_usd: float | None,
        workspace_cap_usd: float | None,
        repo: CostLedgerRepository,
    ) -> None:
        self._kb_cap = Decimal(str(kb_cap_usd)) if kb_cap_usd is not None else None
        self._ws_cap = Decimal(str(workspace_cap_usd)) if workspace_cap_usd is not None else None
        self._repo = repo

    def check(
        self,
        kb_id: str,
        workspace_id: str,
        projected_usd: float,
    ) -> BudgetDecision:
        """Check whether the projected spend would breach a cap.

        Args:
            kb_id: KB for the operation.
            workspace_id: Workspace for the operation.
            projected_usd: Estimated additional cost in USD.

        Returns:
            BudgetDecision:
              - allow              → proceed normally
              - pause_kb_cap       → KB cap would be breached
              - pause_workspace_cap → workspace cap would be breached
        """
        projected = Decimal(str(projected_usd))

        # KB cap check
        if self._kb_cap is not None:
            kb_accrued = self._repo.accrued_usd(kb_id=kb_id)
            if kb_accrued + projected > self._kb_cap:
                return BudgetDecision.pause_kb_cap

        # Workspace cap check
        if self._ws_cap is not None:
            ws_accrued = self._repo.accrued_usd(workspace_id=workspace_id)
            if ws_accrued + projected > self._ws_cap:
                return BudgetDecision.pause_workspace_cap

        return BudgetDecision.allow


__all__ = [
    "BudgetDecision",
    "BudgetGuard",
    "CostLedgerRecord",
    "CostLedgerRepository",
]
