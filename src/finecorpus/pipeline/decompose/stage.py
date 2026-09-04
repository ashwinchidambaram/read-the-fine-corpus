"""Stage 3 — Decompose (Phase 0 pass-through skeleton).

Produces a SegmentSetBatch: one SegmentSet per document, all with empty segments.
The reassembly record is valid: empty covered_region_ids, reassembly_digest = sha256("").

Artifact structure:
  {
    "schema_version": "1.0.0",
    "contract": "segment_set_batch",
    "skeleton": true,
    "segment_sets": [ <SegmentSet>, ... ]
  }

Contract consumption:
  Consumes parse_result_batch (schema_version 1.x).
  Produces segment_set_batch with schema_version "1.0.0".

Phase 1+ replaces _produce with real decomposition.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

from finecorpus.contracts.segment_set import (
    ReassemblyMethod,
    ReassemblyRecord,
    SegmentSet,
)
from finecorpus.contracts.shared.blocks import (
    PermissionFidelity,
    PermissionMode,
    PermissionSource,
    TenancyBlock,
)
from finecorpus.pipeline.stage import Stage

_BATCH_SCHEMA_VERSION = "1.0.0"

# sha256("") — the reassembly_digest for a document with no segments.
_EMPTY_REASSEMBLY_DIGEST = hashlib.sha256(b"").hexdigest()


class SegmentSetBatch(BaseModel):
    """Envelope for the Decompose stage artifact."""

    schema_version: str = Field(description="Envelope schema version (semver).")
    contract: str = Field(description="Nominal contract name.")
    skeleton: bool = Field(description="True = Phase 0 pass-through.")
    segment_sets: list[dict[str, Any]] = Field(description="One SegmentSet per document.")


class DecomposeStage(Stage):
    """Stage 3 — Decompose (skeleton).

    Consumes ParseResultBatch, emits SegmentSetBatch.
    """

    name = "decompose"
    consumed_contract = "parse_result_batch"
    consumed_version_range = None  # Envelope; version check done by ArtifactStore load()
    produced_contract = "segment_set_batch"
    output_model = SegmentSetBatch

    def _produce(self, input_data: dict[str, Any] | None) -> dict[str, Any]:
        """Produce one empty SegmentSet per parse result entry."""
        assert input_data is not None, "Decompose requires ParseResultBatch input"

        # Reconstruct tenancy from the first parse result (all share same tenancy)
        results = input_data.get("results", [])

        # Extract tenancy from the inventory or first result
        if results:
            tenancy_raw = results[0].get("tenancy", {})
        else:
            tenancy_raw = {}

        tenancy = TenancyBlock(
            workspace_id=tenancy_raw.get("workspace_id", ""),
            kb_id=tenancy_raw.get("kb_id", ""),
            permission_mode=tenancy_raw.get("permission_mode", PermissionMode.public_to_kb),
            permission_principals=tenancy_raw.get("permission_principals", []),
            permission_source=tenancy_raw.get("permission_source", PermissionSource.platform),
            permission_fidelity=tenancy_raw.get(
                "permission_fidelity", PermissionFidelity.authoritative
            ),
            permission_resolved_at=None,
        )

        decomposed_at = datetime.now(tz=UTC)
        segment_sets: list[dict[str, Any]] = []

        for parse_result in results:
            document_id = parse_result["document_id"]
            content_hash = parse_result["content_hash"]

            segment_set = SegmentSet(
                schema_version="1.0.0",
                tenancy=tenancy,
                document_id=document_id,
                content_hash=content_hash,
                segments=[],
                reassembly=ReassemblyRecord(
                    method=ReassemblyMethod.document_order_concat,
                    covered_region_ids=[],
                    reassembly_digest=_EMPTY_REASSEMBLY_DIGEST,
                ),
                exclusions=[],
                cross_references=[],
                decomposed_at=decomposed_at,
            )
            segment_sets.append(segment_set.model_dump(mode="json"))

        batch = SegmentSetBatch(
            schema_version=_BATCH_SCHEMA_VERSION,
            contract="segment_set_batch",
            skeleton=True,
            segment_sets=segment_sets,
        )
        return batch.model_dump(mode="json")
