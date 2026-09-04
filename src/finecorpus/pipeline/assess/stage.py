"""Stage 2 — Assess (Phase 0 pass-through skeleton).

Produces a ParseResultBatch artifact: one ParseResult per inventory item,
all marked excluded_pre_parse (no parsing attempted in Phase 0).

Artifact structure:
  {
    "schema_version": "1.0.0",   # envelope version
    "contract": "parse_result_batch",
    "skeleton": true,            # honest marker
    "results": [ <ParseResult>, ... ]
  }

Contract consumption:
  Consumes Inventory (schema_version 1.x, SpecRange(major=1, min_minor=0)).
  Produces a parse_result_batch envelope with schema_version "1.0.0".

Nothing is silently dropped — every Inventory item gets exactly one ParseResult
entry (§6 rule 6, §12).

Phase 1 replaces _produce with real parsing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

from finecorpus.contracts.parse_result import (
    DocumentKind,
    ParseResult,
    ParserRef,
    ParseStatus,
    QualityScore,
    TableStructureRetained,
)
from finecorpus.contracts.shared.blocks import (
    LocatorKind,
    PermissionFidelity,
    PermissionMode,
    PermissionSource,
    SourceLocation,
    TenancyBlock,
)
from finecorpus.contracts.versions import SUPPORTED_INVENTORY
from finecorpus.pipeline.stage import Stage

_BATCH_SCHEMA_VERSION = "1.0.0"
_SKELETON_PARSER = ParserRef(
    name="skeleton",
    version="0.0.0",
    ocr_engine=None,
)
_SKELETON_LOCATION = SourceLocation(
    locator_kind=LocatorKind.byte_range,
    byte_start=0,
    byte_end=0,
)


class ParseResultBatch(BaseModel):
    """Envelope for the Assess stage artifact.

    Holds one ParseResult per document; carries a schema_version so the
    ArtifactStore's malformed-input check (requires schema_version) passes.
    """

    schema_version: str = Field(description="Envelope schema version (semver).")
    contract: str = Field(description="Nominal contract name for this artifact.")
    skeleton: bool = Field(description="True = Phase 0 pass-through; not real parsing.")
    results: list[dict[str, Any]] = Field(
        description="List of ParseResult dicts, one per document."
    )


class AssessStage(Stage):
    """Stage 2 — Assess (skeleton).

    Consumes Inventory, emits ParseResultBatch.
    """

    name = "assess"
    consumed_contract = "inventory"
    consumed_version_range = SUPPORTED_INVENTORY
    produced_contract = "parse_result_batch"
    output_model = ParseResultBatch

    def _produce(self, input_data: dict[str, Any] | None) -> dict[str, Any]:
        """Produce one ParseResult per inventory item.

        All results carry parse_status=excluded_pre_parse — the honest
        'not yet parsed' value (Phase 0 skeleton has not attempted parsing).
        """
        assert input_data is not None, "Assess requires Inventory input"

        # Reconstruct tenancy from inventory
        tenancy_raw = input_data.get("tenancy", {})
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

        parsed_at = datetime.now(tz=UTC)
        results: list[dict[str, Any]] = []

        for item in input_data.get("items", []):
            document_id = item["document_id"]
            content_hash = item["content_hash"]

            parse_result = ParseResult(
                schema_version="1.0.0",
                tenancy=tenancy,
                document_id=document_id,
                content_hash=content_hash,
                parser=_SKELETON_PARSER,
                parsed_at=parsed_at,
                parse_status=ParseStatus.excluded_pre_parse,
                document_kind=DocumentKind.other,
                quality=QualityScore(
                    overall=0.0,
                    text_extraction_ratio=None,
                    table_structure_retained=TableStructureRetained.n_a,
                    is_near_empty=True,
                    mean_ocr_confidence=None,
                ),
                pages=[],
                regions=[],
                boilerplate_candidates=[],
                content_classes=[],
                encoding_issues=[],
                language_distribution=[],
                findings=[],
            )
            results.append(parse_result.model_dump(mode="json"))

        batch = ParseResultBatch(
            schema_version=_BATCH_SCHEMA_VERSION,
            contract="parse_result_batch",
            skeleton=True,
            results=results,
        )
        return batch.model_dump(mode="json")
