"""Tests for the exponential-backoff retry module."""

from __future__ import annotations

import pytest

from finecorpus.embedding._backoff import (
    BackoffConfig,
    _RetryableException,
    parse_retry_after,
    retry_with_backoff,
)
from finecorpus.embedding.base import ProviderError, ProviderUnavailableError


class TestParseRetryAfter:
    def test_integer_string(self) -> None:
        assert parse_retry_after("30") == 30.0

    def test_float_string(self) -> None:
        assert parse_retry_after("1.5") == 1.5

    def test_none_returns_none(self) -> None:
        assert parse_retry_after(None) is None

    def test_empty_returns_none(self) -> None:
        assert parse_retry_after("") is None

    def test_http_date_returns_none(self) -> None:
        # HTTP-date format — not parseable as float
        assert parse_retry_after("Wed, 21 Oct 2025 07:28:00 GMT") is None


class TestRetryWithBackoff:
    def test_succeeds_first_attempt(self) -> None:
        slept: list[float] = []
        result = retry_with_backoff(
            "test",
            "provider",
            "model",
            lambda: "ok",
            config=BackoffConfig(max_attempts=3),
            sleep_fn=slept.append,
        )
        assert result == "ok"
        assert slept == []

    def test_retries_then_succeeds(self) -> None:
        slept: list[float] = []
        call_count = 0

        def _call() -> str:
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise _RetryableException("err", http_status=503, retry_after_seconds=None)
            return "ok"

        result = retry_with_backoff(
            "test",
            "provider",
            "model",
            _call,
            config=BackoffConfig(max_attempts=5),
            sleep_fn=slept.append,
        )
        assert result == "ok"
        assert call_count == 3
        assert len(slept) == 2

    def test_exhaustion_raises_provider_unavailable(self) -> None:
        def _call() -> str:
            raise _RetryableException("err", http_status=429, retry_after_seconds=5.0)

        with pytest.raises(ProviderUnavailableError) as exc_info:
            retry_with_backoff(
                "test",
                "my-provider",
                "my-model",
                _call,
                config=BackoffConfig(max_attempts=2),
                sleep_fn=lambda _: None,
            )
        err = exc_info.value
        assert err.provider_id == "my-provider"
        assert err.model_id == "my-model"
        assert err.attempts == 2
        assert err.http_status == 429
        assert err.retry_after_seconds == 5.0

    def test_non_retryable_exception_propagates(self) -> None:
        class _Fatal(Exception):
            pass

        def _call() -> str:
            raise _Fatal("fatal")

        with pytest.raises(_Fatal):
            retry_with_backoff(
                "test",
                "provider",
                "model",
                _call,
                config=BackoffConfig(max_attempts=3),
                sleep_fn=lambda _: None,
            )

    def test_sleep_called_between_retries(self) -> None:
        slept: list[float] = []

        def _call() -> str:
            raise _RetryableException("err", http_status=503, retry_after_seconds=None)

        with pytest.raises(ProviderUnavailableError):
            retry_with_backoff(
                "test",
                "provider",
                "model",
                _call,
                config=BackoffConfig(max_attempts=4),
                sleep_fn=slept.append,
            )
        # 4 attempts → 3 sleeps
        assert len(slept) == 3

    def test_retry_after_increases_min_sleep(self) -> None:
        slept: list[float] = []
        n = 0

        def _call() -> str:
            nonlocal n
            n += 1
            raise _RetryableException("err", http_status=429, retry_after_seconds=10.0)

        with pytest.raises(ProviderUnavailableError):
            retry_with_backoff(
                "test",
                "provider",
                "model",
                _call,
                config=BackoffConfig(max_attempts=2, cap_delay_seconds=60.0),
                sleep_fn=slept.append,
            )
        # At least one sleep >= 10 (from Retry-After)
        assert slept[0] >= 10.0

    def test_sleep_capped_at_cap_delay(self) -> None:
        slept: list[float] = []

        def _call() -> str:
            raise _RetryableException("err", http_status=503, retry_after_seconds=None)

        cfg = BackoffConfig(max_attempts=5, base_delay_seconds=100.0, cap_delay_seconds=5.0)
        with pytest.raises(ProviderUnavailableError):
            retry_with_backoff(
                "test",
                "provider",
                "model",
                _call,
                config=cfg,
                sleep_fn=slept.append,
            )
        for s in slept:
            assert s <= 5.0

    # -----------------------------------------------------------------------
    # F-001: backoff layer owns retryable-status classification
    # -----------------------------------------------------------------------

    def test_non_retryable_status_raises_provider_error_immediately(self) -> None:
        """F-001: status not in retryable set → ProviderError, no sleep, no retry."""
        slept: list[float] = []
        call_count = 0

        def _call() -> str:
            nonlocal call_count
            call_count += 1
            raise _RetryableException("err", http_status=401, retry_after_seconds=None)

        with pytest.raises(ProviderError) as exc_info:
            retry_with_backoff(
                "test",
                "my-provider",
                "my-model",
                _call,
                config=BackoffConfig(max_attempts=3),
                retryable_status_codes=frozenset({429, 503}),
                sleep_fn=slept.append,
            )
        # Only one attempt — no retry on non-retryable
        assert call_count == 1
        assert slept == []
        assert exc_info.value.provider_id == "my-provider"

    def test_retryable_status_in_set_triggers_retry(self) -> None:
        """F-001: status in retryable set → retried normally."""
        call_count = 0

        def _call() -> str:
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise _RetryableException("err", http_status=503, retry_after_seconds=None)
            return "ok"

        result = retry_with_backoff(
            "test",
            "provider",
            "model",
            _call,
            config=BackoffConfig(max_attempts=5),
            retryable_status_codes=frozenset({503}),
            sleep_fn=lambda _: None,
        )
        assert result == "ok"
        assert call_count == 3

    # -----------------------------------------------------------------------
    # F-005: Retry-After exceeding cap is honoured as-is
    # -----------------------------------------------------------------------

    def test_retry_after_exceeding_cap_honoured_not_truncated(self) -> None:
        """F-005: Retry-After: 120 with cap_delay_seconds=60 → sleep 120, not 60."""
        slept: list[float] = []

        def _call() -> str:
            raise _RetryableException("err", http_status=429, retry_after_seconds=120.0)

        with pytest.raises(ProviderUnavailableError):
            retry_with_backoff(
                "test",
                "provider",
                "model",
                _call,
                config=BackoffConfig(max_attempts=2, cap_delay_seconds=60.0),
                sleep_fn=slept.append,
            )
        # The Retry-After (120) exceeds the cap (60) — must be honoured as-is
        assert len(slept) == 1
        assert slept[0] == 120.0

    def test_retry_after_below_cap_still_capped(self) -> None:
        """F-005: Retry-After within cap is still bounded by jitter cap."""
        slept: list[float] = []

        def _call() -> str:
            raise _RetryableException("err", http_status=429, retry_after_seconds=5.0)

        with pytest.raises(ProviderUnavailableError):
            retry_with_backoff(
                "test",
                "provider",
                "model",
                _call,
                config=BackoffConfig(max_attempts=2, cap_delay_seconds=60.0),
                sleep_fn=slept.append,
            )
        # Retry-After (5) ≤ cap (60) → delay is at least 5 but at most cap
        assert slept[0] >= 5.0
        assert slept[0] <= 60.0
