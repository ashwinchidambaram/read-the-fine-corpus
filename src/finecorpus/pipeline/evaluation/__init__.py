"""finecorpus.pipeline.evaluation — pure-logic evaluation utilities.

Modules:
  metrics    — Pure retrieval-quality metric functions (context recall, context
               precision, aggregate scoring). No I/O, no network, no index/service imports.
  baseline   — §9.3 pinned naive baseline reference config builder and helpers.
  candidates — Configuration-sweep candidate enumeration and deterministic corpus
               sampling (§9.3, M-046, D-07).

These are intentionally pure-logic modules: they compute over in-memory data only.
"""

from finecorpus.pipeline.evaluation.baseline import (
    NAIVE_BASELINE_REFERENCE_ID,
    build_naive_baseline_ref,
    is_near_optimal,
    reference_fingerprint,
    reference_ingestion_config,
)
from finecorpus.pipeline.evaluation.candidates import (
    SweepCandidate,
    enumerate_candidates,
    sample_corpus,
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
    # candidates
    "SweepCandidate",
    "enumerate_candidates",
    "sample_corpus",
]
