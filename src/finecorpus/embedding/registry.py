"""Provider registry and factory for embedding providers.

Reads ``finecorpus.config.Config`` and constructs the appropriate provider
adapters from the ``providers.embedding`` section (reference.md §2.3).

Design
------
- ``build_provider_from_config`` is the public factory.  It returns either
  the cloud (OpenAI) or local (Ollama) provider depending on
  ``providers.embedding.default``.
- Secrets (API key) are read from environment variables only (§6.1, §14.2).
  The config key ``providers.embedding.cloud.*`` contains no credential values.
- The OPENAI_API_KEY env var is the standard OpenAI convention; FINECORPUS_
  prefixed variants are also accepted (reference.md §1 env-var convention).
- Air-gap enforcement (F-003): when ``RTFC_AIRGAP=true`` (or
  ``platform.airgap=True``), requesting a non-local provider raises
  ``ValueError`` immediately at the registry level with an actionable message.
  Provider-level guards remain as defence-in-depth.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from finecorpus.embedding.base import EmbeddingProvider

# Config is only referenced in function signatures as a type annotation.
# With `from __future__ import annotations`, all annotations are lazy strings —
# no runtime import of finecorpus.config is needed here.
# F-04: finecorpus.embedding sits below finecorpus.config in the import-linter
# layers contract; embedding importing config would be an upward back-edge.
# Using `Any` for the runtime parameter type avoids that dependency entirely.

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Environment variable names for secrets (§6.1, §14.2)
# ---------------------------------------------------------------------------

# Checked in order; first non-empty value wins
_OPENAI_KEY_ENV_VARS: list[str] = [
    "FINECORPUS_OPENAI_API_KEY",  # finecorpus-namespaced
    "OPENAI_API_KEY",  # standard OpenAI convention
]

# Providers that are NOT local (cloud egress required)
_CLOUD_PROVIDER_TARGETS: frozenset[str] = frozenset({"openai", "cloud"})
# Providers that are local (no egress)
_LOCAL_PROVIDER_TARGETS: frozenset[str] = frozenset({"ollama", "local"})


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
# Public factory
# ---------------------------------------------------------------------------


def build_provider_from_config(
    config: Any,
    which: str | None = None,
) -> EmbeddingProvider:
    """Build an embedding provider from *config*.

    Parameters
    ----------
    config:
        A fully validated ``finecorpus.config.models.Config`` instance.
    which:
        Override which provider to build (``"cloud"`` or ``"local"``).
        When ``None``, reads ``providers.embedding.default``.

    Returns
    -------
    EmbeddingProvider

    Raises
    ------
    ValueError
        If ``which`` is not ``"cloud"`` or ``"local"``, if the provider is
        ``"openai"`` but no API key is configured, or if air-gap mode is
        active and a non-local provider is requested (F-003).
    RuntimeError
        If ``providers.embedding.default`` is not set and ``which`` is not
        specified.
    """
    emb = config.providers.embedding
    target = which or emb.default

    if target is None:
        raise RuntimeError(
            "No embedding provider selected. Set 'providers.embedding.default' "
            "to 'openai' or 'ollama' in corpus.yaml, or pass 'which' explicitly."
        )

    # F-003: registry-level air-gap enforcement — raises immediately with an
    # actionable message before any provider object is constructed.
    if _is_airgap_active(config) and target in _CLOUD_PROVIDER_TARGETS:
        raise ValueError(
            f"Air-gap mode is active (RTFC_AIRGAP / platform.airgap=true) but "
            f"a cloud provider ('{target}') was requested. "
            f"Cloud providers require outbound HTTP egress which is blocked in "
            f"air-gap mode. Configure a local provider (e.g. 'ollama') instead."
        )

    if target in _CLOUD_PROVIDER_TARGETS:
        return _build_openai(config)
    elif target in _LOCAL_PROVIDER_TARGETS:
        return _build_ollama(config)
    else:
        raise ValueError(
            f"Unknown embedding provider '{target}'. Expected 'openai' (cloud) or 'ollama' (local)."
        )


def build_cloud_provider(
    config: Any,
) -> EmbeddingProvider:
    """Build the cloud (OpenAI) embedding provider from *config*."""
    return _build_openai(config)


def build_local_provider(
    config: Any,
) -> EmbeddingProvider:
    """Build the local (Ollama) embedding provider from *config*."""
    return _build_ollama(config)


def _build_openai(
    config: Any,
) -> EmbeddingProvider:
    """Construct OpenAIProvider from config.providers.embedding.cloud."""
    from finecorpus.embedding.openai_provider import OpenAIProvider

    cloud = config.providers.embedding.cloud

    api_key = _get_openai_api_key()
    if not api_key:
        raise ValueError(
            "OpenAI API key is not configured. "
            "Set OPENAI_API_KEY or FINECORPUS_OPENAI_API_KEY in the environment. "
            "Never write API keys in corpus.yaml (§14.2)."
        )

    return OpenAIProvider(
        api_key=api_key,
        model_id=cloud.model,
        dimensions=cloud.dimensions,
        max_batch_size=cloud.max_batch_size,
        cost_per_1k_tokens=cloud.cost_per_1k_tokens,
        pricing_as_of=cloud.pricing_as_of,
    )


def _build_ollama(
    config: Any,
) -> EmbeddingProvider:
    """Construct OllamaProvider from config.providers.embedding.local."""
    from finecorpus.embedding.ollama_provider import OllamaProvider

    local = config.providers.embedding.local

    return OllamaProvider(
        base_url=local.endpoint,
        model_id=local.model,
        dimensions=local.dimensions,
        batch_mode=local.batch_mode,
    )
