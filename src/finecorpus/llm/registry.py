"""Provider registry and factory for internal LLM providers.

Reads ``finecorpus.config.Config`` and constructs the appropriate provider
adapter from the ``internal_llm`` section (provider-abstraction.md §4.2).

Design
------
- ``build_llm_provider_from_config`` is the public factory.  It reads the
  per-operation config, applies fallback to the default block, and returns the
  appropriate provider.
- Secrets (API key) are read from environment variables only (§6.1, §14.2).
  The config key ``internal_llm.*`` never contains credential values.
- Air-gap enforcement (F-003): when ``RTFC_AIRGAP=true`` (or
  ``platform.airgap=True``), requesting a non-local provider raises
  ``ValueError`` immediately at the registry level with an actionable message.
- API keys come from environment variables ONLY — never YAML, never logged,
  never in exceptions or __repr__ (§6.2, §14.2).

Import-linter note
------------------
``finecorpus.llm`` sits at the same layer as ``finecorpus.embedding`` in the
import-linter contract.  With ``from __future__ import annotations``, all
annotations are lazy strings — no runtime import of ``finecorpus.config``
is needed here.  The ``config`` parameter is typed ``Any`` at runtime to avoid
the upward back-edge.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

from finecorpus.llm.base import LLMProvider
from finecorpus.llm.operations import ResolvedOpConfig

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Named result type (RULING 4: replaces bare 2-tuple return)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResolvedProvider:
    """Named result from ``build_llm_provider_from_config``.

    Using a frozen dataclass instead of a bare 2-tuple prevents element
    transposition bugs (provider/op_config swapped silently) and makes
    call-site intent explicit.

    Attributes
    ----------
    provider:
        Constructed ``LLMProvider`` instance ready for ``run_operation`` calls.
    op_config:
        Fully-resolved ``ResolvedOpConfig`` for the requested operation.
    """

    provider: LLMProvider
    op_config: ResolvedOpConfig


# ---------------------------------------------------------------------------
# Environment variable names for secrets (§6.1, §14.2)
# ---------------------------------------------------------------------------

# Checked in order; first non-empty value wins
_OPENAI_KEY_ENV_VARS: list[str] = [
    "FINECORPUS_OPENAI_API_KEY",
    "OPENAI_API_KEY",
]

# Provider sets
_CLOUD_PROVIDER_TARGETS: frozenset[str] = frozenset({"openai", "cloud"})
_LOCAL_PROVIDER_TARGETS: frozenset[str] = frozenset({"ollama", "local"})
_FAKE_PROVIDER_TARGETS: frozenset[str] = frozenset({"fake"})


def _get_openai_api_key() -> str | None:
    """Read the OpenAI API key from environment variables.

    NEVER reads from config file (§6.1, §14.2).  Returns ``None`` if no key
    is configured.  The returned value MUST NOT be logged.
    """
    for env_var in _OPENAI_KEY_ENV_VARS:
        val = os.environ.get(env_var, "").strip()
        if val:
            return val
    return None


def _is_airgap_active(config: Any) -> bool:
    """Return ``True`` when air-gap mode is active (config or env-var)."""
    return config.platform.airgap or os.environ.get("RTFC_AIRGAP", "").lower() in {
        "1",
        "true",
        "yes",
    }


# ---------------------------------------------------------------------------
# Per-operation config resolution
# ---------------------------------------------------------------------------

# Default max_output_tokens per call (D-21(b): enforcement of budgets is Phase 4;
# this ceiling prevents runaway calls in Phase 3).
_DEFAULT_MAX_OUTPUT_TOKENS = 1024

# Operation name → attribute path on LLMOperationsConfig
_OPERATION_ATTR: dict[str, str] = {
    "classification": "classification",
    "augmentation": "augmentation",
    "question_generation": "question_generation",
    "rewriting": "rewriting",
}


def resolve_op_config(config: Any, operation: str) -> ResolvedOpConfig:
    """Resolve the fully-merged per-operation config for *operation*.

    Resolution order (provider-abstraction.md §4.2):
    1. ``internal_llm.operations.<operation>.<field>`` if non-null
    2. ``internal_llm.default.<field>``

    Parameters
    ----------
    config:
        A fully validated ``finecorpus.config.models.Config`` instance.
    operation:
        One of ``"classification"``, ``"augmentation"``,
        ``"question_generation"``, ``"rewriting"``.

    Returns
    -------
    ResolvedOpConfig

    Raises
    ------
    ValueError
        If the operation name is not recognised, or if the resolved provider
        and model are still ``None`` after applying fallbacks.
    """
    llm = config.internal_llm
    default = llm.default

    attr = _OPERATION_ATTR.get(operation)
    if attr is None:
        raise ValueError(
            f"Unknown LLM operation '{operation}'. Expected one of: {list(_OPERATION_ATTR)}."
        )

    op_cfg = getattr(llm.operations, attr)

    # Per-operation fields fall back to default when None
    provider_id: str | None = op_cfg.provider or default.provider
    model_id: str | None = op_cfg.model or default.model
    temperature: float = (
        op_cfg.temperature if op_cfg.temperature is not None else default.temperature
    )

    # max_output_tokens: use per-op if present, else default config, else hardcoded ceiling
    max_output_tokens: int = getattr(default, "max_output_tokens", _DEFAULT_MAX_OUTPUT_TOKENS)

    max_retries: int = llm.max_retries

    if not provider_id:
        raise ValueError(
            f"No LLM provider configured for operation '{operation}'.  "
            f"Set 'internal_llm.default.provider' (or a per-operation override) "
            f"in corpus.yaml, or run the first-run setup."
        )
    if not model_id:
        raise ValueError(
            f"No LLM model configured for operation '{operation}'.  "
            f"Set 'internal_llm.default.model' (or a per-operation override) "
            f"in corpus.yaml, or run the first-run setup."
        )

    return ResolvedOpConfig(
        provider_id=provider_id,
        model_id=model_id,
        temperature=temperature,
        max_output_tokens=max_output_tokens,
        max_retries=max_retries,
    )


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------


def build_llm_provider_from_config(
    config: Any,
    operation: str,
) -> ResolvedProvider:
    """Build an internal LLM provider from *config* for *operation*.

    Parameters
    ----------
    config:
        A fully validated ``finecorpus.config.models.Config`` instance.
    operation:
        One of ``"classification"``, ``"augmentation"``,
        ``"question_generation"``, ``"rewriting"``.

    Returns
    -------
    ResolvedProvider
        Named structure containing the constructed provider and the
        fully-resolved op config for this operation (temperature,
        max_output_tokens, max_retries).  Using a named structure instead
        of a bare 2-tuple prevents element transposition bugs (RULING 4).

    Raises
    ------
    ValueError
        If the operation is unknown, the resolved provider is unrecognised,
        no API key is found for a cloud provider, or air-gap mode blocks
        a cloud provider (F-003).
    """
    op_config = resolve_op_config(config, operation)
    target = op_config.provider_id

    # F-003: registry-level air-gap enforcement
    if _is_airgap_active(config) and target in _CLOUD_PROVIDER_TARGETS:
        raise ValueError(
            f"Air-gap mode is active (RTFC_AIRGAP / platform.airgap=true) but "
            f"a cloud LLM provider ('{target}') was requested for operation '{operation}'. "
            f"Cloud providers require outbound HTTP egress which is blocked in air-gap mode. "
            f"Configure a local provider (e.g. 'ollama') instead."
        )

    if target in _CLOUD_PROVIDER_TARGETS:
        provider = _build_openai(op_config)
    elif target in _LOCAL_PROVIDER_TARGETS:
        provider = _build_ollama(config, op_config)
    elif target in _FAKE_PROVIDER_TARGETS:
        provider = _build_fake(op_config)
    else:
        raise ValueError(
            f"Unknown internal LLM provider '{target}'. "
            f"Expected 'openai' (cloud), 'ollama' (local), or 'fake' (test)."
        )

    return ResolvedProvider(provider=provider, op_config=op_config)


def _build_openai(op_config: ResolvedOpConfig) -> LLMProvider:
    """Construct OpenAILLMProvider from resolved config."""
    from finecorpus.llm.openai_provider import OpenAILLMProvider

    api_key = _get_openai_api_key()
    if not api_key:
        raise ValueError(
            "OpenAI API key is not configured.  "
            "Set OPENAI_API_KEY or FINECORPUS_OPENAI_API_KEY in the environment.  "
            "Never write API keys in corpus.yaml (§14.2)."
        )

    return OpenAILLMProvider(
        api_key=api_key,
        model_id=op_config.model_id,
        max_output_tokens=op_config.max_output_tokens,
    )


def _build_ollama(config: Any, op_config: ResolvedOpConfig) -> LLMProvider:
    """Construct OllamaLLMProvider from resolved config."""
    from finecorpus.llm.ollama_provider import OllamaLLMProvider

    # Ollama endpoint comes from config; not a secret, but stripped from errors
    llm_cfg = config.internal_llm
    # Use the providers.embedding.local.endpoint as a default for Ollama URL
    # if no dedicated LLM endpoint is configured — consistent with §7.3 design.
    endpoint: str = getattr(llm_cfg, "endpoint", None) or "http://localhost:11434"

    return OllamaLLMProvider(
        base_url=endpoint,
        model_id=op_config.model_id,
        max_output_tokens=op_config.max_output_tokens,
    )


def _build_fake(op_config: ResolvedOpConfig) -> LLMProvider:
    """Construct FakeLLMProvider (test environments only)."""
    from finecorpus.llm.fake import FakeLLMProvider

    return FakeLLMProvider(model_id=op_config.model_id)
