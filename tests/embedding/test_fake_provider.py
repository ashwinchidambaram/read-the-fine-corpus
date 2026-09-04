"""Tests for FakeProvider — deterministic test-only embedding provider."""

from __future__ import annotations

import pytest

from finecorpus.embedding.base import ProviderUnavailableError
from finecorpus.embedding.fake import FakeProvider, _sha256_to_vector


class TestSha256ToVector:
    def test_deterministic_same_input(self) -> None:
        v1 = _sha256_to_vector("hello", 64)
        v2 = _sha256_to_vector("hello", 64)
        assert v1 == v2

    def test_different_inputs_produce_different_vectors(self) -> None:
        v1 = _sha256_to_vector("hello", 64)
        v2 = _sha256_to_vector("world", 64)
        assert v1 != v2

    def test_correct_dimensions(self) -> None:
        for dim in [32, 64, 128, 256, 768, 1536]:
            v = _sha256_to_vector("test", dim)
            assert len(v) == dim, f"Expected {dim} dimensions, got {len(v)}"

    def test_values_in_unit_interval(self) -> None:
        v = _sha256_to_vector("sample text", 64)
        for val in v:
            assert 0.0 <= val < 1.0


class TestFakeProviderCapabilities:
    def test_default_capabilities(self) -> None:
        p = FakeProvider()
        caps = p.capabilities
        assert caps.provider_id == "fake"
        assert caps.model_id == "fake-embed-v1"
        assert caps.vector_dimensions == 64
        assert caps.is_local is True
        assert caps.cost_per_1k_tokens is None
        assert caps.cross_lingual is True

    def test_custom_dimensions(self) -> None:
        p = FakeProvider(dimensions=128)
        assert p.capabilities.vector_dimensions == 128

    def test_custom_model_id(self) -> None:
        p = FakeProvider(model_id="my-model")
        assert p.capabilities.model_id == "my-model"

    def test_capabilities_immutable(self) -> None:
        """ProviderCapabilities is a frozen dataclass."""
        p = FakeProvider()
        with pytest.raises((AttributeError, TypeError)):
            p.capabilities.model_id = "changed"  # type: ignore[misc]

    def test_repr_safe(self) -> None:
        """repr() must not contain any credential material."""
        p = FakeProvider()
        r = repr(p)
        assert "fake-embed-v1" in r
        assert "fake" in r
        # Should not raise or error
        assert isinstance(r, str)


class TestFakeProviderEmbedBatch:
    def test_single_text(self) -> None:
        p = FakeProvider()
        result = p.embed_batch(["hello"], "fake-embed-v1")
        assert len(result.embeddings) == 1
        assert len(result.embeddings[0]) == 64
        assert result.model_id == "fake-embed-v1"
        assert result.provider_id == "fake"

    def test_multiple_texts(self) -> None:
        p = FakeProvider()
        texts = ["hello", "world", "test"]
        result = p.embed_batch(texts, "fake-embed-v1")
        assert len(result.embeddings) == len(texts)
        for vec in result.embeddings:
            assert len(vec) == 64

    def test_deterministic_same_text(self) -> None:
        p = FakeProvider()
        r1 = p.embed_batch(["hello world"], "fake-embed-v1")
        r2 = p.embed_batch(["hello world"], "fake-embed-v1")
        assert r1.embeddings == r2.embeddings

    def test_different_texts_produce_different_vectors(self) -> None:
        p = FakeProvider()
        r = p.embed_batch(["hello", "world"], "fake-embed-v1")
        assert r.embeddings[0] != r.embeddings[1]

    def test_correct_dimensions_in_result(self) -> None:
        for dim in [32, 128, 768]:
            p = FakeProvider(dimensions=dim)
            model = "fake-embed-v1"
            p._model_id = model
            p._caps = p._caps.__class__(
                **{**p.capabilities.__dict__, "vector_dimensions": dim, "model_id": model}
            )
            # Rebuild properly with custom model_id to avoid assertion errors
            p2 = FakeProvider(dimensions=dim)
            r = p2.embed_batch(["test"], p2.capabilities.model_id)
            assert len(r.embeddings[0]) == dim

    def test_wrong_model_id_raises(self) -> None:
        p = FakeProvider()
        with pytest.raises(ValueError, match="model"):
            p.embed_batch(["hello"], "wrong-model")

    def test_empty_texts_raises(self) -> None:
        p = FakeProvider()
        with pytest.raises(ValueError, match="empty"):
            p.embed_batch([], "fake-embed-v1")

    def test_fail_on_embed_raises_provider_unavailable(self) -> None:
        p = FakeProvider(fail_on_embed=True)
        with pytest.raises(ProviderUnavailableError) as exc_info:
            p.embed_batch(["hello"], "fake-embed-v1")
        err = exc_info.value
        assert err.provider_id == "fake"
        assert err.model_id == "fake-embed-v1"

    def test_token_count_positive(self) -> None:
        p = FakeProvider()
        result = p.embed_batch(["hello world"], "fake-embed-v1")
        assert result.input_tokens_used > 0

    def test_order_preserved(self) -> None:
        p = FakeProvider()
        texts = ["alpha", "beta", "gamma"]
        result = p.embed_batch(texts, "fake-embed-v1")
        # Each text's vector should match independent single-call
        for i, text in enumerate(texts):
            single = p.embed_batch([text], "fake-embed-v1")
            assert result.embeddings[i] == single.embeddings[0]


class TestFakeProviderHealthCheck:
    def test_healthy_by_default(self) -> None:
        p = FakeProvider()
        hc = p.health_check()
        assert hc.reachable is True
        assert hc.model_available is True
        assert hc.declared_dimensions_confirmed is True
        assert hc.error is None

    def test_fail_on_health(self) -> None:
        p = FakeProvider(fail_on_health=True)
        hc = p.health_check()
        assert hc.reachable is False
        assert hc.model_available is False
        assert hc.declared_dimensions_confirmed is False
        assert hc.error is not None

    def test_latency_ms_present(self) -> None:
        p = FakeProvider()
        hc = p.health_check()
        assert isinstance(hc.latency_ms, float)
        assert hc.latency_ms >= 0.0


class TestFakeProviderEstimateCost:
    def test_zero_cost(self) -> None:
        p = FakeProvider()
        est = p.estimate_cost(["hello", "world"])
        from decimal import Decimal

        assert est.estimated_cost_usd == Decimal("0.0")

    def test_token_count_positive(self) -> None:
        p = FakeProvider()
        est = p.estimate_cost(["hello world test"])
        assert est.estimated_tokens > 0

    def test_not_exact(self) -> None:
        p = FakeProvider()
        est = p.estimate_cost(["hello"])
        assert est.is_exact is False

    def test_empty_list(self) -> None:
        p = FakeProvider()
        est = p.estimate_cost([])
        from decimal import Decimal

        assert est.estimated_tokens == 0
        assert est.estimated_cost_usd == Decimal("0.0")
