"""Embedding provider abstraction for Read The Fine Corpus.

Implements provider-abstraction.md §2 (embedding provider interface),
§3 (model identity), §5 (query embedding cache), and §6 (secrets handling).

Public surface
--------------
- :class:`ProviderCapabilities` — static capability declaration (§2.1).
- :class:`EmbedBatchResult` — result of a single embed_batch call (§2.2).
- :class:`HealthCheckResult` — result of a health_check call (§2.2).
- :class:`CostEstimate` — result of estimate_cost call (§2.2).
- :class:`EmbeddingProvider` — protocol / ABC that all adapters implement (§2).
- :class:`ProviderUnavailableError` — raised by adapters when the provider is down (§2.2).
- :class:`ProviderError` — raised for adapter-level errors (§2.2).
- :func:`build_provider_from_config` — factory that reads finecorpus.config.Config.
- :func:`get_query_cache` — returns the module-level LRU query cache (§5).
"""

from finecorpus.embedding.base import (
    CostEstimate,
    EmbedBatchResult,
    EmbeddingProvider,
    HealthCheckResult,
    ProviderCapabilities,
    ProviderError,
    ProviderUnavailableError,
)
from finecorpus.embedding.cache import QueryEmbeddingCache, get_query_cache
from finecorpus.embedding.fake import FakeProvider
from finecorpus.embedding.registry import build_provider_from_config

__all__ = [
    "ProviderCapabilities",
    "EmbedBatchResult",
    "HealthCheckResult",
    "CostEstimate",
    "EmbeddingProvider",
    "ProviderError",
    "ProviderUnavailableError",
    "FakeProvider",
    "QueryEmbeddingCache",
    "get_query_cache",
    "build_provider_from_config",
]
