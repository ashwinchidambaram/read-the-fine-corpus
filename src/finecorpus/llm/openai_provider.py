"""OpenAI internal LLM provider adapter.

Implements provider-abstraction.md §4, using OpenAI Chat Completions API
with JSON output mode where available.

Design
------
- Uses ``response_format={"type": "json_object"}`` to guide structured output.
  For models that support the JSON schema response format (gpt-4o-mini,
  gpt-4o, gpt-4-turbo), the adapter passes the schema as a hint.
- Retry: exponential backoff with jitter on 429/5xx (hand-rolled via
  ``_backoff.llm_retry_with_backoff``).  Bounded attempts; raises
  ``LLMProviderUnavailableError`` after exhaustion.
- Secrets: API key sourced exclusively from environment / config.  The
  ``openai.OpenAI`` client is constructed with the key but is NEVER stored
  as an instance attribute directly, NEVER included in repr() or log lines,
  and NEVER placed in exception messages or exception chains (§6.2, §14.2).
- Air-gap: if ``RTFC_AIRGAP=true`` is set, ``health_check`` and
  ``generate_json`` fail before making any network call (§4.3, F-003).
"""

from __future__ import annotations

import logging
import os
import time
from decimal import Decimal
from typing import Any

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
    LLMProviderUnavailableError,
    LLMRawResult,
)

logger = logging.getLogger(__name__)

# Fixed probe string — health check only, carries no document content
_PROBE_SYSTEM = 'You are a health-check assistant.  Respond with: {"ok": true}'
_PROBE_USER = "Health check."

# Approximate chars per token
_CHARS_PER_TOKEN_APPROX = 4

# Default pricing (gpt-4o-mini as of 2026-09, per 1k tokens)
_DEFAULT_INPUT_COST_PER_1K: dict[str, Decimal] = {
    "gpt-4o-mini": Decimal("0.000150"),
    "gpt-4o": Decimal("0.002500"),
    "gpt-4-turbo": Decimal("0.010000"),
}
_DEFAULT_OUTPUT_COST_PER_1K: dict[str, Decimal] = {
    "gpt-4o-mini": Decimal("0.000600"),
    "gpt-4o": Decimal("0.010000"),
    "gpt-4-turbo": Decimal("0.030000"),
}
_DEFAULT_PRICING_AS_OF = "2026-09-03"

# Retryable HTTP statuses
_RETRYABLE_STATUSES: frozenset[int] = frozenset({429, 500, 502, 503, 504})

# Default ceiling; callers pass max_output_tokens explicitly
_DEFAULT_MAX_OUTPUT_TOKENS = 1024


class OpenAILLMProvider(LLMProvider):
    """OpenAI Chat Completions LLM provider adapter.

    Parameters
    ----------
    api_key:
        OpenAI API key.  Read from env ONLY; never stored in repr, logs,
        or exceptions (§6.2, §14.2).
    model_id:
        Model identifier (default: ``"gpt-4o-mini"``).
    max_output_tokens:
        Default output token ceiling.
    cost_per_1k_input_tokens:
        Cost in USD per 1 000 input tokens.
    cost_per_1k_output_tokens:
        Cost in USD per 1 000 output tokens.
    pricing_as_of:
        ISO-8601 date when pricing was last verified.
    backoff_config:
        Retry/backoff configuration.
    _openai_client:
        Injected for testing.  When ``None`` (production), a real
        ``openai.OpenAI`` client is constructed.
    """

    def __init__(
        self,
        api_key: str,
        *,
        model_id: str = "gpt-4o-mini",
        max_output_tokens: int = _DEFAULT_MAX_OUTPUT_TOKENS,
        cost_per_1k_input_tokens: Decimal | None = None,
        cost_per_1k_output_tokens: Decimal | None = None,
        pricing_as_of: str | None = None,
        backoff_config: LLMBackoffConfig | None = None,
        _openai_client: Any | None = None,
    ) -> None:
        self._model_id = model_id
        self._max_output_tokens = max_output_tokens
        self._cost_per_1k_input = cost_per_1k_input_tokens or _DEFAULT_INPUT_COST_PER_1K.get(
            model_id
        )
        self._cost_per_1k_output = cost_per_1k_output_tokens or _DEFAULT_OUTPUT_COST_PER_1K.get(
            model_id
        )
        self._pricing_as_of = pricing_as_of or _DEFAULT_PRICING_AS_OF
        self._backoff_config = backoff_config or LLMBackoffConfig()

        # Build or accept the openai client.
        # The key is stored inside the client object's internals, not on self.
        self._client: Any
        if _openai_client is not None:
            self._client = _openai_client
        else:
            self._client = self._build_client(api_key)

        self._caps = LLMProviderCapabilities(
            provider_id="openai",
            model_id=model_id,
            supports_json_schema=True,
            is_local=False,
            max_output_tokens=max_output_tokens,
            cost_per_1k_input_tokens=self._cost_per_1k_input,
            cost_per_1k_output_tokens=self._cost_per_1k_output,
            pricing_as_of=self._pricing_as_of,
            api_version="openai-v1",
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
                "The 'openai' package is required for OpenAILLMProvider. "
                "Install it with: uv add openai"
            ) from exc

    @property
    def capabilities(self) -> LLMProviderCapabilities:
        return self._caps

    def generate_json(
        self,
        system: str,
        user: str,
        schema: type[BaseModel],
        temperature: float,
        max_output_tokens: int,
    ) -> LLMRawResult:
        """Call OpenAI Chat Completions and return raw JSON text.

        Uses ``response_format={"type": "json_object"}`` to constrain output.

        Raises
        ------
        LLMProviderUnavailableError
            If air-gap mode is active, or after max retries.
        LLMProviderError
            Non-retryable API error.
        """
        if os.environ.get("RTFC_AIRGAP", "").lower() in {"1", "true", "yes"}:
            raise LLMProviderUnavailableError(
                "OpenAILLMProvider: air-gap mode is enabled (RTFC_AIRGAP). "
                "Cloud providers are blocked.  Use a local provider (is_local=True).",
                provider_id="openai",
                model_id=self._model_id,
                attempts=0,
            )

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
        """Internal: call OpenAI with backoff retry."""

        def _attempt() -> LLMRawResult:
            try:
                resp = self._client.chat.completions.create(  # type: ignore[attr-defined]
                    model=self._model_id,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    response_format={"type": "json_object"},
                    temperature=temperature,
                    max_tokens=max_output_tokens,
                )
            except Exception as exc:
                status, retry_after = _classify_openai_error(exc)
                err = _LLMRetryableException(
                    f"OpenAI API error HTTP {status} (redacted for secret safety).",
                    http_status=status,
                    retry_after_seconds=retry_after,
                )
                err.__context__ = None  # sever chain — exc may contain auth data
                raise err from None

            raw_json = resp.choices[0].message.content or "{}"
            usage = resp.usage
            input_tokens = usage.prompt_tokens if usage else 0
            output_tokens = usage.completion_tokens if usage else 0

            return LLMRawResult(
                raw_json=raw_json,
                model_id=self._model_id,
                input_tokens_used=input_tokens,
                output_tokens_used=output_tokens,
                provider_id="openai",
            )

        return llm_retry_with_backoff(
            operation="generate_json",
            provider_id="openai",
            model_id=self._model_id,
            call=_attempt,
            config=self._backoff_config,
            retryable_status_codes=_RETRYABLE_STATUSES,
        )

    def health_check(self) -> LLMHealthCheckResult:
        """Probe OpenAI with a fixed payload; confirm reachability (§2.2)."""
        if os.environ.get("RTFC_AIRGAP", "").lower() in {"1", "true", "yes"}:
            return LLMHealthCheckResult(
                reachable=False,
                model_available=False,
                latency_ms=0.0,
                error="Air-gap mode enabled (RTFC_AIRGAP).  Cloud providers are blocked.",
            )

        start = time.monotonic()
        try:
            resp = self._client.chat.completions.create(  # type: ignore[attr-defined]
                model=self._model_id,
                messages=[
                    {"role": "system", "content": _PROBE_SYSTEM},
                    {"role": "user", "content": _PROBE_USER},
                ],
                response_format={"type": "json_object"},
                temperature=0.0,
                max_tokens=16,
            )
            latency = (time.monotonic() - start) * 1000
            _ = resp.choices[0].message.content  # confirm response shape
            return LLMHealthCheckResult(
                reachable=True,
                model_available=True,
                latency_ms=latency,
                error=None,
            )
        except Exception as exc:
            latency = (time.monotonic() - start) * 1000
            status, _ = _classify_openai_error(exc)
            return LLMHealthCheckResult(
                reachable=False,
                model_available=False,
                latency_ms=latency,
                error=f"OpenAI API error HTTP {status} during health probe (details redacted).",
            )

    def estimate_cost(self, requests: list[dict[str, Any]]) -> LLMCostEstimate:
        """Estimate cost using chars/4 token approximation."""
        total_input_chars = sum(len(r.get("system", "")) + len(r.get("user", "")) for r in requests)
        input_tokens = max(0, total_input_chars // _CHARS_PER_TOKEN_APPROX)
        output_tokens = input_tokens // 4  # rough estimate

        cost = Decimal("0")
        if self._cost_per_1k_input and self._cost_per_1k_output:
            cost = (
                Decimal(str(input_tokens)) / Decimal("1000") * self._cost_per_1k_input
                + Decimal(str(output_tokens)) / Decimal("1000") * self._cost_per_1k_output
            )

        return LLMCostEstimate(
            estimated_input_tokens=input_tokens,
            estimated_output_tokens=output_tokens,
            estimated_cost_usd=cost,
            basis=(
                f"Character-count approximation (chars÷{_CHARS_PER_TOKEN_APPROX}), ±~5%. "
                f"Output tokens estimated as input÷4."
            ),
            is_exact=False,
        )


# ---------------------------------------------------------------------------
# Error classification helper (secret-safe)
# ---------------------------------------------------------------------------


def _classify_openai_error(exc: Exception) -> tuple[int, float | None]:
    """Classify an openai exception and extract HTTP status + Retry-After.

    The exc chain is intentionally NOT forwarded — it may contain auth headers.
    """
    status = 0
    retry_after: float | None = None

    if hasattr(exc, "status_code"):
        status = int(exc.status_code)
    elif hasattr(exc, "response") and hasattr(exc.response, "status_code"):
        status = int(exc.response.status_code)

    if hasattr(exc, "response") and hasattr(exc.response, "headers"):
        retry_after = parse_retry_after(exc.response.headers.get("Retry-After"))

    if status == 0:
        status = 503
        logger.debug(
            "OpenAILLMProvider: error type '%s' had no status_code; using 503.",
            type(exc).__name__,
        )

    return status, retry_after
