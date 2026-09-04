"""FakeProvider — deterministic test-only embedding provider.

**This provider is FOR TESTS ONLY.**  It must never appear in production
configuration.  Its vectors are derived from sha256(text) and are entirely
non-semantic — they are not real embeddings.

Design
------
- Deterministic: the same text always produces the same vector, across
  processes and interpreter restarts.  No randomness.
- Configurable: dimensions, model_id, and failure injection are all
  constructor parameters.
- Failure injection: ``fail_on_embed`` raises ``ProviderUnavailableError``
  on every embed_batch call; ``fail_on_health`` similarly for health_check.
  Used by unit tests to exercise §15 failure-mode paths.
- Provenance: ``api_version`` encodes the configured dimensions so that
  tests can verify model-identity isolation in the query cache.
"""

from __future__ import annotations

import hashlib
import struct
from decimal import Decimal

from finecorpus.embedding.base import (
    CostEstimate,
    EmbedBatchResult,
    EmbeddingProvider,
    HealthCheckResult,
    ProviderCapabilities,
    ProviderUnavailableError,
)

# Fixed probe string (health check only — carries no document content)
_PROBE = "finecorpus-health-probe-v1"

# Tokens-per-word approximation (4:1 chars-to-tokens is a rough heuristic)
_CHARS_PER_TOKEN_APPROX = 4


def _sha256_to_vector(text: str, dimensions: int) -> list[float]:
    """Derive a deterministic float vector from sha256(text).

    The sha256 digest is interpreted as a sequence of IEEE-754 doubles
    by tiling the digest as needed and using ``struct.unpack_from``.
    The resulting values are in the range [0, 1) after normalisation to
    the unit interval — they are not unit-normalised (L2 = 1) on purpose,
    to keep the code simple and the values obviously non-real.

    The vector is entirely determined by (text, dimensions); no randomness.
    """
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    # Tile the digest to cover all requested dimensions
    tiled = digest * ((dimensions * 2 // len(digest)) + 2)
    vector: list[float] = []
    for i in range(dimensions):
        offset = (i * 2) % (len(tiled) - 1)
        # Unpack one unsigned short (2 bytes) and map to [0, 1)
        (raw,) = struct.unpack_from(">H", tiled, offset)
        vector.append(raw / 65536.0)
    return vector


class FakeProvider(EmbeddingProvider):
    """Deterministic test-only embedding provider.

    **FOR TESTS ONLY.** Not suitable for production use.

    Parameters
    ----------
    dimensions:
        Output vector dimensions (default: 64, small for fast tests).
    model_id:
        Model identifier to declare (default: ``"fake-embed-v1"``).
    fail_on_embed:
        When ``True``, every ``embed_batch`` call raises
        ``ProviderUnavailableError``.
    fail_on_health:
        When ``True``, ``health_check`` returns ``reachable=False``.
    provider_id:
        Provider identifier (default: ``"fake"``).
    """

    def __init__(
        self,
        *,
        dimensions: int = 64,
        model_id: str = "fake-embed-v1",
        fail_on_embed: bool = False,
        fail_on_health: bool = False,
        provider_id: str = "fake",
    ) -> None:
        self._dimensions = dimensions
        self._model_id = model_id
        self._fail_on_embed = fail_on_embed
        self._fail_on_health = fail_on_health
        self._provider_id = provider_id
        self._caps = ProviderCapabilities(
            provider_id=provider_id,
            model_id=model_id,
            vector_dimensions=dimensions,
            max_input_tokens=8192,
            max_batch_size=2048,
            supported_languages="*",
            cross_lingual=True,
            is_local=True,
            cost_per_1k_tokens=None,
            pricing_as_of=None,
            api_version=f"fake-v1-dim{dimensions}",
        )

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._caps

    def embed_batch(self, texts: list[str], model_id: str) -> EmbedBatchResult:
        """Return deterministic sha256-derived vectors (test only).

        Raises
        ------
        ValueError
            If ``model_id`` is wrong or ``texts`` is empty.
        ProviderUnavailableError
            If ``fail_on_embed=True``.
        """
        self._assert_nonempty(texts)
        self._assert_model_id(model_id)

        if self._fail_on_embed:
            raise ProviderUnavailableError(
                f"FakeProvider '{self._provider_id}' is configured to fail on embed.",
                provider_id=self._provider_id,
                model_id=self._model_id,
                attempts=1,
                http_status=503,
            )

        embeddings = [_sha256_to_vector(t, self._dimensions) for t in texts]

        # Approximate token count: chars / 4
        total_chars = sum(len(t) for t in texts)
        tokens = max(1, total_chars // _CHARS_PER_TOKEN_APPROX)

        return EmbedBatchResult(
            embeddings=embeddings,
            model_id=self._model_id,
            input_tokens_used=tokens,
            provider_id=self._provider_id,
        )

    def health_check(self) -> HealthCheckResult:
        """Return a healthy result, or ``reachable=False`` if ``fail_on_health=True``."""
        if self._fail_on_health:
            return HealthCheckResult(
                reachable=False,
                model_available=False,
                latency_ms=0.0,
                declared_dimensions_confirmed=False,
                error=f"FakeProvider '{self._provider_id}' is configured to fail on health check.",
            )

        # Embed the probe and confirm dimensions
        probe_vector = _sha256_to_vector(_PROBE, self._dimensions)
        confirmed = len(probe_vector) == self._dimensions
        return HealthCheckResult(
            reachable=True,
            model_available=True,
            latency_ms=0.1,
            declared_dimensions_confirmed=confirmed,
            error=None,
        )

    def estimate_cost(self, texts: list[str]) -> CostEstimate:
        """Estimate cost — always zero for a local/fake provider."""
        total_chars = sum(len(t) for t in texts)
        tokens = max(0, total_chars // _CHARS_PER_TOKEN_APPROX)
        return CostEstimate(
            estimated_tokens=tokens,
            estimated_cost_usd=Decimal("0.0"),
            basis=(
                "FakeProvider: local/test provider with no real cost. "
                "Token count approximated as chars/4."
            ),
            is_exact=False,
        )
