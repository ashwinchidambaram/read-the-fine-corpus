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
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from finecorpus.embedding.base import EmbeddingProvider

if TYPE_CHECKING:
    from finecorpus.config.models import Config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Environment variable names for secrets (§6.1, §14.2)
# ---------------------------------------------------------------------------

# Checked in order; first non-empty value wins
_OPENAI_KEY_ENV_VARS: list[str] = [
    "FINECORPUS_OPENAI_API_KEY",  # finecorpus-namespaced
    "OPENAI_API_KEY",  # standard OpenAI convention
]


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


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------


def build_provider_from_config(
    config: Config,
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
        If ``which`` is not ``"cloud"`` or ``"local"``, or if the provider
        is ``"openai"`` but no API key is configured.
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

    if target in {"openai", "cloud"}:
        return _build_openai(config)
    elif target in {"ollama", "local"}:
        return _build_ollama(config)
    else:
        raise ValueError(
            f"Unknown embedding provider '{target}'. Expected 'openai' (cloud) or 'ollama' (local)."
        )


def build_cloud_provider(
    config: Config,
) -> EmbeddingProvider:
    """Build the cloud (OpenAI) embedding provider from *config*."""
    return _build_openai(config)


def build_local_provider(
    config: Config,
) -> EmbeddingProvider:
    """Build the local (Ollama) embedding provider from *config*."""
    return _build_ollama(config)


def _build_openai(
    config: Config,
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
    config: Config,
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
