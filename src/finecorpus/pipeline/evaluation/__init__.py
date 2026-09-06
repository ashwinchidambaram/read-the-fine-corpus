"""finecorpus.pipeline.evaluation — Retrieval-quality metrics and naive baseline reference.

Modules:
  metrics   — Pure retrieval-quality metric functions (context recall, context precision,
              aggregate scoring). No I/O, no network, no index/service imports.
  baseline  — §9.3 pinned naive baseline reference config builder and helpers.

These are intentionally pure-logic modules: they compute over in-memory data only.
"""

from finecorpus.pipeline.evaluation.baseline import (
    NAIVE_BASELINE_REFERENCE_ID,
    build_naive_baseline_ref,
    is_near_optimal,
    reference_fingerprint,
    reference_ingestion_config,
)
from finecorpus.pipeline.evaluation.metrics import (
    QuestionScore,
    ScoreSummary,
    aggregate_scores,
    context_precision_at_k,
    context_recall_at_k,
)

__all__ = [
    # metrics
    "context_recall_at_k",
    "context_precision_at_k",
    "aggregate_scores",
    "QuestionScore",
    "ScoreSummary",
    # baseline
    "NAIVE_BASELINE_REFERENCE_ID",
    "reference_ingestion_config",
    "build_naive_baseline_ref",
    "reference_fingerprint",
    "is_near_optimal",
]
