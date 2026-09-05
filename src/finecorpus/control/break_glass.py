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

    def get(self, grant_id: str) -> BreakGlassGrantRecord | None:
        """Return a grant by ID, or None if not found.

        Args:
            grant_id: Grant identifier.

        Returns:
            BreakGlassGrantRecord or None.
        """
        stmt = select(BreakGlassGrantRecord).where(BreakGlassGrantRecord.grant_id == grant_id)
        return self._session.execute(stmt).scalar_one_or_none()

    def list_active(self, *, now: datetime | None = None) -> list[BreakGlassGrantRecord]:
        """Return all currently active (non-expired, non-revoked) grants.

        Args:
            now: Reference time (defaults to UTC now).

        Returns:
            List of active BreakGlassGrantRecord ordered by granted_at desc.
        """
        if now is None:
            now = datetime.now(tz=UTC)

        stmt = (
            select(BreakGlassGrantRecord)
            .where(BreakGlassGrantRecord.revoked_at.is_(None))
            .where(BreakGlassGrantRecord.expires_at > now)
            .order_by(BreakGlassGrantRecord.granted_at.desc())
        )
        return list(self._session.execute(stmt).scalars())


# ---------------------------------------------------------------------------
# Grant lifecycle service functions (M-001..M-004)
# ---------------------------------------------------------------------------


def issue_grant(
    *,
    session: Any,
    target_kb_id: str,
    granting_admin_id: str,
    reason: str,
    window: timedelta | None = None,
    notified_principals: list[str] | None = None,
) -> tuple[BreakGlassGrantRecord, Any]:
    """Issue a break-glass grant with mandatory pre-audit (M-001..M-004).

    Grant flow: append audit row FIRST (break_glass_grant), then grant with
    audit_entry_id.  This ensures every grant has an immutable audit record
    even if the grant itself fails or is rolled back.

    M-004: Notification — the grant audit row and a log line serve as the
    notification mechanism in Phase 4.  UI-based notification is deferred to
    Phase 6.  Callers can inspect notified_principals to determine who was
    notified (typically the KB team / workspace editors).

    Args:
        session: SQLAlchemy Session.
        target_kb_id: KB being unlocked.
        granting_admin_id: Admin principal ID creating the grant.
        reason: Non-empty justification (M-001).
        window: Grant duration (None → 4 h default, D-04).
        notified_principals: List of principal IDs notified (log/audit-based, M-004).

    Returns:
        Tuple of (BreakGlassGrantRecord, AuditLogRecord).

    Raises:
        ValueError: If reason is empty (M-001) or window is invalid (D-04).
    """
    from finecorpus.control.audit import AuditAction, AuditLogRepository

    if not reason or not reason.strip():
        raise ValueError("break-glass grant requires a non-empty reason (M-001)")

    effective_window = _validate_window(window)
    notified = notified_principals or []

    audit_repo = AuditLogRepository(session)
    grant_repo = BreakGlassRepository(session)

    # Audit FIRST — M-004: log notification intent before granting.
    audit_record = audit_repo.append(
        entry_type=AuditAction.break_glass_grant,
        actor_id=granting_admin_id,
        target_kb_id=target_kb_id,
        details={
            "reason": reason.strip(),
            "window_hours": effective_window.total_seconds() / 3600,
            "notified_principals": notified,
        },
    )
    session.flush()  # materialize entry_id before FK reference

    import logging as _logging

    _log = _logging.getLogger(__name__)
    _log.info(
        "Break-glass grant issued: admin=%s kb=%s window=%s notified=%s audit=%s",
        granting_admin_id,
        target_kb_id,
        effective_window,
        notified,
        audit_record.entry_id,
    )

    grant_record = grant_repo.grant(
        target_kb_id=target_kb_id,
        granting_admin_id=granting_admin_id,
        reason=reason,
        audit_entry_id=audit_record.entry_id,
        window=effective_window,
        notified_principals=notified,
    )

    return grant_record, audit_record


def sweep_expired_grants(
    *,
    session: Any,
    now: datetime | None = None,
) -> list[tuple[BreakGlassGrantRecord, Any]]:
    """Find expired grants, write audit rows, and log notification lines (M-004).

    This is the expiry sweep function.  It finds grants that have passed their
    expiry but are not yet revoked, writes a break_glass_expiry audit row for
    each, and logs a notification line.  The grants are NOT marked as revoked
    here — they are already expired (expires_at <= now) and the active_grant_for
    / list_active queries exclude them.

    M-004 notification: log-based for Phase 4; UI notification deferred to Phase 6.
    Document: to be wired into a periodic task in Phase 6 (or on-demand sweep).

    Args:
        session: SQLAlchemy Session.
        now: Reference time (defaults to UTC now).

    Returns:
        List of (BreakGlassGrantRecord, AuditLogRecord) for each expired grant.
    """
    import logging as _logging

    _log = _logging.getLogger(__name__)

    from finecorpus.control.audit import AuditAction, AuditLogRepository

    if now is None:
        now = datetime.now(tz=UTC)

    grant_repo = BreakGlassRepository(session)
    audit_repo = AuditLogRepository(session)

    expired = grant_repo.expire_due(now=now)
    results = []

    for grant in expired:
        audit_record = audit_repo.append(
            entry_type=AuditAction.break_glass_expiry,
            actor_id=grant.granting_admin_id,
            target_kb_id=grant.target_kb_id,
            details={
                "grant_id": grant.grant_id,
                "expires_at": grant.expires_at.isoformat(),
                "reason": grant.reason,
            },
        )
        _log.info(
            "Break-glass grant expired: grant=%s admin=%s kb=%s expires_at=%s audit=%s",
            grant.grant_id,
            grant.granting_admin_id,
            grant.target_kb_id,
            grant.expires_at.isoformat(),
            audit_record.entry_id,
        )
        results.append((grant, audit_record))

    return results


__all__ = [
    "BreakGlassGrantRecord",
    "BreakGlassRepository",
    "DEFAULT_WINDOW_HOURS",
    "issue_grant",
    "sweep_expired_grants",
]
