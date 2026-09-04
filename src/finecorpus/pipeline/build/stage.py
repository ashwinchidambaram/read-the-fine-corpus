"""Stage 5 — Build (Phase 1 real implementation).

Consumes SegmentSetBatch (version-checked) + IngestionConfig; for each included
segment produces Chunks via the recursive-char chunker with COMPLETE provenance (§8);
embeds chunks via an injected EmbeddingProvider (batched); writes to a SHADOW
collection via the IndexAdapter (C-4); runs lifecycle validation; makes the shadow
ELIGIBLE for promotion.

Resumability (§6.6)
-------------------
Build checkpoints progress per document in the artifact store as a JSON sidecar
(``build_checkpoint.json`` in the same run directory). On re-run with the same
run_id, completed documents are skipped — their chunks are already written and,
because chunk IDs are deterministic, idempotent upserts would make re-writing safe
too, but the checkpoint avoids redundant embedding API calls. The checkpoint file
contains a list of ``documents_completed`` by document_id.

Promote flag
------------
When ``promote=True`` is passed to ``run_pipeline``, the orchestrator calls
``lifecycle.promote()`` after Build completes. Build itself does not promote;
it only makes the shadow eligible (by running validation). This preserves the
spec's requirement that promote is a separate orchestrator call (§6.6, §10).

Provenance (§8)
---------------
Every chunk carries the FULL provenance block inherited from its segment:
- source_document_id / source_document_version from the SegmentSet.
- source_location from the Segment (the segment-level location, not a per-chunk
  refinement; chunk's char_start/char_end are stored in extra payload fields for
  explain mode but the spec §8 location is the segment's source location).
- structural_path, segment_type, salience_tier, salience_basis, salience_signals,
  language, injection_suspicion, invisible_content_flags, sensitivity_flags from
  the Segment.
- transformations: empty list in Phase 1 — extraction is not a recorded Tier
  transformation (no Tier 1/2/3 applied in Phase 1); Tier 2 augmentation arrives
  in Phase 3.
- confidence: from segment.ocr_confidence if present, else 1.0 (native text).
- trust_level: always untrusted_ingested (§14.1).
- tenancy: from the SegmentSet's tenancy block.

Chunk text byte-identity (§12)
-------------------------------
The chunk text is a contiguous substring of the segment text (T-04 invariant).
No transformation is applied to the text in Phase 1 — it is exactly what the
segment carries.

Cost accrual seed (§16)
-----------------------
BuildResult.cost_accrual accumulates input_tokens_used from each embed_batch
call. This is a seed only — cost attribution to a KB/workspace is Phase 4+.

Skipped / excluded documents
-----------------------------
A segment is excluded if its salience_tier is 'excluded'. The segment is still
iterated (we record it in skipped_segments); it is never silently dropped (§1.4).
Documents are excluded if ALL their segments are excluded.

Schema
------
BuildResult (Phase 1) carries:
  {
    "schema_version": "1.0.0",
    "contract": "build_result",
    "skeleton": None,             # Phase 1 real run
    "chunk_count": <int>,
    "chunks_by_document": { doc_id: count, ... },
    "skipped_documents": [{ "document_id": ..., "reason": ... }, ...],
    "token_accounting": {
        "total_input_tokens": int,
        "total_embed_calls": int,
    },
    "shadow_collection": "<str>",
    "validation_passed": <bool>,
    "promoted": <bool>,
    "report": "<str>",
    "built_at": "<ISO 8601>",
    "chunks": []    # not stored inline (stored in Qdrant)
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
from finecorpus.contracts.ingestion_config import IngestionConfig
from finecorpus.contracts.segment_set import Segment, SegmentSet
from finecorpus.contracts.segment_set_batch import SegmentSetBatch
from finecorpus.contracts.shared.blocks import (
    SalienceTier,
    SourceLocation,
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
    build_point_payload,
)
from finecorpus.index.lifecycle import (
    BuildState,
    create_shadow,
    validate_shadow,
)
from finecorpus.pipeline.build.chunker import ChunkSpan, chunk_segment
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
    """Envelope for the Build stage artifact (Phase 1 real).

    Phase 1 real runs have skeleton=None. Phase 0 had skeleton=True.
    The ``chunks`` field is intentionally empty (chunks live in Qdrant).
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
        description="Cost-accrual seed (§16): total input tokens + embed call count."
    )
    shadow_collection: str = Field(description="The shadow Qdrant collection name written.")
    validation_passed: bool = Field(description="Whether the pre-promotion validation gate passed.")
    promoted: bool = Field(
        description="Whether promotion was requested and completed (promote=True flag)."
    )
    report: str = Field(description="Plain-language summary of the build run.")
    built_at: str = Field(description="When this build artifact was produced (UTC ISO 8601).")
    chunks: list[dict[str, Any]] = Field(
        description="Intentionally empty — chunks are in Qdrant, not inline."
    )


# ---------------------------------------------------------------------------
# Checkpoint helpers (resumability §6.6)
# ---------------------------------------------------------------------------


def _checkpoint_path(run_dir: pathlib.Path) -> pathlib.Path:
    return run_dir / "build_checkpoint.json"


def _load_checkpoint(run_dir: pathlib.Path) -> set[str]:
    """Load the set of completed document_ids from the checkpoint file.

    Returns an empty set if the checkpoint does not exist.
    """
    path = _checkpoint_path(run_dir)
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return set(data.get("documents_completed", []))
    except Exception as exc:
        logger.warning("build checkpoint read failed (%s); starting fresh", exc)
        return set()


def _save_checkpoint(run_dir: pathlib.Path, completed: set[str]) -> None:
    """Persist the set of completed document_ids."""
    path = _checkpoint_path(run_dir)
    path.write_text(
        json.dumps({"documents_completed": sorted(completed)}, indent=2),
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
) -> dict[str, Any]:
    """Assemble the full §8 provenance dict for a chunk from this segment.

    transformations is an empty list in Phase 1 (no Tier 1/2 ops applied;
    Tier 2 augmentation arrives in Phase 3).

    confidence = ocr_confidence if present, else 1.0 (native text).
    """
    confidence = seg.ocr_confidence if seg.ocr_confidence is not None else 1.0
    return {
        "source_document_id": segment_set.document_id,
        "source_document_version": segment_set.content_hash,
        "source_location": _segment_source_location(seg),
        "structural_path": list(seg.structural_path),
        "transformations": [],  # empty in Phase 1
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
# Core build logic
# ---------------------------------------------------------------------------


def _process_segment_set(
    segment_set_dict: dict[str, Any],
    ingestion_config: IngestionConfig,
    provider: EmbeddingProvider,
    shadow_collection: str,
    adapter: IndexAdapter,
    doc_build_id: int,
) -> tuple[int, list[dict[str, Any]]]:
    """Process one SegmentSet: chunk + embed + upsert.

    Args:
        segment_set_dict: Raw SegmentSet dict from the batch.
        ingestion_config: The ingestion config (for config_version, chunking params).
        provider: EmbeddingProvider for embedding chunks.
        shadow_collection: The shadow Qdrant collection to write to.
        adapter: IndexAdapter for upsert_points.
        doc_build_id: Unused in Phase 1 (reserved for future incremental builds).

    Returns:
        (chunk_count, skipped_info_list)
        - chunk_count: number of chunks written for this document.
        - skipped_info_list: list of segment-level skip records (for observability).
    """
    # Parse the SegmentSet
    seg_set = SegmentSet.model_validate(segment_set_dict)
    config_version = ingestion_config.config_version
    document_id = seg_set.document_id
    content_hash = seg_set.content_hash

    # Get chunking params from the ingestion config default rule.
    chunking_cfg = ingestion_config.default_rule.chunking
    max_tokens = chunking_cfg.max_tokens
    overlap_tokens = chunking_cfg.overlap_tokens

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

    def _flush(texts: list[str], metas: list[dict[str, Any]]) -> None:
        """Embed and upsert a batch of chunk texts."""
        if not texts:
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

        # Chunk the segment text
        spans: list[ChunkSpan] = chunk_segment(
            segment_text=seg.text,
            max_tokens=max_tokens,
            overlap_tokens=overlap_tokens,
        )

        for span in spans:
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

            provenance_dict = _build_provenance(seg_set, seg)
            tenancy_dict = _build_tenancy(seg_set)

            embedding_ref_dict = {
                "provider": embed_provider_id,
                "model": embed_model_id,
                "dimensions": embed_dimensions,
                "config_version": config_version,
            }

            augmentation_dict = {
                "parent_breadcrumb": None,
                "table_description": None,
                "class_context": None,
                "generated_by": None,
            }

            # embedding_input = just chunk text in Phase 1 (no augmentation yet)
            embedding_input = span.text

            payload = build_point_payload(
                chunk_id=chunk_id,
                provenance=provenance_dict,
                tenancy=tenancy_dict,
                text=span.text,
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

            texts_buffer.append(embedding_input)
            chunk_meta_buffer.append(
                {
                    "point_id": point_id,
                    "payload": payload,
                }
            )
            chunk_count += 1

            # Flush embed+upsert when the buffer is full
            if len(texts_buffer) >= _UPSERT_BATCH_SIZE:
                _flush(texts_buffer, chunk_meta_buffer)
                _upsert_buffered()
                texts_buffer.clear()
                chunk_meta_buffer.clear()
                points_buffer.clear()

    # Flush remainder
    if texts_buffer:
        _flush(texts_buffer, chunk_meta_buffer)
        _upsert_buffered()
        texts_buffer.clear()
        chunk_meta_buffer.clear()
        points_buffer.clear()

    return chunk_count, skipped_segments


# ---------------------------------------------------------------------------
# Build Stage
# ---------------------------------------------------------------------------


class BuildStage(Stage):
    """Stage 5 — Build (Phase 1 real implementation).

    Consumes IngestionConfig, produces BuildResult. Requires:
    - An EmbeddingProvider (injected; FakeProvider in tests).
    - An IndexAdapter (injected; QdrantAdapter in integration tests).
    - A build_id (from the control plane or test harness; monotonically increasing).
    - artifacts_root + run_id (for checkpoint resumability).

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
    ) -> None:
        self._run_started_at = run_started_at or datetime.now(tz=UTC)
        self._provider = embedding_provider
        self._adapter = index_adapter
        self._build_id = build_id
        self._artifacts_root = pathlib.Path(artifacts_root) if artifacts_root else None
        self._run_id = run_id
        self._workspace_id = workspace_id
        self._kb_id = kb_id

    def _produce(self, input_data: dict[str, Any] | None) -> dict[str, Any]:
        """Produce a BuildResult artifact.

        If no provider or adapter is injected, falls back to Phase 0 skeleton.
        """
        assert input_data is not None, "Build requires IngestionConfig input"

        # Skeleton fallback for unit tests that don't inject a provider/adapter
        if self._provider is None or self._adapter is None:
            return self._produce_skeleton(input_data)

        return self._produce_real(input_data)

    def _produce_skeleton(self, input_data: dict[str, Any]) -> dict[str, Any]:
        """Phase 0 skeleton behaviour (no provider/adapter injected)."""
        result = BuildResult(
            schema_version=_BUILD_RESULT_SCHEMA_VERSION,
            contract="build_result",
            skeleton=True,
            chunk_count=0,
            chunks_by_document={},
            skipped_documents=[],
            token_accounting={"total_input_tokens": 0, "total_embed_calls": 0},
            shadow_collection="",
            validation_passed=False,
            promoted=False,
            report=(
                "Build stage: no EmbeddingProvider or IndexAdapter injected. "
                "Skeleton mode — 0 chunks produced. "
                "Inject a provider and adapter via BuildStage(...) for real builds."
            ),
            built_at=self._run_started_at.isoformat(),
            chunks=[],
        )
        return result.model_dump(mode="json")

    def _produce_real(self, input_data: dict[str, Any]) -> dict[str, Any]:
        """Phase 1 real build: chunk + embed + upsert to shadow collection."""
        assert self._provider is not None
        assert self._adapter is not None

        ingestion_config = IngestionConfig.model_validate(input_data)
        config_version = ingestion_config.config_version

        caps = self._provider.capabilities
        model_identity = ModelIdentity(
            provider=caps.provider_id,
            model=caps.model_id,
            dimensions=caps.vector_dimensions,
            config_version=config_version,
        )

        # Create shadow collection
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
        # NOTE: We receive the IngestionConfig as input_data, but we also need
        # the SegmentSetBatch. BuildStage does NOT have direct access to the store
        # from _produce(). We must load it from artifacts_root/run_id/decompose.json.
        segment_batch_dict = self._load_segment_batch()

        # Load checkpoint for resumability
        run_dir = (
            self._artifacts_root / self._run_id if self._artifacts_root and self._run_id else None
        )
        completed_docs: set[str] = _load_checkpoint(run_dir) if run_dir else set()

        # Validate SegmentSetBatch schema
        segment_batch = SegmentSetBatch.model_validate(segment_batch_dict)

        total_chunks = 0
        chunks_by_document: dict[str, int] = {}
        skipped_documents: list[dict[str, Any]] = []
        total_input_tokens = 0
        total_embed_calls = 0

        # Wrap provider to count tokens/calls
        counting_provider = _CountingProvider(self._provider)

        for seg_set_dict in segment_batch.segment_sets:
            doc_id = seg_set_dict.get("document_id", "")

            if doc_id in completed_docs:
                logger.info("build: skipping already-completed document %s", doc_id)
                # Re-count from Qdrant would be expensive; load from checkpoint instead.
                # Since chunk IDs are deterministic, we can safely skip.
                # The count is not recorded here — it's already in the shadow collection.
                continue

            logger.info("build: processing document %s", doc_id)

            chunk_count, skipped_segs = _process_segment_set(
                segment_set_dict=seg_set_dict,
                ingestion_config=ingestion_config,
                provider=counting_provider,
                shadow_collection=shadow_collection,
                adapter=self._adapter,
                doc_build_id=self._build_id,
            )

            if chunk_count == 0:
                reason = "all_segments_excluded" if skipped_segs else "no_text_segments"
                skipped_documents.append({"document_id": doc_id, "reason": reason})
            else:
                chunks_by_document[doc_id] = chunk_count
                total_chunks += chunk_count

            completed_docs.add(doc_id)
            if run_dir:
                _save_checkpoint(run_dir, completed_docs)

        total_input_tokens = counting_provider.total_input_tokens
        total_embed_calls = counting_provider.total_embed_calls

        # Validate shadow
        shadow_ctx.state = BuildState.VALIDATING
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

        # Build report
        n_docs = len(chunks_by_document)
        n_skipped = len(skipped_documents)
        report = (
            f"Build complete: {total_chunks} chunks from {n_docs} documents. "
            f"{n_skipped} documents skipped/excluded. "
            f"Shadow collection: {shadow_collection}. "
            f"Validation: {'PASSED' if validation_passed else 'FAILED'}. "
            f"Token estimate (whitespace-word proxy): {total_input_tokens}."
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
            },
            shadow_collection=shadow_collection,
            validation_passed=validation_passed,
            promoted=False,  # promote is a separate orchestrator call
            report=report,
            built_at=self._run_started_at.isoformat(),
            chunks=[],
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
