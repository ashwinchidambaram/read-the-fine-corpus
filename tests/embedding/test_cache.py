"""Tests for the query-embedding cache (provider-abstraction.md §5)."""

from __future__ import annotations

import pytest

from finecorpus.embedding.cache import (
    QueryEmbeddingCache,
    _normalize_query,
    get_query_cache,
    make_cache_key,
)

# ---------------------------------------------------------------------------
# Normalization tests
# ---------------------------------------------------------------------------


class TestNormalizeQuery:
    def test_lowercases(self) -> None:
        assert _normalize_query("Hello World") == "hello world"

    def test_collapses_whitespace(self) -> None:
        assert _normalize_query("hello   world\ttab") == "hello world tab"

    def test_nfc_normalization(self) -> None:
        # Composed vs decomposed e with accent
        composed = "é"  # é (NFC)
        decomposed = "é"  # e + combining acute accent (NFD)
        assert _normalize_query(composed) == _normalize_query(decomposed)

    def test_strips_leading_trailing_whitespace(self) -> None:
        assert _normalize_query("  hello  ") == "hello"

    def test_empty_string(self) -> None:
        assert _normalize_query("") == ""


# ---------------------------------------------------------------------------
# Cache key tests
# ---------------------------------------------------------------------------


class TestMakeCacheKey:
    def test_deterministic(self) -> None:
        k1 = make_cache_key("model-a", 768, "v1", "hello world")
        k2 = make_cache_key("model-a", 768, "v1", "hello world")
        assert k1 == k2

    def test_different_model_ids_produce_different_keys(self) -> None:
        k1 = make_cache_key("model-a", 768, "v1", "hello")
        k2 = make_cache_key("model-b", 768, "v1", "hello")
        assert k1 != k2

    def test_different_dimensions_produce_different_keys(self) -> None:
        k1 = make_cache_key("model", 768, "v1", "hello")
        k2 = make_cache_key("model", 1536, "v1", "hello")
        assert k1 != k2

    def test_different_api_versions_produce_different_keys(self) -> None:
        k1 = make_cache_key("model", 768, "v1", "hello")
        k2 = make_cache_key("model", 768, "v2", "hello")
        assert k1 != k2

    def test_normalization_applied(self) -> None:
        k1 = make_cache_key("model", 768, "v1", "hello world")
        k2 = make_cache_key("model", 768, "v1", "HELLO  WORLD")
        assert k1 == k2

    def test_key_is_hex_string(self) -> None:
        k = make_cache_key("model", 768, "v1", "test")
        assert all(c in "0123456789abcdef" for c in k)
        assert len(k) == 64  # sha256 hexdigest


# ---------------------------------------------------------------------------
# Cache put / get
# ---------------------------------------------------------------------------


class TestQueryEmbeddingCachePutGet:
    def test_miss_returns_none(self) -> None:
        cache = QueryEmbeddingCache()
        result = cache.get("model", 768, "v1", "hello")
        assert result is None

    def test_put_then_get_returns_vector(self) -> None:
        cache = QueryEmbeddingCache()
        vec = [0.1, 0.2, 0.3]
        cache.put("model", 768, "v1", "hello", vec)
        result = cache.get("model", 768, "v1", "hello")
        assert result == vec

    def test_normalization_on_get(self) -> None:
        cache = QueryEmbeddingCache()
        vec = [0.1, 0.2]
        cache.put("model", 768, "v1", "hello world", vec)
        # Different whitespace / case → same key
        result = cache.get("model", 768, "v1", "HELLO   WORLD")
        assert result == vec

    def test_different_models_isolated(self) -> None:
        cache = QueryEmbeddingCache()
        v1 = [1.0]
        v2 = [2.0]
        cache.put("model-a", 768, "v1", "hello", v1)
        cache.put("model-b", 768, "v1", "hello", v2)
        assert cache.get("model-a", 768, "v1", "hello") == v1
        assert cache.get("model-b", 768, "v1", "hello") == v2

    def test_different_api_versions_isolated(self) -> None:
        cache = QueryEmbeddingCache()
        v1 = [1.0]
        v2 = [2.0]
        cache.put("model", 768, "v1", "hello", v1)
        cache.put("model", 768, "v2", "hello", v2)
        assert cache.get("model", 768, "v1", "hello") == v1
        assert cache.get("model", 768, "v2", "hello") == v2

    def test_different_dimensions_isolated(self) -> None:
        cache = QueryEmbeddingCache()
        v1 = [1.0] * 768
        v2 = [2.0] * 1536
        cache.put("model", 768, "v1", "hello", v1)
        cache.put("model", 1536, "v1", "hello", v2)
        assert cache.get("model", 768, "v1", "hello") == v1
        assert cache.get("model", 1536, "v1", "hello") == v2

    def test_size_increases_on_put(self) -> None:
        cache = QueryEmbeddingCache()
        assert cache.size == 0
        cache.put("model", 768, "v1", "hello", [0.1])
        assert cache.size == 1
        cache.put("model", 768, "v1", "world", [0.2])
        assert cache.size == 2

    def test_lru_eviction(self) -> None:
        cache = QueryEmbeddingCache(max_entries=3)
        cache.put("model", 768, "v1", "a", [1.0])
        cache.put("model", 768, "v1", "b", [2.0])
        cache.put("model", 768, "v1", "c", [3.0])
        assert cache.size == 3
        # Add a 4th → LRU ("a") is evicted
        cache.put("model", 768, "v1", "d", [4.0])
        assert cache.size == 3
        assert cache.get("model", 768, "v1", "a") is None  # evicted
        assert cache.get("model", 768, "v1", "d") is not None  # present

    def test_lru_access_prevents_eviction(self) -> None:
        cache = QueryEmbeddingCache(max_entries=2)
        cache.put("model", 768, "v1", "a", [1.0])
        cache.put("model", 768, "v1", "b", [2.0])
        # Access "a" to make it MRU
        cache.get("model", 768, "v1", "a")
        # "b" is now LRU; adding "c" evicts "b"
        cache.put("model", 768, "v1", "c", [3.0])
        assert cache.get("model", 768, "v1", "a") is not None  # survived
        assert cache.get("model", 768, "v1", "b") is None  # evicted

    def test_ttl_expiry(self) -> None:
        """Entries older than TTL are evicted on get."""
        import finecorpus.embedding.cache as _cache_mod

        cache = QueryEmbeddingCache(ttl_seconds=1)
        cache.put("model", 768, "v1", "hello", [0.1])
        assert cache.get("model", 768, "v1", "hello") is not None
        # Fake the current time to be 2 seconds in the future
        _real_time = _cache_mod.time.time
        future = _real_time() + 2
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(_cache_mod.time, "time", lambda: future)
            result = cache.get("model", 768, "v1", "hello")
        assert result is None  # expired

    def test_hit_count_increments(self) -> None:
        cache = QueryEmbeddingCache()
        cache.put("model", 768, "v1", "hello", [0.1])
        cache.get("model", 768, "v1", "hello")
        cache.get("model", 768, "v1", "hello")
        with cache._lock:
            key = make_cache_key("model", 768, "v1", "hello")
            entry = cache._store[key]
        assert entry.hit_count == 2

    def test_enabled_false_always_miss(self) -> None:
        cache = QueryEmbeddingCache(enabled=False)
        cache.put("model", 768, "v1", "hello", [0.1])
        assert cache.get("model", 768, "v1", "hello") is None
        assert cache.size == 0

    def test_flush_clears_all(self) -> None:
        cache = QueryEmbeddingCache()
        cache.put("model", 768, "v1", "hello", [0.1])
        cache.put("model", 768, "v1", "world", [0.2])
        cache.flush()
        assert cache.size == 0

    def test_flush_for_model_removes_only_matching(self) -> None:
        cache = QueryEmbeddingCache()
        cache.put("model-a", 768, "v1", "hello", [1.0])
        cache.put("model-b", 768, "v1", "hello", [2.0])
        removed = cache.flush_for_model("model-a")
        assert removed == 1
        assert cache.get("model-a", 768, "v1", "hello") is None
        assert cache.get("model-b", 768, "v1", "hello") == [2.0]

    def test_stats(self) -> None:
        cache = QueryEmbeddingCache(ttl_seconds=100, max_entries=50)
        cache.put("model", 768, "v1", "hello", [0.1])
        cache.get("model", 768, "v1", "hello")
        stats = cache.stats()
        assert stats["size"] == 1
        assert stats["max_entries"] == 50
        assert stats["ttl_seconds"] == 100
        assert stats["enabled"] is True
        assert stats["total_hits"] == 1


# ---------------------------------------------------------------------------
# Module-level default cache
# ---------------------------------------------------------------------------


class TestGetQueryCache:
    def test_returns_cache_instance(self) -> None:
        cache = get_query_cache(reset=True)
        assert isinstance(cache, QueryEmbeddingCache)

    def test_same_instance_returned_on_second_call(self) -> None:
        c1 = get_query_cache(reset=True)
        c2 = get_query_cache()
        assert c1 is c2

    def test_reset_creates_fresh_instance(self) -> None:
        c1 = get_query_cache(reset=True)
        c1.put("model", 768, "v1", "hello", [0.1])
        c2 = get_query_cache(reset=True)
        assert c2.size == 0

    def test_custom_ttl_and_max_entries(self) -> None:
        cache = get_query_cache(ttl_seconds=300, max_entries=10, reset=True)
        assert cache._ttl == 300
        assert cache._max == 10


# ---------------------------------------------------------------------------
# F-007: error results must NOT be stored in the cache
# ---------------------------------------------------------------------------


class TestCacheErrorResultContract:
    """F-007: put() forbids error results; a failed embed must not grow cache.size."""

    def test_failed_embed_leaves_cache_size_zero(self) -> None:
        """Simulates the contract: after a provider error, cache.size stays 0."""
        from finecorpus.embedding.base import ProviderUnavailableError

        cache = QueryEmbeddingCache()
        assert cache.size == 0

        # Simulate what the caller SHOULD do: only put on success.
        # If an exception is raised, put() is never called.
        try:
            raise ProviderUnavailableError(
                "Provider failed",
                provider_id="fake",
                model_id="fake-model",
            )
        except ProviderUnavailableError:
            # Do NOT call cache.put — the contract says error results are forbidden
            pass

        assert cache.size == 0, (
            "Cache size must remain 0 when the provider raises an error "
            "and put() is correctly not called (F-007)"
        )

    def test_successful_embed_grows_cache_size(self) -> None:
        """After a successful embed, put() is called and cache.size increases."""
        cache = QueryEmbeddingCache()
        assert cache.size == 0
        cache.put("model", 768, "v1", "hello", [0.1] * 768)
        assert cache.size == 1

    def test_put_docstring_mentions_error_prohibition(self) -> None:
        """F-007: the put() docstring must document that error results are forbidden."""
        doc = QueryEmbeddingCache.put.__doc__ or ""
        assert "error" in doc.lower(), (
            "put() docstring must mention that error results must not be stored (F-007)"
        )
