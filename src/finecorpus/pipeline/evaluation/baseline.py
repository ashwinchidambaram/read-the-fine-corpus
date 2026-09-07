"""§9.3 naive baseline reference configuration — pure functions.

The naive baseline is defined, not improvised (§9.3 spec).  Every reported sweep
delta is measured against this fixed reference configuration so that deltas are
comparable across knowledge bases and across releases.

§9.3 definition:
  - Recursive character splitting at 512 tokens
  - Overlap in [50, 100] tokens — pinned here to 75 (midpoint)
  - Dense retrieval only (no hybrid, no reranking)
  - No augmentation beyond Tier 1 (tier2_enabled=False, tier3_enabled=False)
  - The KB's configured embedding model (caller supplies EmbeddingConfig)

This module has NO I/O, NO network, NO imports from index/retrieval/services.
It may import from finecorpus.contracts and from finecorpus.pipeline.plan.config_version
(pipeline may import pipeline, per the brief).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from finecorpus.contracts.ingestion_config import (
    ChunkingConfig,
    ChunkingStrategy,
    EmbeddingConfig,
    NaiveBaselineRef,
    RetrievalStrategy,
    TransformationSettings,
)

# ---------------------------------------------------------------------------
# Stable identity for the §9.3 reference
# ---------------------------------------------------------------------------

NAIVE_BASELINE_REFERENCE_ID: str = "naive-baseline-v1"
"""Stable identifier for the §9.3 pinned reference configuration.

If the reference ever changes, this ID must be incremented so that previously
reported baselines can be marked as measured against the old reference (§9.3).
"""

_NAIVE_BASELINE_DESCRIPTION: str = (
    "§9.3 naive baseline: recursive_char 512 tokens / 75-token overlap / "
    "dense retrieval / Tier 1 only / KB embedding model."
)

# ---------------------------------------------------------------------------
# Reference parameter constants (§9.3 pinned values)
# ---------------------------------------------------------------------------

_REF_CHUNK_STRATEGY: ChunkingStrategy = ChunkingStrategy.recursive_char
_REF_MAX_TOKENS: int = 512
_REF_OVERLAP_TOKENS: int = 75  # midpoint of the [50, 100] §9.3 range
_REF_RETRIEVAL_STRATEGY: RetrievalStrategy = RetrievalStrategy.dense


# ---------------------------------------------------------------------------
# Reference config builder
# ---------------------------------------------------------------------------


def reference_ingestion_config(embedding: EmbeddingConfig) -> dict[str, Any]:
    """Return the build-affecting fields of the §9.3 pinned reference configuration.

    Because IngestionConfig requires many provenance/tenancy fields that are
    KB-specific and outside the scope of a pure baseline, this function returns
    the build-affecting sub-config as a plain dict that can be:
      1. Fingerprinted via reference_fingerprint().
      2. Embedded in a full IngestionConfig by the sweep orchestrator.

    The dict contains exactly the fields that vary between sweep candidates and
    are build-affecting: chunking, transformation settings, retrieval strategy,
    and embedding identity.

    Args:
        embedding: The KB's configured embedding model.

    Returns:
        Dict with keys: chunking, transformation, retrieval_strategy, embedding.
    """
    chunking = ChunkingConfig(
        strategy=_REF_CHUNK_STRATEGY,
        max_tokens=_REF_MAX_TOKENS,
        overlap_tokens=_REF_OVERLAP_TOKENS,
        tokenizer="whitespace_word",
        respect_headings=False,
        atomic_rows=None,
        repeat_headers_on_split=None,
        split_boundaries=None,
    )
    transformation = TransformationSettings(
        tier1_enabled=True,
        tier1_operations=[],  # Tier 1 on, but no specific operations required by baseline
        tier2_enabled=False,
        tier2_operations=[],
        tier3_enabled=False,
        tier3_settings=None,
        retain_original_ref=False,
        mark_rewritten_chunks=False,
    )
    return {
        "chunking": chunking.model_dump(mode="json"),
        "transformation": transformation.model_dump(mode="json"),
        "retrieval_strategy": _REF_RETRIEVAL_STRATEGY.value,
        "embedding": embedding.model_dump(mode="json"),
    }


def build_naive_baseline_ref(embedding: EmbeddingConfig) -> NaiveBaselineRef:
    """Build the NaiveBaselineRef contract object for the §9.3 pinned reference.

    NaiveBaselineRef has extra="forbid", so only its declared fields are set:
    reference_id and description.

    The embedding is accepted so that the description can note the KB's model
    identity, making the reference traceable to the specific embedding used.

    Args:
        embedding: The KB's configured embedding model.

    Returns:
        NaiveBaselineRef with the §9.3 reference_id and description.
    """
    return NaiveBaselineRef(
        reference_id=NAIVE_BASELINE_REFERENCE_ID,
        description=(
            f"{_NAIVE_BASELINE_DESCRIPTION} "
            f"embedding={embedding.provider}/{embedding.model} "
            f"dims={embedding.dimensions}"
        ),
    )


# ---------------------------------------------------------------------------
# Reference fingerprint (M-049)
# ---------------------------------------------------------------------------


def reference_fingerprint(embedding: EmbeddingConfig) -> str:
    """Deterministic sha256 fingerprint of the §9.3 reference build-affecting fields.

    Used for M-049 reference-change detection: if the fingerprint changes across
    releases, previously reported baselines were measured against a different
    reference and MUST be marked as such (§9.3).

    The fingerprint covers exactly the build-affecting fields returned by
    reference_ingestion_config() PLUS only the build-affecting subset of EmbeddingConfig:
    provider, model, dimensions, and normalize.  The field supports_languages is
    intentionally excluded because it governs language-support capability checks (§7.6)
    at retrieval time and does NOT affect the produced vectors or chunk identity.
    Including it would cause spurious M-049 false positives whenever an operator updates
    the language-support metadata without changing the embedding model itself.

    Build-affecting embedding fields: provider, model, dimensions, normalize.
    Non-build-affecting (excluded): supports_languages.

    It is stable across Python runs and process restarts because it is derived from
    canonical JSON (sorted keys, compact separators).

    Args:
        embedding: The KB's configured embedding model.  Changes to the build-affecting
                   fields (provider, model, dimensions, normalize) change the fingerprint.
                   Changes to supports_languages alone do NOT change the fingerprint.

    Returns:
        64-character lowercase hex sha256 digest.
    """
    config_fields = reference_ingestion_config(embedding)
    # Override the embedding entry with only build-affecting fields to exclude
    # supports_languages (non-build-affecting; must not cause M-049 false positives).
    config_fields = dict(config_fields)
    config_fields["embedding"] = {
        "provider": embedding.provider,
        "model": embedding.model,
        "dimensions": embedding.dimensions,
        "normalize": embedding.normalize,
    }
    canonical = json.dumps(config_fields, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# M-051: near-optimal helper
# ---------------------------------------------------------------------------


def is_near_optimal(
    candidate_score: float,
    reference_score: float,
    noise_margin: float,
) -> bool:
    """Return True when no candidate score beats the reference beyond the noise margin.

    M-051 semantics: the sweep reports "default already near-optimal for this corpus"
    when the best candidate score does not exceed the reference score by more than
    noise_margin.  This is an honest finding — the baseline is already competitive
    and manufacturing a delta would misrepresent the result.

    Pure predicate: no side effects.

    Args:
        candidate_score:  The best candidate's score (e.g. mean recall or precision).
        reference_score:  The §9.3 naive baseline score on the same metric.
        noise_margin:     Configurable threshold above which a delta is considered
                          meaningful.  Typical values: 0.01–0.05.

    Returns:
        True  — candidate does not beat reference by more than noise_margin
                (report "near-optimal").
        False — candidate clearly beats reference (report the delta honestly).
    """
    return (candidate_score - reference_score) <= noise_margin


__all__ = [
    "NAIVE_BASELINE_REFERENCE_ID",
    "reference_ingestion_config",
    "build_naive_baseline_ref",
    "reference_fingerprint",
    "is_near_optimal",
]
