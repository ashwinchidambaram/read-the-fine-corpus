"""Phase 5 services-layer promotion wrapper — scores eval set then calls lifecycle.

This module is the legal meeting point for retrieval + index:
  - services layer (above both retrieval and index) may import from both.
  - lifecycle (index layer) NEVER imports from retrieval or services.

score_and_validate_shadow:
  1. Scores the shadow KB's eval set via score_eval_set (retrieval path).
  2. Fetches the current live baseline from EvalBaselineRepository.
  3. Calls lifecycle.validate_shadow with injected shadow_eval_score and
     live_baseline_score so lifecycle remains layer-legal.
  4. On PASSED, calls lifecycle.promote to execute the two-phase alias swap.

Callers that do NOT have an eval set configured should pass eval_set_questions=None;
the shadow_eval_score will be None, and lifecycle will SKIP the eval gates gracefully.

Spec references: §5, M-054 Gate 3/4, §9.4 (promotion scoring).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy.orm import Session

from finecorpus.contracts.eval_set import ConfidenceLevel, EvalQuestion
from finecorpus.control.eval_store import EvalBaselineRepository
from finecorpus.embedding.base import EmbeddingProvider
from finecorpus.index.adapter import IndexAdapter
from finecorpus.index.lifecycle import (
    BuildContext,
    ValidationResult,
    promote,
    validate_shadow,
)
from finecorpus.services.eval_scoring import ScoreResult, score_eval_set

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default regression threshold
# ---------------------------------------------------------------------------

DEFAULT_EVAL_REGRESSION_THRESHOLD: float = 0.05
"""Default threshold for Gate 4 (§10.4): promotion blocked when
(live_baseline_score - shadow_score) > threshold."""


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def score_and_validate_shadow(
    *,
    ctx: BuildContext,
    adapter: IndexAdapter,
    session: Session,
    # Eval scoring inputs
    eval_set_questions: Sequence[EvalQuestion] | None,
    provider: EmbeddingProvider | None,
    k: int = 10,
    confidence_level: ConfidenceLevel = ConfidenceLevel.provisional,
    eval_regression_threshold: float = DEFAULT_EVAL_REGRESSION_THRESHOLD,
    # Existing validate_shadow params (forwarded verbatim)
    expected_min_chunks: int = 1,
    expected_max_chunks: int | None = None,
    declared_empty: bool = False,
    previous_chunk_count: int | None = None,
    previous_chunks_by_class: dict[str, int] | None = None,
    current_chunks_by_class: dict[str, int] | None = None,
    chunk_count_tolerance_pct: float = 20.0,
    # Promote params (forwarded on pass)
    do_promote: bool = False,
    promoted_at: datetime | None = None,
    available_model_providers: set[str] | None = None,
) -> ValidationResult:
    """Score the shadow eval set, then run pre-promotion gates, optionally promote.

    Step 1 — Eval scoring (when eval_set_questions is provided and provider is not None):
        Run score_eval_set on the shadow KB's eval questions.  The shadow KB is the
        evaluation target (query against kb_id == ctx.kb_id; the alias points at the
        shadow collection while it is live, but for eval purposes we score the KB as
        configured — the caller must ensure the shadow collection is in place).

    Step 2 — Baseline fetch:
        Fetch the current live baseline (EvalBaselineRepository.get_current) for the
        KB.  If no baseline exists yet, live_baseline_score is None.

    Step 3 — validate_shadow with injected scores:
        Call lifecycle.validate_shadow with shadow_eval_score and live_baseline_score
        injected.  Lifecycle NEVER imports retrieval/services — scores are passed in.

    Step 4 — Promote (optional):
        When do_promote=True and validation passes, call lifecycle.promote.

    Args:
        ctx: BuildContext for the build being promoted.
        adapter: IndexAdapter instance.
        session: SQLAlchemy Session bound to the control-plane DB.
        eval_set_questions: Questions from the eval set, or None when no eval configured.
        provider: EmbeddingProvider for scoring.  Must not be None when eval_set_questions
            is provided.  Pass None when skipping eval scoring.
        k: Top-k cutoff for recall/precision.
        confidence_level: Eval set confidence level (M-044 propagation).
        eval_regression_threshold: Gate 4 threshold (default 0.05).
        expected_min_chunks: Forwarded to validate_shadow.
        expected_max_chunks: Forwarded to validate_shadow.
        declared_empty: Forwarded to validate_shadow.
        previous_chunk_count: Forwarded to validate_shadow.
        previous_chunks_by_class: Forwarded to validate_shadow.
        current_chunks_by_class: Forwarded to validate_shadow.
        chunk_count_tolerance_pct: Forwarded to validate_shadow.
        do_promote: When True and validation passes, execute the alias swap.
        promoted_at: Override promotion timestamp (forwarded to promote).
        available_model_providers: Forwarded to promote.

    Returns:
        ValidationResult from validate_shadow (all gate results visible).
    """
    # ------------------------------------------------------------------
    # Step 1: Eval scoring
    # ------------------------------------------------------------------
    score_result: ScoreResult | None = None
    shadow_eval_score: float | None = None
    scoring_error: str | None = None

    if eval_set_questions is not None and provider is not None:
        try:
            score_result = score_eval_set(
                eval_set_questions,
                kb_id=ctx.kb_id,
                adapter=adapter,
                provider=provider,
                session=session,
                k=k,
                confidence_level=confidence_level,
            )
            shadow_eval_score = score_result.recall
            logger.info(
                "score_and_validate_shadow: kb=%r scored %d questions "
                "shadow_recall=%.4f confidence=%s",
                ctx.kb_id,
                score_result.n_scored,
                shadow_eval_score,
                confidence_level,
            )
        except Exception as exc:
            scoring_error = str(exc)
            logger.error(
                "score_and_validate_shadow: eval scoring failed for kb=%r: %s",
                ctx.kb_id,
                exc,
            )
            # scoring_error propagates to validate_shadow via shadow_eval_score=None
            # with an eval set configured → Gate 3 FAIL (see lifecycle semantics).
    elif eval_set_questions is not None and provider is None:
        # Eval questions were supplied but no provider was given.  Scoring cannot
        # run — Gate 3 will FAIL with a clear diagnostic naming the missing provider.
        scoring_error = (
            "eval scoring did not run: a provider (EmbeddingProvider) is required "
            "but provider=None was supplied to score_and_validate_shadow. "
            "Pass an EmbeddingProvider instance to enable eval scoring."
        )
        logger.error(
            "score_and_validate_shadow: kb=%r eval_set_questions supplied but "
            "provider=None — scoring skipped, Gate 3 will FAIL. "
            "Supply a provider to enable eval scoring.",
            ctx.kb_id,
        )

    # ------------------------------------------------------------------
    # Step 2: Fetch live baseline
    # ------------------------------------------------------------------
    live_baseline_score: float | None = None
    try:
        baseline_repo = EvalBaselineRepository(session)
        baseline_record = baseline_repo.get_current(ctx.kb_id)
        if baseline_record is not None:
            live_baseline_score = baseline_record.recall
            logger.debug(
                "score_and_validate_shadow: kb=%r live_baseline_recall=%.4f",
                ctx.kb_id,
                live_baseline_score,
            )
    except Exception as exc:
        logger.warning(
            "score_and_validate_shadow: could not fetch baseline for kb=%r: %s",
            ctx.kb_id,
            exc,
        )

    # ------------------------------------------------------------------
    # Step 3: Validate shadow with injected eval scores
    #
    # We use a separate sentinel to tell lifecycle whether eval was
    # CONFIGURED (eval_set_questions provided) vs not configured.
    # When configured but scoring failed → shadow_eval_score=None is passed
    # so lifecycle Gate 3 properly FAILs ("eval configured but score unavailable").
    # When not configured → we pass shadow_eval_score=None without the sentinel;
    # lifecycle interprets None as "no eval configured" → SKIPPED.
    #
    # The sentinel is conveyed via: if eval_set_questions is not None and
    # shadow_eval_score is None → scoring failed → pass special sentinel value
    # that lifecycle can distinguish.  Since lifecycle only receives plain floats
    # or None, and we rely on the gate semantics (configured+None → FAIL,
    # not-configured+None → SKIP), we use:
    #   eval_configured=True  passed as a separate bool param to validate_shadow.
    # ------------------------------------------------------------------
    eval_configured = eval_set_questions is not None

    result = validate_shadow(
        adapter=adapter,
        shadow_collection=ctx.shadow_collection,
        expected_min_chunks=expected_min_chunks,
        expected_max_chunks=expected_max_chunks,
        declared_empty=declared_empty,
        previous_chunk_count=previous_chunk_count,
        previous_chunks_by_class=previous_chunks_by_class,
        current_chunks_by_class=current_chunks_by_class,
        chunk_count_tolerance_pct=chunk_count_tolerance_pct,
        shadow_eval_score=shadow_eval_score,
        live_baseline_score=live_baseline_score,
        eval_regression_threshold=eval_regression_threshold,
        eval_configured=eval_configured,
        scoring_error=scoring_error,
    )

    # ------------------------------------------------------------------
    # Step 4: Promote on pass (optional)
    # ------------------------------------------------------------------
    if do_promote and result.passed:
        promote(
            adapter=adapter,
            session=session,
            ctx=ctx,
            expected_min_chunks=expected_min_chunks,
            expected_max_chunks=expected_max_chunks,
            declared_empty=declared_empty,
            promoted_at=promoted_at,
            available_model_providers=available_model_providers,
        )

    return result


__all__ = [
    "DEFAULT_EVAL_REGRESSION_THRESHOLD",
    "score_and_validate_shadow",
]
