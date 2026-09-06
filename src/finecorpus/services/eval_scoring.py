"""Phase 5 eval scoring — run eval questions through retrieval and compute metrics.

Retrieval-binding layer (C-5: services above retrieval/pipeline).

This module is the bridge between the eval substrate (control/eval_store,
pipeline/evaluation/metrics) and the retrieval service.  It scores an eval set by
running each usable question through the live retrieval service (auth-disabled,
KB-scoped tenancy), computing context recall@k and context precision@k, and
returning an aggregated ScoreResult.

Tenancy design:
  - Eval runs INSIDE the KB's own tenancy scope (not bypassing it).
  - auth_enabled=False → only the kb_id filter applies (no workspace or
    permission_principals clause); this matches the eval runtime which does not
    carry a service-principal API key.
  - Callers that need auth enforcement must supply a principal; the default
    None + auth_enabled=False path is correct for offline eval runs.

Usable-question filter:
  - review_status ∈ {reviewed_kept, reviewed_edited} AND non-empty
    expected_segment_ids → scored.
  - empty expected_segment_ids → QuestionScore(scorable=False), excluded from
    recall mean by aggregate_scores.
  - Other review statuses (unreviewed, reviewed_rejected) → excluded entirely
    from scoring (per §9.1: human review optional, but rejected questions must
    not inflate recall/precision).
  - The ScoreResult carries the eval set's confidence_level (provisional
    propagates) so callers can report it prominently (§9.2, M-044).

Spec references: §9.1, §9.2, §9.3, §9.4, M-044, M-054 Gate 3/4.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy.orm import Session

from finecorpus.contracts.eval_set import ConfidenceLevel, EvalQuestion, ReviewStatus
from finecorpus.control.auth import Principal
from finecorpus.embedding.base import EmbeddingProvider
from finecorpus.index.adapter import IndexAdapter
from finecorpus.pipeline.evaluation.metrics import (
    QuestionScore,
    ScoreSummary,
    aggregate_scores,
    context_precision_at_k,
    context_recall_at_k,
)
from finecorpus.retrieval.service import query as _retrieval_query
from finecorpus.telemetry import QUALITY_RETRIEVAL_PRECISION_AT_K, QUALITY_RETRIEVAL_RECALL_AT_K

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# ScoreResult
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScoreResult:
    """Aggregated eval scoring result for a KB eval set.

    Attributes:
        summary:            Full ScoreSummary (mean recall, mean precision, per-type breakdowns).
        confidence_level:   Propagated from the eval set (§9.2, M-044).
                            provisional when any question is unreviewed; callers MUST surface this.
        k:                  The top-k cutoff used for recall/precision computation.
        n_scored:           Number of questions that were actually scored (scorable=True).
        n_total:            Total questions in the input set (including excluded/non-scorable).
    """

    summary: ScoreSummary
    confidence_level: ConfidenceLevel
    k: int
    n_scored: int
    n_total: int

    @property
    def recall(self) -> float:
        """Mean context recall@k across scored questions."""
        return self.summary.mean_recall

    @property
    def precision(self) -> float:
        """Mean context precision@k across scored questions."""
        return self.summary.mean_precision

    @property
    def per_type(self) -> dict:
        """Recall/precision breakdowns by question type."""
        return {
            qt.value: {
                "recall": self.summary.recall_by_type.get(qt, 0.0),
                "precision": self.summary.precision_by_type.get(qt, 0.0),
            }
            for qt in set(list(self.summary.recall_by_type) + list(self.summary.precision_by_type))
        }


# ---------------------------------------------------------------------------
# Usable-question filter
# ---------------------------------------------------------------------------

_USABLE_STATUSES = frozenset([ReviewStatus.reviewed_kept, ReviewStatus.reviewed_edited])


def _is_usable(q: EvalQuestion) -> bool:
    """Return True when the question's review_status qualifies for scoring."""
    return q.review_status in _USABLE_STATUSES


# ---------------------------------------------------------------------------
# Main scoring function
# ---------------------------------------------------------------------------


def score_eval_set(
    eval_set_or_questions: Sequence[EvalQuestion],
    *,
    kb_id: str,
    adapter: IndexAdapter,
    provider: EmbeddingProvider,
    session: Session,
    k: int,
    confidence_level: ConfidenceLevel = ConfidenceLevel.provisional,
    principal: Principal | None = None,
    auth_enabled: bool = False,
    collection_override: str | None = None,
) -> ScoreResult:
    """Score an eval set by running each usable question through retrieval.

    Filters to usable questions (review_status in {reviewed_kept, reviewed_edited}),
    then for each usable question calls retrieval.service.query with explain=True to
    obtain the candidate chunk_ids, computes context_recall_at_k and
    context_precision_at_k, and aggregates via aggregate_scores.

    Questions with empty expected_segment_ids are marked scorable=False so the
    recall mean is computed only over meaningful questions (§9.3 sentinel-exclusion
    semantics in aggregate_scores).

    Args:
        eval_set_or_questions: Sequence of EvalQuestion objects to score.
        kb_id: Knowledge-base to query (tenancy scope).
        adapter: IndexAdapter instance.
        provider: EmbeddingProvider (must match the alias record's model identity).
        session: SQLAlchemy Session bound to the control-plane DB.
        k: Top-k cutoff for recall/precision computation.
        confidence_level: Eval set confidence level (provisional propagates — M-044).
        principal: Optional authenticated principal.  For offline eval, pass None
            and leave auth_enabled=False (KB tenancy filter still applied).
        auth_enabled: When False (default), only the kb_id tenancy filter is applied.
        collection_override: When set, each retrieval query searches this collection
            directly instead of resolving the KB alias.  Used by the eval sweep to
            score candidates against their own scratch collections.  The tenancy filter
            is still applied (using the collection name as the filter scope so it
            matches the payloads written during scratch ingestion).  Normal callers
            must leave this as None.

    Returns:
        ScoreResult with aggregated recall/precision, confidence_level, and counts.
    """
    questions = list(eval_set_or_questions)
    n_total = len(questions)

    per_question: list[QuestionScore] = []

    for q in questions:
        if not _is_usable(q):
            # Skip entirely (rejected / unreviewed); do not produce a QuestionScore.
            logger.debug(
                "score_eval_set: skipping question %r (review_status=%s)",
                q.question_id,
                q.review_status,
            )
            continue

        expected_ids: list[str] = q.expected_segment_ids or []
        scorable = bool(expected_ids)

        # Run retrieval with explain=True to get candidate chunk IDs.
        # When collection_override is set, retrieval queries that collection
        # directly (bypass alias resolution) but still applies tenancy filter.
        response = _retrieval_query(
            kb_id=kb_id,
            query_text=q.text,
            provider=provider,
            adapter=adapter,
            session=session,
            top_k=k,
            explain=True,
            principal=principal,
            auth_enabled=auth_enabled,
            collection_override=collection_override,
        )

        # Extract retrieved chunk IDs in score order from explain block.
        retrieved_ids: list[str] = []
        if response.explain is not None:
            retrieved_ids = [c.chunk_id for c in response.explain.candidates]
        elif response.results:
            retrieved_ids = [r.chunk_id for r in response.results]

        recall = context_recall_at_k(retrieved_ids, expected_ids, k)
        precision = context_precision_at_k(retrieved_ids, expected_ids, k)

        per_question.append(
            QuestionScore(
                question_id=q.question_id,
                question_type=q.question_type,
                recall=recall,
                precision=precision,
                scorable=scorable,
            )
        )

    summary = aggregate_scores(per_question)
    n_scored = sum(1 for qs in per_question if qs.scorable)

    # Populate quality gauges per kb_id.
    QUALITY_RETRIEVAL_RECALL_AT_K.labels(kb_id=kb_id).set(summary.mean_recall)
    QUALITY_RETRIEVAL_PRECISION_AT_K.labels(kb_id=kb_id).set(summary.mean_precision)

    logger.info(
        "score_eval_set: kb=%r k=%d n_total=%d n_scored=%d "
        "recall=%.4f precision=%.4f confidence=%s",
        kb_id,
        k,
        n_total,
        n_scored,
        summary.mean_recall,
        summary.mean_precision,
        confidence_level,
    )

    return ScoreResult(
        summary=summary,
        confidence_level=confidence_level,
        k=k,
        n_scored=n_scored,
        n_total=n_total,
    )


__all__ = [
    "ScoreResult",
    "score_eval_set",
]
