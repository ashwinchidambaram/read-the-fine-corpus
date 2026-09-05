"""Break-glass grant management (§2.3, M-001..M-004).

Provides:
  - BreakGlassGrantRecord   ORM model
  - BreakGlassRepository    grant / active_grant_for / expire_due / revoke

Key invariants (M-001..M-004 storage side):
  - M-001: reason must be non-empty — enforced at grant() time.
  - M-004: expires_at is NOT NULL and must be a finite positive duration —
    validated at grant() time (D-04 ruling: rejects None/0/negative windows).
  - Default window: 4 hours (D-04).
  - Every grant is linked to an audit_log entry (audit_entry_id FK).

The HTTP layer (Phase 4 later) is responsible for notifying principals and
reading active grants; this module is storage only.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, String, Text, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from finecorpus.control.metadata import Base

# ---------------------------------------------------------------------------
# D-04 default window
# ---------------------------------------------------------------------------

DEFAULT_WINDOW_HOURS: int = 4


def _validate_window(window: timedelta | None) -> timedelta:
    """Validate and return the grant window.

    Args:
        window: Requested duration.  None → use default (4 h, D-04).

    Returns:
        Validated timedelta.

    Raises:
        ValueError: If the window is None, zero, or negative.
    """
    if window is None:
        return timedelta(hours=DEFAULT_WINDOW_HOURS)
    if window.total_seconds() <= 0:
        raise ValueError(
            f"break-glass grant window must be a finite positive duration (D-04); got {window!r}"
        )
    return window


# ---------------------------------------------------------------------------
# ORM model
# ---------------------------------------------------------------------------


class BreakGlassGrantRecord(Base):
    """ORM model for the ``break_glass_grants`` table (§2.3)."""

    __tablename__ = "break_glass_grants"

    grant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    target_kb_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    granting_admin_id: Mapped[str] = mapped_column(String(255), nullable=False)
    reason: Mapped[str] = mapped_column(Text(), nullable=False)
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    notified_principals: Mapped[Any] = mapped_column(JSON, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    audit_entry_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("audit_log.entry_id", name="fk_break_glass_audit_entry"),
        nullable=False,
    )

    def __repr__(self) -> str:
        return (
            f"BreakGlassGrantRecord("
            f"grant_id={self.grant_id!r}, "
            f"target_kb_id={self.target_kb_id!r}, "
            f"granting_admin_id={self.granting_admin_id!r})"
        )


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------


class BreakGlassRepository:
    """Storage operations for break-glass grants (§2.3, M-001..M-004).

    Args:
        session: SQLAlchemy Session bound to the control-plane engine.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def grant(
        self,
        *,
        target_kb_id: str,
        granting_admin_id: str,
        reason: str,
        audit_entry_id: str,
        window: timedelta | None = None,
        notified_principals: list[str] | None = None,
        granted_at: datetime | None = None,
    ) -> BreakGlassGrantRecord:
        """Create a break-glass grant.

        Args:
            target_kb_id: KB being unlocked for admin access.
            granting_admin_id: Admin principal ID creating the grant.
            reason: Non-empty justification for the grant (M-001).
            audit_entry_id: FK to the corresponding audit_log entry.
            window: Grant duration.  None → 4 h default (D-04).
                    Zero / negative → raises ValueError.
            notified_principals: List of principal IDs notified of the grant.
            granted_at: Grant timestamp (defaults to UTC now).

        Returns:
            New BreakGlassGrantRecord (added to session, not committed).

        Raises:
            ValueError: If reason is empty (M-001) or window is invalid (D-04).
        """
        if not reason or not reason.strip():
            raise ValueError("break-glass grant requires a non-empty reason (M-001)")

        effective_window = _validate_window(window)

        if granted_at is None:
            granted_at = datetime.now(tz=UTC)

        expires_at = granted_at + effective_window
        grant_id = secrets.token_hex(16)

        record = BreakGlassGrantRecord(
            grant_id=grant_id,
            target_kb_id=target_kb_id,
            granting_admin_id=granting_admin_id,
            reason=reason.strip(),
            granted_at=granted_at,
            expires_at=expires_at,
            notified_principals=notified_principals or [],
            revoked_at=None,
            audit_entry_id=audit_entry_id,
        )
        self._session.add(record)
        return record

    def active_grant_for(
        self, kb_id: str, admin_id: str, *, now: datetime | None = None
    ) -> BreakGlassGrantRecord | None:
        """Return an active (non-expired, non-revoked) grant for (kb_id, admin_id).

        Args:
            kb_id: Target KB identifier.
            admin_id: Admin principal ID.
            now: Reference time (defaults to UTC now).

        Returns:
            BreakGlassGrantRecord or None.
        """
        if now is None:
            now = datetime.now(tz=UTC)

        stmt = (
            select(BreakGlassGrantRecord)
            .where(BreakGlassGrantRecord.target_kb_id == kb_id)
            .where(BreakGlassGrantRecord.granting_admin_id == admin_id)
            .where(BreakGlassGrantRecord.revoked_at.is_(None))
            .where(BreakGlassGrantRecord.expires_at > now)
            .order_by(BreakGlassGrantRecord.granted_at.desc())
            .limit(1)
        )
        return self._session.execute(stmt).scalar_one_or_none()

    def expire_due(self, now: datetime | None = None) -> list[BreakGlassGrantRecord]:
        """Return grants that have passed their expiry but are not yet revoked.

        Used by the expiry-notification sweep to notify principals.

        Args:
            now: Reference time (defaults to UTC now).

        Returns:
            List of expired, un-revoked BreakGlassGrantRecord.
        """
        if now is None:
            now = datetime.now(tz=UTC)

        stmt = (
            select(BreakGlassGrantRecord)
            .where(BreakGlassGrantRecord.expires_at <= now)
            .where(BreakGlassGrantRecord.revoked_at.is_(None))
        )
        return list(self._session.execute(stmt).scalars())

    def revoke(
        self,
        grant_id: str,
        *,
        revoked_at: datetime | None = None,
    ) -> BreakGlassGrantRecord:
        """Revoke a break-glass grant.

        Args:
            grant_id: Grant identifier.
            revoked_at: Revocation timestamp (defaults to UTC now).

        Returns:
            Updated BreakGlassGrantRecord.

        Raises:
            KeyError: If the grant is not found.
            ValueError: If already revoked.
        """
        stmt = select(BreakGlassGrantRecord).where(BreakGlassGrantRecord.grant_id == grant_id)
        record = self._session.execute(stmt).scalar_one_or_none()
        if record is None:
            raise KeyError(f"grant_id {grant_id!r} not found")
        if record.revoked_at is not None:
            raise ValueError(f"grant {grant_id!r} is already revoked")
        if revoked_at is None:
            revoked_at = datetime.now(tz=UTC)
        record.revoked_at = revoked_at
        return record


__all__ = [
    "BreakGlassGrantRecord",
    "BreakGlassRepository",
    "DEFAULT_WINDOW_HOURS",
]
