"""Tests for OllamaProvider against mocked HTTP (no live calls).

Uses respx or httpx transport mocking via pytest monkeypatching.
Tests cover:
  - embed_batch single-text mode
  - embed_batch batch mode
  - model_id validation
  - retry/backoff on 503/429
  - exhaustion raises ProviderUnavailableError
  - health check happy + unhappy paths
  - api_version from /api/show digest
  - cost estimation (always zero, tokens approximated)
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from finecorpus.embedding._backoff import BackoffConfig
from finecorpus.embedding.base import ProviderError, ProviderUnavailableError
from finecorpus.embedding.ollama_provider import OllamaProvider

# ---------------------------------------------------------------------------
# HTTP transport mock helpers
# ---------------------------------------------------------------------------


def _make_embed_response(
    embeddings: list[list[float]],
    status: int = 200,
) -> httpx.Response:
    """Build a minimal Ollama /api/embed httpx response."""
    body = json.dumps({"embeddings": embeddings}).encode()
    return httpx.Response(status, content=body)


def _make_show_response(digest: str | None = None, status: int = 200) -> httpx.Response:
    body: dict[str, Any] = {}
    if digest:
        body["digest"] = digest
    return httpx.Response(status, content=json.dumps(body).encode())


def _make_error_response(status: int) -> httpx.Response:
    body = json.dumps({"error": f"HTTP {status}"}).encode()
    return httpx.Response(status, content=body)


# ---------------------------------------------------------------------------
# Custom transport for mocking httpx.Client
# ---------------------------------------------------------------------------


class _StubTransport(httpx.BaseTransport):
    """Returns pre-configured responses in order."""

    def __init__(self, responses: list[httpx.Response]) -> None:
        self._responses = list(responses)
        self._index = 0

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if self._index >= len(self._responses):
            return httpx.Response(503, content=b'{"error":"no more responses"}')
        resp = self._responses[self._index]
        self._index += 1
        # Attach the request for inspection
        resp._request = request  # type: ignore[attr-defined]
        return resp


def _make_client_with_responses(responses: list[httpx.Response]) -> httpx.Client:
    transport = _StubTransport(responses)
    return httpx.Client(transport=transport, base_url="http://localhost:11434")


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestOllamaProviderConstruction:
    def test_known_model_defaults(self) -> None:
        client = _make_client_with_responses([_make_show_response()])
        p = OllamaProvider(model_id="nomic-embed-text", _http_client=client)
        assert p.capabilities.vector_dimensions == 768
        assert p.capabilities.provider_id == "ollama"
        assert p.capabilities.is_local is True

    def test_bge_m3_defaults(self) -> None:
        client = _make_client_with_responses([_make_show_response()])
        p = OllamaProvider(model_id="bge-m3", _http_client=client)
        assert p.capabilities.vector_dimensions == 1024
        assert p.capabilities.cross_lingual is True

    def test_unknown_model_without_dimensions_raises(self) -> None:
        client = _make_client_with_responses([_make_show_response()])
        with pytest.raises(ValueError, match="dimensions must be supplied"):
            OllamaProvider(model_id="unknown-model", _http_client=client)

    def test_unknown_model_with_explicit_dims(self) -> None:
        client = _make_client_with_responses([_make_show_response()])
        p = OllamaProvider(model_id="custom", dimensions=256, _http_client=client)
        assert p.capabilities.vector_dimensions == 256

    def test_api_version_from_digest(self) -> None:
        digest = "sha256:abc123def456abc123def456abc123def456abc123def456abc123def456abcd"
        client = _make_client_with_responses([_make_show_response(digest=digest)])
        p = OllamaProvider(model_id="nomic-embed-text", _http_client=client)
        assert digest[:71] in p.capabilities.api_version

    def test_api_version_unknown_when_show_fails(self) -> None:
        client = _make_client_with_responses([_make_error_response(404)])
        p = OllamaProvider(model_id="nomic-embed-text", _http_client=client)
        assert p.capabilities.api_version == "unknown"

    def test_repr_safe(self) -> None:
        client = _make_client_with_responses([_make_show_response()])
        p = OllamaProvider(model_id="nomic-embed-text", _http_client=client)
        r = repr(p)
        assert "ollama" in r
        assert "nomic-embed-text" in r


# ---------------------------------------------------------------------------
# embed_batch — single-text mode (default)
# ---------------------------------------------------------------------------


class TestOllamaEmbedBatchSingle:
    def test_single_text_success(self) -> None:
        vec = [0.1] * 768
        show = _make_show_response()
        embed = _make_embed_response([vec])
        client = _make_client_with_responses([show, embed])
        p = OllamaProvider(model_id="nomic-embed-text", _http_client=client)
        result = p.embed_batch(["hello"], "nomic-embed-text")
        assert len(result.embeddings) == 1
        assert len(result.embeddings[0]) == 768
        assert result.provider_id == "ollama"

    def test_multiple_texts_calls_api_per_text(self) -> None:
        vecs = [[float(i)] * 768 for i in range(3)]
        show = _make_show_response()
        embeds = [_make_embed_response([v]) for v in vecs]
        client = _make_client_with_responses([show] + embeds)
        p = OllamaProvider(model_id="nomic-embed-text", _http_client=client)
        result = p.embed_batch(["a", "b", "c"], "nomic-embed-text")
        assert len(result.embeddings) == 3

    def test_wrong_model_id_raises(self) -> None:
        client = _make_client_with_responses([_make_show_response()])
        p = OllamaProvider(model_id="nomic-embed-text", _http_client=client)
        with pytest.raises(ValueError, match="model"):
            p.embed_batch(["hello"], "wrong-model")

    def test_empty_texts_raises(self) -> None:
        client = _make_client_with_responses([_make_show_response()])
        p = OllamaProvider(model_id="nomic-embed-text", _http_client=client)
        with pytest.raises(ValueError, match="empty"):
            p.embed_batch([], "nomic-embed-text")

    def test_empty_embedding_response_raises_provider_error(self) -> None:
        show = _make_show_response()
        bad_resp = httpx.Response(200, content=b'{"embeddings":[]}')
        client = _make_client_with_responses([show, bad_resp])
        p = OllamaProvider(model_id="nomic-embed-text", _http_client=client)
        with pytest.raises(ProviderError):
            p.embed_batch(["hello"], "nomic-embed-text")

    def test_token_count_positive(self) -> None:
        show = _make_show_response()
        embed = _make_embed_response([[0.1] * 768])
        client = _make_client_with_responses([show, embed])
        p = OllamaProvider(model_id="nomic-embed-text", _http_client=client)
        result = p.embed_batch(["hello world"], "nomic-embed-text")
        assert result.input_tokens_used > 0


# ---------------------------------------------------------------------------
# embed_batch — batch mode
# ---------------------------------------------------------------------------


class TestOllamaEmbedBatchMode:
    def test_batch_mode_single_api_call(self) -> None:
        vecs = [[0.1] * 768, [0.2] * 768, [0.3] * 768]
        show = _make_show_response()
        embed = _make_embed_response(vecs)
        client = _make_client_with_responses([show, embed])
        p = OllamaProvider(model_id="nomic-embed-text", batch_mode=True, _http_client=client)
        result = p.embed_batch(["a", "b", "c"], "nomic-embed-text")
        assert len(result.embeddings) == 3

    def test_batch_mode_wrong_count_raises_provider_error(self) -> None:
        # Returns only 1 embedding for 2 texts
        show = _make_show_response()
        embed = _make_embed_response([[0.1] * 768])
        client = _make_client_with_responses([show, embed])
        p = OllamaProvider(model_id="nomic-embed-text", batch_mode=True, _http_client=client)
        with pytest.raises(ProviderError):
            p.embed_batch(["a", "b"], "nomic-embed-text")


# ---------------------------------------------------------------------------
# Retry / backoff
# ---------------------------------------------------------------------------


class TestOllamaRetry:
    def test_retries_on_503_then_succeeds(self) -> None:
        show = _make_show_response()
        err = _make_error_response(503)
        success = _make_embed_response([[0.1] * 768])
        client = _make_client_with_responses([show, err, success])
        cfg = BackoffConfig(max_attempts=3)
        p = OllamaProvider(model_id="nomic-embed-text", backoff_config=cfg, _http_client=client)
        with patch("finecorpus.embedding._backoff.time.sleep"):
            result = p.embed_batch(["hello"], "nomic-embed-text")
        assert len(result.embeddings) == 1

    def test_exhausted_raises_provider_unavailable(self) -> None:
        show = _make_show_response()
        errors = [_make_error_response(503)] * 3
        client = _make_client_with_responses([show] + errors)
        cfg = BackoffConfig(max_attempts=3)
        p = OllamaProvider(model_id="nomic-embed-text", backoff_config=cfg, _http_client=client)
        with patch("finecorpus.embedding._backoff.time.sleep"):
            with pytest.raises(ProviderUnavailableError) as exc_info:
                p.embed_batch(["hello"], "nomic-embed-text")
        assert exc_info.value.provider_id == "ollama"
        assert exc_info.value.attempts == 3

    def test_connection_error_is_retryable(self) -> None:
        """httpx.ConnectError should trigger retry logic."""
        vec = [0.1] * 768
        success = _make_embed_response([vec])

        class _FailOnce(httpx.BaseTransport):
            def __init__(self) -> None:
                self._n = 0

            def handle_request(self, req: httpx.Request) -> httpx.Response:
                if req.url.path == "/api/show":
                    return _make_show_response()
                self._n += 1
                if self._n == 1:
                    raise httpx.ConnectError("connection refused")
                return success

        transport = _FailOnce()
        client = httpx.Client(transport=transport, base_url="http://localhost:11434")
        cfg = BackoffConfig(max_attempts=3)
        p = OllamaProvider(model_id="nomic-embed-text", backoff_config=cfg, _http_client=client)
        with patch("finecorpus.embedding._backoff.time.sleep"):
            result = p.embed_batch(["hello"], "nomic-embed-text")
        assert len(result.embeddings) == 1


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


class TestOllamaHealthCheck:
    def test_healthy(self) -> None:
        show = _make_show_response()
        probe = _make_embed_response([[0.1] * 768])
        client = _make_client_with_responses([show, probe])
        p = OllamaProvider(model_id="nomic-embed-text", _http_client=client)
        hc = p.health_check()
        assert hc.reachable is True
        assert hc.model_available is True
        assert hc.declared_dimensions_confirmed is True
        assert hc.error is None

    def test_endpoint_unreachable(self) -> None:
        class _AlwaysConnectError(httpx.BaseTransport):
            def handle_request(self, req: httpx.Request) -> httpx.Response:
                raise httpx.ConnectError("refused")

        transport = _AlwaysConnectError()
        client = httpx.Client(transport=transport, base_url="http://localhost:11434")
        # Skip /api/show failure for construction; use dimensions explicitly
        p = OllamaProvider(model_id="nomic-embed-text", dimensions=768, _http_client=client)
        hc = p.health_check()
        assert hc.reachable is False
        assert hc.model_available is False

    def test_dimension_mismatch(self) -> None:
        # nomic-embed-text configured as 768 but probe returns 512
        show = _make_show_response()
        probe = _make_embed_response([[0.1] * 512])  # wrong dims
        client = _make_client_with_responses([show, probe])
        p = OllamaProvider(model_id="nomic-embed-text", _http_client=client)
        hc = p.health_check()
        assert hc.declared_dimensions_confirmed is False
        assert hc.error is not None

    def test_non_200_means_model_unavailable(self) -> None:
        show = _make_show_response()
        probe = _make_error_response(404)
        client = _make_client_with_responses([show, probe])
        p = OllamaProvider(model_id="nomic-embed-text", _http_client=client)
        hc = p.health_check()
        assert hc.model_available is False

    def test_error_message_does_not_contain_base_url(self) -> None:
        """Error messages must not contain the base_url (may have auth — §6.2)."""

        class _ConnectError(httpx.BaseTransport):
            def handle_request(self, req: httpx.Request) -> httpx.Response:
                raise httpx.ConnectError("refused")

        client = httpx.Client(
            transport=_ConnectError(),
            base_url="http://user:secretpassword@localhost:11434",
        )
        p = OllamaProvider(model_id="nomic-embed-text", dimensions=768, _http_client=client)
        hc = p.health_check()
        assert "secretpassword" not in (hc.error or "")


# ---------------------------------------------------------------------------
# Cost estimation
# ---------------------------------------------------------------------------


class TestOllamaEstimateCost:
    def test_zero_cost(self) -> None:
        client = _make_client_with_responses([_make_show_response()])
        p = OllamaProvider(model_id="nomic-embed-text", _http_client=client)
        est = p.estimate_cost(["hello world"])
        assert est.estimated_cost_usd == Decimal("0.0")

    def test_positive_token_count(self) -> None:
        client = _make_client_with_responses([_make_show_response()])
        p = OllamaProvider(model_id="nomic-embed-text", _http_client=client)
        est = p.estimate_cost(["hello world test"])
        assert est.estimated_tokens > 0

    def test_no_network_call(self) -> None:
        """estimate_cost MUST NOT make network calls."""
        show = _make_show_response()

        class _RecordingTransport(httpx.BaseTransport):
            def __init__(self) -> None:
                self.call_count = 0
                self._inner = _StubTransport([show])

            def handle_request(self, req: httpx.Request) -> httpx.Response:
                self.call_count += 1
                return self._inner.handle_request(req)

        transport = _RecordingTransport()
        http = httpx.Client(transport=transport, base_url="http://localhost:11434")
        p = OllamaProvider(model_id="nomic-embed-text", _http_client=http)
        initial_count = transport.call_count
        p.estimate_cost(["hello"])
        assert transport.call_count == initial_count  # no new calls
