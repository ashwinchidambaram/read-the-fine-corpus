"""Integration tests for embedding providers — requires live services.

These tests are marked with the ``provider_integration`` pytest marker and
are SKIPPED by default.

To run them:
    uv run pytest -m provider_integration tests/embedding/test_integration.py -v

Requirements:
  - **Ollama** integration: Ollama running at localhost:11434. If the
    ``nomic-embed-text`` model is missing, the test pulls it via the API
    (~270 MB; acceptable per the implementation brief).
  - **OpenAI** integration: ``OPENAI_API_KEY`` (or ``FINECORPUS_OPENAI_API_KEY``)
    must be set in the environment. Tests are skipped if the key is absent.

These tests assert real embedding round-trips and that capability declarations
match reality (dimensions, non-zero vectors). They do NOT assert semantic
quality of embeddings.
"""

from __future__ import annotations

import os

import pytest

# ---------------------------------------------------------------------------
# Pytest marker
# ---------------------------------------------------------------------------

pytestmark = pytest.mark.provider_integration

_SKIP_REASON = (
    "provider_integration tests require live provider services. "
    "Run with: uv run pytest -m provider_integration"
)


# ---------------------------------------------------------------------------
# Ollama integration
# ---------------------------------------------------------------------------


def _ollama_reachable() -> bool:
    """Return True if Ollama is reachable at localhost:11434."""
    try:
        import httpx

        resp = httpx.get("http://localhost:11434/api/tags", timeout=3.0)
        return resp.status_code == 200
    except Exception:
        return False


def _ollama_pull_if_needed(model: str) -> None:
    """Pull *model* via the Ollama API if it is not already present."""
    import httpx

    # Check if model is already available
    try:
        resp = httpx.get("http://localhost:11434/api/tags", timeout=5.0)
        if resp.status_code == 200:
            models = [m.get("name", "") for m in resp.json().get("models", [])]
            if any(model in m for m in models):
                return
    except Exception:
        pass

    # Pull the model (blocking; may take a few minutes for first pull)
    print(f"\nPulling Ollama model '{model}' (this may take a while)...")
    with httpx.Client(timeout=600.0) as client:
        with client.stream("POST", "http://localhost:11434/api/pull", json={"name": model}) as r:
            for line in r.iter_lines():
                if line:
                    pass  # consume stream; progress logged by Ollama


@pytest.fixture(scope="module")
def ollama_available() -> bool:
    reachable = _ollama_reachable()
    if not reachable:
        pytest.skip("Ollama is not reachable at localhost:11434")
    return True


class TestOllamaIntegration:
    """Live Ollama integration tests.

    Skipped if Ollama is not reachable. Model is pulled if missing.
    """

    def test_health_check_returns_ok(self, ollama_available: bool) -> None:
        _ollama_pull_if_needed("nomic-embed-text")
        from finecorpus.embedding.ollama_provider import OllamaProvider

        p = OllamaProvider(model_id="nomic-embed-text")
        hc = p.health_check()
        assert hc.reachable is True, f"Ollama unreachable: {hc.error}"
        assert hc.model_available is True, f"Model unavailable: {hc.error}"
        assert hc.declared_dimensions_confirmed is True, (
            f"Dimension mismatch: {hc.error}. "
            f"Declared={p.capabilities.vector_dimensions}, "
            f"Check the model is nomic-embed-text (768 dims)."
        )
        assert hc.latency_ms > 0

    def test_embed_batch_single_text(self, ollama_available: bool) -> None:
        _ollama_pull_if_needed("nomic-embed-text")
        from finecorpus.embedding.ollama_provider import OllamaProvider

        p = OllamaProvider(model_id="nomic-embed-text")
        result = p.embed_batch(["This is a test sentence for embedding."], "nomic-embed-text")
        assert len(result.embeddings) == 1
        vec = result.embeddings[0]
        assert len(vec) == 768, f"Expected 768 dimensions, got {len(vec)}"
        assert any(v != 0.0 for v in vec), "Embedding vector is all zeros"

    def test_embed_batch_multiple_texts(self, ollama_available: bool) -> None:
        _ollama_pull_if_needed("nomic-embed-text")
        from finecorpus.embedding.ollama_provider import OllamaProvider

        texts = [
            "First sentence about artificial intelligence.",
            "Second sentence about machine learning.",
            "Third sentence about data science.",
        ]
        p = OllamaProvider(model_id="nomic-embed-text")
        result = p.embed_batch(texts, "nomic-embed-text")
        assert len(result.embeddings) == 3
        for vec in result.embeddings:
            assert len(vec) == 768

    def test_capabilities_dimensions_match_reality(self, ollama_available: bool) -> None:
        _ollama_pull_if_needed("nomic-embed-text")
        from finecorpus.embedding.ollama_provider import OllamaProvider

        p = OllamaProvider(model_id="nomic-embed-text")
        assert p.capabilities.vector_dimensions == 768
        assert p.capabilities.is_local is True
        assert p.capabilities.cost_per_1k_tokens is None

    def test_estimate_cost_is_zero(self, ollama_available: bool) -> None:
        from decimal import Decimal

        from finecorpus.embedding.ollama_provider import OllamaProvider

        p = OllamaProvider(model_id="nomic-embed-text")
        est = p.estimate_cost(["hello world"])
        assert est.estimated_cost_usd == Decimal("0.0")
        assert est.estimated_tokens > 0

    def test_api_version_captured(self, ollama_available: bool) -> None:
        _ollama_pull_if_needed("nomic-embed-text")
        from finecorpus.embedding.ollama_provider import OllamaProvider

        p = OllamaProvider(model_id="nomic-embed-text")
        # api_version should be a sha256 digest or "unknown"
        assert isinstance(p.capabilities.api_version, str)
        assert len(p.capabilities.api_version) > 0

    def test_provider_unavailable_error_on_bad_endpoint(self) -> None:
        """Provider at unreachable endpoint raises ProviderUnavailableError."""
        from unittest.mock import patch

        from finecorpus.embedding._backoff import BackoffConfig
        from finecorpus.embedding.base import ProviderUnavailableError
        from finecorpus.embedding.ollama_provider import OllamaProvider

        p = OllamaProvider(
            base_url="http://localhost:19999",  # nothing there
            model_id="nomic-embed-text",
            dimensions=768,
            backoff_config=BackoffConfig(max_attempts=2),
        )
        with patch("finecorpus.embedding._backoff.time.sleep"):
            with pytest.raises(ProviderUnavailableError):
                p.embed_batch(["hello"], "nomic-embed-text")


# ---------------------------------------------------------------------------
# OpenAI integration
# ---------------------------------------------------------------------------


def _openai_key_present() -> str | None:
    """Return the OpenAI API key if configured, else None."""
    for var in ("FINECORPUS_OPENAI_API_KEY", "OPENAI_API_KEY"):
        val = os.environ.get(var, "").strip()
        if val:
            return val
    return None


@pytest.fixture(scope="module")
def openai_api_key() -> str:
    key = _openai_key_present()
    if not key:
        pytest.skip(
            "OPENAI_API_KEY (or FINECORPUS_OPENAI_API_KEY) is not set; "
            "skipping live OpenAI integration tests."
        )
    return key


class TestOpenAIIntegration:
    """Live OpenAI integration tests.

    Skipped if OPENAI_API_KEY is not set.
    """

    def test_health_check_returns_ok(self, openai_api_key: str) -> None:
        from finecorpus.embedding.openai_provider import OpenAIProvider

        p = OpenAIProvider(openai_api_key)
        hc = p.health_check()
        assert hc.reachable is True, f"OpenAI unreachable: {hc.error}"
        assert hc.model_available is True
        assert hc.declared_dimensions_confirmed is True, f"Dimension mismatch: {hc.error}"

    def test_embed_batch_single_text(self, openai_api_key: str) -> None:
        from finecorpus.embedding.openai_provider import OpenAIProvider

        p = OpenAIProvider(openai_api_key)
        result = p.embed_batch(["This is a test sentence."], "text-embedding-3-small")
        assert len(result.embeddings) == 1
        vec = result.embeddings[0]
        assert len(vec) == 1536
        assert any(v != 0.0 for v in vec)

    def test_capabilities_dimensions_match_reality(self, openai_api_key: str) -> None:
        from finecorpus.embedding.openai_provider import OpenAIProvider

        p = OpenAIProvider(openai_api_key)
        assert p.capabilities.vector_dimensions == 1536
        assert p.capabilities.is_local is False
        assert p.capabilities.cost_per_1k_tokens is not None

    def test_estimate_cost_nonzero(self, openai_api_key: str) -> None:
        from decimal import Decimal

        from finecorpus.embedding.openai_provider import OpenAIProvider

        p = OpenAIProvider(openai_api_key)
        est = p.estimate_cost(["hello world test sentence"])
        assert est.estimated_cost_usd > Decimal("0")
        assert est.estimated_tokens > 0
