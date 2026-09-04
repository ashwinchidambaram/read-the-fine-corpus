"""OpenAI embedding provider adapter.

Implements provider-abstraction.md §7.1.

Design
------
- Model: ``text-embedding-3-small`` (default, 1536 dims) or
  ``text-embedding-3-large`` (3072 dims).
- Batch limit: 2048 texts per API call (OpenAI API limit).
- Retry: exponential backoff with jitter on 429/5xx (hand-rolled via
  ``_backoff.retry_with_backoff``).  Bounded attempts; raises
  ``ProviderUnavailableError`` after exhaustion.  NO model fallback ever.
- Rate-limit surfacing: ``Retry-After`` header from 429 responses is carried
  through to ``ProviderUnavailableError.retry_after_seconds`` (§15).
- Cost estimation: ``cost_per_1k_tokens * (chars/4)`` approximation when
  ``cost_per_1k_tokens`` is configured; marks ``is_exact=False``.
- Secrets: API key sourced exclusively from config/env.  The ``openai.OpenAI``
  client is constructed with the key but it is NEVER stored as an instance
  attribute, NEVER included in ``repr()`` or log lines, and NEVER placed in
  exception messages or exception chains (§6.2, §14.2, §18.3 test 10).
- Air-gap: if ``RTFC_AIRGAP=true`` is set, ``health_check`` fails before
  making any network call.

Token counting
--------------
The tiktoken library is not a hard dependency (heavy package; adds ~40 MB).
When available it is used for more accurate counting; when absent, a
chars-÷-4 approximation is used and ``is_exact=False``.
"""

from __future__ import annotations

import logging
import os
import time
from decimal import Decimal
from typing import Any

from finecorpus.embedding._backoff import (
    BackoffConfig,
    _RetryableException,
    parse_retry_after,
    retry_with_backoff,
)
from finecorpus.embedding.base import (
    CostEstimate,
    EmbedBatchResult,
    EmbeddingProvider,
    HealthCheckResult,
    ProviderCapabilities,
    ProviderError,
)

logger = logging.getLogger(__name__)

# Fixed probe string used by health_check only — carries no document content
_PROBE = "finecorpus-health-probe-v1"

# Approximate chars per token for cost estimation when tiktoken is unavailable
_CHARS_PER_TOKEN_APPROX = 4

# Pricing as of 2026-09 (text-embedding-3-small: $0.020 per 1M tokens = $0.00002 per 1k)
_DEFAULT_COST_PER_1K: dict[str, Decimal] = {
    "text-embedding-3-small": Decimal("0.00002"),
    "text-embedding-3-large": Decimal("0.00013"),
}
_DEFAULT_PRICING_AS_OF = "2026-09-03"

# Known model dimensions
_MODEL_DIMENSIONS: dict[str, int] = {
    "text-embedding-3-small": 1536,
    "text-embedding-3-large": 3072,
}

# OpenAI batch limit
_OPENAI_MAX_BATCH = 2048

# Retryable HTTP statuses
_RETRYABLE_STATUSES: frozenset[int] = frozenset({429, 500, 502, 503, 504})


def _count_tokens_approx(texts: list[str]) -> int:
    """Approximate token count using chars/4 heuristic (±~5%)."""
    return max(1, sum(len(t) for t in texts) // _CHARS_PER_TOKEN_APPROX)


def _try_tiktoken_count(model_id: str, texts: list[str]) -> int | None:
    """Attempt to use tiktoken for exact token counting.

    Returns ``None`` if tiktoken is not installed.
    """
    try:
        import tiktoken  # type: ignore[import]

        enc = tiktoken.encoding_for_model(model_id)
        return sum(len(enc.encode(t)) for t in texts)
    except Exception:  # ImportError or tiktoken model not found
        return None


class OpenAIProvider(EmbeddingProvider):
    """OpenAI embedding provider adapter (text-embedding-3-* models).

    Parameters
    ----------
    api_key:
        OpenAI API key.  Read from config/env ONLY; never stored in repr,
        logs, or exceptions (§6.2, §14.2).
    model_id:
        Model identifier (default: ``"text-embedding-3-small"``).
    dimensions:
        Expected output dimensions (must match model).
    max_batch_size:
        Maximum texts per API call (default: 2048).
    cost_per_1k_tokens:
        Cost in USD per 1 000 tokens.
    pricing_as_of:
        ISO-8601 date when pricing was last verified.
    backoff_config:
        Retry/backoff configuration.
    _openai_client:
        Injected for testing.  When ``None`` (production), a real
        ``openai.OpenAI`` client is constructed from ``api_key``.
    """

    def __init__(
        self,
        api_key: str,
        *,
        model_id: str = "text-embedding-3-small",
        dimensions: int | None = None,
        max_batch_size: int = _OPENAI_MAX_BATCH,
        cost_per_1k_tokens: Decimal | None = None,
        pricing_as_of: str | None = None,
        backoff_config: BackoffConfig | None = None,
        _openai_client: Any | None = None,
    ) -> None:
        # Resolve dimensions from model map if not supplied
        resolved_dims = dimensions or _MODEL_DIMENSIONS.get(model_id)
        if resolved_dims is None:
            raise ValueError(
                f"Unknown model '{model_id}' for OpenAIProvider: dimensions must be supplied "
                f"explicitly for non-standard models."
            )

        self._model_id = model_id
        self._dimensions = resolved_dims
        self._max_batch_size = max_batch_size
        self._cost_per_1k = cost_per_1k_tokens or _DEFAULT_COST_PER_1K.get(model_id)
        self._pricing_as_of = pricing_as_of or _DEFAULT_PRICING_AS_OF
        self._backoff_config = backoff_config or BackoffConfig()

        # Build or accept the openai client.
        # The key is stored inside the client object's internals, not on self.
        # We keep only a reference to the client, never to the raw key.
        # Typed as Any to avoid importing openai at module level (heavy dependency).
        self._client: Any
        if _openai_client is not None:
            self._client = _openai_client
        else:
            self._client = self._build_client(api_key)

        self._caps = ProviderCapabilities(
            provider_id="openai",
            model_id=model_id,
            vector_dimensions=resolved_dims,
            max_input_tokens=8191,  # OpenAI limit for text-embedding-3-*
            max_batch_size=max_batch_size,
            supported_languages="*",
            cross_lingual=True,
            is_local=False,
            cost_per_1k_tokens=self._cost_per_1k,
            pricing_as_of=self._pricing_as_of,
            api_version="openai-v1",  # OpenAI does not surface a per-model hash
        )

    @staticmethod
    def _build_client(api_key: str) -> object:
        """Construct an ``openai.OpenAI`` client.

        Separated into a staticmethod so the constructor does not hold a
        reference to the raw key beyond this call.
        """
        try:
            from openai import OpenAI  # type: ignore[import]

            return OpenAI(api_key=api_key)
        except ImportError as exc:
            raise ImportError(
                "The 'openai' package is required for OpenAIProvider. "
                "Install it with: uv add openai"
            ) from exc

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._caps

    def embed_batch(self, texts: list[str], model_id: str) -> EmbedBatchResult:
        """Embed texts via the OpenAI API with exponential-backoff retry.

        Splits into sub-batches up to ``max_batch_size`` internally.

        Raises
        ------
        ValueError
            Wrong model ID or empty texts.
        ProviderError
            Provider returned fewer embeddings than inputs.
        ProviderUnavailableError
            After ``backoff_config.max_attempts`` retries are exhausted.
        """
        self._assert_nonempty(texts)
        self._assert_model_id(model_id)

        # Check air-gap before any network call
        if os.environ.get("RTFC_AIRGAP", "").lower() in {"1", "true", "yes"}:
            from finecorpus.embedding.base import ProviderUnavailableError

            raise ProviderUnavailableError(
                "OpenAIProvider: air-gap mode is enabled (RTFC_AIRGAP). "
                "Cloud providers are blocked. Use a local provider (is_local=True).",
                provider_id="openai",
                model_id=self._model_id,
                attempts=0,
            )

        all_embeddings: list[list[float]] = []
        total_tokens = 0

        # Split into batches
        for i in range(0, len(texts), self._max_batch_size):
            chunk = texts[i : i + self._max_batch_size]
            result = self._embed_chunk_with_retry(chunk, model_id)
            all_embeddings.extend(result["embeddings"])
            total_tokens += result["tokens"]

        # Final sanity check
        if len(all_embeddings) != len(texts):
            raise ProviderError(
                f"OpenAI returned {len(all_embeddings)} embeddings for {len(texts)} inputs. "
                f"Partial results cannot be safely associated with source texts (§2.2).",
                provider_id="openai",
                model_id=self._model_id,
            )

        return EmbedBatchResult(
            embeddings=all_embeddings,
            model_id=model_id,
            input_tokens_used=total_tokens,
            provider_id="openai",
        )

    def _embed_chunk_with_retry(self, texts: list[str], model_id: str) -> dict:
        """Call the OpenAI embed API for one batch with backoff retry.

        Providers raise ``_RetryableException`` for ALL non-200 HTTP responses;
        the backoff layer in ``retry_with_backoff`` decides whether the status
        code is retryable (F-001).
        """

        def _attempt() -> dict:
            try:
                resp = self._client.embeddings.create(model=model_id, input=texts)  # type: ignore[attr-defined]
            except Exception as exc:
                # Classify the exception — never expose the raw exc chain
                # (it may contain auth headers).
                status, retry_after = _classify_openai_error(exc)
                # Raise for ALL non-200 statuses; backoff layer classifies
                # retryable vs non-retryable (F-001).
                err = _RetryableException(
                    f"OpenAI API error HTTP {status} (redacted for secret safety).",
                    http_status=status,
                    retry_after_seconds=retry_after,
                )
                err.__context__ = None  # sever chain — exc may contain auth data
                raise err from None

            embeddings = [item.embedding for item in resp.data]
            if len(embeddings) != len(texts):
                raise ProviderError(
                    f"OpenAI returned {len(embeddings)} embeddings for {len(texts)} inputs.",
                    provider_id="openai",
                    model_id=model_id,
                )
            tokens = resp.usage.total_tokens if resp.usage else _count_tokens_approx(texts)
            return {"embeddings": embeddings, "tokens": tokens}

        return retry_with_backoff(
            operation="embed_batch",
            provider_id="openai",
            model_id=model_id,
            call=_attempt,
            config=self._backoff_config,
            retryable_status_codes=_RETRYABLE_STATUSES,
        )

    def health_check(self) -> HealthCheckResult:
        """Probe OpenAI with a fixed string; confirm dimensions (§2.2)."""
        if os.environ.get("RTFC_AIRGAP", "").lower() in {"1", "true", "yes"}:
            return HealthCheckResult(
                reachable=False,
                model_available=False,
                latency_ms=0.0,
                declared_dimensions_confirmed=False,
                error=(
                    "Air-gap mode enabled (RTFC_AIRGAP). Cloud providers are blocked. "
                    "provider-abstraction.md §2.2."
                ),
            )

        start = time.monotonic()
        try:
            resp = self._client.embeddings.create(  # type: ignore[attr-defined]
                model=self._model_id,
                input=[_PROBE],
            )
            latency = (time.monotonic() - start) * 1000

            probe_vec = resp.data[0].embedding
            dims_ok = len(probe_vec) == self._dimensions
            if not dims_ok:
                logger.error(
                    "OpenAI health_check: declared dimensions=%d but probe returned %d. "
                    "This is a fatal configuration error.",
                    self._dimensions,
                    len(probe_vec),
                )
            return HealthCheckResult(
                reachable=True,
                model_available=True,
                latency_ms=latency,
                declared_dimensions_confirmed=dims_ok,
                error=(
                    None
                    if dims_ok
                    else (f"Dimension mismatch: declared {self._dimensions}, got {len(probe_vec)}.")
                ),
            )
        except Exception as exc:
            latency = (time.monotonic() - start) * 1000
            status, _ = _classify_openai_error(exc)
            # Sanitise: never put exc string in the result (may contain auth context)
            return HealthCheckResult(
                reachable=False,
                model_available=False,
                latency_ms=latency,
                declared_dimensions_confirmed=False,
                error=f"OpenAI API error HTTP {status} during health probe (details redacted).",
            )

    def estimate_cost(self, texts: list[str]) -> CostEstimate:
        """Estimate cost using tiktoken (when available) or chars/4 heuristic."""
        tiktoken_count = _try_tiktoken_count(self._model_id, texts)
        if tiktoken_count is not None:
            total_tokens = tiktoken_count
            basis = f"tiktoken exact count for model '{self._model_id}'."
            is_exact = True
        else:
            total_tokens = _count_tokens_approx(texts)
            basis = (
                f"Character-count approximation (chars ÷ {_CHARS_PER_TOKEN_APPROX}), ±~5%. "
                f"Install 'tiktoken' for exact counts."
            )
            is_exact = False

        if self._cost_per_1k is not None:
            cost = Decimal(str(total_tokens)) / Decimal("1000") * self._cost_per_1k
        else:
            cost = Decimal("0")
            basis += " Cost per 1k tokens not configured; estimate is $0."
            is_exact = False

        return CostEstimate(
            estimated_tokens=total_tokens,
            estimated_cost_usd=cost,
            basis=basis,
            is_exact=is_exact,
        )


# ---------------------------------------------------------------------------
# Error classification helper (secret-safe)
# ---------------------------------------------------------------------------


def _classify_openai_error(exc: Exception) -> tuple[int, float | None]:
    """Classify an openai exception and extract HTTP status + Retry-After.

    Returns ``(http_status, retry_after_seconds)``.

    The ``exc`` chain is intentionally NOT forwarded to callers; it may
    contain auth headers from the response (§6.2, §14.2).
    """
    # Try to extract status from openai SDK exception types
    status = 0
    retry_after: float | None = None

    exc_type = type(exc).__name__
    # openai.APIStatusError (and subclasses) have .status_code
    if hasattr(exc, "status_code"):
        status = int(exc.status_code)
    elif hasattr(exc, "response") and hasattr(exc.response, "status_code"):
        status = int(exc.response.status_code)

    # Try to extract Retry-After from headers (may not be present)
    if hasattr(exc, "response") and hasattr(exc.response, "headers"):
        retry_after = parse_retry_after(exc.response.headers.get("Retry-After"))

    if status == 0:
        # Connection error or unknown — treat as 503
        status = 503
        logger.debug("OpenAI error type '%s' had no status_code; using 503.", exc_type)

    return status, retry_after
