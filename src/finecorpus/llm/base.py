"""Core data models and abstract base for internal LLM providers.

Implements provider-abstraction.md §4 (internal LLM operations interface).

Design notes
------------
- ``LLMProviderCapabilities`` mirrors the embedding layer's ``ProviderCapabilities``
  pattern: a plain frozen dataclass, never the result of a network call.
- ``LLMProvider`` is the abstract base.  Concrete adapters MUST subclass it
  and implement all abstract methods.
- ``generate_json`` is the ONLY method that makes model calls.  It accepts a
  JSON schema (as a Pydantic BaseModel subclass) and returns raw JSON text.
  Schema validation happens in ``operations.py``, not here.
- ``LLMCostEstimate`` is intentionally NOT a reuse of the embedding layer's
  ``CostEstimate`` — it has separate input/output token fields reflecting the
  chat-completion billing model.  Siblings cannot import each other
  (import-linter layers contract).
- ``LLMProviderUnavailableError`` and ``LLMProviderError`` mirror the embedding
  error taxonomy but are independent types so callers can catch them separately.
- Credential material MUST NOT appear in exception messages, repr, or __cause__
  chains (§6.2, §14.2).
"""

from __future__ import annotations

import abc
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Capability declaration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LLMProviderCapabilities:
    """Static capability declaration for an internal LLM provider.

    Populated at adapter-construction time.  MUST NOT make network calls.

    Attributes
    ----------
    provider_id:
        Stable string identifier (``"openai"``, ``"ollama"``, ``"fake"``).
    model_id:
        Exact model identifier as the provider accepts it.
    supports_json_schema:
        ``True`` when the provider enforces a JSON schema in the response
        (e.g. OpenAI's ``response_format={"type": "json_schema"}``).
        ``False`` means the adapter relies on prompt-guided JSON + regex parsing.
    is_local:
        ``True`` if the model runs locally with no egress (M-037).
    max_output_tokens:
        Maximum output tokens the model accepts.  The caller enforces this
        ceiling before dispatch.
    cost_per_1k_input_tokens:
        Cost in USD per 1 000 input tokens.  ``None`` for local providers.
    cost_per_1k_output_tokens:
        Cost in USD per 1 000 output tokens.  ``None`` for local providers.
    pricing_as_of:
        ISO-8601 date string when pricing was last verified.  ``None`` for
        local providers.
    api_version:
        Opaque version string for observability and drift diagnosis.
    """

    provider_id: str
    model_id: str
    supports_json_schema: bool
    is_local: bool
    max_output_tokens: int
    cost_per_1k_input_tokens: Decimal | None
    cost_per_1k_output_tokens: Decimal | None
    pricing_as_of: str | None
    api_version: str


# ---------------------------------------------------------------------------
# Operation results
# ---------------------------------------------------------------------------


@dataclass
class LLMRawResult:
    """Result of a single ``generate_json`` call.

    Attributes
    ----------
    raw_json:
        The raw JSON text returned by the provider (before schema validation).
        Schema validation is the caller's responsibility (``operations.py``).
    model_id:
        Model ID as confirmed by the provider response.
    input_tokens_used:
        Input (prompt) tokens consumed.
    output_tokens_used:
        Output (completion) tokens consumed.
    provider_id:
        Echoed provider identifier.
    """

    raw_json: str
    model_id: str
    input_tokens_used: int
    output_tokens_used: int
    provider_id: str


@dataclass
class LLMHealthCheckResult:
    """Result of a ``health_check()`` call.

    Attributes
    ----------
    reachable:
        Whether the provider endpoint responded.
    model_available:
        Whether the declared model ID is available at the provider.
    latency_ms:
        Round-trip latency of the probe call.
    error:
        Optional human-readable error string.  MUST NOT include credential
        material (§6.2, §14.2).
    """

    reachable: bool
    model_available: bool
    latency_ms: float
    error: str | None = None


@dataclass
class LLMCostEstimate:
    """Result of ``estimate_cost(requests)`` — pure function, no network calls.

    This is deliberately NOT a reuse of the embedding layer's ``CostEstimate``
    because the chat-completion billing model tracks input and output tokens
    separately (input is typically cheaper than output).

    Attributes
    ----------
    estimated_input_tokens:
        Total input tokens if the request list were submitted.
    estimated_output_tokens:
        Estimated output tokens (approximation; exact count unknown pre-flight).
    estimated_cost_usd:
        Estimated cost in USD.  ``0.0`` for local providers.
    basis:
        Human-readable statement of the estimation basis and uncertainty.
    is_exact:
        ``False`` whenever the estimate uses an approximation.
    """

    estimated_input_tokens: int
    estimated_output_tokens: int
    estimated_cost_usd: Decimal
    basis: str
    is_exact: bool


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class LLMProviderError(Exception):
    """Raised by an adapter for non-transient provider errors.

    MUST NOT include any credential material in the message (§6.2, §14.2).
    """

    def __init__(self, message: str, provider_id: str, model_id: str) -> None:
        super().__init__(message)
        self.provider_id = provider_id
        self.model_id = model_id


class LLMProviderUnavailableError(Exception):
    """Raised when the provider cannot be reached or has exhausted retries.

    The operations layer catches this and follows §15 single-document-failure
    semantics (record and continue at document level; halt at class-wide level).

    MUST NOT include any credential material in the message or in any
    attribute (§6.2, §14.2).  The ``__cause__`` chain is checked by the
    secret-absence test.

    Attributes
    ----------
    provider_id:
        Identifies which provider failed.
    model_id:
        The model that was being requested.
    retry_after_seconds:
        Seconds from the provider's ``Retry-After`` header (or ``None``).
    attempts:
        Number of attempts made before giving up.
    http_status:
        HTTP status code from the final failure (or ``None`` for connection
        errors).
    """

    def __init__(
        self,
        message: str,
        provider_id: str,
        model_id: str,
        retry_after_seconds: float | None = None,
        attempts: int = 1,
        http_status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.provider_id = provider_id
        self.model_id = model_id
        self.retry_after_seconds = retry_after_seconds
        self.attempts = attempts
        self.http_status = http_status


# ---------------------------------------------------------------------------
# Abstract base class
# ---------------------------------------------------------------------------


class LLMProvider(abc.ABC):
    """Abstract base class for all internal LLM provider adapters.

    Concrete subclasses: ``OpenAILLMProvider``, ``OllamaLLMProvider``,
    ``FakeLLMProvider``.

    The platform calls only three methods: ``generate_json``, ``health_check``,
    and ``estimate_cost``.  All other logic is internal to the adapter.

    Capabilities are declared via the ``capabilities`` property, which MUST NOT
    make network calls.
    """

    @property
    @abc.abstractmethod
    def capabilities(self) -> LLMProviderCapabilities:
        """Static capability declaration.  MUST NOT make network calls."""

    @abc.abstractmethod
    def generate_json(
        self,
        system: str,
        user: str,
        schema: type[BaseModel],
        temperature: float,
        max_output_tokens: int,
    ) -> LLMRawResult:
        """Call the provider and return raw JSON text for schema validation.

        The caller (``operations.py``) is responsible for schema validation.
        This method MUST NOT validate against *schema* — it only uses *schema*
        to pass a response-format hint to the provider when supported.

        Parameters
        ----------
        system:
            System message (instruction frame).  MUST NOT contain raw corpus
            text — that belongs in *user* inside data delimiters (§14.1).
        user:
            User message.  Corpus text is placed here inside declared
            delimiters (provider-abstraction.md §4.4).
        schema:
            Pydantic BaseModel subclass describing the expected output shape.
            Used as a response-format hint where the provider supports it.
        temperature:
            Sampling temperature 0.0–1.0.  Resolved by ``operations.py``
            from per-operation config, then passed here.
        max_output_tokens:
            Hard ceiling on output tokens.  Decision D-21(b): enforcement of
            per-call budgets is Phase 4; this ceiling prevents runaway calls.

        Returns
        -------
        LLMRawResult
            Contains ``raw_json`` ready for ``Output.model_validate_json()``.

        Raises
        ------
        LLMProviderError
            Non-transient provider error (e.g. bad request, schema refusal).
        LLMProviderUnavailableError
            Provider unreachable after all retry attempts.
        """

    @abc.abstractmethod
    def health_check(self) -> LLMHealthCheckResult:
        """Probe the provider without sending corpus content.

        Uses a fixed, constant probe that carries no information.
        """

    @abc.abstractmethod
    def estimate_cost(self, requests: list[dict[str, Any]]) -> LLMCostEstimate:
        """Estimate cost of *requests* without making any network calls.

        Pure function: MUST NOT call the provider, MUST NOT generate text.

        Parameters
        ----------
        requests:
            List of dicts with keys ``system`` (str) and ``user`` (str) —
            the same payloads that would be passed to ``generate_json``.
        """

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        """Safe repr — never includes credential material (§6.2)."""
        caps = self.capabilities
        return (
            f"{self.__class__.__name__}("
            f"provider_id={caps.provider_id!r}, "
            f"model_id={caps.model_id!r}, "
            f"is_local={caps.is_local})"
        )

    def model_dump(self) -> dict[str, Any]:
        """Return a dict representation for observability (no credentials)."""
        caps = self.capabilities
        return {
            "provider_id": caps.provider_id,
            "model_id": caps.model_id,
            "is_local": caps.is_local,
            "api_version": caps.api_version,
            "supports_json_schema": caps.supports_json_schema,
        }
