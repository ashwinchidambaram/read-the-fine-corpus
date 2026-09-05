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
        chunking_cfg = ingestion_config.default_rule.chunking
        max_tokens = chunking_cfg.max_tokens
        overlap_tokens = chunking_cfg.overlap_tokens

        for seg in seg_set.segments:
            # Skip excluded segments (they aren't indexed)
            if seg.salience_tier == SalienceTier.excluded:
                continue
            if not seg.text:
                continue

            # Get the applicable rule
            rule = _get_rule_for_segment(seg.segment_type.value, ingestion_config)

            # Apply Tier 1 to get canonical text for token counting
            tier1_text = seg.text
            if rule.transformation.tier1_enabled and rule.transformation.tier1_operations:
                from finecorpus.pipeline.build.transform import apply_tier1

                tier1_text, _ = apply_tier1(seg.text, rule.transformation.tier1_operations)

            # Estimate chunk texts from this segment
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


__all__ = [
    "IngestionCostEstimate",
    "estimate_ingestion_cost",
]
