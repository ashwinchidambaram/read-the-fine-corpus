"""Tombstone log and replay tracking (§17.1).

Design choice: SEPARATE MUTABLE TABLE for replay tracking.

  tombstone_log     → append-only (entry_id, kb_id, document_id, kind, ...)
  tombstone_replays → mutable (entry_id FK, collection, replayed_at) PK(entry_id, collection)

This keeps tombstone_log strictly append-only while allowing
per-collection replay acknowledgement to be written freely.

The alternative (replayed_into JSONB on tombstone_log) was rejected because
it would require UPDATEs on tombstone_log, violating the append-only invariant
and making the PG immutability trigger impossible to apply uniformly.

App-level immutability: TombstoneRepository exposes NO update or delete methods
on tombstone_log.  The PG trigger (created in migration 0002) enforces this at
the DB layer.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, String, Text, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from finecorpus.control.metadata import Base

# ---------------------------------------------------------------------------
# ORM models
# ---------------------------------------------------------------------------


class TombstoneRecord(Base):
    """ORM model for the ``tombstone_log`` table (§17.1, APPEND-ONLY)."""

    __tablename__ = "tombstone_log"

    entry_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    kb_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    document_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # 'delete' | 'purge'
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reason: Mapped[str] = mapped_column(Text(), nullable=False)
    deleted_by: Mapped[str] = mapped_column(String(255), nullable=False)
    snapshots_destroyed: Mapped[Any] = mapped_column(JSON, nullable=True)

    def __repr__(self) -> str:
        return (
            f"TombstoneRecord("
            f"entry_id={self.entry_id!r}, "
            f"kb_id={self.kb_id!r}, "
            f"document_id={self.document_id!r}, "
            f"kind={self.kind!r})"
        )


class TombstoneReplayRecord(Base):
    """Mutable replay-tracking companion to tombstone_log.

    One row per (tombstone entry, collection) once that collection has
    acknowledged and replayed the tombstone.

    Primary key: (entry_id, collection) — composite, so a collection can only
    be marked replayed once per tombstone entry.
    """

    __tablename__ = "tombstone_replays"

    entry_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("tombstone_log.entry_id", name="fk_tombstone_replay_entry"),
        primary_key=True,
    )
    collection: Mapped[str] = mapped_column(String(255), primary_key=True)
    replayed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    def __repr__(self) -> str:
        return f"TombstoneReplayRecord(entry_id={self.entry_id!r}, collection={self.collection!r})"


# Valid tombstone kinds
TOMBSTONE_KINDS: frozenset[str] = frozenset({"delete", "purge"})


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------


class TombstoneRepository:
    """Append-only storage for tombstone_log + mutable tombstone_replays.

    IMPORTANT: NO update or delete methods are exposed for tombstone_log.

    Args:
        session: SQLAlchemy Session bound to the control-plane engine.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def append(
        self,
        *,
        kb_id: str,
        document_id: str,
        kind: str,
        reason: str,
        deleted_by: str,
        snapshots_destroyed: list[str] | None = None,
        deleted_at: datetime | None = None,
    ) -> TombstoneRecord:
        """Append a new tombstone entry.

        Args:
            kb_id: KB identifier.
            document_id: Document being deleted/purged.
            kind: 'delete' or 'purge'.
            reason: Human-readable reason for the deletion.
            deleted_by: Actor ID performing the deletion.
            snapshots_destroyed: Optional list of snapshot names destroyed during purge.
            deleted_at: Deletion timestamp (defaults to UTC now).

        Returns:
            New TombstoneRecord (added to session, not committed).

        Raises:
            ValueError: If kind is not 'delete' or 'purge'.
        """
        if kind not in TOMBSTONE_KINDS:
            raise ValueError(f"tombstone kind must be one of {TOMBSTONE_KINDS!r}; got {kind!r}")

        if deleted_at is None:
            deleted_at = datetime.now(tz=UTC)

        entry_id = secrets.token_hex(16)
        record = TombstoneRecord(
            entry_id=entry_id,
            kb_id=kb_id,
            document_id=document_id,
            kind=kind,
            deleted_at=deleted_at,
            reason=reason,
            deleted_by=deleted_by,
            snapshots_destroyed=snapshots_destroyed,
        )
        self._session.add(record)
        return record

    def unreplayed_for(self, kb_id: str, collection: str) -> list[TombstoneRecord]:
        """Return tombstone entries for ``kb_id`` not yet replayed into ``collection``.

        Used by the reindex / purge job to find tombstones that need to be
        applied to a given collection.

        Args:
            kb_id: KB identifier.
            collection: Qdrant collection name.

        Returns:
            List of TombstoneRecord not yet acknowledged for this collection.
        """
        # Subquery: entry_ids already replayed into this collection
        replayed_subq = (
            select(TombstoneReplayRecord.entry_id)
            .where(TombstoneReplayRecord.collection == collection)
            .scalar_subquery()
        )

        stmt = (
            select(TombstoneRecord)
            .where(TombstoneRecord.kb_id == kb_id)
            .where(TombstoneRecord.entry_id.not_in(replayed_subq))
            .order_by(TombstoneRecord.deleted_at.asc())
        )
        return list(self._session.execute(stmt).scalars())

    def mark_replayed(
        self,
        entry_id: str,
        collection: str,
        *,
        replayed_at: datetime | None = None,
    ) -> TombstoneReplayRecord:
        """Mark a tombstone entry as replayed into a collection.

        Args:
            entry_id: Tombstone entry identifier.
            collection: Collection that applied the tombstone.
            replayed_at: Timestamp (defaults to UTC now).

        Returns:
            New TombstoneReplayRecord (added to session, not committed).

        Raises:
            ValueError: If the tombstone entry does not exist.
        """
        # Verify the tombstone entry exists
        stmt = select(TombstoneRecord).where(TombstoneRecord.entry_id == entry_id)
        tomb = self._session.execute(stmt).scalar_one_or_none()
        if tomb is None:
            raise ValueError(f"tombstone entry_id {entry_id!r} not found")

        if replayed_at is None:
            replayed_at = datetime.now(tz=UTC)

        replay = TombstoneReplayRecord(
            entry_id=entry_id,
            collection=collection,
            replayed_at=replayed_at,
        )
        self._session.add(replay)
        return replay

    def get(self, entry_id: str) -> TombstoneRecord | None:
        """Fetch a tombstone record by entry_id.

        Args:
            entry_id: Entry identifier.

        Returns:
            TombstoneRecord or None.
        """
        stmt = select(TombstoneRecord).where(TombstoneRecord.entry_id == entry_id)
        return self._session.execute(stmt).scalar_one_or_none()

    def list_for_kb(self, kb_id: str) -> list[TombstoneRecord]:
        """Return all tombstone entries for a KB.

        Args:
            kb_id: KB identifier.

        Returns:
            List of TombstoneRecord ordered by deleted_at ASC.
        """
        stmt = (
            select(TombstoneRecord)
            .where(TombstoneRecord.kb_id == kb_id)
            .order_by(TombstoneRecord.deleted_at.asc())
        )
        return list(self._session.execute(stmt).scalars())


__all__ = [
    "TOMBSTONE_KINDS",
    "TombstoneRecord",
    "TombstoneReplayRecord",
    "TombstoneRepository",
]
