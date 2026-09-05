"""Ollama internal LLM provider adapter (M-037 local model support).

Implements provider-abstraction.md §4 using the Ollama /api/chat endpoint
with JSON format mode.

Design
------
- Model: any Ollama-hosted model (llama3.1, mistral, gemma, etc.).
- JSON enforcement: uses ``format: "json"`` in the Ollama chat request.
  This is the lowest-common-denominator approach — supported by all recent
  Ollama versions.
- Retry: exponential backoff with jitter on connection errors / 5xx
  (hand-rolled via ``_backoff.llm_retry_with_backoff``).
- Air-gap: local provider, so air-gap mode does NOT block it (``is_local=True``).
- Secret-safe: Ollama endpoints may include auth tokens in the URL.  The URL
  is stored but NEVER included in exception messages (§6.2, §14.2).
- Constructor makes NO network calls (pure construction).
"""

from __future__ import annotations

import logging
import time
from decimal import Decimal
from typing import Any

import httpx
from pydantic import BaseModel

from finecorpus.llm._backoff import (
    LLMBackoffConfig,
    _LLMRetryableException,
    llm_retry_with_backoff,
    parse_retry_after,
)
from finecorpus.llm.base import (
    LLMCostEstimate,
    LLMHealthCheckResult,
    LLMProvider,
    LLMProviderCapabilities,
    LLMProviderError,
    LLMRawResult,
)

logger = logging.getLogger(__name__)

# Fixed probe — health check only, carries no document content
_PROBE_SYSTEM = 'You are a health-check assistant.  Respond with: {"ok": true}'
_PROBE_USER = "Health check."

# Approximate chars per token
_CHARS_PER_TOKEN_APPROX = 4

# Default HTTP timeout for Ollama calls (seconds)
_DEFAULT_TIMEOUT = 120.0  # LLM calls are slower than embedding

# Retryable HTTP statuses
_RETRYABLE_STATUSES: frozenset[int] = frozenset({429, 500, 502, 503, 504})

# Default ceiling; callers pass max_output_tokens explicitly
_DEFAULT_MAX_OUTPUT_TOKENS = 1024


class OllamaLLMProvider(LLMProvider):
    """Ollama Chat LLM provider adapter.

    Parameters
    ----------
    base_url:
        Ollama HTTP endpoint (default: ``"http://localhost:11434"``).
        May include auth token in the URL, but that is stripped from all
        logs and exception messages (§6.2, §14.2).
    model_id:
        Ollama model name (default: ``"llama3.1"``).
    max_output_tokens:
        Default output token ceiling passed to Ollama's ``num_predict``.
    backoff_config:
        Retry/backoff configuration.
    _http_client:
        Injected ``httpx.Client`` for testing.  When ``None``, a real client
        is constructed.
    """

    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        *,
        model_id: str = "llama3.1",
        max_output_tokens: int = _DEFAULT_MAX_OUTPUT_TOKENS,
        backoff_config: LLMBackoffConfig | None = None,
        _http_client: httpx.Client | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model_id = model_id
        self._max_output_tokens = max_output_tokens
        self._backoff_config = backoff_config or LLMBackoffConfig()

        self._caps = LLMProviderCapabilities(
            provider_id="ollama",
            model_id=model_id,
            supports_json_schema=False,  # Ollama uses format="json", no schema enforcement
            is_local=True,
            max_output_tokens=max_output_tokens,
            cost_per_1k_input_tokens=None,
            cost_per_1k_output_tokens=None,
            pricing_as_of=None,
            api_version="unresolved",  # resolved by health_check
        )

        self._http = _http_client or httpx.Client(
            base_url=self._base_url,
            timeout=_DEFAULT_TIMEOUT,
        )

        # Resolved on first health_check() call (F-002 pattern from embedding)
        self._api_version: str = "unresolved"

    @property
    def capabilities(self) -> LLMProviderCapabilities:
        if self._api_version == self._caps.api_version:
            return self._caps
        # Rebuild caps with updated api_version (frozen dataclass)
        self._caps = LLMProviderCapabilities(
            provider_id=self._caps.provider_id,
            model_id=self._caps.model_id,
            supports_json_schema=self._caps.supports_json_schema,
            is_local=self._caps.is_local,
            max_output_tokens=self._caps.max_output_tokens,
            cost_per_1k_input_tokens=self._caps.cost_per_1k_input_tokens,
            cost_per_1k_output_tokens=self._caps.cost_per_1k_output_tokens,
            pricing_as_of=self._caps.pricing_as_of,
            api_version=self._api_version,
        )
        return self._caps

    def _resolve_api_version(self) -> str:
        """Fetch model sha256 digest from Ollama /api/show (OQ-P-4 pattern)."""
        try:
            resp = self._http.post("/api/show", json={"name": self._model_id})
            if resp.status_code == 200:
                data = resp.json()
                digest = data.get("digest") or data.get("model_info", {}).get("digest")
                if digest and isinstance(digest, str):
                    return digest[:71]
            return "unknown"
        except Exception:
            return "unknown"

    def generate_json(
        self,
        system: str,
        user: str,
        schema: type[BaseModel],
        temperature: float,
        max_output_tokens: int,
    ) -> LLMRawResult:
        """Call Ollama /api/chat with format="json" and return raw JSON text.

        Raises
        ------
        LLMProviderUnavailableError
            After max retries.
        LLMProviderError
            Non-retryable API error.
        """
        return self._call_with_retry(
            system=system,
            user=user,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
        )

    def _call_with_retry(
        self,
        system: str,
        user: str,
        temperature: float,
        max_output_tokens: int,
    ) -> LLMRawResult:
        """Internal: call Ollama /api/chat with backoff retry."""

        def _attempt() -> LLMRawResult:
            # Sentinel pattern: collect error info inside the except block, then
            # raise OUTSIDE it so Python does not re-attach the original httpx
            # exception (which may contain the URL including any auth token) as
            # __context__ on the _LLMRetryableException (§6.2, §14.2).
            _conn_err_type: str | None = None

            try:
                payload: dict[str, Any] = {
                    "model": self._model_id,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "format": "json",
                    "stream": False,
                    "options": {
                        "temperature": temperature,
                        "num_predict": max_output_tokens,
                    },
                }
                resp = self._http.post("/api/chat", json=payload)
            except (httpx.ConnectError, httpx.TimeoutException) as exc:
                _conn_err_type = type(exc).__name__

            # Raise OUTSIDE the except block — exception chain is clean.
            if _conn_err_type is not None:
                raise _LLMRetryableException(
                    f"Ollama connection error during generate_json (type={_conn_err_type}).",
                    http_status=503,
                    retry_after_seconds=None,
                )

            if resp.status_code != 200:
                retry_after = parse_retry_after(resp.headers.get("Retry-After"))
                raise _LLMRetryableException(
                    f"Ollama HTTP {resp.status_code} during generate_json.",
                    http_status=resp.status_code,
                    retry_after_seconds=retry_after,
                )

            data = resp.json()
            raw_json = data.get("message", {}).get("content", "")
            if not raw_json:
                raise LLMProviderError(
                    "Ollama response missing message content.",
                    provider_id="ollama",
                    model_id=self._model_id,
                )

            # Token usage (Ollama returns prompt_eval_count / eval_count)
            input_tokens = data.get("prompt_eval_count", 0)
            output_tokens = data.get("eval_count", 0)
            if input_tokens == 0:
                total_chars = len(system) + len(user)
                input_tokens = max(1, total_chars // _CHARS_PER_TOKEN_APPROX)

            return LLMRawResult(
                raw_json=raw_json,
                model_id=self._model_id,
                input_tokens_used=input_tokens,
                output_tokens_used=output_tokens,
                provider_id="ollama",
            )

        return llm_retry_with_backoff(
            operation="generate_json",
            provider_id="ollama",
            model_id=self._model_id,
            call=_attempt,
            config=self._backoff_config,
            retryable_status_codes=_RETRYABLE_STATUSES,
        )

    def health_check(self) -> LLMHealthCheckResult:
        """Probe Ollama with a fixed payload; confirm model availability."""
        if self._api_version == "unresolved":
            self._api_version = self._resolve_api_version()

        start = time.monotonic()
        try:
            payload: dict[str, Any] = {
                "model": self._model_id,
                "messages": [
                    {"role": "system", "content": _PROBE_SYSTEM},
                    {"role": "user", "content": _PROBE_USER},
                ],
                "format": "json",
                "stream": False,
                "options": {"temperature": 0.0, "num_predict": 16},
            }
            resp = self._http.post("/api/chat", json=payload)
            latency = (time.monotonic() - start) * 1000

            if resp.status_code != 200:
                return LLMHealthCheckResult(
                    reachable=True,
                    model_available=False,
                    latency_ms=latency,
                    error=f"Ollama returned HTTP {resp.status_code} for health probe.",
                )

            data = resp.json()
            content = data.get("message", {}).get("content", "")
            if not content:
                return LLMHealthCheckResult(
                    reachable=True,
                    model_available=False,
                    latency_ms=latency,
                    error="Ollama health probe returned empty response content.",
                )

            return LLMHealthCheckResult(
                reachable=True,
                model_available=True,
                latency_ms=latency,
                error=None,
            )
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            latency = (time.monotonic() - start) * 1000
            return LLMHealthCheckResult(
                reachable=False,
                model_available=False,
                latency_ms=latency,
                error=f"Ollama endpoint not reachable (type={type(exc).__name__}).",
            )
        except Exception as exc:
            latency = (time.monotonic() - start) * 1000
            return LLMHealthCheckResult(
                reachable=False,
                model_available=False,
                latency_ms=latency,
                error=f"Unexpected error during Ollama LLM health probe: {type(exc).__name__}.",
            )

    def estimate_cost(self, requests: list[dict[str, Any]]) -> LLMCostEstimate:
        """Estimate cost — always zero for a local provider."""
        total_chars = sum(len(r.get("system", "")) + len(r.get("user", "")) for r in requests)
        tokens = max(0, total_chars // _CHARS_PER_TOKEN_APPROX)
        return LLMCostEstimate(
            estimated_input_tokens=tokens,
            estimated_output_tokens=tokens // 4,
            estimated_cost_usd=Decimal("0.0"),
            basis=(
                f"Ollama local provider: no cost.  "
                f"Token count approximated as chars÷{_CHARS_PER_TOKEN_APPROX}."
            ),
            is_exact=False,
        )
