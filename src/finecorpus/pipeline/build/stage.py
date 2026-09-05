"""Stage 5 — Build (Phase 3: tier wiring, preview/dry_run, augmentation).

Consumes SegmentSetBatch (version-checked) + IngestionConfig; for each included
segment produces Chunks via the transformation pipeline:

  1. Tier 1 (apply_tier1): structure normalisation → canonical text
  2. Chunker (chunk_segment_dispatch): split canonical text into ChunkSpans
  3. Tier 2 (augment_chunk + compose_embedding_input): contextual augmentation
     in SEPARATE fields (never merged into chunk text — T-04 invariant)
  4. Embed + upsert (skipped in dry_run mode)

T-04 byte-identity invariant
-----------------------------
``chunk.text`` is ALWAYS a pure contiguous slice of the segment's Tier-1
canonical text.  Tier 2 augmentation goes into dedicated augmentation fields
(``table_description``, ``parent_breadcrumb``, ``class_context``).  The
embedding input (``embedding_input``) may frame augmentation context around
the chunk text but the chunk text itself is never modified.

``ChunkSpan`` is a frozen dataclass — ``augment_chunk`` structurally cannot
mutate it.

Double-record fix (Phase 3)
----------------------------
Prior versions emitted a ``table_to_markdown`` TransformationRecord from both
``_build_table_to_markdown_record()`` (independent) AND from ``apply_tier1``
when ``Tier1Operation.table_to_markdown`` appeared in the class rule.  This
created a double-recording defect.

Phase 3 resolution: ``_build_table_to_markdown_record()`` is REMOVED.
Provenance now flows EXCLUSIVELY through ``apply_tier1``'s records.  The
class rule must include ``table_to_markdown`` in its ``tier1_operations`` for
the provenance record to appear.

Tier toggles
------------
- ``tier1_enabled=False``: ``apply_tier1`` is skipped; no Tier 1 records;
  chunk text = raw segment text.
- ``tier2_enabled=False``: ``augment_chunk`` is skipped; no LLM construction;
  all augmentation fields are ``None``.

Dry-run mode (M-038 preview)
-----------------------------
``BuildStage(dry_run=True)`` runs the full Tier 1 + chunk + Tier 2 path but
SKIPS embedding and Qdrant upsert.  The resulting chunks (with augmentation +
provenance) are emitted inline in the BuildResult ``chunks`` field so the
preview path can display them without a live Qdrant instance.

Resumability (§6.6)
-------------------
Build checkpoints progress per document in the artifact store as a JSON sidecar
(``build_checkpoint.json`` in the same run directory). On re-run with the same
run_id, completed documents are skipped — their chunks are already written.

LLM augmentation cache
-----------------------
Table descriptions are cached per ``(content_hash, segment_path, model_id)``
in ``llm_cache.json`` in the run directory.  Resumed builds get cache hits
for previously described tables — no re-billing.

Schema
------
BuildResult (Phase 3) carries:
  {
    "schema_version": "1.0.0",
    "contract": "build_result",
    "skeleton": None,
    "chunk_count": <int>,
    "chunks_by_document": { doc_id: count, ... },
    "skipped_documents": [{ "document_id": ..., "reason": ... }, ...],
    "token_accounting": {
        "total_input_tokens": int,
        "total_embed_calls": int,
        "llm_call_count": int,
    },
    "shadow_collection": "<str>",
    "validation_passed": <bool>,
    "promoted": <bool>,
    "report": "<str>",
    "built_at": "<ISO 8601>",
    "dry_run": <bool>,
    "chunks": [...]   # non-empty only in dry_run mode
  }
"""

from __future__ import annotations

import json
import logging
import pathlib
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

from finecorpus.contracts.chunk_id import derive_chunk_id, derive_point_id
from finecorpus.contracts.ingestion_config import IngestionConfig, Tier2Operation
from finecorpus.contracts.segment_set import Segment, SegmentSet
from finecorpus.contracts.segment_set_batch import SegmentSetBatch
from finecorpus.contracts.shared.blocks import (
    SalienceTier,
    SourceLocation,
    TransformationRecord,
    TrustLevel,
)
from finecorpus.contracts.versions import SUPPORTED_INGESTION_CONFIG
from finecorpus.embedding.base import (
    CostEstimate,
    EmbedBatchResult,
    EmbeddingProvider,
    HealthCheckResult,
    ProviderCapabilities,
)
from finecorpus.index.adapter import (
    IndexAdapter,
    ModelIdentity,
    alias_name,
    build_point_payload,
    collection_name,
)
from finecorpus.index.lifecycle import (
    BuildContext,
    BuildState,
    create_shadow,
    validate_shadow,
)
from finecorpus.pipeline.build.augment import (
    Augmentation,
    AugmentationClient,
    augment_chunk,
    compose_embedding_input,
)
from finecorpus.pipeline.build.chunker import ChunkSpan
from finecorpus.pipeline.build.transform import apply_tier1
from finecorpus.pipeline.stage import Stage

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema version
# ---------------------------------------------------------------------------

_BUILD_RESULT_SCHEMA_VERSION = "1.0.0"

# How many chunks to upsert per call to the adapter.
_UPSERT_BATCH_SIZE = 100

# Chunk schema version stamped on every chunk payload.
_CHUNK_SCHEMA_VERSION = "1.0.0"


# ---------------------------------------------------------------------------
# BuildResult artifact model
# ---------------------------------------------------------------------------


class BuildResult(BaseModel):
    """Envelope for the Build stage artifact (Phase 3 real).

    Phase 1/3 real runs have skeleton=None. Phase 0 had skeleton=True.
    The ``chunks`` field is empty for normal (non-dry_run) runs — chunks live
    in Qdrant.  In dry_run mode, ``chunks`` contains the preview payload.
    """

    schema_version: str = Field(description="Envelope schema version (semver).")
    contract: str = Field(description="Nominal contract name.")
    skeleton: bool | None = Field(
        default=None,
        description="True = Phase 0 skeleton; None = real Phase 1+ run.",
    )
    chunk_count: int = Field(description="Total chunks written to the shadow collection.")
    chunks_by_document: dict[str, int] = Field(
        description="Per-document chunk counts (document_id -> chunk count)."
    )
    skipped_documents: list[dict[str, Any]] = Field(
        description=(
            "Documents skipped, with reasons. 'excluded' = all segments excluded-tier. "
            "'no_text' = no text segments."
        )
    )
    token_accounting: dict[str, int] = Field(
        description=(
            "Cost-accrual seed (§16): total input tokens + embed call count + llm_call_count."
        )
    )
    shadow_collection: str = Field(description="The shadow Qdrant collection name written.")
    validation_passed: bool = Field(description="Whether the pre-promotion validation gate passed.")
    promoted: bool = Field(
        description="Whether promotion was requested and completed (promote=True flag)."
    )
    report: str = Field(description="Plain-language summary of the build run.")
    built_at: str = Field(description="When this build artifact was produced (UTC ISO 8601).")
    dry_run: bool = Field(
        default=False,
        description="True when dry_run mode — no embedding/upsert; chunks inline for preview.",
    )
    chunks: list[dict[str, Any]] = Field(
        description=(
            "Inline chunks for dry_run/preview (empty in normal runs — chunks live in Qdrant)."
        )
    )


# ---------------------------------------------------------------------------
# Checkpoint helpers (resumability §6.6)
# ---------------------------------------------------------------------------


def _checkpoint_path(run_dir: pathlib.Path) -> pathlib.Path:
    return run_dir / "build_checkpoint.json"


def _load_checkpoint(run_dir: pathlib.Path) -> tuple[set[str], dict[str, int]]:
    """Load completed document_ids and per-document chunk counts from the checkpoint file.

    Returns:
        (completed_doc_ids, chunk_counts_by_doc) — both empty if the checkpoint does not exist.
        chunk_counts_by_doc maps document_id -> chunk count for previously completed docs.
    """
    path = _checkpoint_path(run_dir)
    if not path.exists():
        return set(), {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        completed = set(data.get("documents_completed", []))
        counts: dict[str, int] = data.get("chunk_counts_by_document", {})
        return completed, counts
    except Exception as exc:
        logger.warning("build checkpoint read failed (%s); starting fresh", exc)
        return set(), {}


def _save_checkpoint(
    run_dir: pathlib.Path, completed: set[str], chunk_counts: dict[str, int]
) -> None:
    """Persist completed document_ids and per-document chunk counts."""
    path = _checkpoint_path(run_dir)
    path.write_text(
        json.dumps(
            {
                "documents_completed": sorted(completed),
                "chunk_counts_by_document": {k: chunk_counts[k] for k in sorted(chunk_counts)},
            },
            indent=2,
        ),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Provenance helpers
# ---------------------------------------------------------------------------


def _segment_source_location(seg: Segment) -> dict[str, Any]:
    """Serialise the segment's source location to a plain dict."""
    loc: SourceLocation = seg.location
    return {
        "locator_kind": loc.locator_kind.value,
        "page_start": loc.page_start,
        "page_end": loc.page_end,
        "byte_start": loc.byte_start,
        "byte_end": loc.byte_end,
        "char_start": loc.char_start,
        "char_end": loc.char_end,
        "cell_range": loc.cell_range,
        "dom_path": loc.dom_path,
        "bbox": loc.bbox,
        "coordinate_note": loc.coordinate_note,
    }


def _build_provenance(
    segment_set: SegmentSet,
    seg: Segment,
    transformation_records: list[TransformationRecord],
) -> dict[str, Any]:
    """Assemble the full §8 provenance dict for a chunk from this segment.

    Phase 3: ``transformation_records`` flows from ``apply_tier1`` +
    ``augment_chunk`` + ``compose_embedding_input``.  The old
    ``_build_table_to_markdown_record`` independent emission has been REMOVED
    — records now flow exclusively through the transformation pipeline to avoid
    double-recording (Phase 3 defect fix).

    confidence = ocr_confidence if present, else 1.0 (native text).
    """
    confidence = seg.ocr_confidence if seg.ocr_confidence is not None else 1.0

    return {
        "source_document_id": segment_set.document_id,
        "source_document_version": segment_set.content_hash,
        "source_location": _segment_source_location(seg),
        "structural_path": list(seg.structural_path),
        "transformations": [r.model_dump(mode="json") for r in transformation_records],
        "confidence": confidence,
        "ocr_confidence": seg.ocr_confidence,
        "segment_type": seg.segment_type.value,
        "salience_tier": seg.salience_tier.value,
        "salience_basis": seg.salience_basis.value,
        "salience_signals": [
            {
                "kind": signal.kind.value,
                "implied_tier": signal.implied_tier.value,
                "won": signal.won,
                "detail": signal.detail,
            }
            for signal in seg.salience_signals
        ],
        "language": seg.language,
        "injection_suspicion": seg.injection_suspicion,
        "invisible_content_flags": [f.value for f in seg.invisible_content_flags],
        "sensitivity_flags": [f.value for f in seg.sensitivity_flags],
        "trust_level": TrustLevel.untrusted_ingested.value,
    }


def _build_tenancy(segment_set: SegmentSet) -> dict[str, Any]:
    """Return the tenancy block dict from the SegmentSet."""
    t = segment_set.tenancy
    return {
        "workspace_id": t.workspace_id,
        "kb_id": t.kb_id,
        "permission_mode": t.permission_mode.value,
        "permission_principals": list(t.permission_principals),
        "permission_source": t.permission_source.value,
        "permission_fidelity": t.permission_fidelity.value,
        "permission_resolved_at": (
            t.permission_resolved_at.isoformat() if t.permission_resolved_at else None
        ),
    }


# ---------------------------------------------------------------------------
# Class-rule lookup
# ---------------------------------------------------------------------------


def _get_rule_for_segment(seg: Segment, ingestion_config: IngestionConfig) -> Any:
    """Return the ClassRule for ``seg``, falling back to default_rule."""
    for rule in ingestion_config.class_rules:
        if rule.segment_class == seg.segment_type:
            return rule
    return ingestion_config.default_rule


def _get_class_description(seg: Segment, ingestion_config: IngestionConfig) -> str | None:
    """Return the class description text for ``seg``'s type, or None."""
    for cd in ingestion_config.class_descriptions:
        if cd.segment_class == seg.segment_type:
            return cd.description
    return None


# ---------------------------------------------------------------------------
# Core per-segment-set processing
# ---------------------------------------------------------------------------


def _process_segment_set(
    segment_set_dict: dict[str, Any],
    ingestion_config: IngestionConfig,
    provider: EmbeddingProvider,
    shadow_collection: str,
    adapter: IndexAdapter,
    doc_build_id: int,
    llm_client_factory: Any | None,
    dry_run: bool,
) -> tuple[int, list[dict[str, Any]], list[dict[str, Any]]]:
    """Process one SegmentSet: tier1 + chunk + tier2 + embed + upsert.

    Args:
        segment_set_dict: Raw SegmentSet dict from the batch.
        ingestion_config: The ingestion config.
        provider: EmbeddingProvider (not used in dry_run).
        shadow_collection: The shadow Qdrant collection to write to.
        adapter: IndexAdapter for upsert_points (not used in dry_run).
        doc_build_id: Unused (reserved).
        llm_client_factory: Callable(content_hash, segment_path) -> AugmentationClient | None.
            None means no tier2 augmentation.
        dry_run: When True, skip embedding and upsert; return chunks inline.

    Returns:
        (chunk_count, skipped_info_list, inline_chunks)
        - chunk_count: number of chunks processed for this document.
        - skipped_info_list: list of segment-level skip records.
        - inline_chunks: list of chunk dicts (non-empty only in dry_run mode).
    """
    seg_set = SegmentSet.model_validate(segment_set_dict)
    config_version = ingestion_config.config_version
    document_id = seg_set.document_id
    content_hash = seg_set.content_hash

    caps = provider.capabilities
    embed_model_id = caps.model_id
    embed_provider_id = caps.provider_id
    embed_dimensions = caps.vector_dimensions

    # Accumulate points to upsert
    points_buffer: list[dict[str, Any]] = []
    texts_buffer: list[str] = []
    chunk_meta_buffer: list[dict[str, Any]] = []  # parallel to texts_buffer

    chunk_count = 0
    skipped_segments: list[dict[str, Any]] = []
    inline_chunks: list[dict[str, Any]] = []

    def _flush(texts: list[str], metas: list[dict[str, Any]]) -> None:
        """Embed and stage a batch of chunk texts."""
        if not texts or dry_run:
            return
        result = provider.embed_batch(texts, embed_model_id)
        for i, (_text, meta) in enumerate(zip(texts, metas, strict=True)):
            vector = result.embeddings[i]
            point = {
                "id": meta["point_id"],
                "vector": vector,
                "payload": meta["payload"],
            }
            points_buffer.append(point)

    def _upsert_buffered() -> None:
        """Upsert all buffered points in batches."""
        if dry_run:
            return
        for batch_start in range(0, len(points_buffer), _UPSERT_BATCH_SIZE):
            batch = points_buffer[batch_start : batch_start + _UPSERT_BATCH_SIZE]
            adapter.upsert_points(shadow_collection, batch)

    for seg in seg_set.segments:
        # Skip excluded-tier segments (record them; never silently drop)
        if seg.salience_tier == SalienceTier.excluded:
            skipped_segments.append(
                {
                    "segment_id": seg.segment_id,
                    "reason": "excluded_salience_tier",
                }
            )
            continue

        # Skip segments with no text (figure-region etc.)
        if not seg.text:
            skipped_segments.append(
                {
                    "segment_id": seg.segment_id,
                    "reason": "no_text",
                }
            )
            continue

        # Resolve the class rule for this segment
        rule = _get_rule_for_segment(seg, ingestion_config)
        class_description = _get_class_description(seg, ingestion_config)
        max_tokens = rule.chunking.max_tokens
        overlap_tokens = rule.chunking.overlap_tokens

        # ------------------------------------------------------------------
        # Tier 1: Structure normalisation → canonical text + records
        # ------------------------------------------------------------------
        tier1_records: list[TransformationRecord] = []
        if rule.transformation.tier1_enabled and rule.transformation.tier1_operations:
            canonical_text, tier1_records = apply_tier1(
                seg.text, rule.transformation.tier1_operations
            )
        else:
            canonical_text = seg.text

        # ------------------------------------------------------------------
        # Chunking (dispatch by strategy — code_syntax goes to code splitter)
        # ------------------------------------------------------------------
        from finecorpus.pipeline.build.chunker import chunk_segment_dispatch

        spans: list[ChunkSpan] = chunk_segment_dispatch(
            segment_text=canonical_text,
            strategy=rule.chunking.strategy,
            max_tokens=max_tokens,
            overlap_tokens=overlap_tokens,
        )

        # ------------------------------------------------------------------
        # ONE LLM client per segment (for table descriptions)
        # Tier2: augment_chunk is called per SPAN but the LLM call is cached
        # per segment (same content_hash + segment_path → same cache key).
        # ------------------------------------------------------------------
        seg_llm_client: AugmentationClient | None = None
        if (
            rule.transformation.tier2_enabled
            and llm_client_factory is not None
            and Tier2Operation.table_description in set(rule.transformation.tier2_operations)
        ):
            seg_llm_client = llm_client_factory(content_hash, seg.segment_path)

        for span in spans:
            # --------------------------------------------------------------
            # Tier 2: Augmentation (does NOT touch span.text — T-04)
            # --------------------------------------------------------------
            augmentation = Augmentation()
            tier2_aug_records: list[TransformationRecord] = []
            tier2_compose_records: list[TransformationRecord] = []

            if rule.transformation.tier2_enabled:
                augmentation, tier2_aug_records = augment_chunk(
                    span=span,
                    segment=seg,
                    rule=rule,
                    class_description=class_description,
                    client=seg_llm_client,
                )

                # Count LLM calls (only for table_description spans)
                if seg_llm_client is not None and hasattr(seg_llm_client, "call_count"):
                    # call_count is tracked at factory level; just note it
                    pass

            # Compose embedding input (framing augmentation around chunk text)
            embedding_input, tier2_compose_records = compose_embedding_input(
                augmentation, span.text
            )

            # All transformation records in pipeline order
            all_records = tier1_records + tier2_aug_records + tier2_compose_records

            # --------------------------------------------------------------
            # IDs and provenance
            # --------------------------------------------------------------
            chunk_id = derive_chunk_id(
                document_id=document_id,
                content_hash=content_hash,
                config_version=config_version,
                segment_path=seg.segment_path,
                chunk_index=span.chunk_index,
            )
            point_id = derive_point_id(
                document_id=document_id,
                content_hash=content_hash,
                config_version=config_version,
                segment_path=seg.segment_path,
                chunk_index=span.chunk_index,
            )

            provenance_dict = _build_provenance(seg_set, seg, all_records)
            tenancy_dict = _build_tenancy(seg_set)

            embedding_ref_dict = {
                "provider": embed_provider_id,
                "model": embed_model_id,
                "dimensions": embed_dimensions,
                "config_version": config_version,
            }

            augmentation_dict = {
                "parent_breadcrumb": augmentation.parent_breadcrumb,
                "table_description": augmentation.table_description,
                "class_context": augmentation.class_context,
                "generated_by": (
                    seg_llm_client.__class__.__name__
                    if seg_llm_client is not None and augmentation.table_description is not None
                    else None
                ),
            }

            payload = build_point_payload(
                chunk_id=chunk_id,
                provenance=provenance_dict,
                tenancy=tenancy_dict,
                text=span.text,  # T-04: chunk text is the pure canonical slice
                embedding_ref=embedding_ref_dict,
                augmentation=augmentation_dict,
                extra={
                    "schema_version": _CHUNK_SCHEMA_VERSION,
                    "chunk_index": span.chunk_index,
                    "token_count": span.token_count,
                    "embedding_input": embedding_input,
                    # Char offsets within segment, for explain mode
                    "chunk_char_start": span.char_start,
                    "chunk_char_end": span.char_end,
                },
            )

            if dry_run:
                # Emit chunk inline for preview
                inline_chunks.append(
                    {
                        "chunk_id": chunk_id,
                        "point_id": str(point_id),
                        "text": span.text,
                        "embedding_input": embedding_input,
                        "augmentation": augmentation_dict,
                        "provenance": provenance_dict,
                        "chunk_index": span.chunk_index,
                        "token_count": span.token_count,
                        "document_id": document_id,
                        "segment_path": seg.segment_path,
                        # Position offsets within canonical text (for T-04 position-exact check)
                        "char_start": span.char_start,
                        "char_end": span.char_end,
                        # Canonical text (tier-1 output) for T-04 position-exact comparison
                        "canonical_text": canonical_text,
                    }
                )
            else:
                texts_buffer.append(embedding_input)
                chunk_meta_buffer.append(
                    {
                        "point_id": point_id,
                        "payload": payload,
                    }
                )

            chunk_count += 1

            # Flush embed+upsert when the buffer is full
            if not dry_run and len(texts_buffer) >= _UPSERT_BATCH_SIZE:
                _flush(texts_buffer, chunk_meta_buffer)
                _upsert_buffered()
                texts_buffer.clear()
                chunk_meta_buffer.clear()
                points_buffer.clear()

    # Flush remainder
    if not dry_run and texts_buffer:
        _flush(texts_buffer, chunk_meta_buffer)
        _upsert_buffered()
        texts_buffer.clear()
        chunk_meta_buffer.clear()
        points_buffer.clear()

    return chunk_count, skipped_segments, inline_chunks


# ---------------------------------------------------------------------------
# Build Stage
# ---------------------------------------------------------------------------


class BuildStage(Stage):
    """Stage 5 — Build (Phase 3 real implementation).

    Consumes IngestionConfig, produces BuildResult. Requires:
    - An EmbeddingProvider (injected; FakeProvider in tests).
    - An IndexAdapter (injected; QdrantAdapter in integration tests).
    - A build_id (from the control plane or test harness; monotonically increasing).
    - artifacts_root + run_id (for checkpoint resumability).

    Phase 3 additions:
    - ``dry_run=True``: full Tier1/2 transform path but no embedding/upsert.
    - ``llm_provider``: injected LLMProvider for table descriptions.
    - ``llm_op_config``: ResolvedOpConfig for the augmentation operation.

    When provider/adapter are None (test shortcut for unit tests), the stage
    falls back to the Phase 0 skeleton behaviour.

    Args:
        run_started_at: Single run timestamp threaded from the orchestrator.
        embedding_provider: EmbeddingProvider to use for embedding chunks.
            If None, falls back to Phase 0 skeleton.
        index_adapter: IndexAdapter to write chunks to.
            If None, falls back to Phase 0 skeleton.
        build_id: Build ID for the shadow collection name.
        artifacts_root: Root directory for checkpoint file.
        run_id: Run ID for checkpoint file.
        workspace_id: Owning workspace (for BuildContext).
        kb_id: Owning KB (for BuildContext and alias).
        dry_run: When True, skip embedding and upsert; emit chunks inline.
        llm_provider: Optional LLMProvider for Tier 2 table descriptions.
        llm_op_config: Optional ResolvedOpConfig for the augmentation operation.
    """

    name = "build"
    consumed_contract = "ingestion_config"
    consumed_version_range = SUPPORTED_INGESTION_CONFIG
    produced_contract = "build_result"
    output_model = BuildResult

    def __init__(
        self,
        run_started_at: datetime | None = None,
        embedding_provider: EmbeddingProvider | None = None,
        index_adapter: IndexAdapter | None = None,
        build_id: int = 1,
        artifacts_root: str | pathlib.Path | None = None,
        run_id: str | None = None,
        workspace_id: str = "",
        kb_id: str = "",
        dry_run: bool = False,
        llm_provider: Any | None = None,
        llm_op_config: Any | None = None,
    ) -> None:
        self._run_started_at = run_started_at or datetime.now(tz=UTC)
        self._provider = embedding_provider
        self._adapter = index_adapter
        self._build_id = build_id
        self._artifacts_root = pathlib.Path(artifacts_root) if artifacts_root else None
        self._run_id = run_id
        self._workspace_id = workspace_id
        self._kb_id = kb_id
        self._dry_run = dry_run
        self._llm_provider = llm_provider
        self._llm_op_config = llm_op_config

    def _produce(self, input_data: dict[str, Any] | None) -> dict[str, Any]:
        """Produce a BuildResult artifact.

        If no provider or adapter is injected (and not dry_run), falls back to
        Phase 0 skeleton.
        """
        assert input_data is not None, "Build requires IngestionConfig input"

        # Skeleton fallback for unit tests that don't inject a provider/adapter
        # (but NOT for dry_run — dry_run is a real path even without an adapter)
        if not self._dry_run and (self._provider is None or self._adapter is None):
            return self._produce_skeleton(input_data)

        # dry_run with no provider: use a fake embedding provider for dry_run
        if self._dry_run and self._provider is None:
            from finecorpus.embedding.fake import FakeProvider

            provider: EmbeddingProvider = FakeProvider()
        else:
            assert self._provider is not None
            provider = self._provider

        return self._produce_real(input_data, provider)

    def _produce_skeleton(self, input_data: dict[str, Any]) -> dict[str, Any]:
        """Phase 0 skeleton behaviour (no provider/adapter injected)."""
        result = BuildResult(
            schema_version=_BUILD_RESULT_SCHEMA_VERSION,
            contract="build_result",
            skeleton=True,
            chunk_count=0,
            chunks_by_document={},
            skipped_documents=[],
            token_accounting={
                "total_input_tokens": 0,
                "total_embed_calls": 0,
                "llm_call_count": 0,
            },
            shadow_collection="",
            validation_passed=False,
            promoted=False,
            report=(
                "Build stage: no EmbeddingProvider or IndexAdapter injected. "
                "Skeleton mode — 0 chunks produced. "
                "Inject a provider and adapter via BuildStage(...) for real builds."
            ),
            built_at=self._run_started_at.isoformat(),
            dry_run=False,
            chunks=[],
        )
        return result.model_dump(mode="json")

    def _produce_real(
        self, input_data: dict[str, Any], provider: EmbeddingProvider
    ) -> dict[str, Any]:
        """Phase 3 real build: tier1 + chunk + tier2 + embed + upsert to shadow."""
        assert provider is not None

        ingestion_config = IngestionConfig.model_validate(input_data)
        config_version = ingestion_config.config_version

        caps = provider.capabilities
        model_identity = ModelIdentity(
            provider=caps.provider_id,
            model=caps.model_id,
            dimensions=caps.vector_dimensions,
            config_version=config_version,
        )

        # In dry_run mode, skip shadow collection creation entirely
        shadow_collection = ""
        shadow_ctx = None

        if not self._dry_run:
            assert self._adapter is not None
            # Create shadow collection — or reattach to an existing one (resume case).
            expected_shadow = collection_name(self._kb_id, self._build_id)
            if self._adapter.collection_exists(expected_shadow):
                logger.info(
                    "build: shadow collection '%s' already exists — resuming into it",
                    expected_shadow,
                )
                shadow_ctx = BuildContext(
                    kb_id=self._kb_id,
                    workspace_id=self._workspace_id,
                    build_id=self._build_id,
                    shadow_collection=expected_shadow,
                    alias=alias_name(self._kb_id),
                    model_identity=model_identity,
                    state=BuildState.INGESTING,
                )
            else:
                shadow_ctx = create_shadow(
                    adapter=self._adapter,
                    kb_id=self._kb_id,
                    workspace_id=self._workspace_id,
                    build_id=self._build_id,
                    model_identity=model_identity,
                )
            shadow_collection = shadow_ctx.shadow_collection
            shadow_ctx.state = BuildState.INGESTING

        # Load the SegmentSetBatch from the store (previous stage artifact)
        segment_batch_dict = self._load_segment_batch()

        # Load checkpoint for resumability
        run_dir = (
            self._artifacts_root / self._run_id if self._artifacts_root and self._run_id else None
        )
        if run_dir:
            completed_docs, prior_chunk_counts = _load_checkpoint(run_dir)
        else:
            completed_docs, prior_chunk_counts = set(), {}

        # Validate SegmentSetBatch schema
        segment_batch = SegmentSetBatch.model_validate(segment_batch_dict)

        total_chunks = 0
        chunks_by_document: dict[str, int] = {}
        skipped_documents: list[dict[str, Any]] = []
        total_input_tokens = 0
        total_embed_calls = 0
        all_inline_chunks: list[dict[str, Any]] = []
        llm_total_calls = 0

        # Fold prior (checkpoint) chunk counts into totals so BuildResult reflects the
        # whole shadow collection, not just the newly-processed documents (F-02).
        for prior_doc_id, prior_count in prior_chunk_counts.items():
            if prior_count > 0:
                chunks_by_document[prior_doc_id] = prior_count
                total_chunks += prior_count

        # Wrap provider to count tokens/calls (skip in dry_run)
        if not self._dry_run:
            counting_provider = _CountingProvider(provider)
        else:
            counting_provider = provider  # type: ignore[assignment]

        # Build LLM client factory if a provider is available
        llm_factory = None
        if self._llm_provider is not None and self._llm_op_config is not None:
            llm_factory = _LLMClientFactory(
                provider=self._llm_provider,
                op_config=self._llm_op_config,
                run_dir=run_dir,
            )

        for seg_set_dict in segment_batch.segment_sets:
            doc_id = seg_set_dict.get("document_id", "")

            if doc_id in completed_docs and not self._dry_run:
                logger.info("build: skipping already-completed document %s", doc_id)
                continue

            logger.info("build: processing document %s", doc_id)

            chunk_count, skipped_segs, inline_chunks = _process_segment_set(
                segment_set_dict=seg_set_dict,
                ingestion_config=ingestion_config,
                provider=counting_provider,
                shadow_collection=shadow_collection,
                adapter=self._adapter,  # type: ignore[arg-type]
                doc_build_id=self._build_id,
                llm_client_factory=llm_factory,
                dry_run=self._dry_run,
            )

            if chunk_count == 0:
                reason = "all_segments_excluded" if skipped_segs else "no_text_segments"
                skipped_documents.append({"document_id": doc_id, "reason": reason})
            else:
                chunks_by_document[doc_id] = chunk_count
                total_chunks += chunk_count

            all_inline_chunks.extend(inline_chunks)

            completed_docs.add(doc_id)
            if run_dir and not self._dry_run:
                _save_checkpoint(run_dir, completed_docs, chunks_by_document)

        # Collect embedding token/call stats
        if not self._dry_run and isinstance(counting_provider, _CountingProvider):
            total_input_tokens = counting_provider.total_input_tokens
            total_embed_calls = counting_provider.total_embed_calls

        # Collect LLM call count
        if llm_factory is not None:
            llm_total_calls = llm_factory.total_call_count

        # Validate shadow (skip in dry_run)
        validation_passed = True  # dry_run always "passes"
        if not self._dry_run and shadow_ctx is not None:
            shadow_ctx.state = BuildState.VALIDATING
            assert self._adapter is not None
            validation = validate_shadow(
                adapter=self._adapter,
                shadow_collection=shadow_collection,
                expected_min_chunks=max(1, total_chunks),
                declared_empty=(total_chunks == 0),
            )
            validation_passed = validation.passed
            if not validation_passed:
                logger.error(
                    "build: validation FAILED for shadow '%s': %s",
                    shadow_collection,
                    validation.detail,
                )
                shadow_ctx.state = BuildState.VALIDATION_FAILED
            else:
                shadow_ctx.state = BuildState.INGESTING  # eligible for promote

        # Orphan scan (post-build warning; §10.5)
        # Scans the shadow collection for document IDs present in the index but
        # NOT in the current batch.  Orphans indicate leftover chunks from a
        # prior build that were not cleaned up.  This is a WARNING only — it
        # never blocks build or promotion.
        orphan_warning = ""
        if not self._dry_run and shadow_ctx is not None and self._adapter is not None:
            try:
                from finecorpus.pipeline.deletion import scan_orphans

                known_doc_ids: set[str] = set(chunks_by_document.keys())
                orphans = scan_orphans(
                    adapter=self._adapter,
                    collection=shadow_collection,
                    known_document_ids=known_doc_ids,
                )
                if orphans:
                    orphan_warning = (
                        f" ORPHAN WARNING: {len(orphans)} orphan document ID(s) found in "
                        f"shadow collection (not in current batch): {orphans[:10]}"
                        f"{'...' if len(orphans) > 10 else ''}. "
                        f"Consider running incremental cleanup (§10.5)."
                    )
                    logger.warning(
                        "build: %d orphan document ID(s) in shadow '%s': %s",
                        len(orphans),
                        shadow_collection,
                        orphans[:10],
                    )
            except Exception as exc:  # noqa: BLE001
                logger.debug("build: scan_orphans failed (non-fatal): %s", exc)

        # Build report
        n_docs = len(chunks_by_document)
        n_skipped = len(skipped_documents)
        dry_run_note = " [DRY RUN — no embedding/upsert performed]" if self._dry_run else ""
        report = (
            f"Build{'(dry_run)' if self._dry_run else ''} complete: "
            f"{total_chunks} chunks from {n_docs} documents. "
            f"{n_skipped} documents skipped/excluded. "
            f"Shadow collection: {shadow_collection or '(none — dry_run)'}. "
            f"Validation: {'PASSED' if validation_passed else 'FAILED'}."
            f"{dry_run_note}"
            f"{orphan_warning}"
        )

        result = BuildResult(
            schema_version=_BUILD_RESULT_SCHEMA_VERSION,
            contract="build_result",
            skeleton=None,
            chunk_count=total_chunks,
            chunks_by_document=chunks_by_document,
            skipped_documents=skipped_documents,
            token_accounting={
                "total_input_tokens": total_input_tokens,
                "total_embed_calls": total_embed_calls,
                "llm_call_count": llm_total_calls,
            },
            shadow_collection=shadow_collection,
            validation_passed=validation_passed,
            promoted=False,  # promote is a separate orchestrator call
            report=report,
            built_at=self._run_started_at.isoformat(),
            dry_run=self._dry_run,
            chunks=all_inline_chunks if self._dry_run else [],
        )
        return result.model_dump(mode="json")

    def _load_segment_batch(self) -> dict[str, Any]:
        """Load the decompose artifact (SegmentSetBatch) from the run directory.

        Raises:
            RuntimeError: If artifacts_root/run_id is not configured or the
                artifact does not exist.
        """
        if not self._artifacts_root or not self._run_id:
            raise RuntimeError(
                "BuildStage requires artifacts_root and run_id to load the "
                "SegmentSetBatch from the previous stage. Pass them to the constructor."
            )
        path = self._artifacts_root / self._run_id / "decompose.json"
        if not path.exists():
            raise RuntimeError(
                f"SegmentSetBatch artifact not found at {path}. "
                "Has the Decompose stage run for this run_id?"
            )
        return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Counting provider wrapper
# ---------------------------------------------------------------------------


class _CountingProvider(EmbeddingProvider):
    """Thin wrapper that counts input tokens + embed calls (§16 cost seed)."""

    def __init__(self, inner: EmbeddingProvider) -> None:
        self._inner = inner
        self.total_input_tokens: int = 0
        self.total_embed_calls: int = 0

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._inner.capabilities

    def embed_batch(self, texts: list[str], model_id: str) -> EmbedBatchResult:
        result = self._inner.embed_batch(texts, model_id)
        self.total_input_tokens += result.input_tokens_used
        self.total_embed_calls += 1
        return result

    def health_check(self) -> HealthCheckResult:
        return self._inner.health_check()

    def estimate_cost(self, texts: list[str]) -> CostEstimate:
        return self._inner.estimate_cost(texts)


# ---------------------------------------------------------------------------
# LLM client factory (shared cache + per-segment binding)
# ---------------------------------------------------------------------------


class _LLMClientFactory:
    """Creates per-segment LLMBuildClients sharing a persistent cache.

    The factory tracks total LLM calls across all segments so BuildResult
    can report ``llm_call_count``.
    """

    def __init__(self, provider: Any, op_config: Any, run_dir: pathlib.Path | None) -> None:
        from finecorpus.pipeline.build.llm_client import LLMBuildClient

        # Root client holding the shared cache
        self._root = LLMBuildClient(
            provider=provider,
            op_config=op_config,
            run_dir=run_dir,
            content_hash="",
            segment_path="",
        )

    def __call__(self, content_hash: str, segment_path: str) -> Any:
        """Return a per-segment client bound to (content_hash, segment_path)."""
        return self._root.for_segment(content_hash, segment_path)

    @property
    def total_call_count(self) -> int:
        """Total LLM calls made across all segments."""
        return self._root.call_count
