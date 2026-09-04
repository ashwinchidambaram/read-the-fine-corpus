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
import re
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


# Public re-export of the ORM declarative base so that test code can import
# ``Base`` from this module without depending on the private ``_Base`` name.
# Plain assignment (not TypeAlias) keeps the object usable at runtime
# (e.g. ``Base.metadata.create_all(engine)`` works as expected).
Base = _Base


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
        alias:                    Alias name, e.g. ``rtfc_{kb_id}``. Primary key.
        kb_id:                    Knowledge-base UUID (stable, the FK anchor).
        workspace_id:             Owning workspace UUID.
        collection_name:          Current live collection name.
        build_id:                 Build ID of the current collection (for ordering).
        embedding_provider:       Denormalized from ModelIdentity.provider.
        embedding_model:          Denormalized from ModelIdentity.model.
        embedding_dimensions:     Denormalized from ModelIdentity.dimensions.
        config_version:           Denormalized from ModelIdentity.config_version.
        promoted_at:              UTC timestamp of the last successful promotion.
        previous_collection:      N-1 collection name (null before second promotion).
        previous_embedding_provider:  N-1 model identity — provider. Written at
            promote time alongside ``previous_collection``; swapped wholesale on
            rollback so the record is always self-consistent (§10.3).
        previous_embedding_model:     N-1 model identity — model identifier.
        previous_embedding_dimensions: N-1 model identity — vector dimensionality.
        previous_config_version:      N-1 model identity — config version hash.
    """

    __tablename__ = "alias_records"

    alias: Mapped[str] = mapped_column(String(255), primary_key=True)
    # index=False: indexes are created explicitly in the Alembic migration (F-02).
    kb_id: Mapped[str] = mapped_column(String(64), nullable=False, index=False)
    workspace_id: Mapped[str] = mapped_column(String(64), nullable=False, index=False)
    collection_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    build_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    embedding_provider: Mapped[str | None] = mapped_column(String(128), nullable=True)
    embedding_model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    embedding_dimensions: Mapped[int | None] = mapped_column(Integer, nullable=True)
    config_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    promoted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # N-1 collection + denormalized model identity (written at promote time, §10.3)
    previous_collection: Mapped[str | None] = mapped_column(String(255), nullable=True)
    previous_embedding_provider: Mapped[str | None] = mapped_column(String(128), nullable=True)
    previous_embedding_model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    previous_embedding_dimensions: Mapped[int | None] = mapped_column(Integer, nullable=True)
    previous_config_version: Mapped[str | None] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:
        return (
            f"AliasRecord(alias={self.alias!r}, "
            f"collection={self.collection_name!r}, "
            f"build_id={self.build_id!r})"
        )


# ---------------------------------------------------------------------------
# Engine factory
# ---------------------------------------------------------------------------


def _redact_dsn(dsn: str) -> str:
    """Return the DSN with the password component replaced by ``***``.

    Prevents credentials from leaking into exception messages or log output.

    Args:
        dsn: PostgreSQL connection URL (any scheme).

    Returns:
        DSN string with the password redacted, or the original string if no
        password component is detected.
    """
    return re.sub(r"(://[^:@/]+:)[^@]+(@)", r"\1***\2", dsn)


def create_engine(dsn: str, **kwargs: Any) -> Engine:
    """Create a SQLAlchemy engine from a PostgreSQL DSN.

    The engine is created with ``hide_parameters=True`` so that bound
    parameter values (which may include credentials) are not included in
    exception messages or tracebacks.  The DSN password component is
    additionally redacted before the URL is stored in the engine's repr.

    Args:
        dsn: PostgreSQL connection URL.
        **kwargs: Additional keyword arguments forwarded to SQLAlchemy's
            ``create_engine`` (e.g. ``pool_size``, ``echo``).

    Returns:
        Configured SQLAlchemy Engine.
    """
    kwargs.setdefault("hide_parameters", True)

    try:
        return sa_create_engine(dsn, **kwargs)
    except Exception as exc:
        # Re-raise with the password redacted so it cannot appear in tracebacks.
        raise type(exc)(str(exc).replace(dsn, _redact_dsn(dsn))) from None


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
            previous_embedding_provider=None,
            previous_embedding_model=None,
            previous_embedding_dimensions=None,
            previous_config_version=None,
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
        - Saves the current model identity into ``previous_*`` fields alongside
          ``previous_collection`` so that rollback can restore the full N-1
          model identity without any external reads (§10.3, F-01).
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

        # Snapshot current live model identity into previous_* fields before
        # overwriting.  This ensures rollback_to_previous() can restore the
        # complete N-1 model identity without any external reads (§10.3).
        record.previous_collection = record.collection_name
        record.previous_embedding_provider = record.embedding_provider
        record.previous_embedding_model = record.embedding_model
        record.previous_embedding_dimensions = record.embedding_dimensions
        record.previous_config_version = record.config_version

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

        Precondition: the alias record MUST already exist (call ``get`` and check
        before calling this method).  A missing record indicates the alias was
        never promoted, which is a programming error; this method will raise
        ``ValueError`` in that case.

        After rollback, the record is fully self-consistent — the N-1 collection
        AND its model identity (provider/model/dimensions/config_version) become
        the live values, while the current values are moved into the previous_*
        fields.  No external reads are required (§10.3, F-01).

        After rollback:
        - ``collection_name``          = former ``previous_collection`` (N-1 is now live).
        - ``embedding_provider``       = former ``previous_embedding_provider``.
        - ``embedding_model``          = former ``previous_embedding_model``.
        - ``embedding_dimensions``     = former ``previous_embedding_dimensions``.
        - ``config_version``           = former ``previous_config_version``.
        - ``previous_collection``      = former ``collection_name`` (N is now standby).
        - ``previous_embedding_*``     = former live values (N's model identity).

        This mirrors the Qdrant alias retarget that must have already succeeded.

        Args:
            alias: Alias name.

        Returns:
            Updated AliasRecord.

        Raises:
            ValueError: If the alias record does not exist.
            ValueError: If there is no previous_collection (rollback impossible).
        """
        record = self.get(alias)
        if record is None:
            raise ValueError(f"Alias record '{alias}' not found; cannot rollback")
        if not record.previous_collection:
            raise ValueError(
                f"No N-1 collection available for alias '{alias}'; rollback impossible"
            )

        # Wholesale swap of collection pointer AND model identity (pure in-record
        # swap; no external reads needed — §10.3).
        old_collection = record.collection_name
        old_provider = record.embedding_provider
        old_model = record.embedding_model
        old_dimensions = record.embedding_dimensions
        old_config_version = record.config_version

        record.collection_name = record.previous_collection
        record.embedding_provider = record.previous_embedding_provider
        record.embedding_model = record.previous_embedding_model
        record.embedding_dimensions = record.previous_embedding_dimensions
        record.config_version = record.previous_config_version

        record.previous_collection = old_collection
        record.previous_embedding_provider = old_provider
        record.previous_embedding_model = old_model
        record.previous_embedding_dimensions = old_dimensions
        record.previous_config_version = old_config_version

        record.promoted_at = datetime.now(tz=UTC)

        logger.info(
            "Rolled back alias '%s': %s -> %s",
            alias,
            old_collection,
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

        Precondition: the alias record MUST already exist in the control-plane DB.
        ``startup_reconcile`` only calls this method after confirming that
        ``db_record is not None``.  An empty ``kb_id`` or ``workspace_id`` is
        never acceptable — empty-string tenancy would corrupt the record and make
        it unqueryable.  If the record is missing, ``startup_reconcile`` logs the
        inconsistency and marks it as a repair failure rather than calling this
        method (F-06).

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

        Raises:
            ValueError: If no alias record exists (caller must not call this
                method when the record is missing — see startup_reconcile).
        """
        record = self.get(alias)
        if record is None:
            raise ValueError(
                f"Alias record '{alias}' not found; reconcile_from_qdrant requires a "
                f"pre-existing record.  Empty-string tenancy (kb_id='', workspace_id='') "
                f"is never acceptable.  Investigate why the record is absent."
            )

        record.previous_collection = record.collection_name
        record.previous_embedding_provider = record.embedding_provider
        record.previous_embedding_model = record.embedding_model
        record.previous_embedding_dimensions = record.embedding_dimensions
        record.previous_config_version = record.config_version

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
    "Base",
    "_redact_dsn",
    "create_engine",
    "create_tables",
]
