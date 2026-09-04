"""Qdrant implementation of IndexAdapter (§4.4, reference backend).

Wraps ``qdrant-client`` to satisfy the adapter interface defined in
``finecorpus.index.adapter``. All Qdrant-specific types stay within this
module; the adapter boundary returns only the value objects from
``adapter.py``.

Spec references: §4.4, §4.2 C-2/C-3, index-lifecycle.md §2, §4.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from qdrant_client import QdrantClient
from qdrant_client.http import models as qm

from finecorpus.index.adapter import (
    AliasNotFoundError,
    AliasSwapError,
    BackendCapabilities,
    CollectionInfo,
    CollectionNotFoundError,
    IndexAdapter,
    IndexError,
    SearchResult,
)
from finecorpus.index.adapter import (
    collection_name as _collection_name,
)

logger = logging.getLogger(__name__)

# Qdrant payload index field for provenance.source_document_id filtering
_SOURCE_DOC_FIELD = "provenance.source_document_id"
# Qdrant payload index field for tenancy.kb_id filtering
_KB_ID_FIELD = "tenancy.kb_id"
# Metadata key used to store model identity on collections
_METADATA_KEY = "rtfc_metadata"


class QdrantAdapter(IndexAdapter):
    """IndexAdapter implementation backed by Qdrant (v1.15+).

    Phase 1: dense search only. Hybrid search declared unsupported (M-009/M-010).
    Atomic alias swap via Qdrant's native UpdateAliases API (guarantees zero-error
    window — §4.1, §18.3 test 1).

    Args:
        url: Qdrant HTTP/gRPC URL (e.g. ``http://localhost:6333``).
        api_key: Optional Qdrant API key.
        timeout: Request timeout in seconds.
    """

    def __init__(
        self,
        url: str = "http://localhost:6333",
        api_key: str | None = None,
        timeout: int = 30,
    ) -> None:
        self._client = QdrantClient(url=url, api_key=api_key, timeout=timeout)
        self._url = url

    # ------------------------------------------------------------------
    # Capability surface
    # ------------------------------------------------------------------

    def capabilities(self) -> BackendCapabilities:
        """Qdrant Phase 1 capabilities: dense + payload filtering + atomic alias."""
        return BackendCapabilities(
            dense_search=True,
            hybrid_search=False,  # Phase 1: declared unsupported (M-009/M-010)
            payload_filtering=True,
            atomic_alias_swap=True,
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
        """Create a shadow Qdrant collection for a build.

        Creates payload indexes on ``provenance.source_document_id`` and
        ``tenancy.kb_id`` for efficient filtering at retrieval time.

        Returns:
            The canonical collection name (``rtfc_{kb_id}_{build_id:08d}``).
        """
        name = _collection_name(kb_id, build_id)
        try:
            self._client.create_collection(
                collection_name=name,
                vectors_config=qm.VectorParams(
                    size=dimensions,
                    distance=qm.Distance.COSINE,
                ),
            )
        except Exception as exc:
            raise IndexError(f"Failed to create collection '{name}': {exc}") from exc

        # Create payload indexes for efficient filtering (provenance + tenancy)
        try:
            self._client.create_payload_index(
                collection_name=name,
                field_name=_SOURCE_DOC_FIELD,
                field_schema=qm.PayloadSchemaType.KEYWORD,
            )
            self._client.create_payload_index(
                collection_name=name,
                field_name=_KB_ID_FIELD,
                field_schema=qm.PayloadSchemaType.KEYWORD,
            )
        except Exception as exc:
            logger.warning(
                "Failed to create payload indexes on '%s': %s — "
                "filtering will still work but may be slower",
                name,
                exc,
            )

        if metadata:
            self.set_collection_metadata(name, metadata)

        logger.info("Created shadow collection '%s' (dimensions=%d)", name, dimensions)
        return name

    def drop_collection(self, collection: str) -> None:
        """Drop a collection from Qdrant."""
        if not self.collection_exists(collection):
            raise CollectionNotFoundError(f"Collection '{collection}' does not exist")
        try:
            self._client.delete_collection(collection_name=collection)
        except Exception as exc:
            raise IndexError(f"Failed to drop collection '{collection}': {exc}") from exc
        logger.info("Dropped collection '%s'", collection)

    def get_collection_info(self, collection: str) -> CollectionInfo:
        """Return summary info for a collection."""
        if not self.collection_exists(collection):
            raise CollectionNotFoundError(f"Collection '{collection}' does not exist")
        try:
            info = self._client.get_collection(collection_name=collection)
        except Exception as exc:
            raise IndexError(f"Failed to get collection info for '{collection}': {exc}") from exc

        vectors_config = info.config.params.vectors
        if isinstance(vectors_config, qm.VectorParams):
            vector_size = vectors_config.size
        else:
            # Named vectors config; take the first
            vector_size = next(iter(vectors_config.values())).size if vectors_config else 0

        point_count = info.points_count or 0
        metadata = self.get_collection_metadata(collection)

        return CollectionInfo(
            name=collection,
            vector_size=vector_size,
            point_count=point_count,
            metadata=metadata,
        )

    def collection_exists(self, collection: str) -> bool:
        """Return True if the collection exists."""
        try:
            return self._client.collection_exists(collection_name=collection)
        except Exception as exc:
            raise IndexError(f"Failed to check collection existence '{collection}': {exc}") from exc

    # ------------------------------------------------------------------
    # Write path
    # ------------------------------------------------------------------

    def upsert_points(
        self,
        collection: str,
        points: list[dict[str, Any]],
    ) -> None:
        """Upsert a batch of points into a shadow collection.

        Each point dict must have:
        - ``id``: UUID (from ``contracts.derive_point_id``).
        - ``vector``: list[float].
        - ``payload``: dict from ``build_point_payload()``.
        """
        if not points:
            return

        qdrant_points = []
        for p in points:
            point_id = p["id"]
            # Accept UUID objects or UUID strings
            if isinstance(point_id, UUID):
                pid: str | UUID = point_id
            else:
                pid = UUID(str(point_id))

            qdrant_points.append(
                qm.PointStruct(
                    id=pid,
                    vector=p["vector"],
                    payload=p["payload"],
                )
            )

        try:
            self._client.upsert(
                collection_name=collection,
                points=qdrant_points,
                wait=True,
            )
        except Exception as exc:
            raise IndexError(
                f"Failed to upsert {len(points)} points into '{collection}': {exc}"
            ) from exc

    def delete_by_document(
        self,
        collection: str,
        source_document_id: str,
    ) -> int:
        """Delete all points for a document by payload filter (config-invariant).

        Keyed on ``provenance.source_document_id`` — never on chunk IDs — so
        deletions survive config_version changes (§10.5, §12.1, OQ-L determinism).

        TOCTOU note: the returned deleted-point count is computed as
        ``count_before - count_after`` using two separate Qdrant ``count``
        calls that bracket the ``delete`` call.  A concurrent upsert or delete
        for the same collection between these calls would cause the returned
        count to be inaccurate.  Callers MUST treat this value as informational
        (for logging / metrics) rather than as a precise audit count.  The
        actual deletion is still correct — only the reported count may be off
        under high concurrency.
        """
        if not self.collection_exists(collection):
            raise CollectionNotFoundError(f"Collection '{collection}' does not exist")

        # Count before for return value (see TOCTOU note above)
        count_before = self.count_points(collection)

        filt = qm.Filter(
            must=[
                qm.FieldCondition(
                    key=_SOURCE_DOC_FIELD,
                    match=qm.MatchValue(value=source_document_id),
                )
            ]
        )
        try:
            self._client.delete(
                collection_name=collection,
                points_selector=qm.FilterSelector(filter=filt),
                wait=True,
            )
        except Exception as exc:
            raise IndexError(
                f"Failed to delete points for document '{source_document_id}' "
                f"in '{collection}': {exc}"
            ) from exc

        count_after = self.count_points(collection)
        deleted = count_before - count_after
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

    def set_collection_metadata(
        self,
        collection: str,
        metadata: dict[str, Any],
    ) -> None:
        """Merge key-value metadata into the Qdrant collection payload index.

        Qdrant does not have native key-value metadata on collections. We store
        a metadata point (UUID zero = reserved sentinel) that holds the metadata
        dict in its payload. This is the standard pattern for collection-level
        metadata in Qdrant.
        """
        # Store as a sentinel point with a reserved ID (all-zeros UUID)
        sentinel_id = UUID("00000000-0000-0000-0000-000000000001")
        try:
            # Use a zero vector (the sentinel is never searched against)
            info = self._client.get_collection(collection_name=collection)
            vectors_config = info.config.params.vectors
            if isinstance(vectors_config, qm.VectorParams):
                dims = vectors_config.size
            else:
                dims = next(iter(vectors_config.values())).size if vectors_config else 1

            # Fetch existing sentinel if any, then merge
            existing: dict[str, Any] = {}
            try:
                results = self._client.retrieve(
                    collection_name=collection,
                    ids=[sentinel_id],
                    with_payload=True,
                )
                if results and results[0].payload:
                    existing = dict(results[0].payload.get(_METADATA_KEY, {}))
            except Exception:
                pass

            existing.update(metadata)

            self._client.upsert(
                collection_name=collection,
                points=[
                    qm.PointStruct(
                        id=sentinel_id,
                        vector=[0.0] * dims,
                        payload={_METADATA_KEY: existing, "_is_metadata_sentinel": True},
                    )
                ],
                wait=True,
            )
        except Exception as exc:
            raise IndexError(f"Failed to set metadata on collection '{collection}': {exc}") from exc

    def get_collection_metadata(self, collection: str) -> dict[str, Any]:
        """Return the key-value metadata stored on a collection."""
        sentinel_id = UUID("00000000-0000-0000-0000-000000000001")
        try:
            results = self._client.retrieve(
                collection_name=collection,
                ids=[sentinel_id],
                with_payload=True,
            )
            if results and results[0].payload:
                return dict(results[0].payload.get(_METADATA_KEY, {}))
            return {}
        except Exception:
            return {}

    # ------------------------------------------------------------------
    # Alias operations
    # ------------------------------------------------------------------

    def create_alias(self, alias: str, collection: str) -> None:
        """Create a new alias pointing to collection."""
        if self.alias_exists(alias):
            raise IndexError(f"Alias '{alias}' already exists")
        try:
            self._client.update_collection_aliases(
                change_aliases_operations=[
                    qm.CreateAliasOperation(
                        create_alias=qm.CreateAlias(
                            collection_name=collection,
                            alias_name=alias,
                        )
                    )
                ]
            )
        except Exception as exc:
            raise AliasSwapError(f"Failed to create alias '{alias}': {exc}") from exc
        logger.info("Created alias '%s' -> '%s'", alias, collection)

    def retarget_alias(self, alias: str, new_collection: str) -> None:
        """Atomically retarget an alias to a new collection (§4.1).

        Uses Qdrant's UpdateAliases API which is documented as atomic:
        from the moment the call returns, all new resolutions point at
        new_collection. This is the Phase 1 implementation of the two-phase
        alias swap (§4.3).
        """
        if not self.collection_exists(new_collection):
            raise CollectionNotFoundError(f"Target collection '{new_collection}' does not exist")
        try:
            self._client.update_collection_aliases(
                change_aliases_operations=[
                    qm.CreateAliasOperation(
                        create_alias=qm.CreateAlias(
                            collection_name=new_collection,
                            alias_name=alias,
                        )
                    )
                ]
            )
        except Exception as exc:
            raise AliasSwapError(
                f"Failed to retarget alias '{alias}' -> '{new_collection}': {exc}"
            ) from exc
        logger.info("Retargeted alias '%s' -> '%s'", alias, new_collection)

    def resolve_alias(self, alias: str) -> str | None:
        """Return the collection name the alias points to, or None."""
        try:
            aliases = self._client.get_aliases()
            for a in aliases.aliases:
                if a.alias_name == alias:
                    return a.collection_name
            return None
        except Exception as exc:
            raise IndexError(f"Failed to resolve alias '{alias}': {exc}") from exc

    def alias_exists(self, alias: str) -> bool:
        """Return True if the alias exists."""
        return self.resolve_alias(alias) is not None

    def delete_alias(self, alias: str) -> None:
        """Delete an alias from Qdrant."""
        if not self.alias_exists(alias):
            raise AliasNotFoundError(f"Alias '{alias}' does not exist")
        try:
            self._client.update_collection_aliases(
                change_aliases_operations=[
                    qm.DeleteAliasOperation(delete_alias=qm.DeleteAlias(alias_name=alias))
                ]
            )
        except Exception as exc:
            raise AliasSwapError(f"Failed to delete alias '{alias}': {exc}") from exc
        logger.info("Deleted alias '%s'", alias)

    # ------------------------------------------------------------------
    # Search (dense; hybrid declared unsupported — M-009/M-010)
    # ------------------------------------------------------------------

    def search(
        self,
        alias: str,
        query_vector: list[float],
        top_k: int = 10,
        payload_filter: dict[str, Any] | None = None,
    ) -> list[SearchResult]:
        """Dense vector search via alias (C-3).

        Payload filters are applied server-side before scoring — they are
        first-class operations, not post-filtering hacks.

        The alias resolves internally; the collection name never leaves
        this adapter (C-2, C-3).
        """
        # The search API accepts alias names directly in Qdrant
        qdrant_filter: qm.Filter | None = None
        if payload_filter:
            qdrant_filter = _dict_to_qdrant_filter(payload_filter)

        # Exclude the metadata sentinel from search results
        sentinel_exclusion = qm.Filter(
            must_not=[
                qm.FieldCondition(
                    key="_is_metadata_sentinel",
                    match=qm.MatchValue(value=True),
                )
            ]
        )
        if qdrant_filter is not None:
            combined = qm.Filter(
                must=[
                    qdrant_filter,
                    sentinel_exclusion,
                ]
            )
        else:
            combined = sentinel_exclusion

        try:
            response = self._client.query_points(
                collection_name=alias,  # Qdrant resolves alias → collection
                query=query_vector,
                limit=top_k,
                query_filter=combined,
                with_payload=True,
            )
        except Exception as exc:
            raise IndexError(f"Search via alias '{alias}' failed: {exc}") from exc

        return [
            SearchResult(
                point_id=str(r.id),
                chunk_id=r.payload.get("chunk_id", str(r.id)) if r.payload else str(r.id),
                score=r.score,
                payload=dict(r.payload) if r.payload else {},
            )
            for r in response.points
        ]

    # search_hybrid is inherited from IndexAdapter and raises UnsupportedCapabilityError.

    # ------------------------------------------------------------------
    # Point count
    # ------------------------------------------------------------------

    def count_points(self, collection: str) -> int:
        """Return the number of non-sentinel points in a collection."""
        if not self.collection_exists(collection):
            raise CollectionNotFoundError(f"Collection '{collection}' does not exist")
        try:
            # Exclude the metadata sentinel point
            result = self._client.count(
                collection_name=collection,
                count_filter=qm.Filter(
                    must_not=[
                        qm.FieldCondition(
                            key="_is_metadata_sentinel",
                            match=qm.MatchValue(value=True),
                        )
                    ]
                ),
                exact=True,
            )
            return result.count
        except Exception as exc:
            raise IndexError(f"Failed to count points in '{collection}': {exc}") from exc

    def list_collections(self) -> list[str]:
        """Return all collection names known to Qdrant."""
        try:
            return [c.name for c in self._client.get_collections().collections]
        except Exception as exc:
            raise IndexError(f"Failed to list collections: {exc}") from exc

    def list_aliases(self) -> list:
        """Return all aliases known to Qdrant as AliasRecord value objects.

        Uses the public ``get_aliases()`` client method — not ``_client``
        internals — so the adapter boundary is clean (F-03).

        Returns:
            List of ``AliasRecord`` value objects (alias_name → collection_name).
        """
        from finecorpus.index.adapter import AliasRecord as AdapterAliasRecord

        try:
            response = self._client.get_aliases()
            return [
                AdapterAliasRecord(
                    alias_name=a.alias_name,
                    collection_name=a.collection_name,
                )
                for a in response.aliases
            ]
        except Exception as exc:
            raise IndexError(f"Failed to list aliases: {exc}") from exc


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _dict_to_qdrant_filter(filter_dict: dict[str, Any]) -> qm.Filter:
    """Convert a simple equality filter dict to a Qdrant Filter.

    Supports a flat ``{field: value}`` dict where each entry becomes a
    ``FieldCondition(match=MatchValue(value=value))``.  All conditions are
    AND-joined (``must``).

    This is intentionally minimal for Phase 1. The retrieval service builds
    the filter dict and passes it here; more complex filter expressions can be
    supported later.
    """
    conditions = []
    for key, value in filter_dict.items():
        conditions.append(
            qm.FieldCondition(
                key=key,
                match=qm.MatchValue(value=value),
            )
        )
    return qm.Filter(must=conditions)


__all__ = ["QdrantAdapter"]
