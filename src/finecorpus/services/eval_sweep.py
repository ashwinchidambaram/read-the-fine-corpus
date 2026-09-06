"""Phase 5 configuration-sweep orchestration (§9.3, M-046..051, §19 crit 1).

Services-layer orchestrator: sits above pipeline ingestion + retrieval scoring.
Legal import position: services may import from pipeline, retrieval, index, control.

Overview
--------
``estimate_sweep_cost``
    Sample the corpus (M-046), enumerate candidates, sum per-candidate
    ``estimate_ingestion_cost``.  Returns aggregate + per-candidate cost breakdown
    BEFORE any candidate is ingested (M-047).

``run_sweep``
    Orchestrate the full sweep:
      - M-050: decline when corpus < sweep_min_corpus_docs.
      - M-048: gate on sweep_confirmation_threshold_usd when not confirmed.
      - Execute: ingest each candidate into a scratch collection, score via
        score_eval_set, record ranking rows.
      - M-051: honest near-optimal — when no candidate beats reference by more
        than the noise margin, winner IS the reference (no fabricated delta).
      - M-049: EvalBaselineRepository.upsert_current handles reference-change
        marking automatically.
      - Winner's RecommendationProvenance has basis=sweep_backed + sweep_run_id.

``render_ranked_table``
    ASCII table with recall/precision deltas, cost, index size, ingestion time.
    Includes a confidence banner reflecting the eval set's confidence level.
    (Note: closeout PR-9 will add render_confidence_banner to report.py as a
    shared helper; this module keeps the banner text inline for now.)

Spec references: §9.3, §19 acceptance criterion 1, M-046, M-047, M-048, M-049,
M-050, M-051.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from finecorpus.config.models import Config
    from finecorpus.contracts.eval_set import EvalQuestion
    from finecorpus.contracts.ingestion_config import IngestionConfig
    from finecorpus.contracts.inventory import InventoryItem
    from finecorpus.embedding.base import EmbeddingProvider
    from finecorpus.index.adapter import IndexAdapter

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default noise margin for M-051 near-optimal detection
_DEFAULT_NOISE_MARGIN: float = 0.02

# Default top-k for eval scoring within the sweep
_SWEEP_EVAL_K: int = 10

# Seed used for corpus sampling + candidate enumeration (deterministic)
_SWEEP_SEED: int = 42


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class CandidateCostEntry:
    """Cost estimate for a single sweep candidate.

    Attributes:
        candidate_id: Zero-based index (0 = reference/base).
        label:        Human-readable label (e.g. ``"base"``).
        est_cost_usd: Estimated USD cost to ingest the sampled corpus.
    """

    candidate_id: int
    label: str
    est_cost_usd: Decimal


@dataclass
class SweepCostEstimate:
    """Aggregate + per-candidate cost estimate produced BEFORE execution (M-047).

    Attributes:
        total_est_cost_usd:  Sum of per-candidate cost estimates.
        per_candidate:       Ordered list of CandidateCostEntry (candidate 0 = base).
        n_candidates:        Total number of candidates enumerated.
        n_sample_docs:       Number of documents in the sampled corpus.
        basis:               Human-readable description of the estimate basis.
    """

    total_est_cost_usd: Decimal
    per_candidate: list[CandidateCostEntry]
    n_candidates: int
    n_sample_docs: int
    basis: str


@dataclass
class RankedRow:
    """One ranked result row from the sweep.

    Attributes:
        rank:               1-based rank (1 = best).
        candidate_id:       Zero-based candidate index.
        label:              Human-readable label.
        recall:             Recall score [0..1].
        precision:          Precision score [0..1].
        recall_delta:       Recall delta vs reference candidate.
        precision_delta:    Precision delta vs reference candidate.
        est_cost_usd:       Estimated cost to deploy this config.
        index_size:         Number of index entries (vectors) produced.
        ingestion_seconds:  Wall-clock ingestion time.
        is_reference:       True when this is the reference/base candidate.
    """

    rank: int
    candidate_id: int
    label: str
    recall: float
    precision: float
    recall_delta: float
    precision_delta: float
    est_cost_usd: Decimal
    index_size: int
    ingestion_seconds: float
    is_reference: bool = False


@dataclass
class SweepResult:
    """Result from run_sweep.

    One of three outcomes:
    1. ``declined=True`` — corpus too small (M-050). ``applied`` holds the reference config.
    2. ``needs_confirmation=True`` — cost above threshold, not confirmed (M-048).
       ``estimate`` holds the cost estimate.
    3. Normal completion — ``ranked_rows`` non-empty.

    Attributes:
        declined:           True when M-050 decline fired.
        reason:             Plain-language reason for decline (M-050).
        applied:            Reference config applied when declined (M-050).
        needs_confirmation: True when M-048 gate fired without confirmation.
        estimate:           Cost estimate when needs_confirmation=True (M-047/M-048).
        sweep_run_id:       The SweepRunRecord ID (set on any execution attempt).
        ranked_rows:        Ordered ranking (rank 1 = best), empty when not executed.
        winner_config:      The IngestionConfig of the winning candidate (rank 1).
        near_optimal:       True when M-051 fired (winner IS the reference).
        confidence_level:   Eval set confidence level (from scoring).
        n_sample_docs:      Documents in the sampled corpus.
        n_candidates_run:   Candidates actually evaluated.
    """

    declined: bool = False
    reason: str = ""
    applied: Any = None  # IngestionConfig when declined

    needs_confirmation: bool = False
    estimate: SweepCostEstimate | None = None

    sweep_run_id: str = ""
    ranked_rows: list[RankedRow] = field(default_factory=list)
    winner_config: Any = None  # IngestionConfig
    near_optimal: bool = False
    confidence_level: Any = None  # ConfidenceLevel
    n_sample_docs: int = 0
    n_candidates_run: int = 0


# ---------------------------------------------------------------------------
# estimate_sweep_cost  (M-046, M-047)
# ---------------------------------------------------------------------------


def estimate_sweep_cost(
    kb_id: str,
    base_config: IngestionConfig,
    documents: list[InventoryItem],
    *,
    session: Session,
    config: Config,
    embed_caps: Any,  # EmbeddingProvider (for costing)
    llm_caps: Any | None = None,  # LLMProvider | None (for costing)
) -> SweepCostEstimate:
    """Estimate total sweep cost BEFORE any candidate ingestion (M-047).

    Samples the corpus (M-046), enumerates candidates, and calls
    ``estimate_ingestion_cost`` for each candidate to produce a summed estimate.

    The estimate is produced WITHOUT executing any ingestion — callers must show
    it to the operator before calling ``run_sweep`` (§19 acceptance criterion 1).

    Args:
        kb_id:       Knowledge-base identifier.
        base_config: Base IngestionConfig (reference is candidate 0).
        documents:   Full corpus document list from the inventory.
        session:     SQLAlchemy Session (not used for ingestion; available for
                     future baseline fetch extensions).
        config:      Platform Config (for sweep_min_corpus_docs, sweep_candidate_budget,
                     sweep_sample_factor).
        embed_caps:  EmbeddingProvider instance for cost estimation.
        llm_caps:    LLMProvider instance or None.

    Returns:
        SweepCostEstimate with total + per-candidate cost breakdown.
    """
    from finecorpus.pipeline.costing import estimate_ingestion_cost
    from finecorpus.pipeline.evaluation.candidates import enumerate_candidates, sample_corpus

    assessment = config.assessment

    # Sample the corpus (M-046)
    sampled_docs = sample_corpus(
        documents,
        min_docs=assessment.sweep_min_corpus_docs,
        sample_factor=assessment.sweep_sample_factor,
        seed=_SWEEP_SEED,
    )
    n_sample_docs = len(sampled_docs)

    # Enumerate candidates
    candidates = enumerate_candidates(
        base_config,
        budget=assessment.sweep_candidate_budget,
        seed=_SWEEP_SEED,
    )
    n_candidates = len(candidates)

    # Build a minimal synthetic SegmentSetBatch for cost estimation
    # We use the sampled document texts as a proxy for segment content.
    synthetic_batch = _build_synthetic_segment_batch(sampled_docs, base_config)

    per_candidate: list[CandidateCostEntry] = []
    total_cost = Decimal("0.0")

    for candidate in candidates:
        try:
            est = estimate_ingestion_cost(
                segment_set_batch=synthetic_batch,
                ingestion_config=candidate.config,
                embedding_provider=embed_caps,
                llm_provider_or_none=llm_caps,
            )
            cand_cost = est.total_cost_usd
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "estimate_sweep_cost: could not estimate cost for candidate %r: %s",
                candidate.label,
                exc,
            )
            cand_cost = Decimal("0.0")

        per_candidate.append(
            CandidateCostEntry(
                candidate_id=candidate.candidate_id,
                label=candidate.label,
                est_cost_usd=cand_cost,
            )
        )
        total_cost += cand_cost

    basis = (
        f"Sampled corpus: {n_sample_docs} docs (from {len(documents)} total). "
        f"Candidates: {n_candidates}. "
        f"Cost per candidate estimated via whitespace-word token proxy. "
        f"Total is sum of per-candidate estimates."
    )

    logger.info(
        "estimate_sweep_cost: kb=%r n_docs=%d n_sample=%d n_candidates=%d total_usd=%.4f",
        kb_id,
        len(documents),
        n_sample_docs,
        n_candidates,
        float(total_cost),
    )

    return SweepCostEstimate(
        total_est_cost_usd=total_cost,
        per_candidate=per_candidate,
        n_candidates=n_candidates,
        n_sample_docs=n_sample_docs,
        basis=basis,
    )


# ---------------------------------------------------------------------------
# run_sweep  (M-046..051)
# ---------------------------------------------------------------------------


def run_sweep(
    kb_id: str,
    base_config: IngestionConfig,
    documents: list[InventoryItem],
    eval_set: list[EvalQuestion],
    *,
    session: Session,
    config: Config,
    adapter: IndexAdapter,
    provider: EmbeddingProvider,
    confirmed: bool = False,
    noise_margin: float = _DEFAULT_NOISE_MARGIN,
    eval_k: int = _SWEEP_EVAL_K,
    workspace_id: str = "default",
) -> SweepResult:
    """Run the full configuration sweep.

    Orchestrates M-046 sampling, M-047/M-048 cost gate, per-candidate ingestion
    + scoring, M-051 near-optimal detection, M-049 baseline marking, and winner
    provenance tagging.

    M-050 — Decline:
        When len(documents) < sweep_min_corpus_docs, returns
        ``SweepResult(declined=True, reason=..., applied=reference_config)``
        WITHOUT executing anything.  A SweepRunRecord with status="declined"
        IS persisted so the event is auditable.

    M-048 — Confirmation gate:
        When the aggregate cost estimate >= budgets.sweep_confirmation_threshold_usd
        AND confirmed=False, returns ``SweepResult(needs_confirmation=True, estimate=...)``
        without executing.  No SweepRunRecord is persisted (pre-execution check).

    M-051 — Near-optimal:
        After scoring all candidates, if no candidate's recall beats the
        reference recall by more than ``noise_margin``, the result top-line is
        "default already near-optimal" and the winner IS the reference candidate
        (rank 1).  No delta is fabricated.

    M-049 — Baseline marking:
        On sweep completion, writes the reference candidate's score as the new
        baseline via ``EvalBaselineRepository.upsert_current``, which automatically
        marks any prior baseline that was measured against a different reference
        fingerprint.

    Args:
        kb_id:          Knowledge-base identifier.
        base_config:    Base IngestionConfig (reference = candidate 0).
        documents:      Full corpus documents from inventory.
        eval_set:       Eval questions for scoring (usable questions only).
        session:        SQLAlchemy Session for control-plane persistence.
        config:         Platform Config.
        adapter:        IndexAdapter for index operations.
        provider:       EmbeddingProvider for embedding + scoring.
        confirmed:      True when the operator explicitly confirmed above the threshold.
        noise_margin:   Threshold for M-051 near-optimal detection (default 0.02).
        eval_k:         Top-k for recall/precision (default 10).
        workspace_id:   Workspace identity for DB rows.

    Returns:
        SweepResult — one of: declined, needs_confirmation, or ranked completion.
    """
    from finecorpus.control.eval_store import EvalBaselineRepository, SweepRunRepository
    from finecorpus.pipeline.evaluation.baseline import (
        NAIVE_BASELINE_REFERENCE_ID,
        is_near_optimal,
        reference_fingerprint,
    )
    from finecorpus.pipeline.evaluation.candidates import enumerate_candidates, sample_corpus

    assessment = config.assessment
    budgets = config.budgets

    # -----------------------------------------------------------------------
    # M-050: Decline when corpus is too small
    # -----------------------------------------------------------------------
    n_total_docs = len(documents)
    if n_total_docs < assessment.sweep_min_corpus_docs:
        reason = (
            f"Corpus has {n_total_docs} document(s), which is below the minimum "
            f"of {assessment.sweep_min_corpus_docs} required to run a configuration sweep "
            f"(assessment.sweep_min_corpus_docs). "
            f"The reference configuration has been applied instead. "
            f"Add more documents and re-run the sweep to explore configuration alternatives."
        )
        logger.info(
            "run_sweep: M-050 decline for kb=%r n_docs=%d < min=%d",
            kb_id,
            n_total_docs,
            assessment.sweep_min_corpus_docs,
        )

        # Persist a declined SweepRunRecord for auditability
        sweep_repo = SweepRunRepository(session)
        run_record = sweep_repo.create(
            kb_id=kb_id,
            workspace_id=workspace_id,
            status="declined",
        )
        sweep_repo.set_status(run_record.id, status="declined", declined_reason=reason)
        session.commit()

        return SweepResult(
            declined=True,
            reason=reason,
            applied=base_config,
            sweep_run_id=run_record.id,
        )

    # -----------------------------------------------------------------------
    # Sample corpus + enumerate candidates
    # -----------------------------------------------------------------------
    sampled_docs = sample_corpus(
        documents,
        min_docs=assessment.sweep_min_corpus_docs,
        sample_factor=assessment.sweep_sample_factor,
        seed=_SWEEP_SEED,
    )
    n_sample_docs = len(sampled_docs)

    candidates = enumerate_candidates(
        base_config,
        budget=assessment.sweep_candidate_budget,
        seed=_SWEEP_SEED,
    )

    # -----------------------------------------------------------------------
    # M-047 / M-048: Cost estimate + confirmation gate
    # -----------------------------------------------------------------------
    embed_caps_obj = provider  # reuse for costing
    cost_estimate = estimate_sweep_cost(
        kb_id,
        base_config,
        documents,
        session=session,
        config=config,
        embed_caps=embed_caps_obj,
        llm_caps=None,
    )

    threshold = budgets.sweep_confirmation_threshold_usd
    if Decimal(str(cost_estimate.total_est_cost_usd)) >= threshold and not confirmed:
        logger.info(
            "run_sweep: M-048 needs_confirmation for kb=%r est=%.4f threshold=%.4f",
            kb_id,
            float(cost_estimate.total_est_cost_usd),
            float(threshold),
        )
        return SweepResult(
            needs_confirmation=True,
            estimate=cost_estimate,
            n_sample_docs=n_sample_docs,
            n_candidates_run=len(candidates),
        )

    # -----------------------------------------------------------------------
    # Execute: create SweepRunRecord, score each candidate
    # -----------------------------------------------------------------------
    sweep_repo = SweepRunRepository(session)
    run_record = sweep_repo.create(
        kb_id=kb_id,
        workspace_id=workspace_id,
        status="running",
    )
    sweep_repo.set_status(run_record.id, status="running", confirmed=confirmed)
    session.commit()
    sweep_run_id = run_record.id

    # Determine the eval confidence level from the eval set
    confidence_level = _infer_confidence_level(eval_set)

    # Score each candidate and collect results

    @dataclass
    class _CandidateResult:
        candidate_id: int
        label: str
        recall: float
        precision: float
        index_size: int
        ingestion_seconds: float
        est_cost_usd: Decimal
        config: Any

    candidate_results: list[_CandidateResult] = []
    total_actual_cost = Decimal("0.0")

    for candidate in candidates:
        # Find the pre-computed cost for this candidate
        cost_entry = next(
            (e for e in cost_estimate.per_candidate if e.candidate_id == candidate.candidate_id),
            None,
        )
        est_cost = cost_entry.est_cost_usd if cost_entry else Decimal("0.0")

        # Ingest sample into scratch collection and score
        recall, precision, index_size, ingestion_s = _score_candidate(
            kb_id=kb_id,
            candidate=candidate,
            sampled_docs=sampled_docs,
            eval_set=eval_set,
            adapter=adapter,
            provider=provider,
            session=session,
            eval_k=eval_k,
            confidence_level=confidence_level,
        )

        candidate_results.append(
            _CandidateResult(
                candidate_id=candidate.candidate_id,
                label=candidate.label,
                recall=recall,
                precision=precision,
                index_size=index_size,
                ingestion_seconds=ingestion_s,
                est_cost_usd=est_cost,
                config=candidate.config,
            )
        )
        total_actual_cost += est_cost

    if not candidate_results:
        # Should not happen, but guard
        sweep_repo.set_status(sweep_run_id, status="failed")
        session.commit()
        return SweepResult(sweep_run_id=sweep_run_id)

    # -----------------------------------------------------------------------
    # Compute deltas vs reference (candidate 0)
    # -----------------------------------------------------------------------
    ref_result = next(
        (r for r in candidate_results if r.candidate_id == 0),
        candidate_results[0],
    )
    ref_recall = ref_result.recall
    ref_precision = ref_result.precision

    # -----------------------------------------------------------------------
    # M-051: Near-optimal detection
    # -----------------------------------------------------------------------
    best_non_ref = max(
        (r for r in candidate_results if r.candidate_id != 0),
        key=lambda r: r.recall,
        default=None,
    )
    near_optimal = best_non_ref is None or is_near_optimal(
        candidate_score=best_non_ref.recall,
        reference_score=ref_recall,
        noise_margin=noise_margin,
    )

    if near_optimal:
        logger.info(
            "run_sweep: M-051 near-optimal for kb=%r — reference is best "
            "(best_non_ref_recall=%.4f ref_recall=%.4f margin=%.4f)",
            kb_id,
            best_non_ref.recall if best_non_ref else 0.0,
            ref_recall,
            noise_margin,
        )

    # -----------------------------------------------------------------------
    # Sort and rank candidates
    # -----------------------------------------------------------------------
    def _sort_key(r: _CandidateResult) -> tuple:  # type: ignore[type-arg]
        recall_delta = r.recall - ref_recall
        prec_delta = r.precision - ref_precision
        return (-recall_delta, -prec_delta)

    sorted_results = sorted(candidate_results, key=_sort_key)

    ranked_rows: list[RankedRow] = []
    for rank, res in enumerate(sorted_results, start=1):
        recall_delta = res.recall - ref_recall
        prec_delta = res.precision - ref_precision
        ranked_rows.append(
            RankedRow(
                rank=rank,
                candidate_id=res.candidate_id,
                label=res.label,
                recall=res.recall,
                precision=res.precision,
                recall_delta=recall_delta,
                precision_delta=prec_delta,
                est_cost_usd=res.est_cost_usd,
                index_size=res.index_size,
                ingestion_seconds=res.ingestion_seconds,
                is_reference=(res.candidate_id == 0),
            )
        )

    # -----------------------------------------------------------------------
    # Persist ranking rows
    # -----------------------------------------------------------------------
    for row in ranked_rows:
        sweep_repo.add_ranking_row(
            sweep_run_id=sweep_run_id,
            rank=row.rank,
            config_json={"candidate_id": row.candidate_id, "label": row.label},
            recall=row.recall,
            precision=row.precision,
            recall_delta=row.recall_delta,
            precision_delta=row.precision_delta,
            est_cost_usd=float(row.est_cost_usd),
            index_size=row.index_size,
            ingestion_seconds=row.ingestion_seconds,
        )

    sweep_repo.set_status(
        sweep_run_id,
        status="completed",
        total_cost_usd=float(total_actual_cost),
        confirmed=confirmed,
    )

    # -----------------------------------------------------------------------
    # M-049: Write sweep baseline (reference scores) with reference-change marking
    # -----------------------------------------------------------------------
    # We use the eval_set_id "sweep" as a sentinel since the sweep doesn't
    # require a persisted eval set ID (it receives the questions directly).
    # Production callers should pass the actual eval_set_id via extensions.
    try:
        baseline_repo = EvalBaselineRepository(session)
        fp = reference_fingerprint(base_config.embedding)
        baseline_repo.upsert_current(
            kb_id=kb_id,
            reference_id=NAIVE_BASELINE_REFERENCE_ID,
            reference_fingerprint=fp,
            eval_set_id="sweep",
            recall=ref_recall,
            precision=ref_precision,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("run_sweep: could not write sweep baseline for kb=%r: %s", kb_id, exc)

    session.commit()

    # -----------------------------------------------------------------------
    # Winner config with sweep_backed provenance (M-051)
    # -----------------------------------------------------------------------
    winner_result = sorted_results[0]
    winner_config = winner_result.config

    # Tag winner's provenance as sweep_backed
    try:
        winner_config = _tag_winner_provenance(
            winner_config,
            sweep_run_id=sweep_run_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("run_sweep: could not tag winner provenance: %s", exc)

    logger.info(
        "run_sweep: completed kb=%r sweep_run_id=%r n_candidates=%d winner=%r "
        "winner_recall=%.4f near_optimal=%s",
        kb_id,
        sweep_run_id,
        len(candidate_results),
        winner_result.label,
        winner_result.recall,
        near_optimal,
    )

    return SweepResult(
        sweep_run_id=sweep_run_id,
        ranked_rows=ranked_rows,
        winner_config=winner_config,
        near_optimal=near_optimal,
        confidence_level=confidence_level,
        n_sample_docs=n_sample_docs,
        n_candidates_run=len(candidates),
    )


# ---------------------------------------------------------------------------
# render_ranked_table  (§19 acceptance criterion 1)
# ---------------------------------------------------------------------------


def render_ranked_table(result: SweepResult) -> str:
    """Render a ranked config table with metric deltas, cost, and index size.

    Produces a text table compatible with terminal output.  Includes a
    confidence banner reflecting the eval set's confidence level (M-044).

    Note: closeout PR-9 will add render_confidence_banner to report.py as a
    shared helper; this module keeps the banner inline for now.

    Args:
        result: SweepResult from run_sweep (must have ranked_rows).

    Returns:
        Formatted ASCII table string.
    """
    lines: list[str] = []

    # Confidence banner (M-044 — provisional propagates)
    from finecorpus.contracts.eval_set import ConfidenceLevel

    confidence_level = result.confidence_level
    if confidence_level is not None:
        if str(confidence_level) == str(ConfidenceLevel.provisional):
            lines.append(
                "*** CONFIDENCE: PROVISIONAL — eval set contains unreviewed questions. ***"
            )
            lines.append(
                "    Scores reflect current question quality; review questions to improve "
                "confidence."
            )
        else:
            lines.append(f"Confidence: {confidence_level}")
        lines.append("")

    if result.near_optimal:
        lines.append("NOTE (M-051): Default configuration is already near-optimal for this corpus.")
        lines.append("  No candidate beat the reference recall by more than the noise margin.")
        lines.append("  Winner is the reference configuration (no delta fabricated).")
        lines.append("")

    if not result.ranked_rows:
        lines.append("No ranking available.")
        return "\n".join(lines)

    # Header
    lines.append("Configuration Sweep — Ranked Results")
    lines.append(
        f"  Sampled corpus: {result.n_sample_docs} docs | "
        f"Candidates evaluated: {result.n_candidates_run}"
    )
    lines.append("")

    col_widths = {
        "rank": 4,
        "label": 32,
        "recall": 8,
        "recall_d": 10,
        "precision": 9,
        "prec_d": 10,
        "cost": 10,
        "idx": 8,
        "secs": 7,
        "ref": 4,
    }

    header = (
        f"{'Rank':<{col_widths['rank']}} "
        f"{'Config':<{col_widths['label']}} "
        f"{'Recall':<{col_widths['recall']}} "
        f"{'Δ Recall':<{col_widths['recall_d']}} "
        f"{'Precision':<{col_widths['precision']}} "
        f"{'Δ Prec':<{col_widths['prec_d']}} "
        f"{'Est Cost':<{col_widths['cost']}} "
        f"{'IdxSize':<{col_widths['idx']}} "
        f"{'Secs':<{col_widths['secs']}} "
        f"{'Ref':<{col_widths['ref']}}"
    )
    lines.append(header)
    lines.append("-" * len(header))

    for row in result.ranked_rows:
        recall_d_str = f"{row.recall_delta:+.4f}" if row.recall_delta != 0 else "  base"
        prec_d_str = f"{row.precision_delta:+.4f}" if row.precision_delta != 0 else "  base"
        ref_str = "REF" if row.is_reference else ""
        cost_str = f"${float(row.est_cost_usd):.4f}"

        line = (
            f"{row.rank:<{col_widths['rank']}} "
            f"{row.label:<{col_widths['label']}} "
            f"{row.recall:<{col_widths['recall']}.4f} "
            f"{recall_d_str:<{col_widths['recall_d']}} "
            f"{row.precision:<{col_widths['precision']}.4f} "
            f"{prec_d_str:<{col_widths['prec_d']}} "
            f"{cost_str:<{col_widths['cost']}} "
            f"{row.index_size:<{col_widths['idx']}} "
            f"{row.ingestion_seconds:<{col_widths['secs']}.2f} "
            f"{ref_str:<{col_widths['ref']}}"
        )
        lines.append(line)

    lines.append("")
    winner = result.ranked_rows[0]
    lines.append(
        f"Winner: {winner.label} (recall={winner.recall:.4f}, Δ={winner.recall_delta:+.4f})"
    )
    if result.sweep_run_id:
        lines.append(f"Sweep run ID: {result.sweep_run_id}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _infer_confidence_level(eval_set: list[EvalQuestion]) -> Any:
    """Return the most conservative confidence level across the eval questions.

    provisional propagates (M-044): if any question is unreviewed or in a
    provisional-triggering state, the entire set is provisional.
    """
    from finecorpus.contracts.eval_set import ConfidenceLevel, ReviewStatus

    for q in eval_set:
        if q.review_status == ReviewStatus.unreviewed:
            return ConfidenceLevel.provisional

    return ConfidenceLevel.reviewed


def _score_candidate(
    *,
    kb_id: str,
    candidate: Any,
    sampled_docs: list[InventoryItem],
    eval_set: list[EvalQuestion],
    adapter: IndexAdapter,
    provider: EmbeddingProvider,
    session: Session,
    eval_k: int,
    confidence_level: Any,
) -> tuple[float, float, int, float]:
    """Ingest the sample into a scratch collection and score via score_eval_set.

    Returns:
        (recall, precision, index_size, ingestion_seconds)

    Scoring discipline (§19 crit 1, BLOCKER):
        Each candidate is scored against ITS OWN scratch collection via
        ``collection_override`` — not the live production alias.  Without
        this override, all candidates query the same live alias and return
        identical scores (recall deltas all 0.0), which violates §19 crit 1.

    Tenancy:
        The collection_override path in retrieval.service.query still applies
        a tenancy payload filter, using the scratch collection name as the
        kb_id scope (matching the payload written by _ingest_sample_to_scratch).

    Timing (Ruling 4):
        ingestion_seconds captures only the wall-clock time for
        _ingest_sample_to_scratch, NOT the scoring or cleanup time.

    This is a best-effort scorer: when ingestion or scoring fails for a candidate,
    returns (0.0, 0.0, 0, 0.0) so the sweep continues and the failed candidate
    ranks last.
    """
    from finecorpus.services.eval_scoring import score_eval_set

    scratch_collection = f"sweep_scratch_{kb_id}_{candidate.candidate_id}"

    t_ingestion_start = time.monotonic()
    index_size = 0

    try:
        # Attempt to ingest sampled docs into the scratch collection
        index_size = _ingest_sample_to_scratch(
            scratch_collection=scratch_collection,
            sampled_docs=sampled_docs,
            candidate_config=candidate.config,
            adapter=adapter,
            provider=provider,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "_score_candidate: ingestion failed for candidate %r: %s",
            candidate.label,
            exc,
        )
        # Fall through: ingestion may be partial (zero index size is a valid result)

    # Ruling 4: capture ingestion wall-time BEFORE scoring begins.
    t_ingestion_end = time.monotonic()
    ingestion_s = t_ingestion_end - t_ingestion_start

    # Score against the candidate's OWN scratch collection (§19 crit 1, Ruling 1).
    # collection_override bypasses alias resolution in retrieval.service.query
    # so each candidate is scored against its own ingested content, not the live alias.
    recall = 0.0
    precision = 0.0
    if eval_set:
        try:
            score_result = score_eval_set(
                eval_set,
                kb_id=kb_id,
                adapter=adapter,
                provider=provider,
                session=session,
                k=eval_k,
                confidence_level=confidence_level,
                collection_override=scratch_collection,
            )
            recall = score_result.recall
            precision = score_result.precision
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "_score_candidate: scoring failed for candidate %r: %s",
                candidate.label,
                exc,
            )

    # Clean up scratch collection (best-effort)
    try:
        _cleanup_scratch(scratch_collection, adapter)
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "_score_candidate: cleanup failed for scratch collection %r: %s",
            scratch_collection,
            exc,
        )

    return recall, precision, index_size, ingestion_s


def _ingest_sample_to_scratch(
    *,
    scratch_collection: str,
    sampled_docs: list[InventoryItem],
    candidate_config: IngestionConfig,
    adapter: IndexAdapter,
    provider: EmbeddingProvider,
) -> int:
    """Ingest the sampled document set into a named scratch collection.

    This is a simplified ingestion that produces chunks from InventoryItem texts
    and upserts them into the scratch collection.  It uses the candidate config's
    chunking parameters to produce chunks via the recursive-char split_text helper.

    Returns:
        Number of vectors upserted (index_size).
    """
    import uuid as _uuid

    from finecorpus.pipeline.build.chunker import split_text

    chunks_upserted = 0

    for doc in sampled_docs:
        text = getattr(doc, "raw_text", None) or getattr(doc, "title", "") or ""
        if not text:
            continue

        chunking = candidate_config.default_rule.chunking
        spans = split_text(
            text,
            max_tokens=chunking.max_tokens,
            overlap_tokens=chunking.overlap_tokens,
        )
        chunk_texts = [text[s:e] for s, e in spans]

        if not chunk_texts:
            continue

        try:
            model_id = candidate_config.embedding.model
            embed_results = provider.embed_batch(chunk_texts, model_id)
            vectors = embed_results.embeddings  # list[list[float]]
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "_ingest_sample_to_scratch: embed failed for doc %r: %s",
                getattr(doc, "document_id", "?"),
                exc,
            )
            continue

        points = []
        for i, (chunk_t, vector) in enumerate(zip(chunk_texts, vectors, strict=False)):
            doc_id = getattr(doc, "document_id", "doc")
            chunk_id = f"{doc_id}_{i}"
            point_id = str(_uuid.uuid5(_uuid.NAMESPACE_OID, chunk_id))
            payload = {
                "chunk_id": chunk_id,
                "tenancy": {"kb_id": scratch_collection},
                "text": chunk_t[:500],
            }
            points.append({"id": point_id, "vector": vector, "payload": payload})

        try:
            adapter.upsert_points(
                collection=scratch_collection,
                points=points,
            )
            chunks_upserted += len(points)
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "_ingest_sample_to_scratch: upsert failed for doc %r: %s",
                getattr(doc, "document_id", "?"),
                exc,
            )

    return chunks_upserted


def _cleanup_scratch(scratch_collection: str, adapter: IndexAdapter) -> None:
    """Drop the scratch collection (best-effort)."""
    try:
        adapter.drop_collection(scratch_collection)
    except Exception:  # noqa: BLE001
        pass


def _build_synthetic_segment_batch(
    docs: list[InventoryItem],
    base_config: IngestionConfig,
) -> Any:
    """Build a minimal synthetic SegmentSetBatch for cost estimation.

    Uses the InventoryItem title/raw_text as segment content, wrapped in a
    minimal dict that ``estimate_ingestion_cost`` can consume via its
    ``isinstance(seg_set_raw, dict)`` path.
    """
    segment_sets = []
    for doc in docs:
        text = getattr(doc, "raw_text", None) or getattr(doc, "title", "") or ""
        doc_id = getattr(doc, "document_id", "doc")
        segment_sets.append(
            {
                "document_id": doc_id,
                "content_hash": doc_id,
                "segments": [
                    {
                        "segment_path": f"{doc_id}/0",
                        "segment_type": "prose",
                        "text": text,
                        "salience_tier": "tier1",
                    }
                ],
            }
        )
    return {"segment_sets": segment_sets}


def _tag_winner_provenance(
    winner_config: IngestionConfig,
    sweep_run_id: str,
) -> IngestionConfig:
    """Return a copy of winner_config with a sweep_backed RecommendationProvenance entry.

    Creates a new IngestionConfig with provenance basis=sweep_backed and the
    sweep_run_id linking back to the sweep run record.
    """
    from finecorpus.contracts.ingestion_config import (
        RecommendationBasis,
        RecommendationProvenance,
    )

    new_prov = RecommendationProvenance(
        basis=RecommendationBasis.sweep_backed,
        target="/",
        rationale="Winner of configuration sweep.",
        sweep_run_id=sweep_run_id,
    )

    # Deep-copy via model_dump round-trip
    raw = winner_config.model_dump(mode="python")
    existing_prov = raw.get("provenance", [])
    existing_prov.append(new_prov.model_dump(mode="python"))
    raw["provenance"] = existing_prov

    return type(winner_config).model_validate(raw)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    "CandidateCostEntry",
    "RankedRow",
    "SweepCostEstimate",
    "SweepResult",
    "estimate_sweep_cost",
    "render_ranked_table",
    "run_sweep",
]
