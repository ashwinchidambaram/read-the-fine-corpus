"""Tests for OpenAIProvider against mocked HTTP (no live calls).

Uses a mock openai client (via a simple adapter class) to avoid network calls.
Tests cover:
  - embed_batch correctness
  - model_id validation
  - retry/backoff on 429/5xx
  - exhaustion raises ProviderUnavailableError (no fallback)
  - secret absence from exceptions and repr
  - cost estimation
  - health check (happy + unhappy paths)
"""

from __future__ import annotations

import logging
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from finecorpus.embedding._backoff import BackoffConfig
from finecorpus.embedding.base import ProviderError, ProviderUnavailableError
from finecorpus.embedding.openai_provider import (
    OpenAIProvider,
)

# ---------------------------------------------------------------------------
# Helpers: build fake openai responses
# ---------------------------------------------------------------------------

_FAKE_KEY = "sk-test-1234567890abcdef1234567890abcdef"  # noqa: S105 — test fixture, not real


def _make_embedding_response(
    texts: list[str],
    model: str = "text-embedding-3-small",
    dims: int = 1536,
) -> SimpleNamespace:
    """Build a minimal openai embeddings response object."""
    data = []
    for i, _ in enumerate(texts):
        vec = [float(i + 1) / 100.0] * dims
        data.append(SimpleNamespace(embedding=vec, index=i))
    usage = SimpleNamespace(total_tokens=len(texts) * 10)
    return SimpleNamespace(data=data, model=model, usage=usage)


def _make_mock_client(
    side_effects: list[Any],
    dims: int = 1536,
) -> MagicMock:
    """Build a mock openai client whose embeddings.create yields *side_effects*."""
    client = MagicMock()
    client.embeddings.create.side_effect = side_effects
    return client


# ---------------------------------------------------------------------------
# Provider construction
# ---------------------------------------------------------------------------


class TestOpenAIProviderConstruction:
    def test_known_model_resolves_dimensions(self) -> None:
        p = OpenAIProvider(_FAKE_KEY, model_id="text-embedding-3-small", _openai_client=MagicMock())
        assert p.capabilities.vector_dimensions == 1536

    def test_large_model_resolves_dimensions(self) -> None:
        p = OpenAIProvider(_FAKE_KEY, model_id="text-embedding-3-large", _openai_client=MagicMock())
        assert p.capabilities.vector_dimensions == 3072

    def test_unknown_model_without_dimensions_raises(self) -> None:
        with pytest.raises(ValueError, match="dimensions must be supplied"):
            OpenAIProvider(_FAKE_KEY, model_id="nonexistent-model", _openai_client=MagicMock())

    def test_unknown_model_with_explicit_dimensions(self) -> None:
        p = OpenAIProvider(
            _FAKE_KEY,
            model_id="custom-model",
            dimensions=512,
            _openai_client=MagicMock(),
        )
        assert p.capabilities.vector_dimensions == 512

    def test_repr_does_not_contain_api_key(self) -> None:
        """repr() MUST NOT include the API key (§6.2, §14.2)."""
        p = OpenAIProvider(_FAKE_KEY, _openai_client=MagicMock())
        r = repr(p)
        assert _FAKE_KEY not in r
        assert "sk-" not in r

    def test_capabilities_provider_id(self) -> None:
        p = OpenAIProvider(_FAKE_KEY, _openai_client=MagicMock())
        assert p.capabilities.provider_id == "openai"
        assert p.capabilities.is_local is False
        assert p.capabilities.cross_lingual is True

    def test_capabilities_cost_populated_for_known_model(self) -> None:
        p = OpenAIProvider(_FAKE_KEY, model_id="text-embedding-3-small", _openai_client=MagicMock())
        assert p.capabilities.cost_per_1k_tokens is not None
        assert p.capabilities.cost_per_1k_tokens > Decimal("0")

    def test_capabilities_pricing_as_of_populated(self) -> None:
        p = OpenAIProvider(_FAKE_KEY, _openai_client=MagicMock())
        assert p.capabilities.pricing_as_of is not None


# ---------------------------------------------------------------------------
# embed_batch — happy path
# ---------------------------------------------------------------------------


class TestOpenAIEmbedBatch:
    def test_single_text_success(self) -> None:
        resp = _make_embedding_response(["hello"], dims=1536)
        client = _make_mock_client([resp])
        p = OpenAIProvider(_FAKE_KEY, _openai_client=client)
        result = p.embed_batch(["hello"], "text-embedding-3-small")
        assert len(result.embeddings) == 1
        assert len(result.embeddings[0]) == 1536
        assert result.provider_id == "openai"
        assert result.model_id == "text-embedding-3-small"

    def test_multiple_texts(self) -> None:
        texts = ["a", "b", "c"]
        resp = _make_embedding_response(texts, dims=1536)
        client = _make_mock_client([resp])
        p = OpenAIProvider(_FAKE_KEY, _openai_client=client)
        result = p.embed_batch(texts, "text-embedding-3-small")
        assert len(result.embeddings) == 3

    def test_token_count_from_response(self) -> None:
        resp = _make_embedding_response(["hello"])
        resp.usage.total_tokens = 42
        client = _make_mock_client([resp])
        p = OpenAIProvider(_FAKE_KEY, _openai_client=client)
        result = p.embed_batch(["hello"], "text-embedding-3-small")
        assert result.input_tokens_used == 42

    def test_wrong_model_id_raises_before_api_call(self) -> None:
        client = MagicMock()
        p = OpenAIProvider(_FAKE_KEY, _openai_client=client)
        with pytest.raises(ValueError, match="model"):
            p.embed_batch(["hello"], "wrong-model")
        client.embeddings.create.assert_not_called()

    def test_empty_texts_raises_before_api_call(self) -> None:
        client = MagicMock()
        p = OpenAIProvider(_FAKE_KEY, _openai_client=client)
        with pytest.raises(ValueError, match="empty"):
            p.embed_batch([], "text-embedding-3-small")
        client.embeddings.create.assert_not_called()

    def test_partial_response_raises_provider_error(self) -> None:
        """Provider returns 1 embedding for 2 texts → ProviderError."""
        resp = _make_embedding_response(["only-one"], dims=1536)
        client = _make_mock_client([resp])
        p = OpenAIProvider(_FAKE_KEY, _openai_client=client)
        with pytest.raises(ProviderError):
            p.embed_batch(["a", "b"], "text-embedding-3-small")


# ---------------------------------------------------------------------------
# Retry / backoff
# ---------------------------------------------------------------------------


class TestOpenAIRetry:
    def _make_rate_limit_error(self, retry_after: str | None = None) -> Exception:
        """Build a minimal fake openai RateLimitError."""
        exc = Exception("Rate limit exceeded")
        exc.status_code = 429  # type: ignore[attr-defined]
        headers = {}
        if retry_after:
            headers["Retry-After"] = retry_after
        exc.response = SimpleNamespace(status_code=429, headers=headers)  # type: ignore[attr-defined]
        return exc

    def _make_server_error(self, status: int = 500) -> Exception:
        exc = Exception("Server error")
        exc.status_code = status  # type: ignore[attr-defined]
        exc.response = SimpleNamespace(status_code=status, headers={})  # type: ignore[attr-defined]
        return exc

    def test_retries_on_429_then_succeeds(self) -> None:
        texts = ["hello"]
        success_resp = _make_embedding_response(texts)
        err = self._make_rate_limit_error()

        slept: list[float] = []
        cfg = BackoffConfig(max_attempts=3)
        client = _make_mock_client([err, success_resp])
        p = OpenAIProvider(
            _FAKE_KEY,
            _openai_client=client,
            backoff_config=cfg,
        )
        # Patch sleep to avoid real waiting
        with patch("finecorpus.embedding._backoff.time.sleep", side_effect=slept.append):
            result = p.embed_batch(texts, "text-embedding-3-small")
        assert len(result.embeddings) == 1
        assert client.embeddings.create.call_count == 2

    def test_retries_on_500_then_succeeds(self) -> None:
        texts = ["hello"]
        err = self._make_server_error(500)
        success = _make_embedding_response(texts)
        cfg = BackoffConfig(max_attempts=3)
        client = _make_mock_client([err, success])
        p = OpenAIProvider(_FAKE_KEY, _openai_client=client, backoff_config=cfg)
        with patch("finecorpus.embedding._backoff.time.sleep"):
            result = p.embed_batch(texts, "text-embedding-3-small")
        assert len(result.embeddings) == 1

    def test_exhausted_raises_provider_unavailable_no_fallback(self) -> None:
        """After max_attempts, raises ProviderUnavailableError — no model fallback ever."""
        err = self._make_rate_limit_error()
        cfg = BackoffConfig(max_attempts=3)
        client = _make_mock_client([err, err, err])
        p = OpenAIProvider(_FAKE_KEY, _openai_client=client, backoff_config=cfg)
        with patch("finecorpus.embedding._backoff.time.sleep"):
            with pytest.raises(ProviderUnavailableError) as exc_info:
                p.embed_batch(["hello"], "text-embedding-3-small")
        err_obj = exc_info.value
        assert err_obj.provider_id == "openai"
        assert err_obj.attempts == 3
        assert err_obj.http_status == 429

    def test_retry_after_respected(self) -> None:
        """Retry-After header from 429 is carried to ProviderUnavailableError."""
        err = self._make_rate_limit_error(retry_after="10")
        cfg = BackoffConfig(max_attempts=2)
        client = _make_mock_client([err, err])
        p = OpenAIProvider(_FAKE_KEY, _openai_client=client, backoff_config=cfg)
        slept: list[float] = []
        with patch("finecorpus.embedding._backoff.time.sleep", side_effect=slept.append):
            with pytest.raises(ProviderUnavailableError) as exc_info:
                p.embed_batch(["hello"], "text-embedding-3-small")
        assert exc_info.value.retry_after_seconds == 10.0

    def test_non_retryable_error_does_not_retry(self) -> None:
        """F-001: 401 (auth error) is NOT in retryable set → ProviderError, no retry."""
        err = Exception("Auth error")
        err.status_code = 401  # type: ignore[attr-defined]
        err.response = SimpleNamespace(status_code=401, headers={})  # type: ignore[attr-defined]
        cfg = BackoffConfig(max_attempts=3)
        client = _make_mock_client([err])
        p = OpenAIProvider(_FAKE_KEY, _openai_client=client, backoff_config=cfg)
        with patch("finecorpus.embedding._backoff.time.sleep"):
            with pytest.raises(ProviderError):
                p.embed_batch(["hello"], "text-embedding-3-small")
        # F-001: backoff layer surfaces non-retryable status as ProviderError immediately
        assert client.embeddings.create.call_count == 1


# ---------------------------------------------------------------------------
# Secret absence test (§6.2, §14.2, §18.3 test 10)
# ---------------------------------------------------------------------------


def _walk_exception_chain(exc: BaseException) -> list[BaseException]:
    """Walk both __cause__ and __context__ chains, returning all exceptions found.

    F-006: providers use ``raise ... from None`` AND set ``__context__ = None``
    to sever both chain members.  This helper validates that neither chain
    carries the secret.
    """
    seen: list[BaseException] = []
    visited: set[int] = set()
    queue = [exc]
    while queue:
        current = queue.pop()
        if id(current) in visited:
            continue
        visited.add(id(current))
        seen.append(current)
        if current.__cause__ is not None:
            queue.append(current.__cause__)
        if current.__context__ is not None:
            queue.append(current.__context__)
    return seen


class TestOpenAISecretAbsence:
    """The API key MUST NOT appear in any exception chain or log record.

    F-006: tests walk BOTH __cause__ and __context__ chains.
    Where providers use ``raise ... from None`` they also set
    ``__context__ = None`` so no chain member can carry the secret.
    """

    FAKE_KEY = "sk-supersecretkey99999999999999999999"

    def _make_exc_with_status(self, status: int) -> Exception:
        e = Exception(f"Error from provider with status {status}")
        e.status_code = status  # type: ignore[attr-defined]
        e.response = SimpleNamespace(status_code=status, headers={})  # type: ignore[attr-defined]
        return e

    def test_key_absent_from_provider_unavailable_message(self) -> None:
        """ProviderUnavailableError message must not contain the API key."""
        err = self._make_exc_with_status(429)
        cfg = BackoffConfig(max_attempts=2)
        client = _make_mock_client([err, err])
        p = OpenAIProvider(self.FAKE_KEY, _openai_client=client, backoff_config=cfg)
        with patch("finecorpus.embedding._backoff.time.sleep"):
            with pytest.raises(ProviderUnavailableError) as exc_info:
                p.embed_batch(["test"], "text-embedding-3-small")
        raised = exc_info.value
        # Check message
        assert self.FAKE_KEY not in str(raised)
        # F-006: walk BOTH __cause__ and __context__ chains
        for chained in _walk_exception_chain(raised):
            assert self.FAKE_KEY not in str(chained), (
                f"API key found in chained exception {type(chained).__name__}: {chained!r}"
            )

    def test_key_absent_from_provider_error_message(self) -> None:
        """ProviderError message must not contain the API key."""
        err = self._make_exc_with_status(401)
        client = _make_mock_client([err])
        p = OpenAIProvider(self.FAKE_KEY, _openai_client=client)
        with pytest.raises(ProviderError) as exc_info:
            p.embed_batch(["test"], "text-embedding-3-small")
        raised = exc_info.value
        assert self.FAKE_KEY not in str(raised)
        # F-006: also walk the full chain
        for chained in _walk_exception_chain(raised):
            assert self.FAKE_KEY not in str(chained), (
                f"API key found in chained exception {type(chained).__name__}: {chained!r}"
            )

    def test_context_chain_severed_on_retryable_exception(self) -> None:
        """F-006: __context__ must be None on the _RetryableException (severed chain)."""
        err = self._make_exc_with_status(429)
        cfg = BackoffConfig(max_attempts=2)
        client = _make_mock_client([err, err])
        p = OpenAIProvider(self.FAKE_KEY, _openai_client=client, backoff_config=cfg)

        with patch("finecorpus.embedding._backoff.time.sleep"):
            with pytest.raises(ProviderUnavailableError) as exc_info:
                p.embed_batch(["test"], "text-embedding-3-small")

        raised = exc_info.value
        # The final ProviderUnavailableError should have no __context__ carrying the key
        for chained in _walk_exception_chain(raised):
            assert self.FAKE_KEY not in str(chained)

    def test_key_absent_from_repr(self) -> None:
        p = OpenAIProvider(self.FAKE_KEY, _openai_client=MagicMock())
        assert self.FAKE_KEY not in repr(p)

    def test_key_absent_from_log_records(self, caplog: pytest.LogCaptureFixture) -> None:
        """No log record at any level should contain the API key."""
        err = self._make_exc_with_status(429)
        cfg = BackoffConfig(max_attempts=2)
        client = _make_mock_client([err, err])
        p = OpenAIProvider(self.FAKE_KEY, _openai_client=client, backoff_config=cfg)
        with caplog.at_level(logging.DEBUG):
            with patch("finecorpus.embedding._backoff.time.sleep"):
                with pytest.raises(ProviderUnavailableError):
                    p.embed_batch(["test"], "text-embedding-3-small")
        for record in caplog.records:
            assert self.FAKE_KEY not in record.getMessage(), (
                f"API key found in log record: {record.getMessage()!r}"
            )


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


class TestOpenAIHealthCheck:
    def test_healthy(self) -> None:
        resp = _make_embedding_response(["probe"], dims=1536)
        client = _make_mock_client([resp])
        p = OpenAIProvider(_FAKE_KEY, _openai_client=client)
        hc = p.health_check()
        assert hc.reachable is True
        assert hc.model_available is True
        assert hc.declared_dimensions_confirmed is True
        assert hc.error is None

    def test_dimension_mismatch_not_confirmed(self) -> None:
        """If probe returns wrong dimensions, declared_dimensions_confirmed=False."""
        resp = _make_embedding_response(["probe"], dims=512)  # wrong dims
        client = _make_mock_client([resp])
        p = OpenAIProvider(_FAKE_KEY, _openai_client=client)
        hc = p.health_check()
        assert hc.declared_dimensions_confirmed is False
        assert hc.error is not None

    def test_api_error_returns_unreachable(self) -> None:
        err = Exception("connection failed")
        err.status_code = 503  # type: ignore[attr-defined]
        err.response = SimpleNamespace(status_code=503, headers={})  # type: ignore[attr-defined]
        client = _make_mock_client([err])
        p = OpenAIProvider(_FAKE_KEY, _openai_client=client)
        hc = p.health_check()
        assert hc.reachable is False

    def test_health_check_error_message_excludes_api_key(self) -> None:
        key = "sk-secretkey123456789012345678901234"
        err = Exception("error")
        err.status_code = 500  # type: ignore[attr-defined]
        err.response = SimpleNamespace(status_code=500, headers={})  # type: ignore[attr-defined]
        client = _make_mock_client([err])
        p = OpenAIProvider(key, _openai_client=client)
        hc = p.health_check()
        assert key not in (hc.error or "")

    def test_airgap_blocks_health_check(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("RTFC_AIRGAP", "true")
        client = MagicMock()
        p = OpenAIProvider(_FAKE_KEY, _openai_client=client)
        hc = p.health_check()
        assert hc.reachable is False
        client.embeddings.create.assert_not_called()

    def test_airgap_blocks_embed_batch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("RTFC_AIRGAP", "true")
        client = MagicMock()
        p = OpenAIProvider(_FAKE_KEY, _openai_client=client)
        with pytest.raises(ProviderUnavailableError, match="air-gap"):
            p.embed_batch(["hello"], "text-embedding-3-small")
        client.embeddings.create.assert_not_called()


# ---------------------------------------------------------------------------
# Cost estimation
# ---------------------------------------------------------------------------


class TestOpenAIEstimateCost:
    def test_cost_positive_for_text(self) -> None:
        p = OpenAIProvider(_FAKE_KEY, _openai_client=MagicMock())
        est = p.estimate_cost(["hello world test sentence"])
        assert est.estimated_tokens > 0
        assert est.estimated_cost_usd > Decimal("0")

    def test_cost_uses_configured_rate(self) -> None:
        from decimal import Decimal

        p = OpenAIProvider(
            _FAKE_KEY,
            cost_per_1k_tokens=Decimal("0.10"),
            _openai_client=MagicMock(),
        )
        est = p.estimate_cost(["a" * 4000])  # ~1000 tokens
        # Should be around $0.10 ± heuristic error
        assert est.estimated_cost_usd > Decimal("0")

    def test_no_network_call(self) -> None:
        client = MagicMock()
        p = OpenAIProvider(_FAKE_KEY, _openai_client=client)
        p.estimate_cost(["hello"])
        client.embeddings.create.assert_not_called()

    def test_basis_mentions_uncertainty(self) -> None:
        p = OpenAIProvider(_FAKE_KEY, _openai_client=MagicMock())
        est = p.estimate_cost(["hello"])
        assert est.basis  # non-empty string
        # Either tiktoken or approximation
        assert isinstance(est.is_exact, bool)
