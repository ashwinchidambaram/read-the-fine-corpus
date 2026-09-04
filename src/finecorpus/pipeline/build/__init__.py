"""Stage 5 — Build (Phase 0 skeleton).

Pass-through skeleton: consumes IngestionConfig, produces a BuildResult artifact.

The BuildResult carries:
  - Zero chunks (no embedding, no index write in Phase 0).
  - A build report stating why: skeleton phase.

Artifact structure:
  {
    "schema_version": "1.0.0",
    "contract": "build_result",
    "skeleton": true,
    "chunk_count": 0,
    "report": "Phase 0 skeleton: 0 chunks produced. ...",
    "chunks": []
  }

Phase 1+ replaces _produce with real building.
"""

from finecorpus.pipeline.build.stage import BuildStage

__all__ = ["BuildStage"]
