"""Postgres/pgvector implementation of IndexAdapter (§4.4, Phase 7 WU-B).

Proves the ``IndexAdapter`` abstraction is not Qdrant-locked: the entire vector
index runs inside PostgreSQL using the `pgvector <https://github.com/pgvector/pgvector>`_
extension.  It reuses the existing ``storage.postgres`` DSN (a separate,
operator-selected index database is also supported by passing a different DSN).

Data model
----------
Each *collection* is a dedicated table::

    CREATE TABLE <collection> (
        point_id  UUID PRIMARY KEY,
        embedding vector(<dim>),
        payload   JSONB NOT NULL
    );

Two shared bookkeeping tables live alongside the collection tables:

- ``rtfc_pgvector_collections(name, vector_size, metadata JSONB)`` — the
  collection registry + model-identity metadata (mirrors the Qdrant metadata
  sentinel point, but as a first-class row rather than a hidden point).
- ``rtfc_pgvector_aliases(alias_name, collection_name)`` — the alias table
  (mirrors Qdrant's native aliases; atomic retarget = single-row UPDATE).

Search = ``ORDER BY embedding <=> query`` (cosine distance operator).  Score is
returned as ``1 - distance`` so that higher = more similar (matching Qdrant's
cosine *similarity* score direction).

Security boundary (SECURITY-CRITICAL)
-------------------------------------
Tenancy/payload filtering in :meth:`search` and :meth:`scroll_all` is a security
boundary and MUST be exactly as strict as Qdrant's.  The flat dotted-key filter
dict produced by ``retrieval.service._build_tenancy_filter`` is translated to
JSONB predicates in :func:`_filter_to_sql`; every clause becomes a mandatory
``AND`` predicate applied *inside* the SQL query (server-side, never
post-filtered in Python).  A cross-tenant leak here is a BLOCKER.

Spec references: §4.4, §4.2 C-2/C-3, §11.4 M-060 (server-side tenancy),
index-lifecycle.md §2, §4.  Ledger: Phase 7 WU-B; D-41 (``scroll_all``).
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from finecorpus.index.adapter import (
    AliasNotFoundError,
    AliasRecord,
    AliasSwapError,
    BackendCapabilities,
    CollectionInfo,
    CollectionNotFoundError,
    IndexAdapter,
    IndexError,
    SearchResult,
    SnapshotError,
    SnapshotRef,
)
from finecorpus.index.adapter import (
    collection_name as _collection_name,
)

logger = logging.getLogger(__name__)

# Shared bookkeeping table names (namespaced so they never collide with a
# collection table, whose names all start with ``rtfc_<kbid>_<buildid>``).
_COLLECTIONS_TABLE = "rtfc_pgvector_collections"
_ALIASES_TABLE = "rtfc_pgvector_aliases"
_SNAPSHOTS_TABLE = "rtfc_pgvector_snapshots"

# A collection/alias name is only ever produced by ``collection_name`` /
# ``alias_name`` in adapter.py (``rtfc_...``), but because these values are
# interpolated into DDL/identifier positions (which cannot be parameterized) we
# defensively validate every identifier before use to prevent SQL injection.
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _validate_identifier(name: str) -> str:
    """Return ``name`` if it is a safe SQL identifier, else raise.

    Identifiers cannot be passed as bound parameters, so every table name that
    is interpolated into a statement passes through here.  This is defence in
    depth — the values always originate from ``collection_name``/``alias_name``.
    """
    if not _IDENT_RE.match(name) or len(name) > 63:
        raise IndexError(f"Unsafe SQL identifier: {name!r}")
    return name


class PgVectorAdapter(IndexAdapter):
    """IndexAdapter implementation backed by PostgreSQL + pgvector.

    Args:
        dsn: PostgreSQL DSN.  Accepts a plain ``postgresql://...`` URL or a
            SQLAlchemy-style ``postgresql+psycopg://...`` URL (the ``+psycopg``
            driver suffix is stripped for the raw psycopg connection).
        ensure_extension: When True (default) run ``CREATE EXTENSION IF NOT
            EXISTS vector`` at construction.  Set False when the operator has
            pre-provisioned the extension and the connecting role lacks
            ``CREATE EXTENSION`` privilege.
    """

    def __init__(self, dsn: str, *, ensure_extension: bool = True) -> None:
        import psycopg
        from pgvector.psycopg import register_vector

        # SQLAlchemy DSNs carry a ``+driver`` suffix psycopg does not understand.
        raw_dsn = dsn.replace("postgresql+psycopg://", "postgresql://")
        raw_dsn = raw_dsn.replace("postgres+psycopg://", "postgresql://")
        self._dsn = raw_dsn
        self._conn = psycopg.connect(raw_dsn, autocommit=True)

        if ensure_extension:
            try:
                self._conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
            except Exception as exc:  # pragma: no cover - env dependent
                raise IndexError(
                    "pgvector extension is not available and could not be created. "
                    "Install the pgvector extension in the target database "
                    f"(CREATE EXTENSION vector): {exc}"
                ) from exc

        register_vector(self._conn)
        self._ensure_bookkeeping_tables()

    # ------------------------------------------------------------------
    # Internal DDL / helpers
    # ------------------------------------------------------------------

    def _ensure_bookkeeping_tables(self) -> None:
        self._conn.execute(
            f"CREATE TABLE IF NOT EXISTS {_COLLECTIONS_TABLE} ("
            "  name TEXT PRIMARY KEY,"
            "  vector_size INTEGER NOT NULL,"
            "  metadata JSONB NOT NULL DEFAULT '{}'::jsonb"
            ")"
        )
        self._conn.execute(
            f"CREATE TABLE IF NOT EXISTS {_ALIASES_TABLE} ("
            "  alias_name TEXT PRIMARY KEY,"
            "  collection_name TEXT NOT NULL"
            ")"
        )
        self._conn.execute(
            f"CREATE TABLE IF NOT EXISTS {_SNAPSHOTS_TABLE} ("
            "  snapshot_id TEXT PRIMARY KEY,"
            "  collection TEXT NOT NULL,"
            "  vector_size INTEGER NOT NULL,"
            "  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,"
            "  created_at TIMESTAMPTZ NOT NULL,"
            "  data JSONB NOT NULL"
            ")"
        )

    def _vector_size(self, collection: str) -> int:
        row = self._conn.execute(
            f"SELECT vector_size FROM {_COLLECTIONS_TABLE} WHERE name = %s",
            (collection,),
        ).fetchone()
        if row is None:
            raise CollectionNotFoundError(f"Collection '{collection}' does not exist")
        return int(row[0])

    # ------------------------------------------------------------------
    # Capability surface
    # ------------------------------------------------------------------

    def capabilities(self) -> BackendCapabilities:
        """pgvector Phase-7 capabilities: dense + payload filtering + atomic alias.

        Hybrid search stays unsupported (parity with Qdrant Phase 1).  Snapshots
        are supported via an in-database JSONB copy (see ``snapshot_collection``).
        """
        return BackendCapabilities(
            dense_search=True,
            hybrid_search=False,
            payload_filtering=True,
            atomic_alias_swap=True,
            snapshots=True,
        )

    # ------------------------------------------------------------------
    # Collection lifecycle
    # ------------------------------------------------------------------

    def create_collection(
        self,
        kb_id: str,
        build_id: int,
        dimensions: int,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        name = _validate_identifier(_collection_name(kb_id, build_id))
        try:
            self._conn.execute(
                f"CREATE TABLE {name} ("
                "  point_id UUID PRIMARY KEY,"
                f"  embedding vector({int(dimensions)}),"
                "  payload JSONB NOT NULL"
                ")"
            )
            # HNSW index for cosine distance (vector_cosine_ops).
            self._conn.execute(
                f"CREATE INDEX {name}_embedding_idx ON {name} "
                "USING hnsw (embedding vector_cosine_ops)"
            )
            # Expression indexes on the tenancy filter fields (parity with the
            # Qdrant payload indexes on tenancy.kb_id / provenance.source_document_id).
            self._conn.execute(
                f"CREATE INDEX {name}_kb_id_idx ON {name} ((payload #>> '{{tenancy,kb_id}}'))"
            )
            self._conn.execute(
                f"CREATE INDEX {name}_srcdoc_idx ON {name} "
                "((payload #>> '{provenance,source_document_id}'))"
            )
            self._conn.execute(
                f"INSERT INTO {_COLLECTIONS_TABLE} (name, vector_size, metadata) "
                "VALUES (%s, %s, %s::jsonb)",
                (name, int(dimensions), json.dumps(metadata or {})),
            )
        except Exception as exc:
            raise IndexError(f"Failed to create collection '{name}': {exc}") from exc

        logger.info("Created pgvector collection '%s' (dimensions=%d)", name, dimensions)
        return name

    def drop_collection(self, collection: str) -> None:
        if not self.collection_exists(collection):
            raise CollectionNotFoundError(f"Collection '{collection}' does not exist")
        name = _validate_identifier(collection)
        try:
            self._conn.execute(f"DROP TABLE IF EXISTS {name}")
            self._conn.execute(f"DELETE FROM {_COLLECTIONS_TABLE} WHERE name = %s", (collection,))
        except Exception as exc:
            raise IndexError(f"Failed to drop collection '{collection}': {exc}") from exc
        logger.info("Dropped collection '%s'", collection)

    def get_collection_info(self, collection: str) -> CollectionInfo:
        vector_size = self._vector_size(collection)
        return CollectionInfo(
            name=collection,
            vector_size=vector_size,
            point_count=self.count_points(collection),
            metadata=self.get_collection_metadata(collection),
        )

    def collection_exists(self, collection: str) -> bool:
        row = self._conn.execute(
            f"SELECT 1 FROM {_COLLECTIONS_TABLE} WHERE name = %s", (collection,)
        ).fetchone()
        return row is not None

    # ------------------------------------------------------------------
    # Write path
    # ------------------------------------------------------------------

    def upsert_points(self, collection: str, points: list[dict[str, Any]]) -> None:
        if not points:
            return
        if not self.collection_exists(collection):
            raise CollectionNotFoundError(f"Collection '{collection}' does not exist")
        name = _validate_identifier(collection)

        rows = []
        for p in points:
            point_id = p["id"]
            pid = str(point_id) if isinstance(point_id, UUID) else str(UUID(str(point_id)))
            rows.append((pid, p["vector"], json.dumps(p["payload"])))

        try:
            with self._conn.cursor() as cur:
                cur.executemany(
                    f"INSERT INTO {name} (point_id, embedding, payload) "
                    "VALUES (%s, %s, %s::jsonb) "
                    "ON CONFLICT (point_id) DO UPDATE SET "
                    "embedding = EXCLUDED.embedding, payload = EXCLUDED.payload",
                    rows,
                )
        except Exception as exc:
            raise IndexError(
                f"Failed to upsert {len(points)} points into '{collection}': {exc}"
            ) from exc

    def delete_by_document(self, collection: str, source_document_id: str) -> int:
        if not self.collection_exists(collection):
            raise CollectionNotFoundError(f"Collection '{collection}' does not exist")
        name = _validate_identifier(collection)
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    f"DELETE FROM {name} "
                    "WHERE payload #>> '{provenance,source_document_id}' = %s",
                    (source_document_id,),
                )
                deleted = cur.rowcount
        except Exception as exc:
            raise IndexError(
                f"Failed to delete points for document '{source_document_id}' "
                f"in '{collection}': {exc}"
            ) from exc
        logger.info(
            "Deleted %d points for document '%s' from '%s'",
            deleted,
            source_document_id,
            collection,
        )
        return deleted

    # ------------------------------------------------------------------
    # Collection metadata (model identity pinning)
    # ------------------------------------------------------------------

    def set_collection_metadata(self, collection: str, metadata: dict[str, Any]) -> None:
        if not self.collection_exists(collection):
            raise CollectionNotFoundError(f"Collection '{collection}' does not exist")
        # MERGE (matches QdrantAdapter behaviour): existing || new.
        try:
            self._conn.execute(
                f"UPDATE {_COLLECTIONS_TABLE} SET metadata = metadata || %s::jsonb WHERE name = %s",
                (json.dumps(metadata), collection),
            )
        except Exception as exc:
            raise IndexError(f"Failed to set metadata on collection '{collection}': {exc}") from exc

    def get_collection_metadata(self, collection: str) -> dict[str, Any]:
        row = self._conn.execute(
            f"SELECT metadata FROM {_COLLECTIONS_TABLE} WHERE name = %s", (collection,)
        ).fetchone()
        if row is None:
            raise CollectionNotFoundError(f"Collection '{collection}' does not exist")
        md = row[0]
        return dict(md) if md else {}

    # ------------------------------------------------------------------
    # Alias operations
    # ------------------------------------------------------------------

    def create_alias(self, alias: str, collection: str) -> None:
        if self.alias_exists(alias):
            raise IndexError(f"Alias '{alias}' already exists")
        try:
            self._conn.execute(
                f"INSERT INTO {_ALIASES_TABLE} (alias_name, collection_name) VALUES (%s, %s)",
                (alias, collection),
            )
        except Exception as exc:
            raise AliasSwapError(f"Failed to create alias '{alias}': {exc}") from exc
        logger.info("Created alias '%s' -> '%s'", alias, collection)

    def retarget_alias(self, alias: str, new_collection: str) -> None:
        if not self.collection_exists(new_collection):
            raise CollectionNotFoundError(f"Target collection '{new_collection}' does not exist")
        # Atomic single-row upsert: from the moment this returns, all resolutions
        # point at new_collection (parity with Qdrant's atomic alias swap).
        try:
            self._conn.execute(
                f"INSERT INTO {_ALIASES_TABLE} (alias_name, collection_name) VALUES (%s, %s) "
                "ON CONFLICT (alias_name) DO UPDATE SET collection_name = EXCLUDED.collection_name",
                (alias, new_collection),
            )
        except Exception as exc:
            raise AliasSwapError(
                f"Failed to retarget alias '{alias}' -> '{new_collection}': {exc}"
            ) from exc
        logger.info("Retargeted alias '%s' -> '%s'", alias, new_collection)

    def resolve_alias(self, alias: str) -> str | None:
        row = self._conn.execute(
            f"SELECT collection_name FROM {_ALIASES_TABLE} WHERE alias_name = %s", (alias,)
        ).fetchone()
        return str(row[0]) if row is not None else None

    def alias_exists(self, alias: str) -> bool:
        return self.resolve_alias(alias) is not None

    def delete_alias(self, alias: str) -> None:
        if not self.alias_exists(alias):
            raise AliasNotFoundError(f"Alias '{alias}' does not exist")
        try:
            self._conn.execute(f"DELETE FROM {_ALIASES_TABLE} WHERE alias_name = %s", (alias,))
        except Exception as exc:
            raise AliasSwapError(f"Failed to delete alias '{alias}': {exc}") from exc
        logger.info("Deleted alias '%s'", alias)

    def _resolve_for_query(self, alias: str) -> str:
        """Resolve an alias (or fall back to a raw collection name) for querying.

        The retrieval service always passes an alias; the ``collection_override``
        path may pass a raw collection name.  Mirror the Qdrant behaviour where
        the backend accepts either.
        """
        resolved = self.resolve_alias(alias)
        if resolved is not None:
            return resolved
        if self.collection_exists(alias):
            return alias
        raise AliasNotFoundError(f"Alias '{alias}' does not resolve to a collection")

    # ------------------------------------------------------------------
    # Search (dense; hybrid declared unsupported — parity with Qdrant)
    # ------------------------------------------------------------------

    def search(
        self,
        alias: str,
        query_vector: list[float],
        top_k: int = 10,
        payload_filter: dict[str, Any] | None = None,
    ) -> list[SearchResult]:
        """Dense cosine search via alias (C-3).

        SECURITY-CRITICAL: ``payload_filter`` is translated to mandatory JSONB
        ``AND`` predicates by :func:`_filter_to_sql` and applied server-side in
        the SQL ``WHERE`` clause — never post-filtered in Python.  This enforces
        the same tenancy isolation as Qdrant (§11.4 M-060).

        Filtered-recall note (best-effort mitigation)
        ---------------------------------------------
        The tenancy/permission filter is a SQL ``WHERE`` post-filter over the
        HNSW ANN candidate set.  With the pgvector default ``hnsw.ef_search=40``
        a filter that matches only sparse rows (e.g. a
        ``tenancy.permission_principals`` read inside a large KB) can return
        FEWER than ``top_k`` valid rows even when more exist — silently missing
        results the caller is entitled to.  To mitigate we raise the HNSW
        candidate pool for the duration of this query, scoped to a transaction
        via ``SET LOCAL`` so it never leaks to other statements:

        - ``SET LOCAL hnsw.ef_search`` is scaled to the request
          (``GREATEST(top_k * 4, 100)``, capped at 1000).
        - ``SET LOCAL hnsw.iterative_scan = 'relaxed_order'`` is set
          defensively (pgvector >= 0.8); it is issued inside a nested
          ``try/except`` so an older pgvector that does not know the GUC does
          not fail the search.

        Filtered ANN recall therefore remains best-effort; this raising of the
        candidate pool is the mitigation.  The tenancy filter itself is never
        relaxed.
        """
        collection = self._resolve_for_query(alias)
        name = _validate_identifier(collection)

        where_sql, params = _filter_to_sql(payload_filter)
        where_clause = f"WHERE {where_sql}" if where_sql else ""

        # Raise the HNSW candidate pool proportional to the request so a sparse
        # filter still surfaces up to ``top_k`` entitled rows.  Bound to a sane
        # ceiling (1000) and a floor (100) — see the method docstring.
        ef_search = min(max(int(top_k) * 4, 100), 1000)

        # ``<=>`` is pgvector's cosine-distance operator (0 = identical, 2 =
        # opposite).  Score = 1 - distance so higher = more similar (matching the
        # direction of Qdrant's cosine similarity score).
        sql = (
            f"SELECT point_id, payload, 1 - (embedding <=> %s) AS score "
            f"FROM {name} {where_clause} "
            "ORDER BY embedding <=> %s LIMIT %s"
        )
        query_params = [query_vector, *params, query_vector, int(top_k)]
        try:
            # ``SET LOCAL`` only takes effect inside an explicit transaction, so
            # run the GUC tuning and the query in one transaction block.  The
            # connection is autocommit=True, so ``transaction()`` opens/commits
            # a scope just for this search.
            with self._conn.transaction():
                with self._conn.cursor() as cur:
                    # Scaled candidate pool; scoped to this transaction only.
                    cur.execute("SET LOCAL hnsw.ef_search = %s", (ef_search,))
                    # Iterative scan materially improves filtered recall on
                    # pgvector >= 0.8.  Guard so an older pgvector that does not
                    # recognise the GUC does not abort the search.
                    try:
                        with self._conn.transaction():
                            cur.execute("SET LOCAL hnsw.iterative_scan = 'relaxed_order'")
                    except Exception:
                        # GUC unknown on this pgvector version — proceed with
                        # just the raised ef_search.
                        logger.debug(
                            "hnsw.iterative_scan unsupported by installed pgvector; "
                            "proceeding with raised ef_search only"
                        )
                    cur.execute(sql, query_params)
                    rows = cur.fetchall()
        except IndexError:
            raise
        except Exception as exc:
            raise IndexError(f"Search via alias '{alias}' failed: {exc}") from exc

        results: list[SearchResult] = []
        for point_id, payload, score in rows:
            pl = dict(payload) if payload else {}
            results.append(
                SearchResult(
                    point_id=str(point_id),
                    chunk_id=pl.get("chunk_id", str(point_id)),
                    score=float(score),
                    payload=pl,
                )
            )
        return results

    # search_hybrid inherited from IndexAdapter → UnsupportedCapabilityError.

    # ------------------------------------------------------------------
    # Scroll (D-41)
    # ------------------------------------------------------------------

    def scroll_all(
        self,
        collection: str,
        payload_filter: dict[str, Any] | None = None,
        *,
        batch_size: int = 500,
    ) -> Iterator[SearchResult]:
        """Iterate every point in a collection via keyset pagination (D-41).

        SECURITY-CRITICAL: applies the same server-side ``_filter_to_sql``
        predicate as :meth:`search`, so a filtered scroll never yields
        off-filter / cross-tenant points.
        """
        if not self.collection_exists(collection):
            raise CollectionNotFoundError(f"Collection '{collection}' does not exist")
        name = _validate_identifier(collection)

        where_sql, params = _filter_to_sql(payload_filter)

        last_id: str | None = None
        while True:
            clauses = []
            page_params: list[Any] = []
            if where_sql:
                clauses.append(where_sql)
                page_params.extend(params)
            if last_id is not None:
                # Cast the keyset cursor explicitly to ``uuid`` — ``point_id`` is
                # a ``uuid`` PK and ``last_id`` is a Python ``str``, so we must not
                # rely on implicit text→uuid coercion for the ``>`` comparison.
                clauses.append("point_id > %s::uuid")
                page_params.append(last_id)
            where_clause = f"WHERE {' AND '.join(clauses)}" if clauses else ""

            sql = f"SELECT point_id, payload FROM {name} {where_clause} ORDER BY point_id LIMIT %s"
            page_params.append(int(batch_size))
            try:
                rows = self._conn.execute(sql, page_params).fetchall()
            except Exception as exc:
                raise IndexError(f"Scroll of collection '{collection}' failed: {exc}") from exc

            if not rows:
                break

            for point_id, payload in rows:
                pl = dict(payload) if payload else {}
                yield SearchResult(
                    point_id=str(point_id),
                    chunk_id=pl.get("chunk_id", str(point_id)),
                    score=0.0,
                    payload=pl,
                )
                last_id = str(point_id)

            if len(rows) < batch_size:
                break

    # ------------------------------------------------------------------
    # Point count
    # ------------------------------------------------------------------

    def count_points(self, collection: str) -> int:
        if not self.collection_exists(collection):
            raise CollectionNotFoundError(f"Collection '{collection}' does not exist")
        name = _validate_identifier(collection)
        try:
            row = self._conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()
        except Exception as exc:
            raise IndexError(f"Failed to count points in '{collection}': {exc}") from exc
        return int(row[0]) if row else 0

    def list_collections(self) -> list[str]:
        rows = self._conn.execute(f"SELECT name FROM {_COLLECTIONS_TABLE} ORDER BY name").fetchall()
        return [str(r[0]) for r in rows]

    def list_aliases(self) -> list[AliasRecord]:
        rows = self._conn.execute(
            f"SELECT alias_name, collection_name FROM {_ALIASES_TABLE} ORDER BY alias_name"
        ).fetchall()
        return [AliasRecord(alias_name=str(r[0]), collection_name=str(r[1])) for r in rows]

    # ------------------------------------------------------------------
    # Snapshot operations (§10.2, §17.1) — in-database JSONB copy
    # ------------------------------------------------------------------

    def snapshot_collection(self, collection_name: str) -> SnapshotRef:
        if not self.collection_exists(collection_name):
            raise CollectionNotFoundError(f"Collection '{collection_name}' does not exist")
        name = _validate_identifier(collection_name)
        import uuid as _uuid

        snapshot_id = f"snap_{collection_name}_{_uuid.uuid4().hex[:8]}"
        created_at = datetime.now(tz=UTC)
        vector_size = self._vector_size(collection_name)
        metadata = self.get_collection_metadata(collection_name)
        try:
            rows = self._conn.execute(
                f"SELECT point_id, embedding::text, payload FROM {name}"
            ).fetchall()
            data = [
                {"point_id": str(pid), "embedding": emb, "payload": pl} for pid, emb, pl in rows
            ]
            self._conn.execute(
                f"INSERT INTO {_SNAPSHOTS_TABLE} "
                "(snapshot_id, collection, vector_size, metadata, created_at, data) "
                "VALUES (%s, %s, %s, %s::jsonb, %s, %s::jsonb)",
                (
                    snapshot_id,
                    collection_name,
                    vector_size,
                    json.dumps(metadata),
                    created_at,
                    json.dumps(data),
                ),
            )
        except Exception as exc:
            raise SnapshotError(
                f"Failed to create snapshot for collection '{collection_name}': {exc}"
            ) from exc

        logger.info("Created snapshot '%s' for collection '%s'", snapshot_id, collection_name)
        return SnapshotRef(
            collection=collection_name,
            snapshot_id=snapshot_id,
            created_at=created_at,
            location=f"pg://{_SNAPSHOTS_TABLE}/{snapshot_id}",
        )

    def restore_snapshot(self, ref: SnapshotRef, new_collection_name: str) -> None:
        if self.collection_exists(new_collection_name):
            raise IndexError(
                f"Cannot restore snapshot: target collection '{new_collection_name}' already "
                "exists.  Restore always goes INTO a new collection — never in place."
            )
        row = self._conn.execute(
            f"SELECT vector_size, metadata, data FROM {_SNAPSHOTS_TABLE} WHERE snapshot_id = %s",
            (ref.snapshot_id,),
        ).fetchone()
        if row is None:
            raise SnapshotError(f"Snapshot '{ref.snapshot_id}' not found")

        vector_size, metadata, data = int(row[0]), dict(row[1] or {}), (row[2] or [])
        # Create the target collection, then bulk-insert the snapshot rows.
        # kb_id/build_id are irrelevant here (the caller supplies the exact name);
        # so we build the table directly rather than via create_collection.
        name = _validate_identifier(new_collection_name)
        try:
            self._conn.execute(
                f"CREATE TABLE {name} ("
                "  point_id UUID PRIMARY KEY,"
                f"  embedding vector({vector_size}),"
                "  payload JSONB NOT NULL"
                ")"
            )
            self._conn.execute(
                f"CREATE INDEX {name}_embedding_idx ON {name} "
                "USING hnsw (embedding vector_cosine_ops)"
            )
            self._conn.execute(
                f"CREATE INDEX {name}_kb_id_idx ON {name} ((payload #>> '{{tenancy,kb_id}}'))"
            )
            self._conn.execute(
                f"INSERT INTO {_COLLECTIONS_TABLE} (name, vector_size, metadata) "
                "VALUES (%s, %s, %s::jsonb)",
                (new_collection_name, vector_size, json.dumps(metadata)),
            )
            if data:
                with self._conn.cursor() as cur:
                    cur.executemany(
                        f"INSERT INTO {name} (point_id, embedding, payload) "
                        "VALUES (%s, %s::vector, %s::jsonb)",
                        [
                            (rec["point_id"], rec["embedding"], json.dumps(rec["payload"]))
                            for rec in data
                        ],
                    )
        except Exception as exc:
            try:
                self._conn.execute(f"DROP TABLE IF EXISTS {name}")
                self._conn.execute(
                    f"DELETE FROM {_COLLECTIONS_TABLE} WHERE name = %s", (new_collection_name,)
                )
            except Exception:
                pass
            raise SnapshotError(
                f"Failed to restore snapshot '{ref.snapshot_id}' into "
                f"'{new_collection_name}': {exc}"
            ) from exc

        logger.info(
            "Restored snapshot '%s' into new collection '%s'",
            ref.snapshot_id,
            new_collection_name,
        )

    def list_snapshots(self, collection_name: str) -> list[SnapshotRef]:
        if not self.collection_exists(collection_name):
            raise CollectionNotFoundError(f"Collection '{collection_name}' does not exist")
        rows = self._conn.execute(
            f"SELECT snapshot_id, created_at FROM {_SNAPSHOTS_TABLE} "
            "WHERE collection = %s ORDER BY created_at",
            (collection_name,),
        ).fetchall()
        refs: list[SnapshotRef] = []
        for snap_id, created_at in rows:
            refs.append(
                SnapshotRef(
                    collection=collection_name,
                    snapshot_id=str(snap_id),
                    created_at=created_at,
                    location=f"pg://{_SNAPSHOTS_TABLE}/{snap_id}",
                )
            )
        return refs

    def delete_snapshot(self, ref: SnapshotRef) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                f"DELETE FROM {_SNAPSHOTS_TABLE} WHERE snapshot_id = %s", (ref.snapshot_id,)
            )
            if cur.rowcount == 0:
                raise SnapshotError(f"Snapshot '{ref.snapshot_id}' not found")
        logger.info("Deleted snapshot '%s'", ref.snapshot_id)


# ---------------------------------------------------------------------------
# Filter translation (SECURITY-CRITICAL — mirrors _dict_to_qdrant_filter)
# ---------------------------------------------------------------------------


def _filter_to_sql(payload_filter: dict[str, Any] | None) -> tuple[str, list[Any]]:
    """Translate a flat dotted-key filter dict to a JSONB ``WHERE`` predicate.

    This is the pgvector counterpart of ``qdrant.backend._dict_to_qdrant_filter``
    and MUST enforce identical semantics — it is the tenancy security boundary
    (§11.4 M-060).  The filter dict is the exact structure produced by
    ``retrieval.service._build_tenancy_filter``:

    - Plain scalar value ``{field: v}``  →  ``payload #>> '{a,b}' = %s``
      (JSONB text-extraction equality — matches Qdrant ``MatchValue``).
    - ``{field: {"__contains__": v}}``   →  ``payload #> '{a,b}' @> to_jsonb(%s)``
      (the JSONB array at ``field`` must contain scalar ``v`` — matches Qdrant's
      ``MatchValue`` on an array field, used for ``tenancy.permission_principals``).

    All clauses are AND-joined (mandatory ``must`` semantics): the caller can
    only narrow, never widen, the result set.  Values are always passed as bound
    parameters; only the JSONB *path* is interpolated, and it is built from a
    validated dotted key (letters/digits/underscore/dot only) so no untrusted
    string reaches the SQL text.

    Returns:
        ``(where_sql, params)`` where ``where_sql`` is empty when no filter is
        supplied.  ``params`` are positional bind parameters in clause order.
    """
    if not payload_filter:
        return "", []

    clauses: list[str] = []
    params: list[Any] = []
    for dotted_key, expected in payload_filter.items():
        path = _jsonb_path(dotted_key)
        if isinstance(expected, dict) and "__contains__" in expected:
            # List-contains: the JSONB array at ``path`` must contain the scalar.
            clauses.append(f"payload #> '{path}' @> to_jsonb(%s::text)")
            params.append(str(expected["__contains__"]))
        else:
            # Scalar equality via JSONB text extraction (#>>).
            clauses.append(f"payload #>> '{path}' = %s")
            params.append(str(expected))
    return " AND ".join(clauses), params


def _jsonb_path(dotted_key: str) -> str:
    """Convert ``a.b.c`` to a validated Postgres JSONB path literal ``{a,b,c}``.

    Each segment is validated to contain only identifier-safe characters so the
    interpolated path literal cannot carry an injection payload (values still go
    through bound parameters; this guards the one interpolated position).
    """
    parts = dotted_key.split(".")
    for part in parts:
        if not re.match(r"^[A-Za-z0-9_]+$", part):
            raise IndexError(f"Unsafe JSONB filter key segment: {part!r}")
    return "{" + ",".join(parts) + "}"


__all__ = ["PgVectorAdapter"]
