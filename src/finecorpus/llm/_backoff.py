"""Exponential backoff with jitter for internal LLM provider retries.

Own copy, modelled on ``finecorpus.embedding._backoff``.  NOT imported from
there because sibling packages cannot import each other (import-linter layers
contract: ``finecorpus.llm`` and ``finecorpus.embedding`` are siblings at the
same layer and may not cross-import).

Implements §15 behaviour: bounded attempts, full-jitter exponential backoff,
Retry-After header respect, secret-safe error messages.

Status-code classification
--------------------------
Providers raise ``_LLMRetryableException`` with the raw HTTP status; the loop
inspects ``exc.http_status`` against ``retryable_status_codes``.  A status NOT
in the retryable set is immediately surfaced as ``LLMProviderError`` (no retry).
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

from finecorpus.llm.base import LLMProviderError, LLMProviderUnavailableError

T = TypeVar("T")

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_DEFAULT_BASE_DELAY_SECONDS = 1.0
_DEFAULT_CAP_DELAY_SECONDS = 60.0
_DEFAULT_MAX_ATTEMPTS = 5

_DEFAULT_RETRYABLE_STATUS_CODES: frozenset[int] = frozenset({429, 500, 502, 503, 504})


@dataclass
class LLMBackoffConfig:
    """Configuration for the LLM exponential-backoff retry loop."""

    max_attempts: int = _DEFAULT_MAX_ATTEMPTS
    base_delay_seconds: float = _DEFAULT_BASE_DELAY_SECONDS
    cap_delay_seconds: float = _DEFAULT_CAP_DELAY_SECONDS


# ---------------------------------------------------------------------------
# Internal sentinel exception
# ---------------------------------------------------------------------------


class _LLMRetryableException(Exception):
    """Raised inside the callable to signal "retry this attempt"."""

    def __init__(self, message: str, http_status: int, retry_after_seconds: float | None) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.retry_after_seconds = retry_after_seconds


# ---------------------------------------------------------------------------
# Public retry helper
# ---------------------------------------------------------------------------


def llm_retry_with_backoff[T](  # noqa: UP047
    operation: str,
    provider_id: str,
    model_id: str,
    call: Callable[[], T],
    *,
    config: LLMBackoffConfig | None = None,
    retryable_status_codes: frozenset[int] = _DEFAULT_RETRYABLE_STATUS_CODES,
    sleep_fn: Callable[[float], None] | None = None,
) -> T:
    """Call *call* up to ``config.max_attempts`` times with exponential backoff.

    Parameters
    ----------
    operation:
        Human-readable description of the operation (for log lines).
    provider_id:
        Provider ID, for error attribution.
    model_id:
        Model ID, for error attribution.
    call:
        Zero-argument callable that performs one attempt.  Raises
        ``_LLMRetryableException`` to signal a retriable failure.
    config:
        Backoff configuration.  Defaults to ``LLMBackoffConfig()``.
    retryable_status_codes:
        HTTP statuses that should trigger a retry.  Others → ``LLMProviderError``.
    sleep_fn:
        Override ``time.sleep`` for tests (avoids real sleeping).

    Returns
    -------
    T
        Whatever *call* returns on success.

    Raises
    ------
    LLMProviderError
        Non-retryable HTTP error.
    LLMProviderUnavailableError
        After all attempts are exhausted.
    """
    cfg = config or LLMBackoffConfig()
    _sleep = sleep_fn if sleep_fn is not None else time.sleep

    last_status: int | None = None
    last_retry_after: float | None = None

    for attempt in range(cfg.max_attempts):
        # Sentinel variables: collect state from the except block, then act
        # OUTSIDE it.  This severs the exception chain so that the original
        # provider exception (which may carry auth data in str/repr) is not
        # reachable via __context__/__cause__ on any error raised to callers.
        _non_retryable_status: int | None = None
        _retryable_status: int | None = None
        _retryable_retry_after: float | None = None

        try:
            return call()  # type: ignore[return-value]
        except _LLMRetryableException as exc:
            if exc.http_status not in retryable_status_codes:
                # Non-retryable: record status, exit the except block, then raise.
                _non_retryable_status = exc.http_status
            else:
                # Retryable: record state for delay calculation outside the block.
                _retryable_status = exc.http_status
                _retryable_retry_after = exc.retry_after_seconds

        # ── Non-retryable path ──────────────────────────────────────────────
        # Raised OUTSIDE the except block so Python does NOT attach the captured
        # _LLMRetryableException as __context__ on the new LLMProviderError.
        if _non_retryable_status is not None:
            raise LLMProviderError(
                f"Provider '{provider_id}' model '{model_id}' returned "
                f"non-retryable HTTP {_non_retryable_status} during '{operation}' "
                f"(redacted for secret safety).",
                provider_id=provider_id,
                model_id=model_id,
            )

        # ── Retryable path ──────────────────────────────────────────────────
        if _retryable_status is not None:
            last_status = _retryable_status
            last_retry_after = _retryable_retry_after

            if attempt + 1 >= cfg.max_attempts:
                break

            cap = min(cfg.cap_delay_seconds, cfg.base_delay_seconds * (2**attempt))
            jitter_delay = random.uniform(0, cap)

            retry_after = _retryable_retry_after or 0.0
            if retry_after > cfg.cap_delay_seconds:
                logger.warning(
                    "LLM provider '%s' model '%s': Retry-After %.1fs exceeds "
                    "cap_delay_seconds %.1fs — honouring as-is.",
                    provider_id,
                    model_id,
                    retry_after,
                    cfg.cap_delay_seconds,
                )
                delay = retry_after
            else:
                delay = max(jitter_delay, retry_after)
                delay = min(delay, cfg.cap_delay_seconds)

            logger.warning(
                "LLM provider '%s' model '%s' attempt %d/%d failed (%s HTTP %d); "
                "retrying in %.1fs.",
                provider_id,
                model_id,
                attempt + 1,
                cfg.max_attempts,
                operation,
                _retryable_status,
                delay,
            )
            _sleep(delay)

    raise LLMProviderUnavailableError(
        f"LLM provider '{provider_id}' model '{model_id}' failed after "
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
    """Parse a ``Retry-After`` header value to seconds, or return ``None``."""
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None
