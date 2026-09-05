"""Per-tenant in-process token bucket rate limiter (ADR-0008, §11.3, M-101).

Implements a thread-safe token bucket per tenant key.  Each retrieval service
replica maintains its own in-process bucket; no external state is required
(ADR-0008: accepted trade-off for a stateless single-replica deployment).

Design decisions (ADR-0008):
- Rate limiting fails **open** for uninitialized or misconfigured buckets.
  If ``rate`` is None the limiter is disabled and all acquires succeed.
- Clock is injectable for deterministic unit tests (no wall-clock nondeterminism).
- Thread-safe via a per-instance ``threading.Lock`` protecting the bucket dict.
- Tenant key: ``principal_id`` when available; falls back to ``kb_id``.

Spec: §11.3 per-tenant rate limits; ADR-0008 in-process token bucket.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

_DEFAULT_BURST_FACTOR: Final[int] = 10  # bucket capacity = rate * burst_factor tokens


@dataclass
class _Bucket:
    """Token bucket state for a single tenant."""

    tokens: float
    last_refill_time: float  # monotonic seconds


class TokenBucketLimiter:
    """In-process per-tenant token bucket rate limiter.

    Args:
        rate: Allowed queries per second per tenant.  ``None`` → limiter disabled
            (all acquires succeed regardless of volume — ADR-0008 fail-open).
        burst_factor: Bucket capacity = ``rate * burst_factor`` tokens (default 10).
            Controls maximum burst allowance above the steady-state rate.
        clock: Callable returning current monotonic time in seconds.  Defaults to
            ``time.monotonic``.  Override in tests for deterministic behaviour.

    Thread safety:
        All operations on the shared bucket dict are protected by ``self._lock``.
        Individual bucket objects are never shared between threads outside the lock.
    """

    def __init__(
        self,
        rate: int | None,
        *,
        burst_factor: int = _DEFAULT_BURST_FACTOR,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._rate = rate  # tokens per second; None → disabled
        self._capacity: float = float(rate * burst_factor) if rate is not None else 0.0
        self._buckets: dict[str, _Bucket] = {}
        self._lock = threading.Lock()
        self._clock: Callable[[], float] = clock if clock is not None else time.monotonic

    @property
    def enabled(self) -> bool:
        """True when the limiter is active (rate is not None)."""
        return self._rate is not None

    def acquire(self, tenant_key: str) -> bool:
        """Attempt to consume one token for ``tenant_key``.

        Returns:
            ``True`` if the request is allowed; ``False`` if rate-limited.
            Always returns ``True`` when the limiter is disabled (rate is None).
        """
        if self._rate is None:
            return True  # disabled — fail open (ADR-0008)

        now = self._clock()

        with self._lock:
            bucket = self._buckets.get(tenant_key)
            if bucket is None:
                # First request for this tenant — start full.
                bucket = _Bucket(tokens=self._capacity, last_refill_time=now)
                self._buckets[tenant_key] = bucket

            # Refill tokens based on elapsed time.
            elapsed = now - bucket.last_refill_time
            refill = elapsed * self._rate
            bucket.tokens = min(self._capacity, bucket.tokens + refill)
            bucket.last_refill_time = now

            if bucket.tokens >= 1.0:
                bucket.tokens -= 1.0
                return True
            return False

    def retry_after_seconds(self, tenant_key: str) -> float:
        """Return the estimated seconds until the next token is available.

        Returns 0.0 when the limiter is disabled.  The value is an estimate
        based on the current bucket state under the same lock to avoid TOCTOU.

        Args:
            tenant_key: Same key passed to ``acquire()``.

        Returns:
            Non-negative float seconds until the next permitted request.
        """
        if self._rate is None:
            return 0.0

        with self._lock:
            bucket = self._buckets.get(tenant_key)
            if bucket is None:
                return 0.0
            deficit = 1.0 - bucket.tokens
            if deficit <= 0.0:
                return 0.0
            return deficit / self._rate


__all__ = ["TokenBucketLimiter"]
