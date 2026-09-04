"""Stage 5 — Build (Phase 0 pass-through skeleton).

Produces a BuildResult: 0 chunks, with a report stating why (skeleton).

Artifact structure:
  {
    "schema_version": "1.0.0",
    "contract": "build_result",
    "skeleton": true,
    "chunk_count": 0,
    "report": "...",
    "chunks": []
  }

Phase 1+ replaces _produce with real chunk building and embedding.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

from finecorpus.contracts.versions import SUPPORTED_INGESTION_CONFIG
from finecorpus.pipeline.stage import Stage

_BUILD_RESULT_SCHEMA_VERSION = "1.0.0"


class BuildResult(BaseModel):
    """Envelope for the Build stage artifact.

    Contains 0 chunks in Phase 0 with an explanatory report.
    """

    schema_version: str = Field(description="Envelope schema version (semver).")
    contract: str = Field(description="Nominal contract name for this artifact.")
    skeleton: bool = Field(description="True = Phase 0 skeleton; no real build performed.")
    chunk_count: int = Field(description="Number of chunks produced (0 in skeleton).")
    report: str = Field(description="Plain-language explanation of why chunk_count is 0.")
    built_at: str = Field(description="When this build artifact was produced (UTC ISO 8601).")
    chunks: list[dict[str, Any]] = Field(
        description="Chunk list (empty in skeleton; Chunk objects in Phase 1+)."
    )


class BuildStage(Stage):
    """Stage 5 — Build (skeleton).

    Consumes IngestionConfig, emits BuildResult with 0 chunks.

    Args:
        run_started_at: Single run timestamp threaded from the orchestrator.
            All stages share this timestamp so no stage calls wall-clock.
            Defaults to the collected_at value already established at run start.
    """

    name = "build"
    consumed_contract = "ingestion_config"
    consumed_version_range = SUPPORTED_INGESTION_CONFIG  # §12 contract at the Plan→Build boundary
    produced_contract = "build_result"
    output_model = BuildResult

    def __init__(self, run_started_at: datetime | None = None) -> None:
        self._run_started_at = run_started_at or datetime.now(tz=UTC)

    def _produce(self, input_data: dict[str, Any] | None) -> dict[str, Any]:
        """Produce a BuildResult with 0 chunks (skeleton)."""
        assert input_data is not None, "Build requires IngestionConfig input"

        result = BuildResult(
            schema_version=_BUILD_RESULT_SCHEMA_VERSION,
            contract="build_result",
            skeleton=True,
            chunk_count=0,
            report=(
                "Phase 0 skeleton: 0 chunks produced. "
                "Build stage has not been implemented yet. "
                "Real chunking, embedding, and shadow-collection writing will happen in Phase 1+. "
                "The IngestionConfig from Plan stage was consumed and validated successfully."
            ),
            built_at=self._run_started_at.isoformat(),
            chunks=[],
        )
        return result.model_dump(mode="json")
