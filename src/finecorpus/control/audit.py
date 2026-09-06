"""Audit log repository (§13.2).

The audit_log table is APPEND-ONLY.  This module enforces that at the
application layer: AuditLogRepository exposes NO update or delete methods.
The PG immutability trigger (created in migration 0002) is the database-level
enforcement; the app-level check fires on all dialects (including SQLite used
in unit tests).

AuditAction covers all governance events that must be recorded per the spec.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

# ---------------------------------------------------------------------------
# JSON column type (JSONB on PG, JSON on SQLite)
# ---------------------------------------------------------------------------
from sqlalchemy import JSON, DateTime, String, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from finecorpus.control.metadata import Base

# ---------------------------------------------------------------------------
# AuditAction enum
# ---------------------------------------------------------------------------


class AuditAction(StrEnum):
    """All governance event types that may appear in audit_log.entry_type."""

    break_glass_grant = "break_glass_grant"
    break_glass_read = "break_glass_read"
    break_glass_expiry = "break_glass_expiry"
    budget_cap_hit = "budget_cap_hit"
    config_change = "config_change"
    confidence_floor_lowered = "confidence_floor_lowered"
    deletion = "deletion"
    drift_detected = "drift_detected"
    key_issued = "key_issued"
    key_revoked = "key_revoked"
    permission_change = "permission_change"
    permission_gap_ack = "permission_gap_ack"
    promotion = "promotion"
    purge = "purge"
    rollback = "rollback"


# ---------------------------------------------------------------------------
# ORM model
# ---------------------------------------------------------------------------


class AuditLogRecord(Base):
    """ORM model for the ``audit_log`` table (§13.2, APPEND-ONLY)."""

    __tablename__ = "audit_log"

    entry_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    entry_type: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(255), nullable=False)
    target_kb_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    target_workspace_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    details: Mapped[Any] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )

    def __repr__(self) -> str:
        return (
            f"AuditLogRecord("
            f"entry_id={self.entry_id!r}, "
            f"entry_type={self.entry_type!r}, "
            f"actor_id={self.actor_id!r})"
        )


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------


class AuditLogRepository:
    """Append-only operations on the ``audit_log`` table.

    IMPORTANT: NO update or delete methods are exposed here.  The table is
    append-only by design (§13.2).

    Args:
        session: SQLAlchemy Session bound to the control-plane engine.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def append(
        self,
        *,
        entry_type: AuditAction,
        actor_id: str,
        details: dict[str, Any],
        target_kb_id: str | None = None,
        target_workspace_id: str | None = None,
        created_at: datetime | None = None,
    ) -> AuditLogRecord:
        """Append a new audit entry.

        Args:
            entry_type: Governance event type.
            actor_id: Actor who performed the action.
            details: Arbitrary JSON details about the event.
            target_kb_id: KB affected (nullable).
            target_workspace_id: Workspace affected (nullable).
            created_at: Timestamp (defaults to UTC now).

        Returns:
            New AuditLogRecord (added to session, not yet committed).
        """
        if created_at is None:
            created_at = datetime.now(tz=UTC)
        entry_id = secrets.token_hex(16)
        record = AuditLogRecord(
            entry_id=entry_id,
            entry_type=str(entry_type),
            actor_id=actor_id,
            target_kb_id=target_kb_id,
            target_workspace_id=target_workspace_id,
            details=details,
            created_at=created_at,
        )
        self._session.add(record)
        return record

    def list_for_kb(
        self,
        kb_id: str,
        *,
        limit: int = 100,
        since: datetime | None = None,
    ) -> list[AuditLogRecord]:
        """Return audit entries for a knowledge base, newest first.

        Args:
            kb_id: KB identifier.
            limit: Maximum number of records to return.
            since: If provided, only return entries created after this time.

        Returns:
            List of AuditLogRecord ordered by created_at DESC.
        """
        stmt = (
            select(AuditLogRecord)
            .where(AuditLogRecord.target_kb_id == kb_id)
            .order_by(AuditLogRecord.created_at.desc())
            .limit(limit)
        )
        if since is not None:
            stmt = stmt.where(AuditLogRecord.created_at > since)
        return list(self._session.execute(stmt).scalars())

    def list_for_workspace(
        self,
        workspace_id: str,
        *,
        limit: int = 100,
        since: datetime | None = None,
    ) -> list[AuditLogRecord]:
        """Return audit entries for a workspace, newest first.

        Args:
            workspace_id: Workspace identifier.
            limit: Maximum number of records to return.
            since: If provided, only return entries created after this time.

        Returns:
            List of AuditLogRecord ordered by created_at DESC.
        """
        stmt = (
            select(AuditLogRecord)
            .where(AuditLogRecord.target_workspace_id == workspace_id)
            .order_by(AuditLogRecord.created_at.desc())
            .limit(limit)
        )
        if since is not None:
            stmt = stmt.where(AuditLogRecord.created_at > since)
        return list(self._session.execute(stmt).scalars())

    def get(self, entry_id: str) -> AuditLogRecord | None:
        """Fetch a single audit entry by ID.

        Args:
            entry_id: Entry identifier.

        Returns:
            AuditLogRecord or None.
        """
        stmt = select(AuditLogRecord).where(AuditLogRecord.entry_id == entry_id)
        return self._session.execute(stmt).scalar_one_or_none()


__all__ = [
    "AuditAction",
    "AuditLogRecord",
    "AuditLogRepository",
]
