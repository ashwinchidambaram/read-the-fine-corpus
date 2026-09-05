"""Vector-backend adapter interface (§4.4).

All clients speak to this interface; collection names are internal to ``index/``.
Spec references: §4.2 (C-3), §4.4, §10, §15, overview.md C-2/C-3.

Phase 1 capabilities:
  - Dense vector search only.
  - Hybrid search declared unsupported and surfaced (not silent) — M-009/M-010.
  - Atomic alias retarget (Qdrant guarantees at-most-one-target per alias).
  - Payload filtering as a first-class operation on every search path.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class IndexError(Exception):
    """Base for all index-layer errors."""


class UnsupportedCapabilityError(IndexError):
    """Raised when the caller requests a capability the backend does not support.

    Per §4.4: "where an adapter cannot support a capability, it MUST declare
    that explicitly and the platform MUST surface the limitation to the user
    rather than silently degrading."

    Attributes:
        capability: The name of the unsupported capability (e.g. ``"hybrid_search"``).
        backend: The backend name (e.g. ``"qdrant"``).
    """

    def __init__(self, capability: str, backend: str, detail: str = "") -> None:
        self.capability = capability
        self.backend = backend
        self.detail = detail
        msg = (
            f"Backend '{backend}' does not support capability '{capability}'. "
            f"This limitation is intentional and must be surfaced to the user "
            f"(§4.4). {detail}".strip()
        )
        super().__init__(msg)


class AliasSwapError(IndexError):
    """Raised when an alias retarget fails at the Qdrant layer (§4.3 Phase-1 failure)."""


class CollectionNotFoundError(IndexError):
    """Raised when the target collection does not exist."""


class AliasNotFoundError(IndexError):
    """Raised when the alias does not exist or resolves to nothing."""


class SnapshotError(IndexError):
    """Raised when a snapshot operation fails at the backend layer.

    See §10.2 (hot/cold retention) and §17.1 (deletion completeness for cold snapshots).
    """


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BackendCapabilities:
    """Declares what the backend supports and what it does not.

    Per §4.4: adapters that cannot support a capability MUST declare it here.
    Callers inspect this before invoking optional capabilities.

    Phase 1 values for Qdrant:
    - ``dense_search``: True
    - ``hybrid_search``: False (declared unsupported; not silent — M-009/M-010)
    - ``payload_filtering``: True
    - ``atomic_alias_swap``: True
    - ``snapshots``: True (Phase 4: snapshot/restore via Qdrant snapshot API)
    """

    dense_search: bool = True
    """Dense (embedding) vector search."""
    hybrid_search: bool = False
    """Hybrid dense+sparse search. Declared unsupported in Phase 1 (M-009/M-010)."""
    payload_filtering: bool = True
    """Server-side payload filter injection on every search path."""
    atomic_alias_swap: bool = True
    """Atomic alias retarget — zero error window (§4, §18.3 test 1)."""
    snapshots: bool = True
    """Collection snapshot create/list/delete/restore (§10.2, §17.1).

    Limitation (Qdrant local-storage): ``restore_snapshot`` uses the Qdrant
    ``recover_snapshot`` API which requires a URL-accessible snapshot location.
    For local-storage deployments the snapshot file URL is a ``file://`` path on
    the Qdrant server's filesystem; callers MUST ensure the restored-into collection
    does not already exist.  Cloud/S3-backed Qdrant deployments may expose an
    external URL instead.
    """

    hybrid_search_unavailable_reason: str = (
        "Hybrid search requires sparse-vector support (SPLADE/BM25). "
        "Phase 1 ships dense-only. Enable hybrid when a sparse embedding provider "
        "is configured (Phase 3+)."
    )
    """Human-readable explanation for why hybrid is unavailable (M-009/M-010)."""


@dataclass
class SearchResult:
    """One result from a dense search.

    Attributes:
        point_id: The UUID point ID in the vector DB.
        chunk_id: The deterministic chunk ID (API-visible; point_id stays internal).
        score: Similarity score from the backend.
        payload: Full payload dict as stored (includes chunk fields + provenance).
    """

    point_id: str
    chunk_id: str
    score: float
    payload: dict[str, Any]


@dataclass
class SnapshotRef:
    """Identifies a collection snapshot (§10.2, §17.1).

    A ``SnapshotRef`` is an opaque handle that callers pass back to ``restore_snapshot``,
    ``delete_snapshot``, etc.  The ``location`` field is a backend-specific URI (e.g. a
    Qdrant ``file://`` path or an external URL) and MUST be treated as opaque by callers.

    Callers MUST NOT promote a restored collection without first replaying the tombstone
    log (§17.1); that responsibility belongs to the lifecycle layer, not this adapter.

    Attributes:
        collection: The collection this snapshot was taken from.
        snapshot_id: Backend-assigned snapshot name/identifier.
        created_at: UTC timestamp when the snapshot was created; ``None`` if the
            backend did not return a creation time.
        location: Backend-specific URI for the snapshot file (e.g. Qdrant local
            ``file://`` path or an external URL).  Passed to ``restore_snapshot``.
    """

    collection: str
    snapshot_id: str
    created_at: datetime | None
    location: str


@dataclass
class CollectionInfo:
    """Lightweight summary of a backend collection.

    Attributes:
        name: The raw collection name (internal; callers should use aliases).
        vector_size: Dimensionality of the stored vectors.
        point_count: Number of points currently in the collection.
        metadata: Key-value metadata stored on the collection.
    """

    name: str
    vector_size: int
    point_count: int
    metadata: dict[str, Any]


@dataclass
class AliasRecord:
    """Minimal alias-resolution record returned by the adapter.

    Note: the full denormalized alias record (including model identity) lives in
    the control-plane DB (``control.metadata.AliasRecord``). This is only what
    the Qdrant adapter layer exposes — the target collection name.

    Attributes:
        alias_name: The alias string (e.g. ``rtfc_{kb_id}``).
        collection_name: The collection the alias currently points to.
    """

    alias_name: str
    collection_name: str


# ---------------------------------------------------------------------------
# Naming helpers (§2.1, §2.2 of index-lifecycle.md)
# ---------------------------------------------------------------------------


def collection_name(kb_id: str, build_id: int) -> str:
    """Derive the canonical Qdrant collection name.

    Format: ``rtfc_{kb_id_nohyphens}_{build_id_zero_padded_8}``

    Per §2.1 of index-lifecycle.md: the naming scheme encodes identity and
    version without being user-visible. ``rtfc_`` namespaces platform collections.
    ``kb_id`` is hyphen-stripped lowercase. ``build_id`` is zero-padded to 8 digits.

    Args:
        kb_id: Knowledge-base UUID (hyphens allowed; stripped here).
        build_id: Monotonically increasing integer from the control plane.

    Returns:
        Canonical collection name string.
    """
    kb_clean = kb_id.replace("-", "").lower()
    return f"rtfc_{kb_clean}_{build_id:08d}"


def alias_name(kb_id: str) -> str:
    """Derive the stable alias name for a knowledge base.

    Format: ``rtfc_{kb_id_nohyphens}``

    Per §2.2: exactly one alias per knowledge base; stable for its lifetime.

    Args:
        kb_id: Knowledge-base UUID (hyphens allowed; stripped here).

    Returns:
        Alias name string.
    """
    kb_clean = kb_id.replace("-", "").lower()
    return f"rtfc_{kb_clean}"


# ---------------------------------------------------------------------------
# Model identity
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelIdentity:
    """Identifies the embedding model used to build a collection.

    Stored in the Qdrant collection metadata AND in the control-plane alias
    record (see ``control.metadata``). Used for the mismatch-fails-closed check
    at retrieval time (§15).

    Attributes:
        provider: Provider name (e.g. ``"openai"``, ``"ollama"``).
        model: Model identifier (e.g. ``"text-embedding-3-small"``).
        dimensions: Vector dimensionality.
        config_version: The ingestion config version hash that produced this collection.
    """

    provider: str
    model: str
    dimensions: int
    config_version: str


# ---------------------------------------------------------------------------
# Chunk point payload schema
# ---------------------------------------------------------------------------


def build_point_payload(
    chunk_id: str,
    provenance: dict[str, Any],
    tenancy: dict[str, Any],
    text: str,
    embedding_ref: dict[str, Any],
    augmentation: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble the full Qdrant point payload for a chunk.

    The payload is the full contract payload — chunk_id, provenance, tenancy,
    text, embedding_ref — plus any extra fields. This is the authoritative
    payload schema stored in Qdrant; filtering is keyed on provenance and
    tenancy fields at retrieval time.

    Args:
        chunk_id: Deterministic chunk ID (e.g. ``"chk_..."``).
        provenance: Full §8 provenance block as a plain dict.
        tenancy: Tenancy block as a plain dict (workspace_id, kb_id, etc.).
        text: Chunk text content.
        embedding_ref: EmbeddingRef fields as a plain dict.
        augmentation: Optional Tier 2 augmentation fields.
        extra: Any additional fields to include verbatim.

    Returns:
        Full payload dict ready for upsert.
    """
    payload: dict[str, Any] = {
        "chunk_id": chunk_id,
        "provenance": provenance,
        "tenancy": tenancy,
        "text": text,
        "embedding_ref": embedding_ref,
    }
    if augmentation is not None:
        payload["augmentation"] = augmentation
    if extra:
        payload.update(extra)
    return payload


# ---------------------------------------------------------------------------
# Adapter ABC
# ---------------------------------------------------------------------------


class IndexAdapter(abc.ABC):
    """Abstract adapter for a vector-DB backend (§4.4).

    All operations that write to or query the vector DB go through this interface.
    Collection names never leave ``index/``; clients hold alias handles or
    collection names that were created by this adapter.

    Implementors MUST:
    - Return ``capabilities()`` that accurately reflects what the backend can do.
    - Raise ``UnsupportedCapabilityError`` — not silently degrade — when called
      for an unsupported capability (§4.4).
    - Keep alias semantics: search operations accept the alias string; internal
      resolution to collection name is done by the adapter.

    Phase 1: Qdrant is the only backend. The interface is defined here so the
    import-linter layer contract is satisfied from day 1.
    """

    # ------------------------------------------------------------------
    # Capability surface
    # ------------------------------------------------------------------

    @abc.abstractmethod
    def capabilities(self) -> BackendCapabilities:
        """Return declared capabilities for this backend instance.

        Callers MUST inspect this before using optional capabilities (hybrid
        search, etc.). The platform MUST surface any unsupported capability as
        an error to the user (§4.4, M-009/M-010).
        """

    # ------------------------------------------------------------------
    # Collection lifecycle
    # ------------------------------------------------------------------

    @abc.abstractmethod
    def create_collection(
        self,
        kb_id: str,
        build_id: int,
        dimensions: int,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Create a shadow collection for a given knowledge base build.

        Per §4.2 C-4: ingestion MUST write only to shadow collections.
        The collection name is derived from ``collection_name(kb_id, build_id)``
        and is never chosen by the caller.

        Args:
            kb_id: Knowledge-base UUID.
            build_id: Build ID allocated by the control plane.
            dimensions: Vector dimensionality for this collection.
            metadata: Optional key-value metadata to store on the collection
                (used for model identity pinning).

        Returns:
            The canonical collection name (internal; not for clients).

        Raises:
            IndexError: If the collection already exists or the backend is
                unavailable.
        """

    @abc.abstractmethod
    def drop_collection(self, collection: str) -> None:
        """Drop a collection from the backend.

        Used during cleanup (retirement, purge). The alias must be retargeted
        away from this collection before calling; the adapter does not check.

        Args:
            collection: The raw collection name to drop.

        Raises:
            CollectionNotFoundError: If the collection does not exist.
        """

    @abc.abstractmethod
    def get_collection_info(self, collection: str) -> CollectionInfo:
        """Return summary information for a collection.

        Args:
            collection: Raw collection name.

        Returns:
            CollectionInfo with point count, vector size, and metadata.

        Raises:
            CollectionNotFoundError: If the collection does not exist.
        """

    @abc.abstractmethod
    def collection_exists(self, collection: str) -> bool:
        """Return True if the collection exists in the backend."""

    # ------------------------------------------------------------------
    # Write path
    # ------------------------------------------------------------------

    @abc.abstractmethod
    def upsert_points(
        self,
        collection: str,
        points: list[dict[str, Any]],
    ) -> None:
        """Upsert a batch of vector points into a shadow collection.

        Each element of ``points`` MUST contain:
        - ``id``: UUID (from ``contracts.derive_point_id``).
        - ``vector``: list[float] with the embedding.
        - ``payload``: dict assembled by ``build_point_payload()``.

        Per C-4, this method MUST only be called on shadow (non-live) collections.
        The adapter does not enforce this — the lifecycle layer does.

        Args:
            collection: Raw shadow collection name.
            points: Batch of point dicts.

        Raises:
            CollectionNotFoundError: If the collection does not exist.
            IndexError: On any backend write failure.
        """

    @abc.abstractmethod
    def delete_by_document(
        self,
        collection: str,
        source_document_id: str,
    ) -> int:
        """Delete all points whose payload ``provenance.source_document_id`` matches.

        Config-invariant deletion: keyed on the stable document identity, not
        chunk IDs (which encode config_version). See §10.5 / §12.1 and
        index-lifecycle.md §9.1.

        Args:
            collection: Raw collection name (may be live, shadow, or N-1).
            source_document_id: The document ID to sweep.

        Returns:
            Number of points deleted.

        Raises:
            CollectionNotFoundError: If the collection does not exist.
        """

    # ------------------------------------------------------------------
    # Collection metadata (model identity pinning)
    # ------------------------------------------------------------------

    @abc.abstractmethod
    def set_collection_metadata(
        self,
        collection: str,
        metadata: dict[str, Any],
    ) -> None:
        """Store key-value metadata on a collection.

        Used to pin model identity: ``provider``, ``model``, ``dimensions``,
        ``config_version`` are stored here so they can be read back without
        consulting the control-plane DB.

        Args:
            collection: Raw collection name.
            metadata: Key-value pairs to merge into the collection metadata.
        """

    @abc.abstractmethod
    def get_collection_metadata(self, collection: str) -> dict[str, Any]:
        """Return the key-value metadata stored on a collection.

        Args:
            collection: Raw collection name.

        Returns:
            Metadata dict (empty dict if none stored).

        Raises:
            CollectionNotFoundError: If the collection does not exist.
        """

    # ------------------------------------------------------------------
    # Alias operations (C-3)
    # ------------------------------------------------------------------

    @abc.abstractmethod
    def create_alias(self, alias: str, collection: str) -> None:
        """Create a new alias pointing to ``collection``.

        Per §2.2: created when the knowledge base is first created, before any
        collection exists. Until the first promotion the alias points at nothing
        (the backend may accept an empty alias or the caller must create it only
        with the first promotion). See lifecycle.py for the exact creation path.

        Args:
            alias: Alias name (e.g. ``rtfc_{kb_id}``).
            collection: Target collection name.

        Raises:
            IndexError: If the alias already exists.
        """

    @abc.abstractmethod
    def retarget_alias(self, alias: str, new_collection: str) -> None:
        """Atomically retarget an alias to a new collection (§4.1 Phase 1).

        Qdrant guarantees that from the moment this call returns success, all
        subsequent alias resolutions point at ``new_collection``. In-flight
        requests that resolved the alias before this call continue against the
        old collection.

        Args:
            alias: Alias name to retarget.
            new_collection: Collection name to point the alias to.

        Raises:
            AliasSwapError: If the retarget fails.
            CollectionNotFoundError: If ``new_collection`` does not exist.
        """

    @abc.abstractmethod
    def resolve_alias(self, alias: str) -> str | None:
        """Return the collection name the alias currently points to.

        Returns ``None`` if the alias does not exist or points to nothing.

        Args:
            alias: Alias name to resolve.

        Returns:
            Collection name, or ``None``.
        """

    @abc.abstractmethod
    def alias_exists(self, alias: str) -> bool:
        """Return True if the alias exists in the backend."""

    @abc.abstractmethod
    def delete_alias(self, alias: str) -> None:
        """Delete an alias from the backend.

        Does not delete the target collection.

        Args:
            alias: Alias name to delete.

        Raises:
            AliasNotFoundError: If the alias does not exist.
        """

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    @abc.abstractmethod
    def search(
        self,
        alias: str,
        query_vector: list[float],
        top_k: int = 10,
        payload_filter: dict[str, Any] | None = None,
    ) -> list[SearchResult]:
        """Dense vector search through an alias (C-3).

        Callers always supply the alias, never the collection name. Payload
        filtering is first-class: tenancy and permission filters are injected
        by the retrieval service and passed here.

        Args:
            alias: Alias name (resolves to the live collection internally).
            query_vector: Dense embedding vector.
            top_k: Number of results to return.
            payload_filter: Optional Qdrant filter expression dict. Applied
                server-side before scoring (payload filters are first-class).

        Returns:
            List of SearchResult ordered by descending score.

        Raises:
            UnsupportedCapabilityError: Never for dense search (always supported).
            AliasNotFoundError: If the alias does not resolve.
        """

    def search_hybrid(
        self,
        alias: str,
        query_vector: list[float],
        sparse_vector: Any = None,
        top_k: int = 10,
        payload_filter: dict[str, Any] | None = None,
    ) -> list[SearchResult]:
        """Hybrid dense+sparse search (declared unsupported in Phase 1 — M-009/M-010).

        This method is provided on the base class so callers can attempt hybrid
        search and receive a clear, surfaced error rather than a missing-method
        error. The Qdrant adapter raises ``UnsupportedCapabilityError`` with
        the reason from ``capabilities().hybrid_search_unavailable_reason``.

        Args:
            alias: Alias name.
            query_vector: Dense embedding vector.
            sparse_vector: Sparse vector (not used in Phase 1).
            top_k: Number of results.
            payload_filter: Optional filter expression.

        Returns:
            Never returns in Phase 1.

        Raises:
            UnsupportedCapabilityError: Always in Phase 1 (hybrid not supported).
        """
        caps = self.capabilities()
        raise UnsupportedCapabilityError(
            capability="hybrid_search",
            backend=self.__class__.__name__,
            detail=caps.hybrid_search_unavailable_reason,
        )

    # ------------------------------------------------------------------
    # Point count (for validation gates)
    # ------------------------------------------------------------------

    @abc.abstractmethod
    def count_points(self, collection: str) -> int:
        """Return the number of points in a collection.

        Used by validation Gate 1 (chunk count bounds check).

        Args:
            collection: Raw collection name.

        Returns:
            Point count.

        Raises:
            CollectionNotFoundError: If the collection does not exist.
        """

    @abc.abstractmethod
    def list_collections(self) -> list[str]:
        """Return all collection names known to the backend.

        Used by the startup reconciliation pass (OQ-L-1) to detect
        collections that exist in Qdrant but not in the control-plane DB.

        Returns:
            List of collection name strings.
        """

    @abc.abstractmethod
    def list_aliases(self) -> list[AliasRecord]:
        """Return all aliases known to the backend.

        Used by the startup reconciliation pass (OQ-L-1) to discover all
        ``rtfc_`` aliases without reaching into adapter internals.  This is
        the public surface for alias enumeration; callers MUST use this method
        rather than accessing ``_client`` directly (F-03).

        Returns:
            List of AliasRecord value objects (alias_name → collection_name).
        """

    # ------------------------------------------------------------------
    # Snapshot operations (§10.2 hot/cold retention, §17.1 deletion completeness)
    # ------------------------------------------------------------------

    @abc.abstractmethod
    def snapshot_collection(self, collection_name: str) -> SnapshotRef:
        """Create a snapshot of a collection and return a reference to it.

        Snapshots are the cold-storage mechanism for N-2, N-3, … index versions
        (§10.2).  The returned ``SnapshotRef.location`` is a backend-specific URI
        that callers pass back to ``restore_snapshot``.

        IMPORTANT: the lifecycle layer is responsible for replaying the tombstone
        log before promoting a restored collection (§17.1).  This method only
        creates the snapshot; it does not enforce deletion-completeness invariants.

        Args:
            collection_name: The raw collection name to snapshot.

        Returns:
            A ``SnapshotRef`` describing the created snapshot.

        Raises:
            CollectionNotFoundError: If the collection does not exist.
            SnapshotError: If the backend snapshot operation fails.
        """

    @abc.abstractmethod
    def restore_snapshot(self, ref: SnapshotRef, new_collection_name: str) -> None:
        """Restore a snapshot INTO a new collection (never in place).

        Callers promote the new collection via an alias swap after tombstone-log
        replay (§17.1).  The adapter MUST NOT overwrite an existing collection;
        if ``new_collection_name`` already exists, this method MUST raise
        ``IndexError``.

        Limitation (Qdrant local-storage): ``recover_snapshot`` requires that the
        snapshot location is accessible as a URL from the Qdrant server.  For local
        deployments the ``ref.location`` is a ``file://`` path on the Qdrant server
        filesystem.  Callers in remote/cloud setups MUST use an externally reachable
        URL (e.g. via Qdrant's snapshot download endpoint).

        Args:
            ref: The ``SnapshotRef`` returned by ``snapshot_collection``.
            new_collection_name: Name for the restored collection.  Must not exist.

        Raises:
            IndexError: If ``new_collection_name`` already exists.
            SnapshotError: If the backend restore operation fails.
        """

    @abc.abstractmethod
    def list_snapshots(self, collection_name: str) -> list[SnapshotRef]:
        """List all snapshots for a collection.

        Args:
            collection_name: The raw collection name whose snapshots to list.

        Returns:
            List of ``SnapshotRef`` objects, ordered by creation time (oldest first)
            where the backend provides timestamps; otherwise backend-native order.

        Raises:
            CollectionNotFoundError: If the collection does not exist.
            SnapshotError: If the list operation fails.
        """

    @abc.abstractmethod
    def delete_snapshot(self, ref: SnapshotRef) -> None:
        """Delete a snapshot from the backend.

        Does not affect the live collection or any alias.

        Args:
            ref: The ``SnapshotRef`` to delete.

        Raises:
            SnapshotError: If the snapshot does not exist or the delete fails.
        """


# ---------------------------------------------------------------------------
# Promoted timestamp helper
# ---------------------------------------------------------------------------


def utc_now() -> datetime:
    """Return current UTC time (timezone-aware)."""

    return datetime.now(tz=UTC)


__all__ = [
    "AliasNotFoundError",
    "AliasRecord",
    "AliasSwapError",
    "BackendCapabilities",
    "CollectionInfo",
    "CollectionNotFoundError",
    "IndexAdapter",
    "IndexError",
    "ModelIdentity",
    "SearchResult",
    "SnapshotError",
    "SnapshotRef",
    "UnsupportedCapabilityError",
    "alias_name",
    "build_point_payload",
    "collection_name",
    "utc_now",
]
