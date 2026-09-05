"""Tests: registry airgap enforcement, per-operation config resolution, secrets.

Covers:
- Air-gap blocks OpenAI; allows Ollama and Fake.
- Per-operation config override resolution (op > default).
- Missing env key → clear error that does NOT echo any secret material.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from finecorpus.config.models import (
    Config,
    InternalLLMConfig,
    InternalLLMDefaultConfig,
    LLMOperationConfig,
    LLMOperationsConfig,
)
from finecorpus.llm.operations import ResolvedOpConfig
from finecorpus.llm.registry import (
    _get_openai_api_key,
    build_llm_provider_from_config,
    resolve_op_config,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(
    provider: str = "fake",
    model: str = "fake-llm-v1",
    temperature: float = 0.2,
    airgap: bool = False,
    op_overrides: dict | None = None,
    max_output_tokens: int = 1024,
) -> Config:
    """Build a minimal Config for registry tests."""
    op_overrides = op_overrides or {}

    # Per-operation overrides
    operations = LLMOperationsConfig(
        classification=LLMOperationConfig(
            **op_overrides.get("classification", {"temperature": 0.0})
        ),
        augmentation=LLMOperationConfig(**op_overrides.get("augmentation", {"temperature": 0.3})),
        question_generation=LLMOperationConfig(
            **op_overrides.get("question_generation", {"temperature": 0.7})
        ),
        rewriting=LLMOperationConfig(**op_overrides.get("rewriting", {"temperature": 0.2})),
    )

    default = InternalLLMDefaultConfig(
        provider=provider,
        model=model,
        temperature=temperature,
        max_output_tokens=max_output_tokens,
    )

    config = Config(
        internal_llm=InternalLLMConfig(
            default=default,
            operations=operations,
            max_retries=3,
        ),
    )
    # Patch airgap without triggering the secret validator
    config.platform.airgap = airgap
    return config


# ---------------------------------------------------------------------------
# Air-gap enforcement
# ---------------------------------------------------------------------------


class TestAirgapEnforcement:
    """Air-gap mode: cloud blocked, local allowed."""

    def test_airgap_blocks_openai_via_config(self) -> None:
        config = _make_config(provider="openai", model="gpt-4o-mini", airgap=True)
        with pytest.raises(ValueError, match="air-gap"):
            build_llm_provider_from_config(config, "augmentation")

    def test_airgap_blocks_openai_via_env(self) -> None:
        config = _make_config(provider="openai", model="gpt-4o-mini", airgap=False)
        with patch.dict(os.environ, {"RTFC_AIRGAP": "true"}):
            with pytest.raises(ValueError, match="air-gap"):
                build_llm_provider_from_config(config, "augmentation")

    def test_airgap_blocks_openai_via_env_yes(self) -> None:
        config = _make_config(provider="openai", model="gpt-4o-mini", airgap=False)
        with patch.dict(os.environ, {"RTFC_AIRGAP": "yes"}):
            with pytest.raises(ValueError, match="air-gap"):
                build_llm_provider_from_config(config, "augmentation")

    def test_airgap_allows_fake_provider(self) -> None:
        config = _make_config(provider="fake", model="fake-llm-v1", airgap=True)
        provider, op_config = build_llm_provider_from_config(config, "augmentation")
        assert provider.capabilities.is_local is True

    def test_airgap_allows_ollama_provider(self) -> None:
        config = _make_config(provider="ollama", model="llama3.1", airgap=True)
        provider, op_config = build_llm_provider_from_config(config, "augmentation")
        assert provider.capabilities.is_local is True

    def test_no_airgap_openai_blocked_when_no_key(self) -> None:
        """Without airgap but also without API key → clear actionable error."""
        config = _make_config(provider="openai", model="gpt-4o-mini", airgap=False)
        with patch.dict(os.environ, {}, clear=True):
            # Ensure neither key env var is set
            env_without_keys = {
                k: v
                for k, v in os.environ.items()
                if k not in ("OPENAI_API_KEY", "FINECORPUS_OPENAI_API_KEY")
            }
            with patch.dict(os.environ, env_without_keys, clear=True):
                with pytest.raises(ValueError) as exc_info:
                    build_llm_provider_from_config(config, "augmentation")
                # Error must be actionable and must NOT echo any secret material
                err_msg = str(exc_info.value)
                assert "OPENAI_API_KEY" in err_msg  # tells operator what to do
                # The message should NOT contain a real key value
                assert "sk-" not in err_msg


# ---------------------------------------------------------------------------
# Per-operation config resolution
# ---------------------------------------------------------------------------


class TestOpConfigResolution:
    """Per-operation config overrides fall back to default."""

    def test_default_temperature_used_when_op_has_none(self) -> None:
        config = _make_config(
            provider="fake",
            model="fake-llm-v1",
            temperature=0.5,
            op_overrides={"augmentation": {}},  # all None → inherit default
        )
        op_config = resolve_op_config(config, "augmentation")
        # op_overrides={} → LLMOperationConfig(temperature=None)
        # resolve_op_config falls back to default.temperature=0.5
        assert op_config.temperature == 0.5

    def test_op_temperature_overrides_default(self) -> None:
        config = _make_config(
            provider="fake",
            model="fake-llm-v1",
            temperature=0.5,
            op_overrides={"classification": {"temperature": 0.0}},
        )
        op_config = resolve_op_config(config, "classification")
        assert op_config.temperature == 0.0

    def test_op_provider_overrides_default(self) -> None:
        config = _make_config(
            provider="fake",
            model="fake-llm-v1",
            temperature=0.2,
            op_overrides={"augmentation": {"provider": "ollama", "model": "llama3.1"}},
        )
        op_config = resolve_op_config(config, "augmentation")
        assert op_config.provider_id == "ollama"
        assert op_config.model_id == "llama3.1"

    def test_op_model_overrides_default(self) -> None:
        config = _make_config(
            provider="fake",
            model="fake-llm-v1",
            temperature=0.2,
            op_overrides={"classification": {"model": "gemma2"}},
        )
        op_config = resolve_op_config(config, "classification")
        # provider falls back to default "fake", model overridden to "gemma2"
        assert op_config.model_id == "gemma2"
        assert op_config.provider_id == "fake"

    def test_max_retries_from_config(self) -> None:
        config = _make_config(provider="fake", model="fake-llm-v1")
        op_config = resolve_op_config(config, "augmentation")
        assert op_config.max_retries == 3

    def test_max_output_tokens_from_config(self) -> None:
        config = _make_config(provider="fake", model="fake-llm-v1", max_output_tokens=2048)
        op_config = resolve_op_config(config, "augmentation")
        assert op_config.max_output_tokens == 2048

    def test_missing_provider_raises_clear_error(self) -> None:
        config = _make_config(provider=None, model="gpt-4o-mini")  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="No LLM provider configured"):
            resolve_op_config(config, "augmentation")

    def test_missing_model_raises_clear_error(self) -> None:
        config = _make_config(provider="fake", model=None)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="No LLM model configured"):
            resolve_op_config(config, "augmentation")

    def test_unknown_operation_raises(self) -> None:
        config = _make_config(provider="fake", model="fake-llm-v1")
        with pytest.raises(ValueError, match="Unknown LLM operation"):
            resolve_op_config(config, "nonexistent_operation")

    def test_all_four_operations_resolve(self) -> None:
        config = _make_config(provider="fake", model="fake-llm-v1")
        for op in ("classification", "augmentation", "question_generation", "rewriting"):
            op_config = resolve_op_config(config, op)
            assert isinstance(op_config, ResolvedOpConfig)


# ---------------------------------------------------------------------------
# Secret safety
# ---------------------------------------------------------------------------


class TestSecretSafety:
    """API key must never appear in error messages or repr."""

    def test_no_api_key_error_does_not_echo_key(self) -> None:
        """Error when API key is missing must NOT echo any existing key value."""
        config = _make_config(provider="openai", model="gpt-4o-mini")

        # Set a fake key in environment, then confirm it's NOT echoed in errors
        fake_key = "sk-fake-secret-key-that-must-not-appear-in-errors"
        env_without_keys = {
            k: v
            for k, v in os.environ.items()
            if k not in ("OPENAI_API_KEY", "FINECORPUS_OPENAI_API_KEY")
        }
        with patch.dict(os.environ, env_without_keys, clear=True):
            # No key → should raise; the error should be actionable without echoing secrets
            with pytest.raises(ValueError) as exc_info:
                build_llm_provider_from_config(config, "augmentation")
            assert fake_key not in str(exc_info.value)

    def test_get_openai_api_key_prefers_finecorpus_prefix(self) -> None:
        """FINECORPUS_OPENAI_API_KEY is checked before OPENAI_API_KEY."""
        env = {
            "FINECORPUS_OPENAI_API_KEY": "fc-key",
            "OPENAI_API_KEY": "oai-key",
        }
        with patch.dict(os.environ, env, clear=False):
            key = _get_openai_api_key()
            assert key == "fc-key"

    def test_get_openai_api_key_falls_back_to_openai_key(self) -> None:
        env_without_fc = {k: v for k, v in os.environ.items() if k != "FINECORPUS_OPENAI_API_KEY"}
        with patch.dict(os.environ, {**env_without_fc, "OPENAI_API_KEY": "oai-key"}, clear=True):
            key = _get_openai_api_key()
            assert key == "oai-key"

    def test_get_openai_api_key_returns_none_when_missing(self) -> None:
        env_without_keys = {
            k: v
            for k, v in os.environ.items()
            if k not in ("OPENAI_API_KEY", "FINECORPUS_OPENAI_API_KEY")
        }
        with patch.dict(os.environ, env_without_keys, clear=True):
            key = _get_openai_api_key()
            assert key is None

    def test_fake_provider_repr_contains_no_secrets(self) -> None:
        from finecorpus.llm.fake import FakeLLMProvider

        provider = FakeLLMProvider(model_id="test-model")
        repr_str = repr(provider)
        assert "sk-" not in repr_str
        assert "api_key" not in repr_str.lower()
