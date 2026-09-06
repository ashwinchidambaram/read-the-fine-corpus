"""Retrieval-quality metrics — pure functions.

Scope: retrieval quality ONLY (context recall and context precision).
This module does NOT assess answer correctness, semantic similarity, or any
LLM-graded quality. Those concerns belong in a separate evaluation layer.

All functions are deterministic pure functions over in-memory data:
  - no I/O
  - no network
  - no imports from finecorpus.index, finecorpus.retrieval, or finecorpus.services

Empty-set conventions (documented at each function):
  - context_recall_at_k:    0.0 when expected_ids is empty (no gold to recall).
  - context_precision_at_k: 0.0 when denominator is zero (no candidates in window).
  - aggregate_scores:       0.0 mean when input list is empty; per-type absent when no
                            questions of that type exist.

Sentinel-exclusion semantics (aggregate_scores):
  - context_recall_at_k returns a 0.0 sentinel when expected_ids is empty.  Including
    that sentinel in the recall mean silently understates quality.  aggregate_scores
    therefore computes mean_recall over only SCORABLE questions (scorable=True), i.e.
    questions whose source had non-empty expected_ids.  Questions with no gold segments
    cannot contribute meaningful information to a recall average.
  - context_precision_at_k denominator is |top_k|, which is always well-defined
    regardless of expected_ids; its 0.0 value for empty-expected questions is a genuine
    "no hits" reading, not a sentinel.  Non-scorable questions ARE included in the
    precision mean (asymmetric treatment, documented on aggregate_scores).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field

from finecorpus.contracts.eval_set import QuestionType


@dataclass(frozen=True)
class QuestionScore:
    """Retrieval scores for a single eval question.

    Attributes:
        question_id:    Identity of the scored question (matches EvalQuestion.question_id).
        question_type:  Stratification class (factual_lookup / interpretive /
                        multi_document_synthesis).
        recall:         context_recall_at_k for this question.
        precision:      context_precision_at_k for this question.
        scorable:       True when the source question had non-empty expected_ids (i.e. the
                        recall value is a genuine score, not the empty-expected sentinel 0.0).
                        aggregate_scores computes mean_recall only over scorable=True questions.
                        Defaults to True so that call sites that pre-filter empty-expected
                        questions are not forced to pass the flag explicitly.
    """

    question_id: str
    question_type: QuestionType
    recall: float
    precision: float
    scorable: bool = True


@dataclass(frozen=True)
class ScoreSummary:
    """Aggregated retrieval scores across a collection of eval questions.

    mean_recall and mean_precision are the arithmetic means over all questions in
    per_question.  recall_by_type / precision_by_type give the same aggregates broken
    out by QuestionType; a type is absent from those dicts when no question of that type
    exists in the input.

    Ordering: per_question preserves the input order.  recall_by_type /
    precision_by_type are sorted by QuestionType value for deterministic output.

    Attributes:
        mean_recall:        Arithmetic mean of recall over all questions.
        mean_precision:     Arithmetic mean of precision over all questions.
        recall_by_type:     Per-QuestionType mean recall, sorted by type value.
        precision_by_type:  Per-QuestionType mean precision, sorted by type value.
        per_question:       Individual scores, in input order.
    """

    mean_recall: float
    mean_precision: float
    recall_by_type: dict[QuestionType, float]
    precision_by_type: dict[QuestionType, float]
    per_question: list[QuestionScore] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Core metric functions
# ---------------------------------------------------------------------------


def context_recall_at_k(
    retrieved_ids: Sequence[str],
    expected_ids: Sequence[str],
    k: int,
) -> float:
    """Fraction of expected (gold) segment IDs found in the top-k retrieved results.

    Formula: |retrieved[:k] ∩ expected| / |expected|

    Empty-set convention: returns 0.0 when expected_ids is empty.
    Rationale: there is nothing to recall; a score of 0.0 is the least-surprising
    sentinel and avoids undefined-division exceptions in the caller.  Questions with
    no gold segments should be excluded from aggregate recall by the caller.

    Args:
        retrieved_ids: Ranked list of retrieved segment IDs (order is significant).
        expected_ids:  Gold segment IDs expected to appear in retrieval.
        k:             Cut-off; only the first k entries of retrieved_ids are considered.
                       If k > len(retrieved_ids), the entire list is used.

    Returns:
        Float in [0.0, 1.0].
    """
    if not expected_ids:
        return 0.0

    expected_set = set(expected_ids)
    top_k = set(retrieved_ids[:k])
    hits = len(top_k & expected_set)
    return hits / len(expected_set)


def context_precision_at_k(
    retrieved_ids: Sequence[str],
    expected_ids: Sequence[str],
    k: int,
) -> float:
    """Fraction of the top-k retrieved results that are relevant (gold) segment IDs.

    Formula: |set(retrieved[:k]) ∩ set(expected)| / min(k, |retrieved[:k]|)

    Set semantics: both the numerator intersection and the denominator use set
    cardinality to match the spec's set-cardinality notation and to remain consistent
    with context_recall_at_k.  Duplicate IDs in retrieved_ids are deduplicated before
    the intersection is computed, so they do not inflate the hit count.

    Empty-set convention: returns 0.0 when the denominator is zero (i.e., when
    retrieved_ids is empty OR k == 0).  This avoids division-by-zero and is the
    least-surprising sentinel for "we retrieved nothing".

    Note: expected_ids being empty also yields 0.0 because the intersection is empty.

    Args:
        retrieved_ids: Ranked list of retrieved segment IDs (order is significant for
                       k-truncation; duplicates are deduplicated before counting).
        expected_ids:  Gold segment IDs considered relevant.
        k:             Cut-off; only the first k entries of retrieved_ids are considered.
                       If k > len(retrieved_ids), the actual retrieved list length is used.

    Returns:
        Float in [0.0, 1.0].
    """
    top_k = list(retrieved_ids[:k])
    denominator = len(top_k)
    if denominator == 0:
        return 0.0

    expected_set = set(expected_ids)
    top_k_set = set(top_k)
    hits = len(top_k_set & expected_set)
    return hits / denominator


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def aggregate_scores(per_question: list[QuestionScore]) -> ScoreSummary:
    """Compute mean recall and precision overall and per QuestionType.

    Deterministic: output ordering of per-type dicts is sorted by QuestionType
    enum value (alphabetical on the string value), so the same input always
    produces identically ordered output regardless of insertion order.

    Empty-list convention: returns 0.0 for mean_recall and mean_precision when
    per_question is empty; recall_by_type and precision_by_type are empty dicts.

    Sentinel-exclusion (recall): context_recall_at_k returns 0.0 when expected_ids is
    empty (a sentinel, not a genuine score).  Including such sentinels in the recall mean
    silently understates quality.  mean_recall is therefore computed over only the SCORABLE
    subset (q.scorable == True).  If no scorable questions exist, mean_recall is 0.0
    (empty-set convention).  Questions with no gold segments cannot contribute meaningful
    information to a recall average and are documented as such.

    Precision asymmetry: context_precision_at_k denominator is |top_k|, not |expected|,
    so its value is well-defined even when expected_ids is empty (0.0 means "no hits in
    retrieved window", a genuine reading).  Non-scorable questions ARE included in the
    precision mean.  This asymmetry is intentional and documented here.

    Args:
        per_question: Individual question scores (QuestionScore instances).

    Returns:
        ScoreSummary with overall and per-type aggregates.
    """
    if not per_question:
        return ScoreSummary(
            mean_recall=0.0,
            mean_precision=0.0,
            recall_by_type={},
            precision_by_type={},
            per_question=[],
        )

    # Overall recall mean — ONLY over scorable questions (sentinel exclusion)
    scorable = [q for q in per_question if q.scorable]
    mean_recall = sum(q.recall for q in scorable) / len(scorable) if scorable else 0.0

    # Overall precision mean — ALL questions (precision is always well-defined)
    mean_precision = sum(q.precision for q in per_question) / len(per_question)

    # Per-type accumulation
    # Recall: exclude non-scorable; precision: include all
    type_recalls: dict[QuestionType, list[float]] = defaultdict(list)
    type_precisions: dict[QuestionType, list[float]] = defaultdict(list)
    for q in per_question:
        if q.scorable:
            type_recalls[q.question_type].append(q.recall)
        type_precisions[q.question_type].append(q.precision)

    # Sort by enum value for deterministic ordering
    # Use union of types from both recall and precision dicts to cover all types
    all_types = set(type_recalls.keys()) | set(type_precisions.keys())
    sorted_types = sorted(all_types, key=lambda qt: qt.value)

    recall_by_type = {
        qt: sum(type_recalls[qt]) / len(type_recalls[qt]) for qt in sorted_types if type_recalls[qt]
    }
    precision_by_type = {
        qt: sum(type_precisions[qt]) / len(type_precisions[qt]) for qt in sorted_types
    }

    return ScoreSummary(
        mean_recall=mean_recall,
        mean_precision=mean_precision,
        recall_by_type=recall_by_type,
        precision_by_type=precision_by_type,
        per_question=list(per_question),
    )


__all__ = [
    "QuestionScore",
    "ScoreSummary",
    "context_recall_at_k",
    "context_precision_at_k",
    "aggregate_scores",
]
