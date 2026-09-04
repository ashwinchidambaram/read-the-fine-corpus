"""Core data models and abstract base for embedding providers.

Implements provider-abstraction.md §2.1 (capability declaration),
§2.2 (operations), and §2.3 (provider surface area).

Design notes:
- ``ProviderCapabilities`` is a plain dataclass, not an interface method that executes.
  It is populated at adapter-registration time and read by the platform; it never makes
  network calls (§2.1).
- ``EmbeddingProvider`` is an abstract base class.  Concrete adapters MUST subclass it
  and implement all abstract methods.
- ``ProviderUnavailableError`` carries ``retry_after_seconds`` for the §15 backoff logic.
  The adapter that raises it MUST NOT include any credential material in the message or
  ``__cause__`` chain (§6.2, §14.2).
"""

from __future__ import annotations

import abc
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

# ---------------------------------------------------------------------------
# Capability declaration (§2.1)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProviderCapabilities:
    """Static capability declaration for an embedding provider (§2.1).

    This is a plain data structure — not an interface method that executes.
    Populated at adapter-registration time, never during embed calls.

    Attributes
    ----------
    provider_id:
        Stable string identifier (``"openai"``, ``"ollama"``, ``"fake"``).
    model_id:
        Exact model identifier as the provider accepts it
        (``"text-embedding-3-small"``, ``"nomic-embed-text"``, …).
    vector_dimensions:
        Output embedding dimension; pinned, must not vary across calls.
    max_input_tokens:
        Maximum tokens per single input string.  The platform truncates to this
        boundary rather than silently losing tail content (OQ-P-1).
    max_batch_size:
        Maximum number of texts in one ``embed_batch`` call.
    supported_languages:
        BCP-47 language codes, or ``"*"`` for universal coverage.
    cross_lingual:
        Whether the model supports cross-lingual retrieval.
    is_local:
        ``True`` if the model runs locally with no egress.
    cost_per_1k_tokens:
        Cost in USD per 1 000 tokens.  ``None`` for local providers.
    pricing_as_of:
        ISO-8601 date string when ``cost_per_1k_tokens`` was last verified.
        ``None`` for local providers (cost is zero, date is irrelevant).
    api_version:
        Opaque version string recorded in index metadata for drift diagnosis.
        For Ollama models: sha256 digest from ``/api/show`` (OQ-P-4).
    """

    provider_id: str
    model_id: str
    vector_dimensions: int
    max_input_tokens: int
    max_batch_size: int
    supported_languages: list[str] | str  # list[BCP-47] or "*"
    cross_lingual: bool
    is_local: bool
    cost_per_1k_tokens: Decimal | None
    pricing_as_of: str | None  # ISO-8601 date, or None for local providers
    api_version: str


# ---------------------------------------------------------------------------
# Operation results (§2.2)
# ---------------------------------------------------------------------------


@dataclass
class EmbedBatchResult:
    """Result of a single ``embed_batch`` call (§2.2).

    Attributes
    ----------
    embeddings:
        One float vector per input text, same order.  Each vector has exactly
        ``capabilities.vector_dimensions`` elements.
    model_id:
        Model ID as confirmed by the provider response.  Mismatch vs declared
        is logged as a warning.
    input_tokens_used:
        Total tokens consumed; accumulated by the cost-tracking subsystem (§16).
    provider_id:
        Echoed from the provider, confirms routing correctness.
    """

    embeddings: list[list[float]]
    model_id: str
    input_tokens_used: int
    provider_id: str


@dataclass
class HealthCheckResult:
    """Result of a ``health_check()`` call (§2.2).

    Attributes
    ----------
    reachable:
        Whether the provider endpoint responded.
    model_available:
        Whether the declared model ID is available at the provider.
    latency_ms:
        Round-trip latency of the probe call.
    declared_dimensions_confirmed:
        Whether a probe embedding's dimension matches the declared
        ``vector_dimensions``.  A mismatch is a fatal configuration error.
    error:
        Optional human-readable error string.  MUST NOT include any credential
        material (§6.2, §14.2).
    """

    reachable: bool
    model_available: bool
    latency_ms: float
    declared_dimensions_confirmed: bool
    error: str | None = None


@dataclass
class CostEstimate:
    """Result of ``estimate_cost(texts)`` (§2.2).

    This is a pure function — no network calls, no embedding.

    Attributes
    ----------
    estimated_tokens:
        Total tokens if the input list were embedded.
    estimated_cost_usd:
        Estimated cost in USD.  ``0.0`` for local providers.
    basis:
        Human-readable statement of the estimation basis and uncertainty.
    is_exact:
        ``False`` whenever the estimate uses an approximation.
    """

    estimated_tokens: int
    estimated_cost_usd: Decimal
    basis: str
    is_exact: bool


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ProviderError(Exception):
    """Raised by an adapter for non-transient provider errors.

    For example: the provider returned fewer embeddings than inputs (§2.2
    "the adapter MUST raise a ProviderError rather than returning a partial
    result").

    MUST NOT include any credential material in the message (§6.2, §14.2).
    """

    def __init__(self, message: str, provider_id: str, model_id: str) -> None:
        super().__init__(message)
        self.provider_id = provider_id
        self.model_id = model_id


class ProviderUnavailableError(Exception):
    """Raised when the provider cannot be reached or has exhausted retries.

    The ingestion layer catches this and follows the §15 pause-and-retry
    behaviour.  The retrieval layer catches this and fails closed.

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
# Abstract base class (§2.2)
# ---------------------------------------------------------------------------


class EmbeddingProvider(abc.ABC):
    """Abstract base class for all embedding provider adapters.

    Concrete subclasses: ``OpenAIProvider``, ``OllamaProvider``, ``FakeProvider``.

    The platform calls only three methods: ``embed_batch``, ``health_check``,
    and ``estimate_cost``.  All other logic is internal to the adapter.

    Capabilities are declared via the ``capabilities`` property, which is a
    plain data structure — it MUST NOT make network calls.
    """

    @property
    @abc.abstractmethod
    def capabilities(self) -> ProviderCapabilities:
        """Static capability declaration for this provider (§2.1).

        Populated at construction time.  MUST NOT make network calls.
        """

    @abc.abstractmethod
    def embed_batch(self, texts: list[str], model_id: str) -> EmbedBatchResult:
        """Embed a batch of texts and return vectors + usage (§2.2).

        Parameters
        ----------
        texts:
            Non-empty list of UTF-8 strings.  Each must not exceed
            ``capabilities.max_input_tokens``.
        model_id:
            Must match ``capabilities.model_id``.  The adapter MUST reject
            a mismatched model ID rather than silently using a default.

        Returns
        -------
        EmbedBatchResult
            One vector per input text, same order.

        Raises
        ------
        ValueError
            If ``model_id`` does not match ``capabilities.model_id``, or
            if ``texts`` is empty.
        ProviderError
            If the provider returns fewer embeddings than inputs.
        ProviderUnavailableError
            If the provider is unreachable after all retry attempts.
        """

    @abc.abstractmethod
    def health_check(self) -> HealthCheckResult:
        """Probe the provider without embedding document content (§2.2).

        Uses a fixed, constant probe string that carries no information.
        Called at startup (preflight) and periodically by the embedding service.

        Returns
        -------
        HealthCheckResult
        """

    @abc.abstractmethod
    def estimate_cost(self, texts: list[str]) -> CostEstimate:
        """Estimate the cost of embedding *texts* without making any network calls (§2.2).

        Pure function: MUST NOT use network calls, MUST NOT embed any text.

        Parameters
        ----------
        texts:
            List of strings whose cost is to be estimated.

        Returns
        -------
        CostEstimate
        """

    # ------------------------------------------------------------------
    # Shared helpers available to all subclasses
    # ------------------------------------------------------------------

    def _assert_model_id(self, requested: str) -> None:
        """Raise ``ValueError`` if *requested* != ``capabilities.model_id`` (§2.2)."""
        if requested != self.capabilities.model_id:
            raise ValueError(
                f"Provider '{self.capabilities.provider_id}' was asked for model "
                f"'{requested}' but declares model '{self.capabilities.model_id}'. "
                f"The adapter must reject mismatched model IDs (provider-abstraction.md §2.2)."
            )

    def _assert_nonempty(self, texts: list[str]) -> None:
        """Raise ``ValueError`` if *texts* is empty."""
        if not texts:
            raise ValueError("embed_batch requires at least one text (texts was empty).")

    def __repr__(self) -> str:
        """Safe repr — never includes credential material (§6.2)."""
        caps = self.capabilities
        return (
            f"{self.__class__.__name__}("
            f"provider_id={caps.provider_id!r}, "
            f"model_id={caps.model_id!r}, "
            f"dimensions={caps.vector_dimensions})"
        )

    def model_dump(self) -> dict[str, Any]:
        """Return a dict representation for observability (no credentials)."""
        caps = self.capabilities
        return {
            "provider_id": caps.provider_id,
            "model_id": caps.model_id,
            "vector_dimensions": caps.vector_dimensions,
            "is_local": caps.is_local,
            "api_version": caps.api_version,
        }
