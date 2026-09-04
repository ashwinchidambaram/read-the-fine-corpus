"""Stage 4 — Plan (Phase 0 skeleton).

Pass-through skeleton: consumes SegmentSetBatch, emits an IngestionConfig.

The IngestionConfig is minimal but contract-valid:
  - default_rule only (no per-class rules).
  - secret_free_attestation=True (structural guarantee).
  - All recommendations labelled heuristic (honest — no sweep has run).

Phase 1+ will replace with real planning.
"""

from finecorpus.pipeline.plan.stage import PlanStage

__all__ = ["PlanStage"]
