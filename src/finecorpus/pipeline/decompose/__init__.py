"""Stage 3 — Decompose (Phase 0 skeleton).

Pass-through skeleton: consumes ParseResultBatch, emits a SegmentSetBatch.

Each document gets a SegmentSet with:
  - Empty segments list (no decomposition attempted in Phase 0).
  - A valid ReassemblyRecord with empty covered_region_ids.
  - Empty exclusions and cross_references.

Phase 1+ will replace this with real decomposition.
"""

from finecorpus.pipeline.decompose.stage import DecomposeStage

__all__ = ["DecomposeStage"]
