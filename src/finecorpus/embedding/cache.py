"""Query-embedding cache (§5 of provider-abstraction.md).

This module implements an in-memory LRU cache with TTL for query embeddings.
It is query-path only — ingestion embeddings are NOT cached.

Cache key (§5.2)
----------------
``(model_id, vector_dimensions, api_version, normalize(query_text))``

The full tuple is hashed with SHA-256.  The raw components are NOT stored
in the cache entry; only the hash and the embedding vector are stored.

Normalization
-------------
``normalize(query_text)`` = Unicode NFC normalization + whitespace collapse + lowercase.

Model isolation
---------------
Keys include ``model_id``, ``vector_dimensions``, and ``api_version``.
A model upgrade (new api_version) or reconfiguration (new model_id) naturally
produces different keys — no explicit invalidation needed (§5.4).

Cache backend
-------------
Phase 1 implements the in-process LRU only (OQ-C-1 unresolved — Redis vs.
Postgres vs. in-process decision deferred to Phase 4).  The public interface
is designed so the backing store can be swapped later.

Config
------
- ``cache.query_embedding.enabled`` — enable/disable
- ``cache.query_embedding.ttl_seconds`` — entry TTL (default 3600)
- ``cache.query_embedding.max_entries`` — LRU limit (default 1024)

Usage
-----
A module-level default cache is exposed via :func:`get_query_cache`.
The retrieval service can also construct its own :class:`QueryEmbeddingCache`
with custom settings.
"""

from __future__ import annotations

import hashlib
import threading
import time
import unicodedata
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


def _normalize_query(text: str) -> str:
    """Normalize *text* for cache-key purposes (§5.2).

    Applies Unicode NFC normalization, collapses whitespace runs to a single
    space, and lowercases.  This is the only normalization applied — no
    semantic transformation.
    """
    nfc = unicodedata.normalize("NFC", text)
    collapsed = " ".join(nfc.split())  # splits on any whitespace, rejoins with " "
    return collapsed.lower()


def make_cache_key(
    model_id: str,
    vector_dimensions: int,
    api_version: str,
    query_text: str,
) -> str:
    """Derive the SHA-256 cache key from the four-tuple (§5.2).

    The raw components are NOT stored; only the hash is the key.
    """
    normalized = _normalize_query(query_text)
    raw = f"{model_id}\x00{vector_dimensions}\x00{api_version}\x00{normalized}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Cache entry
# ---------------------------------------------------------------------------


@dataclass
class CacheEntry:
    """A single query-embedding cache entry (§5.3).

    Attributes
    ----------
    embedding:
        The float vector.
    model_id:
        Stored for observability.
    created_at:
        Unix timestamp of when this entry was created.
    hit_count:
        Incremented on each cache hit.
    """

    embedding: list[float]
    model_id: str
    created_at: float = field(default_factory=time.time)
    hit_count: int = 0


# ---------------------------------------------------------------------------
# LRU cache with TTL
# ---------------------------------------------------------------------------

_DEFAULT_TTL_SECONDS = 3600
_DEFAULT_MAX_ENTRIES = 1024


class QueryEmbeddingCache:
    """In-memory LRU cache with TTL for query embeddings (§5).

    Thread-safe: uses a ``threading.Lock`` for all mutations.

    Parameters
    ----------
    ttl_seconds:
        Time-to-live for each entry (default: 3600 s / 1 hour).
    max_entries:
        Maximum entries before LRU eviction (default: 1024).
    enabled:
        When ``False``, ``get`` always misses and ``put`` is a no-op.
    """

    def __init__(
        self,
        *,
        ttl_seconds: int = _DEFAULT_TTL_SECONDS,
        max_entries: int = _DEFAULT_MAX_ENTRIES,
        enabled: bool = True,
    ) -> None:
        self._ttl = ttl_seconds
        self._max = max_entries
        self._enabled = enabled
        # OrderedDict used as ordered LRU: most-recently-used at the end
        self._store: OrderedDict[str, CacheEntry] = OrderedDict()
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get(
        self,
        model_id: str,
        vector_dimensions: int,
        api_version: str,
        query_text: str,
    ) -> list[float] | None:
        """Look up an embedding by the four-tuple key.

        Returns the embedding vector on a hit, or ``None`` on a miss or TTL
        expiry.  On a hit, increments ``hit_count`` and moves the entry to
        MRU position.

        Parameters are the same components as ``make_cache_key`` (§5.2).
        """
        if not self._enabled:
            return None

        key = make_cache_key(model_id, vector_dimensions, api_version, query_text)
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            # Check TTL
            if time.time() - entry.created_at > self._ttl:
                del self._store[key]
                return None
            # Hit — move to MRU and increment counter
            self._store.move_to_end(key)
            entry.hit_count += 1
            return entry.embedding

    def put(
        self,
        model_id: str,
        vector_dimensions: int,
        api_version: str,
        query_text: str,
        embedding: list[float],
    ) -> None:
        """Store an embedding in the cache.

        Evicts the LRU entry when at capacity.
        """
        if not self._enabled:
            return

        key = make_cache_key(model_id, vector_dimensions, api_version, query_text)
        with self._lock:
            if key in self._store:
                # Update existing entry and move to MRU
                self._store.move_to_end(key)
                self._store[key].embedding = embedding
                return
            # Evict LRU if at capacity
            while len(self._store) >= self._max:
                self._store.popitem(last=False)
            self._store[key] = CacheEntry(
                embedding=embedding,
                model_id=model_id,
            )

    def flush(self) -> None:
        """Remove all entries from the cache (§5.4 cache-flush operation)."""
        with self._lock:
            self._store.clear()

    def flush_for_model(self, model_id: str) -> int:
        """Remove all entries for a given *model_id*; returns count removed.

        Used by ``corpus cache flush --kb <id>`` (§5.4) to invalidate entries
        after a provider-side silent model update.
        """
        with self._lock:
            to_remove = [k for k, v in self._store.items() if v.model_id == model_id]
            for k in to_remove:
                del self._store[k]
            return len(to_remove)

    @property
    def size(self) -> int:
        """Current number of entries in the cache."""
        with self._lock:
            return len(self._store)

    @property
    def enabled(self) -> bool:
        """Whether the cache is enabled."""
        return self._enabled

    def stats(self) -> dict[str, Any]:
        """Return cache statistics for observability."""
        with self._lock:
            total_hits = sum(e.hit_count for e in self._store.values())
            return {
                "size": len(self._store),
                "max_entries": self._max,
                "ttl_seconds": self._ttl,
                "enabled": self._enabled,
                "total_hits": total_hits,
            }


# ---------------------------------------------------------------------------
# Module-level default cache instance
# ---------------------------------------------------------------------------

_default_cache: QueryEmbeddingCache | None = None
_cache_lock = threading.Lock()


def get_query_cache(
    *,
    ttl_seconds: int = _DEFAULT_TTL_SECONDS,
    max_entries: int = _DEFAULT_MAX_ENTRIES,
    enabled: bool = True,
    reset: bool = False,
) -> QueryEmbeddingCache:
    """Return the module-level default cache, constructing it on first call.

    Parameters
    ----------
    ttl_seconds:
        TTL in seconds (only used on first construction or when ``reset=True``).
    max_entries:
        Max entries (only used on first construction or when ``reset=True``).
    enabled:
        Whether the cache is enabled.
    reset:
        When ``True``, discard the existing cache and create a fresh one.
        Used in tests to ensure isolation between test cases.
    """
    global _default_cache
    with _cache_lock:
        if _default_cache is None or reset:
            _default_cache = QueryEmbeddingCache(
                ttl_seconds=ttl_seconds,
                max_entries=max_entries,
                enabled=enabled,
            )
        return _default_cache


def configure_cache_from_config(config: Any) -> QueryEmbeddingCache:
    """Build and install the cache from a ``finecorpus.config.Config``.

    Called during application startup.  Resets the module-level default.

    Parameters
    ----------
    config:
        A ``finecorpus.config.models.Config`` instance.
    """
    cc = config.cache.query_embedding
    return get_query_cache(
        ttl_seconds=cc.ttl_seconds,
        max_entries=cc.max_entries or _DEFAULT_MAX_ENTRIES,
        enabled=cc.enabled,
        reset=True,
    )
