"""Phase 5 drift detection — §9.4 criterion 2, M-100.

Scheduled and post-reindex drift checks: compare the live index scoring against
the retained current baseline and alert when a regression beyond the configured
threshold is detected.

Layer: services (above retrieval/pipeline; may import from retrieval + control).

Design
------
``run_drift_check`` is the single public entry point:
  1. Load the retained current baseline (EvalBaselineRepository.get_current).
     No baseline → return DriftResult with status "no_baseline".
  2. Load the eval set associated with that baseline.
  3. Score the CURRENT live index via score_eval_set (retrieval path, no
     collection_override — we score the live alias, not a shadow).
  4. Compute Δrecall = baseline.recall - current.recall (positive = regression).
     Compute Δprecision = baseline.precision - current.precision (positive = regression).
  5. If regression (Δrecall > threshold):
       - Emit AuditAction.drift_detected (audit row with details).
       - Set telemetry gauge QUALITY_EVAL_DRIFT_SCORE_VS_BASELINE.
       - LOG at ERROR level (same shape as reindex cap-hit alert).
       - Return DriftResult(regressed=True, ...).
  6. If no regression:
       - Set telemetry gauge.
       - LOG at INFO level.
       - Return DriftResult(regressed=False, ...).

Post-reindex trigger (§9.4 "after every reindex")
-------------------------------------------------
The pipeline-layer JobRunner cannot import services (layer constraint C-5).
Applying the PR-7 fail-loud precedent: the eval_drift job arm in pipeline/jobs.py
FAILS LOUDLY if the job is dispatched via the queue.  The post-reindex drift is
instead triggered by a services-level hook called from the worker after
_run_ingest completes.  The hook enqueues a JobType.eval_drift job (records
intent in the queue) so the drift check is visible in the job log; the ACTUAL
SCORED DRIFT runs from services/ingest_worker.py scheduler_tick after the
enqueued job is claimed.

Cron trigger (§9.4 "on schedule")
-----------------------------------
scheduler_tick in services/ingest_worker.py reads observability.drift_detection_cron;
when set + due, calls run_drift_check directly for each active KB.
Disabled when cron is None.

Spec references: §9.4, M-100, §19 criterion 2.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy.orm import Session

from finecorpus.control.audit import AuditAction, AuditLogRepository
from finecorpus.control.eval_store import EvalBaselineRepository, EvalSetRepository
from finecorpus.embedding.base import EmbeddingProvider
from finecorpus.index.adapter import IndexAdapter
from finecorpus.services.eval_scoring import ScoreResult, score_eval_set

if TYPE_CHECKING:
    from finecorpus.config.models import Config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Telemetry gauge (add to telemetry.py imports at call site)
# ---------------------------------------------------------------------------

# We import lazily to avoid module-scope import failures in envs without
# prometheus_client — the gauge is set inside the function body.
_DRIFT_GAUGE_NAME = "rtfc_quality_eval_drift_score_vs_baseline"
_DRIFT_GAUGE_HELP = (
    "Recall delta between the retained baseline and the current live index scoring. "
    "Positive value = current index is WORSE than the retained baseline. "
    "Zero = no regression. Populated by run_drift_check (§9.4, M-100)."
)


def _get_drift_gauge() -> Any:
    """Lazy-load the drift gauge to avoid circular imports."""
    try:
        from finecorpus import telemetry as _tel

        # Check if QUALITY_EVAL_DRIFT_SCORE_VS_BASELINE exists; add it lazily if not.
        gauge = getattr(_tel, "QUALITY_EVAL_DRIFT_SCORE_VS_BASELINE", None)
        if gauge is not None:
            return gauge

        # Gauge not in telemetry yet — create it here (first call wins).
        from prometheus_client import Gauge

        g = Gauge(
            _DRIFT_GAUGE_NAME,
            _DRIFT_GAUGE_HELP,
            ["kb_id"],
        )
        # Cache on the telemetry module so repeated calls share the same instance.
        _tel.QUALITY_EVAL_DRIFT_SCORE_VS_BASELINE = g  # type: ignore[attr-defined]
        return g
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# DriftResult
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DriftResult:
    """Result of a drift check for a single KB.

    Attributes:
        kb_id: Knowledge-base evaluated.
        status: One of "regressed", "ok", "no_baseline", "no_eval_set",
            "scoring_failed".
        regressed: True when Δrecall > threshold.
        delta_recall: baseline.recall - current.recall (positive = regression).
        delta_precision: baseline.precision - current.precision (positive = regression).
        baseline_recall: The retained baseline recall score (None when no baseline).
        baseline_precision: The retained baseline precision score (None when no baseline).
        current_recall: The scored current recall (None when scoring failed).
        current_precision: The scored current precision (None when scoring failed).
        threshold: The regression threshold used.
        checked_at: UTC timestamp of this check.
        n_scored: Questions scored (0 when status is not ok/regressed).
    """

    kb_id: str
    status: str
    regressed: bool
    delta_recall: float
    delta_precision: float
    baseline_recall: float | None
    baseline_precision: float | None
    current_recall: float | None
    current_precision: float | None
    threshold: float
    checked_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))
    n_scored: int = 0


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def run_drift_check(
    kb_id: str,
    *,
    session: Session,
    adapter: IndexAdapter,
    provider: EmbeddingProvider,
    config: Config,
) -> DriftResult:
    """Run a drift check for a KB against the retained current baseline.

    Scores the LIVE index (no collection_override) and compares to the
    retained EvalBaselineRepository.get_current baseline.

    Layer: services (may import retrieval + control).  Do NOT call from
    pipeline — the pipeline layer cannot import services.

    Args:
        kb_id: Knowledge-base to check.
        session: SQLAlchemy Session bound to the control-plane DB.
        adapter: IndexAdapter for retrieval scoring.
        provider: EmbeddingProvider (must match KB's active index model).
        config: CorpusConfig for threshold + cron settings.

    Returns:
        DriftResult describing the regression status.
    """
    threshold: float = config.assessment.eval_regression_threshold
    checked_at = datetime.now(tz=UTC)

    # ------------------------------------------------------------------
    # Step 1: Load the retained current baseline
    # ------------------------------------------------------------------
    baseline_repo = EvalBaselineRepository(session)
    baseline = baseline_repo.get_current(kb_id)

    if baseline is None:
        logger.info(
            "drift_check: kb=%r no retained baseline — skipping drift check "
            "(run a promotion with eval set configured first).",
            kb_id,
        )
        return DriftResult(
            kb_id=kb_id,
            status="no_baseline",
            regressed=False,
            delta_recall=0.0,
            delta_precision=0.0,
            baseline_recall=None,
            baseline_precision=None,
            current_recall=None,
            current_precision=None,
            threshold=threshold,
            checked_at=checked_at,
            n_scored=0,
        )

    baseline_recall = baseline.recall
    baseline_precision = baseline.precision

    # ------------------------------------------------------------------
    # Step 2: Load the eval set associated with that baseline
    # ------------------------------------------------------------------
    eval_repo = EvalSetRepository(session)
    questions_records = eval_repo.get_questions(baseline.eval_set_id)

    if not questions_records:
        logger.warning(
            "drift_check: kb=%r baseline eval_set_id=%r has no questions — "
            "cannot score; returning 'no_eval_set' status.",
            kb_id,
            baseline.eval_set_id,
        )
        return DriftResult(
            kb_id=kb_id,
            status="no_eval_set",
            regressed=False,
            delta_recall=0.0,
            delta_precision=0.0,
            baseline_recall=baseline_recall,
            baseline_precision=baseline_precision,
            current_recall=None,
            current_precision=None,
            threshold=threshold,
            checked_at=checked_at,
            n_scored=0,
        )

    # Convert ORM records to EvalQuestion contracts
    from finecorpus.contracts.eval_set import (  # noqa: PLC0415
        ConfidenceLevel,
        EvalQuestion,
        GenerationMethod,
        QuestionType,
        ReviewStatus,
    )

    questions: list[EvalQuestion] = []
    for r in questions_records:
        try:
            questions.append(
                EvalQuestion(
                    question_id=r.question_id,
                    text=r.text,
                    question_type=QuestionType(r.question_type),
                    generation_method=GenerationMethod(r.generation_method),
                    review_status=ReviewStatus(r.review_status),
                    source_segment_ids=list(r.source_segment_ids or []),
                    source_unknown=bool(r.source_unknown),
                    expected_segment_ids=list(r.expected_segment_ids or [])
                    if r.expected_segment_ids
                    else None,
                    injection_suspicion=r.injection_suspicion,
                    reviewed_by=r.reviewed_by,
                    reviewed_at=r.reviewed_at,
                    class_description_ref=r.class_description_ref,
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "drift_check: kb=%r skipping question %r (parse error: %s)",
                kb_id,
                r.question_id,
                exc,
            )

    # ------------------------------------------------------------------
    # Step 3: Score the current LIVE index
    # ------------------------------------------------------------------
    k: int = 10
    score_result: ScoreResult | None = None
    try:
        # Load the eval set record to get confidence_level
        eval_set_record = eval_repo.get(baseline.eval_set_id)
        confidence_level = ConfidenceLevel.provisional
        if eval_set_record is not None:
            try:
                confidence_level = ConfidenceLevel(eval_set_record.confidence_level)
            except ValueError:
                pass

        score_result = score_eval_set(
            questions,
            kb_id=kb_id,
            adapter=adapter,
            provider=provider,
            session=session,
            k=k,
            confidence_level=confidence_level,
            # No collection_override — score the live alias (drift detection)
            collection_override=None,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "drift_check: kb=%r scoring failed — %s. Cannot determine drift.",
            kb_id,
            exc,
            exc_info=True,
        )
        return DriftResult(
            kb_id=kb_id,
            status="scoring_failed",
            regressed=False,
            delta_recall=0.0,
            delta_precision=0.0,
            baseline_recall=baseline_recall,
            baseline_precision=baseline_precision,
            current_recall=None,
            current_precision=None,
            threshold=threshold,
            checked_at=checked_at,
            n_scored=0,
        )

    current_recall = score_result.recall
    current_precision = score_result.precision
    delta_recall = baseline_recall - current_recall
    delta_precision = baseline_precision - current_precision

    # ------------------------------------------------------------------
    # Step 4: Set telemetry gauge (always — regression or not)
    # ------------------------------------------------------------------
    gauge = _get_drift_gauge()
    if gauge is not None:
        try:
            gauge.labels(kb_id=kb_id).set(delta_recall)
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------
    # Step 5: Determine regression and emit alert / audit
    # ------------------------------------------------------------------
    regressed = delta_recall > threshold

    if regressed:
        # ERROR-level alert — same shape as reindex cap-hit alert
        logger.error(
            "DRIFT ALERT kb=%r: recall regression detected "
            "(baseline=%.4f current=%.4f delta=%.4f threshold=%.4f). "
            "Review the live index quality and consider reindexing.",
            kb_id,
            baseline_recall,
            current_recall,
            delta_recall,
            threshold,
        )

        # Audit row
        try:
            audit_repo = AuditLogRepository(session)
            audit_repo.append(
                entry_type=AuditAction.drift_detected,
                actor_id="drift-check",
                target_kb_id=kb_id,
                details={
                    "baseline_recall": baseline_recall,
                    "baseline_precision": baseline_precision,
                    "current_recall": current_recall,
                    "current_precision": current_precision,
                    "delta_recall": delta_recall,
                    "delta_precision": delta_precision,
                    "threshold": threshold,
                    "eval_set_id": baseline.eval_set_id,
                    "n_scored": score_result.n_scored,
                    "checked_at": checked_at.isoformat(),
                },
                created_at=checked_at,
            )
            session.commit()
        except Exception as exc:  # noqa: BLE001
            logger.error("drift_check: audit append failed (non-fatal): %s", exc)

        return DriftResult(
            kb_id=kb_id,
            status="regressed",
            regressed=True,
            delta_recall=delta_recall,
            delta_precision=delta_precision,
            baseline_recall=baseline_recall,
            baseline_precision=baseline_precision,
            current_recall=current_recall,
            current_precision=current_precision,
            threshold=threshold,
            checked_at=checked_at,
            n_scored=score_result.n_scored,
        )

    # No regression
    logger.info(
        "drift_check: kb=%r no regression "
        "(baseline=%.4f current=%.4f delta=%.4f threshold=%.4f n_scored=%d)",
        kb_id,
        baseline_recall,
        current_recall,
        delta_recall,
        threshold,
        score_result.n_scored,
    )

    return DriftResult(
        kb_id=kb_id,
        status="ok",
        regressed=False,
        delta_recall=delta_recall,
        delta_precision=delta_precision,
        baseline_recall=baseline_recall,
        baseline_precision=baseline_precision,
        current_recall=current_recall,
        current_precision=current_precision,
        threshold=threshold,
        checked_at=checked_at,
        n_scored=score_result.n_scored,
    )


__all__ = [
    "DriftResult",
    "run_drift_check",
]
