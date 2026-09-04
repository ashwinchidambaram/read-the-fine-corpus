"""Ollama embedding provider adapter.

Implements provider-abstraction.md §7.2.

Design
------
- Models: ``nomic-embed-text`` (768 dims) or ``bge-m3`` (1024 dims).
  Other models are supported with explicit ``dimensions`` parameter.
- Batch mode: default is single-text calls (OQ-P-3 resolution: default
  single-text for maximum compatibility; opt-in batch mode available).
- API version lifecycle (F-002):
  - ``ProviderCapabilities.api_version`` reads ``"unresolved"`` until
    ``health_check()`` is called for the first time.
  - ``health_check()`` fetches the sha256 digest from ``/api/show`` (OQ-P-4)
    and caches it on the instance.  Subsequent calls to ``capabilities`` return
    the resolved value.
  - The constructor makes NO network calls (pure construction).
- Retry: same exponential-backoff pattern as OpenAIProvider (hand-rolled).
  Providers raise ``_RetryableException`` for ALL non-200 HTTP responses;
  the backoff layer in ``retry_with_backoff`` decides what is retryable (F-001).
- Fail-closed: after max_attempts exhaustion, raises ProviderUnavailableError.
- Air-gap: local provider, so air-gap mode does NOT block it (``is_local=True``).
- Secret-safe: Ollama endpoints may have auth tokens.  The endpoint URL is
  stored but never included in exception messages (§6.2, §14.2).  If auth
  is required, callers pass it via the ``Authorization`` header in
  ``extra_headers`` — those headers are stripped from all log/trace output.

Token counting
--------------
Ollama does not return token counts in the embedding response.  We use the
chars÷4 approximation (marked ``is_exact=False``).
"""

from __future__ import annotations

import logging
import time
from decimal import Decimal

import httpx

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

# Fixed probe string — health check only, carries no document content
_PROBE = "finecorpus-health-probe-v1"

# Approximate chars per token
_CHARS_PER_TOKEN_APPROX = 4

# Known model defaults
_MODEL_DEFAULTS: dict[str, dict] = {
    "nomic-embed-text": {
        "dimensions": 768,
        "supported_languages": ["en"],
        "cross_lingual": False,
    },
    "bge-m3": {
        "dimensions": 1024,
        "supported_languages": "*",
        "cross_lingual": True,
    },
}

# Retryable HTTP statuses (passed to backoff layer; F-001)
_RETRYABLE_STATUSES: frozenset[int] = frozenset({429, 500, 502, 503, 504})

# Default HTTP timeout for Ollama calls (seconds)
_DEFAULT_TIMEOUT = 30.0

# Sentinel value for api_version before health_check() resolves it (F-002)
_API_VERSION_UNRESOLVED = "unresolved"


class OllamaProvider(EmbeddingProvider):
    """Ollama embedding provider adapter (nomic-embed-text, bge-m3, etc.).

    Parameters
    ----------
    base_url:
        Ollama HTTP endpoint (default: ``"http://localhost:11434"``).
        May include auth token in the URL, but that is stripped from all
        logs and exception messages (§6.2, §14.2).
    model_id:
        Ollama model name (default: ``"nomic-embed-text"``).
    dimensions:
        Output vector dimensions.  Auto-detected from known models;
        must be supplied for custom models.
    batch_mode:
        When ``True``, use Ollama's batch embedding endpoint (``/api/embed``
        with multiple prompts).  Default ``False`` (OQ-P-3 resolution).
    backoff_config:
        Retry/backoff configuration.
    _http_client:
        Injected ``httpx.Client`` for testing.  When ``None``, a real client
        is constructed.

    Lifecycle note (F-002)
    ----------------------
    The constructor makes NO network calls.  ``capabilities.api_version``
    reports ``"unresolved"`` until ``health_check()`` is invoked for the
    first time.  After ``health_check()`` completes the resolved digest (or
    ``"unknown"`` when /api/show is unavailable) is cached on the instance
    and ``capabilities.api_version`` reflects it on subsequent reads.
    """

    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        *,
        model_id: str = "nomic-embed-text",
        dimensions: int | None = None,
        batch_mode: bool = False,
        backoff_config: BackoffConfig | None = None,
        _http_client: httpx.Client | None = None,
    ) -> None:
        # Strip trailing slash from base_url for consistent URL construction
        self._base_url = base_url.rstrip("/")
        self._model_id = model_id
        self._batch_mode = batch_mode
        self._backoff_config = backoff_config or BackoffConfig()

        # Resolve model defaults
        model_defaults = _MODEL_DEFAULTS.get(model_id, {})
        resolved_dims = dimensions or model_defaults.get("dimensions")
        if resolved_dims is None:
            raise ValueError(
                f"Unknown model '{model_id}' for OllamaProvider: dimensions must be supplied "
                f"explicitly for models not in the known-model list "
                f"({list(_MODEL_DEFAULTS)})."
            )
        self._dimensions = resolved_dims

        supported_langs = model_defaults.get("supported_languages", "*")
        cross_lingual = model_defaults.get("cross_lingual", False)

        # F-002: constructor is pure — no network call.
        # api_version is "unresolved" until health_check() runs for the first time.
        self._api_version: str = _API_VERSION_UNRESOLVED

        self._caps = ProviderCapabilities(
            provider_id="ollama",
            model_id=model_id,
            vector_dimensions=resolved_dims,
            max_input_tokens=8192,
            max_batch_size=2048 if batch_mode else 1,
            supported_languages=supported_langs,
            cross_lingual=cross_lingual,
            is_local=True,
            cost_per_1k_tokens=None,
            pricing_as_of=None,
            api_version=_API_VERSION_UNRESOLVED,
        )

        # Construct or accept http client
        self._http = _http_client or httpx.Client(
            base_url=self._base_url,
            timeout=_DEFAULT_TIMEOUT,
        )

    def _resolve_api_version(self) -> str:
        """Fetch the model's sha256 digest from Ollama /api/show (OQ-P-4).

        Called lazily by ``health_check()`` on the first invocation.
        Returns ``"unknown"`` if the endpoint is unreachable or the field
        is absent.  The base_url is NEVER included in error messages here
        (it may contain auth tokens — §6.2).
        """
        try:
            resp = self._http.post("/api/show", json={"name": self._model_id})
            if resp.status_code == 200:
                data = resp.json()
                # Ollama returns "details" -> "parent_model" or a top-level "digest"
                digest = data.get("digest") or data.get("model_info", {}).get("digest")
                if digest and isinstance(digest, str):
                    # Truncate to a readable prefix (sha256:abc123...)
                    return digest[:71]  # "sha256:" + 64 hex chars
            return "unknown"
        except Exception:
            return "unknown"

    @property
    def capabilities(self) -> ProviderCapabilities:
        # Return a view that reflects the current api_version (may be "unresolved"
        # before health_check() runs, or the resolved digest afterwards).
        if self._api_version == self._caps.api_version:
            return self._caps
        # Rebuild caps with updated api_version (ProviderCapabilities is frozen)
        self._caps = ProviderCapabilities(
            provider_id=self._caps.provider_id,
            model_id=self._caps.model_id,
            vector_dimensions=self._caps.vector_dimensions,
            max_input_tokens=self._caps.max_input_tokens,
            max_batch_size=self._caps.max_batch_size,
            supported_languages=self._caps.supported_languages,
            cross_lingual=self._caps.cross_lingual,
            is_local=self._caps.is_local,
            cost_per_1k_tokens=self._caps.cost_per_1k_tokens,
            pricing_as_of=self._caps.pricing_as_of,
            api_version=self._api_version,
        )
        return self._caps

    def embed_batch(self, texts: list[str], model_id: str) -> EmbedBatchResult:
        """Embed texts via Ollama with exponential-backoff retry.

        In default mode (``batch_mode=False``), calls ``/api/embed`` once per
        text.  In ``batch_mode=True``, sends the full list in one request.

        Raises
        ------
        ValueError
            Wrong model ID or empty texts.
        ProviderError
            Provider returned fewer embeddings than inputs.
        ProviderUnavailableError
            After max retries are exhausted.
        """
        self._assert_nonempty(texts)
        self._assert_model_id(model_id)

        if self._batch_mode:
            all_embeddings = self._embed_batch_call(texts, model_id)
        else:
            all_embeddings = []
            for text in texts:
                vec = self._embed_single_with_retry(text, model_id)
                all_embeddings.append(vec)

        if len(all_embeddings) != len(texts):
            raise ProviderError(
                f"Ollama returned {len(all_embeddings)} embeddings for {len(texts)} inputs. "
                f"Partial results cannot be safely associated with source texts (§2.2).",
                provider_id="ollama",
                model_id=self._model_id,
            )

        total_chars = sum(len(t) for t in texts)
        tokens = max(1, total_chars // _CHARS_PER_TOKEN_APPROX)

        return EmbedBatchResult(
            embeddings=all_embeddings,
            model_id=model_id,
            input_tokens_used=tokens,
            provider_id="ollama",
        )

    def _embed_single_with_retry(self, text: str, model_id: str) -> list[float]:
        """Embed a single text with backoff retry."""

        def _attempt() -> list[float]:
            try:
                resp = self._http.post(
                    "/api/embed",
                    json={"model": model_id, "input": text},
                )
            except (httpx.ConnectError, httpx.TimeoutException) as exc:
                # Raise with http_status=503; backoff layer decides if retryable.
                # Sever the exception chain — exc may contain endpoint URL with auth.
                err = _RetryableException(
                    f"Ollama connection error during embed (type={type(exc).__name__}).",
                    http_status=503,
                    retry_after_seconds=None,
                )
                err.__context__ = None
                raise err from None

            if resp.status_code != 200:
                # Raise for all non-200; backoff layer classifies retryable (F-001).
                retry_after = parse_retry_after(resp.headers.get("Retry-After"))
                raise _RetryableException(
                    f"Ollama HTTP {resp.status_code} during embed.",
                    http_status=resp.status_code,
                    retry_after_seconds=retry_after,
                )

            data = resp.json()
            # Ollama /api/embed returns {"embeddings": [[...]]} (list of lists)
            embeddings = data.get("embeddings", [])
            if not embeddings or not isinstance(embeddings, list):
                raise ProviderError(
                    "Ollama response missing 'embeddings' field.",
                    provider_id="ollama",
                    model_id=model_id,
                )
            return embeddings[0]

        return retry_with_backoff(
            operation="embed_single",
            provider_id="ollama",
            model_id=model_id,
            call=_attempt,
            config=self._backoff_config,
            retryable_status_codes=_RETRYABLE_STATUSES,
        )

    def _embed_batch_call(self, texts: list[str], model_id: str) -> list[list[float]]:
        """Embed all texts in one Ollama /api/embed batch call with retry."""

        def _attempt() -> list[list[float]]:
            try:
                resp = self._http.post(
                    "/api/embed",
                    json={"model": model_id, "input": texts},
                )
            except (httpx.ConnectError, httpx.TimeoutException) as exc:
                err = _RetryableException(
                    f"Ollama connection error during batch embed (type={type(exc).__name__}).",
                    http_status=503,
                    retry_after_seconds=None,
                )
                err.__context__ = None
                raise err from None

            if resp.status_code != 200:
                # Raise for all non-200; backoff layer classifies retryable (F-001).
                retry_after = parse_retry_after(resp.headers.get("Retry-After"))
                raise _RetryableException(
                    f"Ollama HTTP {resp.status_code} during batch embed.",
                    http_status=resp.status_code,
                    retry_after_seconds=retry_after,
                )

            data = resp.json()
            embeddings = data.get("embeddings", [])
            if not isinstance(embeddings, list) or len(embeddings) != len(texts):
                raise ProviderError(
                    f"Ollama batch embed: expected {len(texts)} embeddings, got {len(embeddings)}.",
                    provider_id="ollama",
                    model_id=model_id,
                )
            return embeddings

        return retry_with_backoff(
            operation="embed_batch",
            provider_id="ollama",
            model_id=model_id,
            call=_attempt,
            config=self._backoff_config,
            retryable_status_codes=_RETRYABLE_STATUSES,
        )

    def health_check(self) -> HealthCheckResult:
        """Probe Ollama with a fixed string; confirm model availability and dimensions (§2.2).

        On the first call, also resolves and caches ``api_version`` from
        ``/api/show`` (F-002).  Subsequent calls use the cached value.
        """
        # F-002: resolve api_version lazily on first health_check()
        if self._api_version == _API_VERSION_UNRESOLVED:
            self._api_version = self._resolve_api_version()

        start = time.monotonic()
        try:
            resp = self._http.post(
                "/api/embed",
                json={"model": self._model_id, "input": _PROBE},
            )
            latency = (time.monotonic() - start) * 1000

            if resp.status_code != 200:
                return HealthCheckResult(
                    reachable=True,
                    model_available=False,
                    latency_ms=latency,
                    declared_dimensions_confirmed=False,
                    error=f"Ollama returned HTTP {resp.status_code} for health probe.",
                )

            data = resp.json()
            embeddings = data.get("embeddings", [])
            if not embeddings or not isinstance(embeddings, list) or not embeddings[0]:
                return HealthCheckResult(
                    reachable=True,
                    model_available=False,
                    latency_ms=latency,
                    declared_dimensions_confirmed=False,
                    error="Ollama health probe returned empty embeddings.",
                )

            probe_vec = embeddings[0]
            dims_ok = len(probe_vec) == self._dimensions
            if not dims_ok:
                logger.error(
                    "Ollama health_check: declared dimensions=%d but probe returned %d. "
                    "Fatal configuration error.",
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
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            latency = (time.monotonic() - start) * 1000
            return HealthCheckResult(
                reachable=False,
                model_available=False,
                latency_ms=latency,
                declared_dimensions_confirmed=False,
                # Keep error message generic — do NOT include base_url (may have auth)
                error=f"Ollama endpoint not reachable (type={type(exc).__name__}).",
            )
        except Exception as exc:
            latency = (time.monotonic() - start) * 1000
            return HealthCheckResult(
                reachable=False,
                model_available=False,
                latency_ms=latency,
                declared_dimensions_confirmed=False,
                error=f"Unexpected error during Ollama health probe: {type(exc).__name__}.",
            )

    def estimate_cost(self, texts: list[str]) -> CostEstimate:
        """Estimate cost — always zero for a local provider."""
        total_chars = sum(len(t) for t in texts)
        tokens = max(0, total_chars // _CHARS_PER_TOKEN_APPROX)
        return CostEstimate(
            estimated_tokens=tokens,
            estimated_cost_usd=Decimal("0.0"),
            basis=(
                f"Ollama local provider: no cost. "
                f"Token count approximated as chars÷{_CHARS_PER_TOKEN_APPROX}."
            ),
            is_exact=False,
        )
