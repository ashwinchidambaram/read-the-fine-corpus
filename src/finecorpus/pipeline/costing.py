"""Ingestion cost estimation before Build (§19 acceptance criterion, §6.6).

Provides ``estimate_ingestion_cost`` — a pure, pre-Build cost estimate showing
embedding token count, LLM call count and token estimates, and USD cost per
provider (with explicit basis fields).  The estimate is produced BEFORE any
Build artifact exists so the cost gate can refuse to proceed.

Key design decisions
--------------------
- Token counting uses the **same whitespace-word proxy** as the chunker
  (``len(text.split())``) so estimate and build agree on token counts.
- LLM call counts are estimated from the segment set: one call per table
  segment that has ``table_description`` in its tier2_operations, and one
  call per segment whose class rule has ``class_context`` (for class
  descriptions).  Dedup: ONE call per unique ``(content_hash, segment_path)``
  pair (matching the cache key logic in llm_client.py).
- Local providers have zero marginal cost (``estimated_cost_usd=0.0``).
  The ``zero_marginal_cost`` field is explicit so the caller can present
  a clear "no-cost" message rather than "$0.00 (estimated)".
- The model is NOT a contract — it is an internal Pydantic model for
  display and gate logic only.  Its fields may change without a config_version
  bump.

IngestionCostEstimate fields
----------------------------
- ``embedding_token_count``: estimated tokens for all chunks (whitespace-word proxy)
- ``embedding_cost_usd``: cost for embedding (0 for local)
- ``embedding_provider_id``, ``embedding_model_id``: for display
- ``embedding_cost_per_1k``: provider's declared rate (None for local)
- ``embedding_is_local``: True when the provider is local
- ``embedding_zero_marginal_cost``: True when embedding is free
- ``llm_table_call_count``: number of unique table segments requiring NL description
- ``llm_other_call_count``: additional LLM calls (class_context, etc.)
- ``llm_total_call_count``: sum of above
- ``llm_estimated_input_tokens``: estimated input tokens to the LLM
- ``llm_estimated_output_tokens``: estimated output tokens from the LLM
- ``llm_cost_usd``: cost for LLM calls (0 for local/fake/None)
- ``llm_provider_id``, ``llm_model_id``: for display (None if no LLM)
- ``llm_cost_per_1k_input``, ``llm_cost_per_1k_output``: provider rates (None for local)
- ``llm_is_local``: True when the LLM provider is local
- ``llm_zero_marginal_cost``: True when LLM is free
- ``total_cost_usd``: sum of embedding + LLM costs
- ``tokenizer_name``: "whitespace_word" (matches chunker proxy)
- ``pricing_as_of_embedding``, ``pricing_as_of_llm``: ISO-8601 dates or None
- ``basis``: human-readable statement of estimation basis
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# Approximate tokens per LLM output token for table descriptions.
# Used to estimate output_tokens for the LLM calls.
_LLM_TABLE_DESC_OUTPUT_TOKENS_APPROX = 80

# Approximate input tokens for an augmentation call (system + sample prompt).
# Conservative estimate used when we don't have the actual prompt text.
_LLM_AUG_INPUT_TOKENS_APPROX = 300

# Tokenizer name (must match the chunker proxy)
_TOKENIZER_NAME = "whitespace_word"


# ---------------------------------------------------------------------------
# IngestionCostEstimate model
# ---------------------------------------------------------------------------


class IngestionCostEstimate(BaseModel):
    """Pre-Build ingestion cost estimate (§19 cost gate, §6.6).

    NOT a contract — this is an internal display/gate model.
    Fields may change without affecting config_version.
    """

    # Embedding
    embedding_token_count: int = Field(
        description="Estimated embedding tokens (whitespace-word proxy, matches chunker)."
    )
    embedding_cost_usd: Decimal = Field(
        description="Estimated embedding cost in USD (0 for local providers)."
    )
    embedding_provider_id: str = Field(description="Embedding provider identifier.")
    embedding_model_id: str = Field(description="Embedding model identifier.")
    embedding_cost_per_1k: Decimal | None = Field(
        description="Provider declared rate per 1k tokens (None for local)."
    )
    embedding_is_local: bool = Field(description="True when provider is local.")
    embedding_zero_marginal_cost: bool = Field(
        description="Explicit zero-cost flag — True when embedding is free."
    )
    embedding_pricing_as_of: str | None = Field(
        description="ISO-8601 date when embedding pricing was last verified (None for local)."
    )

    # LLM
    llm_table_call_count: int = Field(
        description="Unique table segments needing NL descriptions (one LLM call each)."
    )
    llm_other_call_count: int = Field(description="Additional LLM calls (class_context etc.).")
    llm_total_call_count: int = Field(description="Total LLM calls estimated.")
    llm_estimated_input_tokens: int = Field(description="Estimated total LLM input tokens.")
    llm_estimated_output_tokens: int = Field(description="Estimated total LLM output tokens.")
    llm_cost_usd: Decimal = Field(
        description="Estimated LLM cost in USD (0 for local/fake/no LLM)."
    )
    llm_provider_id: str | None = Field(
        description="LLM provider identifier (None if no LLM augmentation)."
    )
    llm_model_id: str | None = Field(
        description="LLM model identifier (None if no LLM augmentation)."
    )
    llm_cost_per_1k_input: Decimal | None = Field(
        description="Provider declared input rate per 1k tokens (None for local)."
    )
    llm_cost_per_1k_output: Decimal | None = Field(
        description="Provider declared output rate per 1k tokens (None for local)."
    )
    llm_is_local: bool = Field(description="True when LLM provider is local.", default=False)
    llm_zero_marginal_cost: bool = Field(
        description="Explicit zero-cost flag — True when LLM is free.", default=False
    )
    llm_pricing_as_of: str | None = Field(
        description="ISO-8601 date when LLM pricing was last verified (None for local).",
        default=None,
    )

    # Totals
    total_cost_usd: Decimal = Field(description="Total estimated cost (embedding + LLM).")

    # Basis
    tokenizer_name: str = Field(
        description="Token-counting method (must match chunker proxy).",
        default=_TOKENIZER_NAME,
    )
    basis: str = Field(description="Human-readable statement of estimation basis.")


# ---------------------------------------------------------------------------
# Token counting helper (same proxy as chunker)
# ---------------------------------------------------------------------------


def _count_tokens_proxy(text: str) -> int:
    """Whitespace-word token count proxy — matches chunker (len(text.split()))."""
    return len(text.split())


# ---------------------------------------------------------------------------
# Segment-set inspection helpers
# ---------------------------------------------------------------------------


def _get_rule_for_segment(
    segment_type_value: str,
    ingestion_config: Any,
) -> Any:
    """Return the ClassRule for a segment type (class_rules first, then default_rule)."""
    from finecorpus.contracts.shared.blocks import SegmentType

    try:
        seg_type = SegmentType(segment_type_value)
    except ValueError:
        return ingestion_config.default_rule

    for rule in ingestion_config.class_rules:
        if rule.segment_class == seg_type:
            return rule
    return ingestion_config.default_rule


# ---------------------------------------------------------------------------
# Main estimation function
# ---------------------------------------------------------------------------


def estimate_ingestion_cost(
    segment_set_batch: Any,
    ingestion_config: Any,
    embedding_provider: Any,
    llm_provider_or_none: Any | None,
) -> IngestionCostEstimate:
    """Estimate the cost of ingesting ``segment_set_batch`` with ``ingestion_config``.

    This is a PURE function — no network calls, no embedding, no LLM calls.

    Args:
        segment_set_batch: SegmentSetBatch (parsed or raw dict with ``segment_sets``).
        ingestion_config: IngestionConfig (parsed).
        embedding_provider: EmbeddingProvider instance (for capability info + estimate_cost).
        llm_provider_or_none: LLMProvider instance (for capability info), or None.

    Returns:
        IngestionCostEstimate with all basis fields.
    """
    from finecorpus.contracts.ingestion_config import Tier2Operation
    from finecorpus.contracts.segment_set import SegmentSet
    from finecorpus.contracts.shared.blocks import SalienceTier, SegmentType

    # -- Unpack batch --
    if isinstance(segment_set_batch, dict):
        raw_sets = segment_set_batch.get("segment_sets", [])
    else:
        raw_sets = segment_set_batch.segment_sets

    # Accumulate texts for embedding cost estimate and LLM call counts
    all_chunk_texts: list[str] = []
    # Unique table segments that need LLM descriptions: set of (content_hash, segment_path)
    table_segs_needing_desc: set[tuple[str, str]] = set()
    # class_context calls: per segment that has class_context op
    class_context_call_count = 0

    for seg_set_raw in raw_sets:
        if isinstance(seg_set_raw, dict):
            seg_set = SegmentSet.model_validate(seg_set_raw)
        else:
            seg_set = seg_set_raw

        content_hash = seg_set.content_hash

        for seg in seg_set.segments:
            # Skip excluded segments (they aren't indexed)
            if seg.salience_tier == SalienceTier.excluded:
                continue
            if not seg.text:
                continue

            # Get the applicable rule
            rule = _get_rule_for_segment(seg.segment_type.value, ingestion_config)

            # Resolve chunking config from the per-segment rule (Ruling 1: per-class chunking)
            chunking_cfg = rule.chunking
            max_tokens = chunking_cfg.max_tokens
            overlap_tokens = chunking_cfg.overlap_tokens

            # Apply Tier 1 to get canonical text for token counting
            tier1_text = seg.text
            if rule.transformation.tier1_enabled and rule.transformation.tier1_operations:
                from finecorpus.pipeline.build.transform import apply_tier1

                tier1_text, _ = apply_tier1(seg.text, rule.transformation.tier1_operations)

            # Estimate chunk texts from this segment using per-class chunking config
            chunk_texts = _estimate_chunk_texts(tier1_text, max_tokens, overlap_tokens)
            all_chunk_texts.extend(chunk_texts)

            # LLM calls: table descriptions
            if rule.transformation.tier2_enabled:
                ops = set(rule.transformation.tier2_operations)
                if (
                    Tier2Operation.table_description in ops
                    and seg.segment_type == SegmentType.table
                ):
                    table_segs_needing_desc.add((content_hash, seg.segment_path))

                if Tier2Operation.class_context in ops:
                    class_context_call_count += 1

    # -- Embedding cost --
    embed_caps = embedding_provider.capabilities
    embed_token_count = sum(_count_tokens_proxy(t) for t in all_chunk_texts)
    embed_is_local = embed_caps.is_local
    embed_cost_per_1k = embed_caps.cost_per_1k_tokens
    embed_pricing_as_of = embed_caps.pricing_as_of

    if embed_is_local or embed_cost_per_1k is None:
        embed_cost_usd = Decimal("0.0")
        embed_zero_marginal = True
    else:
        embed_cost_usd = (Decimal(embed_token_count) / Decimal(1000)) * embed_cost_per_1k
        embed_zero_marginal = False

    # -- LLM cost --
    llm_table_count = len(table_segs_needing_desc)
    llm_other_count = class_context_call_count
    llm_total_count = llm_table_count + llm_other_count

    llm_est_input_tokens = llm_total_count * _LLM_AUG_INPUT_TOKENS_APPROX
    llm_est_output_tokens = llm_table_count * _LLM_TABLE_DESC_OUTPUT_TOKENS_APPROX

    if llm_provider_or_none is None:
        llm_cost_usd = Decimal("0.0")
        llm_provider_id = None
        llm_model_id = None
        llm_cost_per_1k_input = None
        llm_cost_per_1k_output = None
        llm_is_local = False
        llm_zero_marginal = True
        llm_pricing_as_of = None
    else:
        llm_caps = llm_provider_or_none.capabilities
        llm_provider_id = llm_caps.provider_id
        llm_model_id = llm_caps.model_id
        llm_is_local = llm_caps.is_local
        llm_cost_per_1k_input = llm_caps.cost_per_1k_input_tokens
        llm_cost_per_1k_output = llm_caps.cost_per_1k_output_tokens
        llm_pricing_as_of = llm_caps.pricing_as_of

        if llm_is_local or (llm_cost_per_1k_input is None and llm_cost_per_1k_output is None):
            llm_cost_usd = Decimal("0.0")
            llm_zero_marginal = True
        else:
            input_cost = (
                (Decimal(llm_est_input_tokens) / Decimal(1000)) * llm_cost_per_1k_input
                if llm_cost_per_1k_input
                else Decimal("0.0")
            )
            output_cost = (
                (Decimal(llm_est_output_tokens) / Decimal(1000)) * llm_cost_per_1k_output
                if llm_cost_per_1k_output
                else Decimal("0.0")
            )
            llm_cost_usd = input_cost + output_cost
            llm_zero_marginal = False

    total_cost_usd = embed_cost_usd + llm_cost_usd

    basis_parts = [
        f"Tokenizer: {_TOKENIZER_NAME} (whitespace word count — same proxy as chunker).",
        (
            f"Embedding: {embed_token_count} tokens via "
            f"{embed_caps.provider_id}/{embed_caps.model_id}."
        ),
    ]
    if embed_is_local:
        basis_parts.append("Embedding provider is local — zero marginal cost.")
    if llm_provider_id:
        basis_parts.append(
            f"LLM: {llm_total_count} calls ({llm_table_count} table descriptions, "
            f"{llm_other_count} other) via {llm_provider_id}/{llm_model_id}."
        )
        if llm_is_local:
            basis_parts.append("LLM provider is local — zero marginal cost.")
    else:
        basis_parts.append("No LLM augmentation configured — zero LLM cost.")

    return IngestionCostEstimate(
        embedding_token_count=embed_token_count,
        embedding_cost_usd=embed_cost_usd,
        embedding_provider_id=embed_caps.provider_id,
        embedding_model_id=embed_caps.model_id,
        embedding_cost_per_1k=embed_cost_per_1k,
        embedding_is_local=embed_is_local,
        embedding_zero_marginal_cost=embed_zero_marginal,
        embedding_pricing_as_of=embed_pricing_as_of,
        llm_table_call_count=llm_table_count,
        llm_other_call_count=llm_other_count,
        llm_total_call_count=llm_total_count,
        llm_estimated_input_tokens=llm_est_input_tokens,
        llm_estimated_output_tokens=llm_est_output_tokens,
        llm_cost_usd=llm_cost_usd,
        llm_provider_id=llm_provider_id,
        llm_model_id=llm_model_id,
        llm_cost_per_1k_input=llm_cost_per_1k_input,
        llm_cost_per_1k_output=llm_cost_per_1k_output,
        llm_is_local=llm_is_local,
        llm_zero_marginal_cost=llm_zero_marginal,
        llm_pricing_as_of=llm_pricing_as_of,
        total_cost_usd=total_cost_usd,
        tokenizer_name=_TOKENIZER_NAME,
        basis=" ".join(basis_parts),
    )


def _estimate_chunk_texts(text: str, max_tokens: int, overlap_tokens: int) -> list[str]:
    """Estimate chunk texts for a segment using the same logic as the chunker.

    Uses whitespace-word splits to avoid importing the chunker (keeps this
    module fast and testable without the full build machinery).

    Returns a list of chunk text strings (may be approximate for very large
    texts, but token counts will match the chunker's proxy).
    """
    words = text.split()
    if not words:
        return []

    total = len(words)
    if total <= max_tokens:
        return [text]

    chunks: list[str] = []
    step = max(1, max_tokens - overlap_tokens)
    start = 0
    while start < total:
        end = min(start + max_tokens, total)
        chunks.append(" ".join(words[start:end]))
        if end >= total:
            break
        start += step

    return chunks if chunks else [text]


# ---------------------------------------------------------------------------
# Unavailable sentinel (Ruling 2: honest cost gate — never fabricate $0.00)
# ---------------------------------------------------------------------------


class CostEstimateUnavailable:
    """Sentinel returned when cost cannot be honestly estimated.

    Returned by ``_try_load_cost_estimate`` (CLI) when a declared non-local
    provider cannot be constructed (e.g. openai declared but no API key).

    The CLI MUST print "cost estimate unavailable for declared provider '<name>'
    (<reason>)" and still require confirmation — it MUST NOT fabricate $0.00.

    Attributes
    ----------
    provider_name:
        The declared provider name (e.g. ``"openai"``).
    reason:
        Human-readable reason for unavailability.  MUST NOT contain credential
        material (§6.2, §14.2).
    unavailable:
        Always ``True`` (type guard for CLI dispatch).
    """

    def __init__(self, provider_name: str, reason: str) -> None:
        self.provider_name = provider_name
        self.reason = reason
        self.unavailable = True

    def __repr__(self) -> str:
        return f"CostEstimateUnavailable(provider={self.provider_name!r})"


# ---------------------------------------------------------------------------
# Provider resolution helper (Ruling 2: honest cost gate — never fabricate $0.00)
# ---------------------------------------------------------------------------


@dataclass
class CostingProviders:
    """Named result from ``resolve_costing_providers``.

    Attributes
    ----------
    embedding_provider:
        Constructed embedding provider instance, or ``None`` when unavailable.
    embedding_unavailable:
        ``True`` when the declared embedding provider could not be constructed
        (e.g. declared 'openai' but no API key in env).  When ``True``,
        ``embedding_provider`` is ``None``.
    embedding_unavailable_reason:
        Human-readable reason for unavailability (e.g. 'no API key found').
        ``None`` when ``embedding_unavailable`` is ``False``.
    llm_provider:
        Constructed LLM provider instance, or ``None`` (no tier-2 or unavailable).
    llm_unavailable:
        ``True`` when the declared LLM provider could not be constructed.
    llm_unavailable_reason:
        Human-readable reason for LLM unavailability.  ``None`` when not unavailable.
    """

    embedding_provider: Any | None
    embedding_unavailable: bool
    embedding_unavailable_reason: str | None
    llm_provider: Any | None
    llm_unavailable: bool
    llm_unavailable_reason: str | None


# Known local / fake provider names (never require an API key)
_EMBEDDING_LOCAL_PROVIDERS: frozenset[str] = frozenset({"ollama", "local", "fake"})
_EMBEDDING_CLOUD_PROVIDERS: frozenset[str] = frozenset({"openai", "cloud"})


def resolve_costing_providers(
    ingestion_config: Any,
    platform_config: Any | None = None,
) -> CostingProviders:
    """Resolve real embedding + LLM providers for cost estimation.

    This is the library-side provider resolution for the pre-Build cost gate
    (Ruling 2: honest cost gate — never fabricate $0.00 for declared paid providers).

    Policy
    ------
    - ``fake`` / ``ollama`` / ``local`` embedding → construct a local provider;
      always succeeds; zero-marginal-cost label is honest.
    - ``openai`` / ``cloud`` embedding → try to construct the real OpenAI provider;
      if the API key is absent from the environment, mark ``embedding_unavailable=True``
      (the caller MUST print "cost estimate unavailable" and NOT $0.00).
    - LLM providers: resolved only when tier-2 is enabled in *any* class rule.
      If no tier-2 rule references an LLM operation, ``llm_provider=None``.

    Args:
        ingestion_config: IngestionConfig (parsed).
        platform_config: Optional full Config for more precise provider construction
            (e.g. Ollama endpoint).  If ``None``, defaults are used for local providers.

    Returns:
        CostingProviders with resolved or unavailable-flagged providers.
    """
    from finecorpus.contracts.ingestion_config import Tier2Operation

    # -- Embedding provider --
    embed_provider_name = ingestion_config.embedding.provider.lower()
    embed_model = ingestion_config.embedding.model
    embed_dims = ingestion_config.embedding.dimensions

    embedding_provider: Any | None = None
    embedding_unavailable = False
    embedding_unavailable_reason: str | None = None

    if embed_provider_name in _EMBEDDING_LOCAL_PROVIDERS:
        # Local / fake — always constructible, zero marginal cost
        if embed_provider_name == "fake":
            from finecorpus.embedding.fake import FakeProvider

            embedding_provider = FakeProvider(
                dimensions=embed_dims,
                model_id=embed_model,
            )
        else:
            # Ollama — use platform config endpoint if available, else default
            try:
                from finecorpus.embedding.ollama_provider import OllamaProvider

                ollama_endpoint = "http://localhost:11434"
                if platform_config is not None:
                    try:
                        ollama_endpoint = platform_config.providers.embedding.local.endpoint
                    except AttributeError:
                        pass
                embedding_provider = OllamaProvider(
                    base_url=ollama_endpoint,
                    model_id=embed_model,
                    dimensions=embed_dims,
                )
            except Exception as exc:
                # Ollama provider import or construction failure — treat as local unavailable
                # (rare; typically means a missing optional dependency)
                embedding_unavailable = True
                embedding_unavailable_reason = (
                    f"local ollama provider could not be constructed: {type(exc).__name__}"
                )

    elif embed_provider_name in _EMBEDDING_CLOUD_PROVIDERS:
        # Cloud (OpenAI) — requires API key from environment
        import os

        api_key: str | None = None
        for env_var in ("FINECORPUS_OPENAI_API_KEY", "OPENAI_API_KEY"):
            val = os.environ.get(env_var, "").strip()
            if val:
                api_key = val
                break

        if api_key is None:
            embedding_unavailable = True
            embedding_unavailable_reason = (
                f"no API key found in env for declared provider '{embed_provider_name}' "
                f"(set OPENAI_API_KEY or FINECORPUS_OPENAI_API_KEY)"
            )
        else:
            try:
                from finecorpus.embedding.openai_provider import OpenAIProvider

                # Pricing: use platform config if available, else None (unknown pricing)
                cost_per_1k: Any | None = None
                pricing_as_of: str | None = None
                if platform_config is not None:
                    try:
                        cloud_cfg = platform_config.providers.embedding.cloud
                        cost_per_1k = getattr(cloud_cfg, "cost_per_1k_tokens", None)
                        pricing_as_of = getattr(cloud_cfg, "pricing_as_of", None)
                    except AttributeError:
                        pass

                embedding_provider = OpenAIProvider(
                    api_key=api_key,
                    model_id=embed_model,
                    dimensions=embed_dims,
                    cost_per_1k_tokens=cost_per_1k,
                    pricing_as_of=pricing_as_of,
                )
            except Exception as exc:
                embedding_unavailable = True
                embedding_unavailable_reason = (
                    f"openai provider construction failed: {type(exc).__name__}"
                )
    else:
        # Unknown provider name — mark unavailable
        embedding_unavailable = True
        embedding_unavailable_reason = (
            f"unknown embedding provider '{embed_provider_name}' in IngestionConfig"
        )

    # -- LLM provider --
    # Resolve only if tier-2 is enabled with LLM operations in any class rule
    llm_provider: Any | None = None
    llm_unavailable = False
    llm_unavailable_reason: str | None = None

    # Check whether any rule demands an LLM operation
    all_rules = list(ingestion_config.class_rules) + [ingestion_config.default_rule]
    needs_llm = False
    for rule in all_rules:
        if rule.transformation.tier2_enabled:
            ops = set(rule.transformation.tier2_operations)
            if Tier2Operation.table_description in ops or Tier2Operation.class_context in ops:
                needs_llm = True
                break

    if needs_llm and platform_config is not None:
        # Try to resolve LLM provider from platform config
        try:
            from finecorpus.llm.registry import build_llm_provider_from_config

            resolved = build_llm_provider_from_config(platform_config, "augmentation")
            llm_provider = resolved.provider
        except ValueError as exc:
            # Missing key or unknown provider
            reason = str(exc)
            # Never include credential material in the reason (strip key-ish text)
            if "key" in reason.lower() or "api" in reason.lower():
                reason = (
                    f"LLM provider construction failed — check API key configuration "
                    f"({type(exc).__name__})"
                )
            llm_unavailable = True
            llm_unavailable_reason = reason
        except Exception as exc:
            llm_unavailable = True
            llm_unavailable_reason = f"LLM provider construction failed: {type(exc).__name__}"
    # When needs_llm is True but platform_config is None, LLM remains None (no penalty).

    return CostingProviders(
        embedding_provider=embedding_provider,
        embedding_unavailable=embedding_unavailable,
        embedding_unavailable_reason=embedding_unavailable_reason,
        llm_provider=llm_provider,
        llm_unavailable=llm_unavailable,
        llm_unavailable_reason=llm_unavailable_reason,
    )


__all__ = [
    "CostEstimateUnavailable",
    "CostingProviders",
    "IngestionCostEstimate",
    "estimate_ingestion_cost",
    "resolve_costing_providers",
]
