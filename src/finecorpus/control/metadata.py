"""Control-plane alias record persistence (index-lifecycle.md §2.3).

SQLAlchemy 2.x ORM for the ``alias_records`` table.  The control-plane DB
(PostgreSQL) is the authority for:
  - Which collection name the alias currently points to.
  - The denormalized model identity used by the retrieval service for the
    mismatch-fails-closed check (§15).
  - The N-1 collection retained for instant rollback (§10.3).

This is the Phase 1 minimal schema.  The full control-plane DB schema (jobs,
tombstone log, inventory, audit log, cost ledger) is out of scope for this
phase and will be added in subsequent phases.

Spec references: index-lifecycle.md §2.3, §4.2 (two-phase swap), §10.3
(rollback), §15 (model identity mismatch).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    DateTime,
    Integer,
    String,
    Text,
    select,
)
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ORM base
# ---------------------------------------------------------------------------


class _Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# alias_records table
# ---------------------------------------------------------------------------


class AliasRecord(_Base):
    """Control-plane alias record (index-lifecycle.md §2.3).

    One row per knowledge base. The authoritative record for:
    - The alias name and its current Qdrant collection target.
    - The denormalized model identity (provider/model/dimensions/config_version)
      used by the retrieval service for the mismatch-fails-closed check (§15).
    - The N-1 collection for instant rollback (§10.3).

    Updated atomically in Phase 2 of the two-phase alias swap (§4.2).

    Columns:
        alias:               Alias name, e.g. ``rtfc_{kb_id}``. Primary key.
        kb_id:               Knowledge-base UUID (stable, the FK anchor).
        workspace_id:        Owning workspace UUID.
        collection_name:     Current live collection name.
        build_id:            Build ID of the current collection (for ordering).
        embedding_provider:  Denormalized from ModelIdentity.provider.
        embedding_model:     Denormalized from ModelIdentity.model.
        embedding_dimensions: Denormalized from ModelIdentity.dimensions.
        config_version:      Denormalized from ModelIdentity.config_version.
        promoted_at:         UTC timestamp of the last successful promotion.
        previous_collection: N-1 collection name (null before second promotion).
    """

    __tablename__ = "alias_records"

    alias: Mapped[str] = mapped_column(String(255), primary_key=True)
    kb_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    workspace_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    collection_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    build_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    embedding_provider: Mapped[str | None] = mapped_column(String(128), nullable=True)
    embedding_model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    embedding_dimensions: Mapped[int | None] = mapped_column(Integer, nullable=True)
    config_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    promoted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    previous_collection: Mapped[str | None] = mapped_column(String(255), nullable=True)

    def __repr__(self) -> str:
        return (
            f"AliasRecord(alias={self.alias!r}, "
            f"collection={self.collection_name!r}, "
            f"build_id={self.build_id!r})"
        )


# ---------------------------------------------------------------------------
# Engine factory
# ---------------------------------------------------------------------------


def create_engine(dsn: str, **kwargs: Any) -> Engine:
    """Create a SQLAlchemy engine from a PostgreSQL DSN.

    Args:
        dsn: PostgreSQL connection URL.
        **kwargs: Additional keyword arguments forwarded to SQLAlchemy's
            ``create_engine`` (e.g. ``pool_size``, ``echo``).

    Returns:
        Configured SQLAlchemy Engine.
    """
    return sa_create_engine(dsn, **kwargs)


def create_tables(engine: Engine) -> None:
    """Create all control-plane tables if they do not exist.

    In production, tables are created by Alembic migrations. This function is
    provided for integration tests that bring up a fresh schema.

    Args:
        engine: Bound SQLAlchemy engine.
    """
    _Base.metadata.create_all(engine)


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------


class AliasRepository:
    """CRUD operations on the ``alias_records`` table.

    Used exclusively by the lifecycle module (``finecorpus.index.lifecycle``).
    The retrieval service reads alias records via this same class; it never
    queries the vector DB for model identity.

    All write operations use explicit transactions; callers must hold a session.

    Args:
        session: SQLAlchemy ``Session`` bound to the control-plane engine.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, alias: str) -> AliasRecord | None:
        """Fetch an alias record by alias name.

        Args:
            alias: Alias name (e.g. ``rtfc_{kb_id}``).

        Returns:
            The AliasRecord or None if not found.
        """
        stmt = select(AliasRecord).where(AliasRecord.alias == alias)
        return self._session.execute(stmt).scalar_one_or_none()

    def get_by_kb(self, kb_id: str) -> AliasRecord | None:
        """Fetch an alias record by knowledge-base ID.

        Args:
            kb_id: KB UUID string.

        Returns:
            The AliasRecord or None if not found.
        """
        stmt = select(AliasRecord).where(AliasRecord.kb_id == kb_id)
        return self._session.execute(stmt).scalar_one_or_none()

    def create(
        self,
        alias: str,
        kb_id: str,
        workspace_id: str,
    ) -> AliasRecord:
        """Create a new alias record with no collection (pre-provisioning state).

        Called when a knowledge base is first created. The alias exists but
        points at nothing until the first promotion completes.

        Args:
            alias: Alias name.
            kb_id: KB UUID.
            workspace_id: Workspace UUID.

        Returns:
            The newly created AliasRecord (added to the session, not committed).
        """
        record = AliasRecord(
            alias=alias,
            kb_id=kb_id,
            workspace_id=workspace_id,
            collection_name=None,
            build_id=None,
            embedding_provider=None,
            embedding_model=None,
            embedding_dimensions=None,
            config_version=None,
            promoted_at=None,
            previous_collection=None,
        )
        self._session.add(record)
        logger.info("Created alias record '%s' for kb '%s'", alias, kb_id)
        return record

    def promote(
        self,
        alias: str,
        new_collection: str,
        new_build_id: int,
        embedding_provider: str,
        embedding_model: str,
        embedding_dimensions: int,
        config_version: str,
        promoted_at: datetime | None = None,
    ) -> AliasRecord:
        """Update the alias record for a successful promotion (§4.2 Phase 2).

        Atomically (within the caller's transaction):
        - Moves the current ``collection_name`` to ``previous_collection``.
        - Sets ``collection_name`` to ``new_collection``.
        - Updates all model identity fields.
        - Sets ``promoted_at`` to now.

        This is Phase 2 of the two-phase alias swap. Phase 1 (Qdrant retarget)
        must have already succeeded. If Phase 2 fails, the startup reconciler
        (``lifecycle.startup_reconcile``) detects the inconsistency and retries.

        Args:
            alias: Alias name.
            new_collection: New live collection name.
            new_build_id: Build ID of the new collection.
            embedding_provider: Provider name (from ModelIdentity).
            embedding_model: Model identifier.
            embedding_dimensions: Vector dimensionality.
            config_version: Config version hash.
            promoted_at: Promotion timestamp; defaults to UTC now.

        Returns:
            Updated AliasRecord.

        Raises:
            ValueError: If the alias record does not exist.
        """
        record = self.get(alias)
        if record is None:
            raise ValueError(f"Alias record '{alias}' not found; cannot promote")

        if promoted_at is None:
            promoted_at = datetime.now(tz=UTC)

        record.previous_collection = record.collection_name
        record.collection_name = new_collection
        record.build_id = new_build_id
        record.embedding_provider = embedding_provider
        record.embedding_model = embedding_model
        record.embedding_dimensions = embedding_dimensions
        record.config_version = config_version
        record.promoted_at = promoted_at

        logger.info(
            "Promoted alias '%s': %s -> %s (build %d)",
            alias,
            record.previous_collection,
            new_collection,
            new_build_id,
        )
        return record

    def rollback_to_previous(self, alias: str) -> AliasRecord:
        """Swap the current collection with the previous (N-1) collection (§10.3).

        After rollback:
        - ``collection_name`` = former ``previous_collection`` (N-1 is now live).
        - ``previous_collection`` = former ``collection_name`` (former live is now N-1).

        This mirrors the Qdrant alias retarget that must have already succeeded.

        Args:
            alias: Alias name.

        Returns:
            Updated AliasRecord.

        Raises:
            ValueError: If there is no previous_collection (rollback impossible).
        """
        record = self.get(alias)
        if record is None:
            raise ValueError(f"Alias record '{alias}' not found; cannot rollback")
        if not record.previous_collection:
            raise ValueError(
                f"No N-1 collection available for alias '{alias}'; rollback impossible"
            )

        old_current = record.collection_name
        record.collection_name = record.previous_collection
        record.previous_collection = old_current
        record.promoted_at = datetime.now(tz=UTC)

        logger.info(
            "Rolled back alias '%s': %s -> %s",
            alias,
            old_current,
            record.collection_name,
        )
        return record

    def reconcile_from_qdrant(
        self,
        alias: str,
        qdrant_collection: str,
        embedding_provider: str,
        embedding_model: str,
        embedding_dimensions: int,
        config_version: str,
        build_id: int,
    ) -> AliasRecord:
        """Repair Phase-2 failure: update the record to match the Qdrant alias state.

        Called by ``startup_reconcile`` when the Qdrant alias points at a
        collection that the control-plane record does not reflect (OQ-L-1).

        The Qdrant alias is the leading state; the control-plane record is the
        lagging state. This method brings the record up to match Qdrant.

        Args:
            alias: Alias name.
            qdrant_collection: Collection name that Qdrant currently resolves to.
            embedding_provider: Model identity from the collection's metadata.
            embedding_model: Model identifier.
            embedding_dimensions: Vector dimensionality.
            config_version: Config version hash.
            build_id: Build ID from the collection name.

        Returns:
            Updated AliasRecord.
        """
        record = self.get(alias)
        if record is None:
            # Create the record from scratch if it is missing
            record = AliasRecord(
                alias=alias,
                kb_id="",  # Will be filled if caller provides it
                workspace_id="",
            )
            self._session.add(record)

        record.previous_collection = record.collection_name
        record.collection_name = qdrant_collection
        record.build_id = build_id
        record.embedding_provider = embedding_provider
        record.embedding_model = embedding_model
        record.embedding_dimensions = embedding_dimensions
        record.config_version = config_version
        record.promoted_at = datetime.now(tz=UTC)

        logger.warning(
            "Reconciled alias '%s': control-plane record updated to match Qdrant state "
            "(collection '%s'). This indicates a Phase-2 failure was repaired (OQ-L-1).",
            alias,
            qdrant_collection,
        )
        return record


__all__ = [
    "AliasRecord",
    "AliasRepository",
    "create_engine",
    "create_tables",
]
