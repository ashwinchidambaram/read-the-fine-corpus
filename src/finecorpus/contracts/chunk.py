"""Contract 5 — Chunk.

Stage boundary: Build → Serve. The spine of the system.
See docs/contracts/chunk.md for the authoritative spec (§8, §7.2, §10.5, §12, §14.1).

The Chunk is what Serve returns. It carries the full §8 provenance block. Its text is
byte-identical to the Tier-1-normalized canonical source unless a Tier 3 transformation
is recorded. Tier 2 augmentation is stored in separate fields. Its ID is deterministic
under §10.5.

When a Tier 3 (tier=3, changed_text=True) transformation rewrote the chunk, ``text`` holds
the REWRITTEN form and ``original_text`` retains the canonical Tier-1 text so the original
is visible at citation time (§7.2 C-R7, D-14). ``original_text`` is None for every non-Tier-3
chunk.

Chunk identity derivation lives in finecorpus.contracts.chunk_id.

Version history:
- 1.0.0: Initial contract.
- 1.1.0: MINOR bump — added ``original_text: str | None`` (D-14, §7.2 C-R7). Populated only
  when a Tier 3 rewrite changed the chunk text; None otherwise. Nullable/defaulted, so consumers
  built for 1.0.0 still validate 1.1.0 chunks.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from finecorpus.contracts.shared.blocks import Provenance, TenancyBlock

# ---------------------------------------------------------------------------
# Augmentation
# ---------------------------------------------------------------------------


class Augmentation(BaseModel):
    """Tier 2 contextual augmentation fields, stored separately (§7.2).

    See docs/contracts/chunk.md Augmentation.
    All optional (a chunk may have none). When present, they are concatenated into
    embedding_input but NEVER into text.

    Invariants:
    - Tier 2 never touches text — every Tier 2 op has changed_text=False.
    - All augmentation lives only in augmentation/embedding_input.
    """

    parent_breadcrumb: str | None = Field(
        default=None,
        description="Heading-path context (§6.4 prose default-on).",
    )
    table_description: str | None = Field(
        default=None,
        description=(
            "Generated NL description of a table's contents (§6.4). "
            "The verbatim table stays in text."
        ),
    )
    class_context: str | None = Field(
        default=None,
        description="Class-context blurb from the class description (§6.5).",
    )
    generated_by: list[str] | None = Field(
        default=None,
        description="Which model(s) produced these fields, for audit. Never a secret (§14.2).",
    )


# ---------------------------------------------------------------------------
# EmbeddingRef
# ---------------------------------------------------------------------------


class EmbeddingRef(BaseModel):
    """Which model/dimensions produced the vector.

    See docs/contracts/chunk.md EmbeddingRef.
    Used for mismatch-fails-closed (§15) and audit.
    """

    provider: str = Field(description="Provider name (not credential).")
    model: str = Field(
        description="Model identity. A query embedded with a different model fails closed (§15)."
    )
    dimensions: int = Field(description="Vector dimensionality.")
    config_version: str = Field(
        description=(
            "The config_version this chunk was built under (§10.5); redundantly stored "
            "on the chunk for orphan/version reasoning even though it is folded into chunk_id."
        )
    )


# ---------------------------------------------------------------------------
# Chunk (root)
# ---------------------------------------------------------------------------


class Chunk(BaseModel):
    """Contract 5 root model — produced by Build, consumed by Serve.

    See docs/contracts/chunk.md Chunk (root).

    Invariants (§12):
    - Provenance complete for every chunk — non-null Provenance, every subfield present (§8, §12).
    - text is byte-identical to the span of the Tier-1-normalized canonical source unless
      a tier=3, changed_text=True transformation is recorded (§7.2, §12).
    - original_text is non-None iff a tier=3, changed_text=True transformation rewrote the
      chunk; it holds the canonical Tier-1 text so the original is visible at citation time
      (§7.2 C-R7, D-14). It is None for every non-Tier-3 chunk.
    - Tier 2 separation: all augmentation is in augmentation/embedding_input, never in text (§7.2).
    - Deterministic ID: chunk_id derives solely from document_id, content_hash, config_version,
      segment_path, chunk_index — no randomness, no sequence, no wall-clock (§10.5).
    - Config version in identity: a config change changes config_version and therefore every
      chunk_id (§10.5), forcing full rebuild (§10.3).
    - tenancy present and server-enforced at retrieval (§11.4, Phase 0 MUST for the fields).

    Chunk identity derivation: see finecorpus.contracts.chunk_id.derive_chunk_id().
    It is impossible to construct a Chunk without providing provenance (non-optional field).
    """

    schema_version: str = Field(description="Contract version (semver).")
    chunk_id: str = Field(
        description=(
            "Deterministic ID (§10.5). Same document + same config → same ID every run. "
            "Includes config_version. Derived via chunk_id.derive_chunk_id()."
        )
    )
    tenancy: TenancyBlock = Field(
        description=(
            "Owning workspace/KB and resolved permission fields; "
            "enforced server-side at retrieval (§11.4)."
        )
    )
    provenance: Provenance = Field(
        description=(
            "The complete §8 provenance block. Non-null, all subfields present. "
            "Non-negotiable test §18.3.3. A Chunk cannot be constructed without this field."
        )
    )
    text: str = Field(
        description=(
            "The chunk's source text. Byte-identical to the span of the Tier-1-normalized "
            "canonical source unless a tier=3, changed_text=True transformation is recorded. "
            "This is what the caller receives."
        )
    )
    original_text: str | None = Field(
        default=None,
        description=(
            "The canonical Tier-1 text this chunk was rewritten FROM (§7.2 C-R7, D-14). "
            "Populated ONLY when a tier=3, changed_text=True transformation rewrote the chunk "
            "(``text`` then holds the rewritten form). None for every non-Tier-3 chunk. "
            "Surfaced at citation time so the original is always visible alongside the rewrite."
        ),
    )
    augmentation: Augmentation = Field(
        description=(
            "Tier 2 contextual augmentation, in separate fields (§7.2). "
            "Embedded with the chunk; never part of text."
        )
    )
    embedding_input: str = Field(
        description=(
            "The exact string embedded (augmentation + chunk). "
            "Recorded for reproducibility and explain mode (§11.5). "
            "Never returned as the chunk to the caller."
        )
    )
    embedding_ref: EmbeddingRef = Field(
        description=(
            "Which model/dimensions produced the vector — "
            "for mismatch-fails-closed (§15) and audit."
        )
    )
    chunk_index: int = Field(
        description="0-based position of this chunk within its segment (part of ID derivation)."
    )
    token_count: int | None = Field(
        default=None,
        description="Token length of text, for observability.",
    )


__all__ = [
    "Augmentation",
    "EmbeddingRef",
    "Chunk",
]
