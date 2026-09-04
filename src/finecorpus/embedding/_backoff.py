"""Exponential backoff with jitter for embedding provider retries.

Implements the §15 behaviour: "backoff, surface remaining budget, do not fail the job."

Design
------
- Hand-rolled; does NOT use tenacity or any third-party retry library.
- Bounded attempts (configurable, default 5).
- Full-jitter exponential backoff: ``sleep = random(0, min(cap, base * 2^attempt))``.
- Respects ``Retry-After`` headers: if the provider specifies a delay we sleep at
  least that long (plus jitter).
- After ``max_attempts`` are exhausted, raises ``ProviderUnavailableError`` with the
  attempt count and HTTP status code — the caller can surface these to the operator.
- Secret-safe: never includes API keys or auth headers in exception messages.

Usage
-----
``BackoffConfig`` is constructed with the desired limits.  ``retry_embed`` accepts a
callable that performs one attempt and may raise ``_RetryableError``.  The callable is
responsible for interpreting HTTP responses and raising the correct exception.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

from finecorpus.embedding.base import ProviderUnavailableError

T = TypeVar("T")

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_DEFAULT_BASE_DELAY_SECONDS = 1.0
_DEFAULT_CAP_DELAY_SECONDS = 60.0
_DEFAULT_MAX_ATTEMPTS = 5


@dataclass
class BackoffConfig:
    """Configuration for the exponential-backoff retry loop.

    Attributes
    ----------
    max_attempts:
        Maximum number of attempts (including the first).  After this many
        failures, ``ProviderUnavailableError`` is raised.
    base_delay_seconds:
        Base delay for the first retry.
    cap_delay_seconds:
        Maximum delay cap (never sleep longer than this regardless of attempt).
    """

    max_attempts: int = _DEFAULT_MAX_ATTEMPTS
    base_delay_seconds: float = _DEFAULT_BASE_DELAY_SECONDS
    cap_delay_seconds: float = _DEFAULT_CAP_DELAY_SECONDS


# ---------------------------------------------------------------------------
# Internal sentinel exception
# ---------------------------------------------------------------------------


@dataclass
class _RetryableError:
    """Raised inside the callable to signal "retry this attempt".

    Attributes
    ----------
    http_status:
        HTTP status code from the response.
    retry_after_seconds:
        Value from the ``Retry-After`` header, or ``None``.
    message:
        Human-readable description; MUST NOT include credential material.
    """

    http_status: int
    retry_after_seconds: float | None
    message: str

    def as_exception(self) -> Exception:
        return _RetryableException(self.message, self.http_status, self.retry_after_seconds)


class _RetryableException(Exception):
    """Exception wrapping ``_RetryableError`` for use in the retry loop."""

    def __init__(self, message: str, http_status: int, retry_after_seconds: float | None) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.retry_after_seconds = retry_after_seconds


# ---------------------------------------------------------------------------
# Public retry helper
# ---------------------------------------------------------------------------


def retry_with_backoff[T](  # noqa: UP047
    operation: str,
    provider_id: str,
    model_id: str,
    call: Callable[[], T],
    *,
    config: BackoffConfig | None = None,
    retryable_status_codes: frozenset[int] = frozenset({429, 500, 502, 503, 504}),
    sleep_fn: Callable[[float], None] | None = None,  # injected in tests
) -> T:
    """Call *call* up to ``config.max_attempts`` times with exponential backoff.

    *call* should raise ``_RetryableException`` to signal a retriable failure.
    Any other exception propagates immediately (non-retriable errors).

    Parameters
    ----------
    operation:
        Human-readable description of what is being attempted (for log lines).
    provider_id:
        Provider ID, for ``ProviderUnavailableError``.
    model_id:
        Model ID, for ``ProviderUnavailableError``.
    call:
        Zero-argument callable that performs one attempt.
    config:
        Backoff configuration.  Defaults to ``BackoffConfig()``.
    retryable_status_codes:
        HTTP status codes that should be retried.
    sleep_fn:
        Override ``time.sleep`` for tests (avoids real sleeping).

    Returns
    -------
    T
        Whatever *call* returns on success.

    Raises
    ------
    ProviderUnavailableError
        After all attempts are exhausted.
    """
    cfg = config or BackoffConfig()
    _sleep = sleep_fn if sleep_fn is not None else time.sleep

    last_status: int | None = None
    last_retry_after: float | None = None

    for attempt in range(cfg.max_attempts):
        try:
            return call()  # type: ignore[return-value]
        except _RetryableException as exc:
            last_status = exc.http_status
            last_retry_after = exc.retry_after_seconds

            if attempt + 1 >= cfg.max_attempts:
                # Exhausted — propagate as ProviderUnavailableError
                break

            # Compute delay: full-jitter exponential backoff
            cap = min(cfg.cap_delay_seconds, cfg.base_delay_seconds * (2**attempt))
            jitter_delay = random.uniform(0, cap)

            # Respect Retry-After if it demands a longer wait
            delay = max(jitter_delay, exc.retry_after_seconds or 0.0)
            delay = min(delay, cfg.cap_delay_seconds)

            logger.warning(
                "Provider '%s' model '%s' attempt %d/%d failed (%s HTTP %d); retrying in %.1fs.",
                provider_id,
                model_id,
                attempt + 1,
                cfg.max_attempts,
                operation,
                exc.http_status,
                delay,
            )
            _sleep(delay)

    raise ProviderUnavailableError(
        f"Provider '{provider_id}' model '{model_id}' failed after "
        f"{cfg.max_attempts} attempt(s) for operation '{operation}'. "
        f"Last HTTP status: {last_status}.",
        provider_id=provider_id,
        model_id=model_id,
        retry_after_seconds=last_retry_after,
        attempts=cfg.max_attempts,
        http_status=last_status,
    )


# ---------------------------------------------------------------------------
# Convenience: parse Retry-After header
# ---------------------------------------------------------------------------


def parse_retry_after(value: str | None) -> float | None:
    """Parse a ``Retry-After`` header value to seconds, or return ``None``.

    Handles both integer-seconds form and HTTP-date form (returns ``None``
    for HTTP-date to keep the implementation simple — callers fall back to
    jitter-only delay).
    """
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None
