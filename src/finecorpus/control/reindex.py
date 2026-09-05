"""Reindex trigger repository (§10.3).

Provides:
  - ReindexTriggerRecord       ORM model
  - ReindexTriggerRepository   get_or_create / list_for_kb / enable / disable /
                               record_fired / record_cap_hit / reset_cap_hits
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from finecorpus.control.metadata import Base

# ---------------------------------------------------------------------------
# ORM model
# ---------------------------------------------------------------------------


class ReindexTriggerRecord(Base):
    """ORM model for the ``reindex_triggers`` table (§10.3)."""

    __tablename__ = "reindex_triggers"

    trigger_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    kb_id: Mapped[str] = mapped_column(String(64), nullable=False, index=False)
    trigger_type: Mapped[str] = mapped_column(String(64), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=True)
    cron_expr: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_fired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_seen_config_version: Mapped[str | None] = mapped_column(Text(), nullable=True)
    consecutive_cap_hits: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)

    def __repr__(self) -> str:
        return (
            f"ReindexTriggerRecord("
            f"trigger_id={self.trigger_id!r}, "
            f"kb_id={self.kb_id!r}, "
            f"trigger_type={self.trigger_type!r}, "
            f"enabled={self.enabled!r})"
        )


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------


class ReindexTriggerRepository:
    """CRUD for ``reindex_triggers``.

    Args:
        session: SQLAlchemy Session bound to the control-plane engine.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_or_create(
        self,
        kb_id: str,
        trigger_type: str,
        *,
        cron_expr: str | None = None,
        enabled: bool = True,
    ) -> tuple[ReindexTriggerRecord, bool]:
        """Fetch an existing trigger or create a new one.

        Args:
            kb_id: Knowledge base identifier.
            trigger_type: Trigger type (e.g. 'scheduled', 'config_change').
            cron_expr: Optional cron expression for scheduled triggers.
            enabled: Whether the trigger starts enabled (only applied on create).

        Returns:
            Tuple of (record, created) where created is True if a new record
            was inserted.
        """
        stmt = (
            select(ReindexTriggerRecord)
            .where(ReindexTriggerRecord.kb_id == kb_id)
            .where(ReindexTriggerRecord.trigger_type == trigger_type)
            .limit(1)
        )
        existing = self._session.execute(stmt).scalar_one_or_none()
        if existing is not None:
            return existing, False

        trigger_id = secrets.token_hex(16)
        record = ReindexTriggerRecord(
            trigger_id=trigger_id,
            kb_id=kb_id,
            trigger_type=trigger_type,
            enabled=enabled,
            cron_expr=cron_expr,
            last_fired_at=None,
            last_seen_config_version=None,
            consecutive_cap_hits=0,
        )
        self._session.add(record)
        return record, True

    def get(self, trigger_id: str) -> ReindexTriggerRecord | None:
        """Fetch a trigger by ID.

        Args:
            trigger_id: Trigger identifier.

        Returns:
            ReindexTriggerRecord or None.
        """
        stmt = select(ReindexTriggerRecord).where(ReindexTriggerRecord.trigger_id == trigger_id)
        return self._session.execute(stmt).scalar_one_or_none()

    def list_for_kb(self, kb_id: str) -> list[ReindexTriggerRecord]:
        """Return all triggers for a KB.

        Args:
            kb_id: Knowledge base identifier.

        Returns:
            List of ReindexTriggerRecord.
        """
        stmt = select(ReindexTriggerRecord).where(ReindexTriggerRecord.kb_id == kb_id)
        return list(self._session.execute(stmt).scalars())

    def enable(self, trigger_id: str) -> ReindexTriggerRecord:
        """Enable a trigger.

        Args:
            trigger_id: Trigger identifier.

        Returns:
            Updated record.
        """
        record = self._get_or_raise(trigger_id)
        record.enabled = True
        return record

    def disable(self, trigger_id: str) -> ReindexTriggerRecord:
        """Disable a trigger.

        Args:
            trigger_id: Trigger identifier.

        Returns:
            Updated record.
        """
        record = self._get_or_raise(trigger_id)
        record.enabled = False
        return record

    def record_fired(
        self,
        trigger_id: str,
        *,
        fired_at: datetime | None = None,
        config_version: str | None = None,
    ) -> ReindexTriggerRecord:
        """Update last_fired_at and optionally last_seen_config_version.

        Args:
            trigger_id: Trigger identifier.
            fired_at: Fire timestamp (defaults to UTC now).
            config_version: Config version at fire time.

        Returns:
            Updated record.
        """
        record = self._get_or_raise(trigger_id)
        if fired_at is None:
            fired_at = datetime.now(tz=UTC)
        record.last_fired_at = fired_at
        if config_version is not None:
            record.last_seen_config_version = config_version
        return record

    def record_cap_hit(self, trigger_id: str) -> ReindexTriggerRecord:
        """Increment consecutive_cap_hits (budget cap prevented this fire).

        Args:
            trigger_id: Trigger identifier.

        Returns:
            Updated record.
        """
        record = self._get_or_raise(trigger_id)
        record.consecutive_cap_hits = record.consecutive_cap_hits + 1
        return record

    def reset_cap_hits(self, trigger_id: str) -> ReindexTriggerRecord:
        """Reset consecutive_cap_hits to 0 after a successful fire.

        Args:
            trigger_id: Trigger identifier.

        Returns:
            Updated record.
        """
        record = self._get_or_raise(trigger_id)
        record.consecutive_cap_hits = 0
        return record

    def _get_or_raise(self, trigger_id: str) -> ReindexTriggerRecord:
        record = self.get(trigger_id)
        if record is None:
            raise KeyError(f"trigger_id {trigger_id!r} not found")
        return record


__all__ = [
    "ReindexTriggerRecord",
    "ReindexTriggerRepository",
]
