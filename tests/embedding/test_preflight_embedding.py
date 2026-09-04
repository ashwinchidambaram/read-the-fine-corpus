"""Tests for the embedding_provider preflight check.

These tests exercise the real preflight check (no longer SKIPPED after Phase 1).
The check is:
  - SKIPPED when providers.embedding.default is not set
  - OK/WARN/FAIL depending on health_check() results
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from finecorpus.config import Config, load_config, run_preflight
from finecorpus.config.preflight import CheckStatus


def write_yaml(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "corpus.yaml"
    p.write_text(textwrap.dedent(content), encoding="utf-8")
    return p


class TestEmbeddingProviderPreflightSkipped:
    """When no provider is configured, the check is SKIPPED — not FAIL."""

    def test_no_default_is_skipped(self, tmp_path: Path) -> None:
        cfg_file = write_yaml(tmp_path, "{}\n")
        cfg = load_config(cfg_file)
        report = run_preflight(cfg)
        emb = next(r for r in report.results if r.name == "embedding_provider")
        assert emb.status == CheckStatus.SKIPPED

    def test_skipped_check_has_informative_message(self, tmp_path: Path) -> None:
        cfg_file = write_yaml(tmp_path, "{}\n")
        cfg = load_config(cfg_file)
        report = run_preflight(cfg)
        emb = next(r for r in report.results if r.name == "embedding_provider")
        assert len(emb.message) > 20


class TestEmbeddingProviderPreflightWithFakeProvider:
    """Inject a FakeProvider via monkeypatching the registry."""

    def _cfg_with_ollama_default(self, tmp_path: Path) -> Config:
        cfg_file = write_yaml(
            tmp_path,
            """
            providers:
              embedding:
                default: "ollama"
                local:
                  model: "nomic-embed-text"
                  dimensions: 768
            """,
        )
        return load_config(cfg_file)

    def test_ok_when_provider_healthy(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import finecorpus.embedding.registry as _registry
        from finecorpus.embedding.fake import FakeProvider

        cfg = self._cfg_with_ollama_default(tmp_path)

        fake = FakeProvider(dimensions=768, model_id="nomic-embed-text")
        monkeypatch.setattr(_registry, "build_provider_from_config", lambda *a, **kw: fake)

        report = run_preflight(cfg)
        emb = next(r for r in report.results if r.name == "embedding_provider")
        assert emb.status == CheckStatus.OK
        assert "confirmed" in emb.message

    def test_fail_when_provider_unreachable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import finecorpus.embedding.registry as _registry
        from finecorpus.embedding.fake import FakeProvider

        cfg = self._cfg_with_ollama_default(tmp_path)
        fake = FakeProvider(dimensions=768, model_id="nomic-embed-text", fail_on_health=True)
        monkeypatch.setattr(_registry, "build_provider_from_config", lambda *a, **kw: fake)

        report = run_preflight(cfg)
        emb = next(r for r in report.results if r.name == "embedding_provider")
        assert emb.status == CheckStatus.FAIL
        assert not report.passed

    def test_fail_when_provider_construction_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import finecorpus.embedding.registry as _registry

        cfg = self._cfg_with_ollama_default(tmp_path)

        def _raise(*a: object, **kw: object) -> None:
            raise ValueError("config error")

        monkeypatch.setattr(_registry, "build_provider_from_config", _raise)

        report = run_preflight(cfg)
        emb = next(r for r in report.results if r.name == "embedding_provider")
        assert emb.status == CheckStatus.FAIL

    def test_airgap_blocks_cloud_provider(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from decimal import Decimal

        import finecorpus.embedding.registry as _registry
        from finecorpus.embedding.base import ProviderCapabilities
        from finecorpus.embedding.fake import FakeProvider

        cfg_file = write_yaml(
            tmp_path,
            """
            platform:
              airgap: true
            providers:
              embedding:
                default: "openai"
                cloud:
                  model: "text-embedding-3-small"
                  dimensions: 1536
            """,
        )
        cfg = load_config(cfg_file)

        # Build a fake that claims is_local=False (cloud)
        class _CloudFake(FakeProvider):
            def __init__(self) -> None:
                super().__init__(dimensions=1536, model_id="text-embedding-3-small")
                self._caps = ProviderCapabilities(
                    provider_id="openai",
                    model_id="text-embedding-3-small",
                    vector_dimensions=1536,
                    max_input_tokens=8191,
                    max_batch_size=2048,
                    supported_languages="*",
                    cross_lingual=True,
                    is_local=False,  # cloud
                    cost_per_1k_tokens=Decimal("0.00002"),
                    pricing_as_of="2026-09-03",
                    api_version="openai-v1",
                )

        monkeypatch.setattr(_registry, "build_provider_from_config", lambda *a, **kw: _CloudFake())

        report = run_preflight(cfg)
        emb = next(r for r in report.results if r.name == "embedding_provider")
        assert emb.status == CheckStatus.FAIL
        assert "air-gap" in emb.message.lower() or "airgap" in emb.message.lower()


class TestPreflightEmbeddingBackwardCompat:
    """Old tests expected SKIPPED; with Phase 1 the check is now real.
    When providers.embedding.default is unset → still SKIPPED (never FAIL).
    """

    def test_no_provider_configured_still_skipped_not_fail(self, tmp_path: Path) -> None:
        cfg_file = write_yaml(tmp_path, "{}\n")
        cfg = load_config(cfg_file)
        report = run_preflight(cfg)
        emb = next(r for r in report.results if r.name == "embedding_provider")
        # SKIPPED is not FAIL → preflight still passes for other checks
        assert emb.status == CheckStatus.SKIPPED
        # SKIPPED counts as "not FAIL" in passed
        assert emb.status != CheckStatus.FAIL
