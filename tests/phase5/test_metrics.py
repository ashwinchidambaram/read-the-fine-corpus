"""Tests for finecorpus.pipeline.evaluation.metrics.

Covers:
- context_recall_at_k: edge cases (empty expected, k > retrieved, no overlap,
  full overlap, partial overlap).
- context_precision_at_k: edge cases (empty expected, empty retrieved, k=0,
  no overlap, full overlap, partial overlap, k > retrieved length).
- aggregate_scores: multiple QuestionTypes, overall means, per-type means,
  empty input, determinism (same input → identical output).
"""

from __future__ import annotations

import pytest

from finecorpus.contracts.eval_set import QuestionType
from finecorpus.pipeline.evaluation.metrics import (
    QuestionScore,
    ScoreSummary,
    aggregate_scores,
    context_precision_at_k,
    context_recall_at_k,
)

# ---------------------------------------------------------------------------
# context_recall_at_k
# ---------------------------------------------------------------------------


class TestContextRecallAtK:
    # --- Empty-set convention ---

    def test_empty_expected_returns_zero(self) -> None:
        """Convention: 0.0 when expected_ids is empty (nothing to recall)."""
        assert context_recall_at_k(["a", "b"], [], k=5) == 0.0

    def test_empty_expected_and_empty_retrieved_returns_zero(self) -> None:
        assert context_recall_at_k([], [], k=5) == 0.0

    # --- No overlap ---

    def test_no_overlap_returns_zero(self) -> None:
        assert context_recall_at_k(["x", "y", "z"], ["a", "b"], k=3) == 0.0

    # --- Full overlap ---

    def test_full_overlap_all_expected_in_top_k(self) -> None:
        assert context_recall_at_k(["a", "b", "c"], ["a", "b", "c"], k=3) == 1.0

    def test_full_overlap_expected_subset_of_retrieved(self) -> None:
        assert context_recall_at_k(["a", "b", "c", "d"], ["a", "b"], k=4) == 1.0

    # --- Partial overlap ---

    def test_partial_overlap_half(self) -> None:
        # 2 of 4 expected found in top-5
        result = context_recall_at_k(["a", "b", "x", "y", "z"], ["a", "b", "c", "d"], k=5)
        assert result == pytest.approx(0.5)

    def test_partial_overlap_one_of_three(self) -> None:
        result = context_recall_at_k(["a", "x", "y"], ["a", "b", "c"], k=3)
        assert result == pytest.approx(1 / 3)

    # --- k truncation ---

    def test_k_truncates_retrieved(self) -> None:
        # expected "b" is at index 1 — visible at k=2, not at k=1
        assert context_recall_at_k(["a", "b"], ["b"], k=1) == 0.0
        assert context_recall_at_k(["a", "b"], ["b"], k=2) == 1.0

    def test_k_larger_than_retrieved_uses_full_list(self) -> None:
        # k=100 but only 3 results; "c" is included
        assert context_recall_at_k(["a", "b", "c"], ["c"], k=100) == 1.0

    def test_k_zero_returns_zero(self) -> None:
        # No results considered → intersection empty → 0/|expected|
        result = context_recall_at_k(["a", "b"], ["a"], k=0)
        assert result == 0.0

    # --- Duplicates in retrieved_ids ---

    def test_duplicates_in_retrieved_treated_as_set(self) -> None:
        # "a" appears twice in retrieved; intersection is still |{"a"}|
        result = context_recall_at_k(["a", "a", "b"], ["a", "b"], k=3)
        # |{a, a, b} ∩ {a, b}| = |{a, b}| = 2; |expected| = 2 → 1.0
        assert result == 1.0

    # --- Return type ---

    def test_returns_float(self) -> None:
        assert isinstance(context_recall_at_k(["a"], ["a"], k=1), float)


# ---------------------------------------------------------------------------
# context_precision_at_k
# ---------------------------------------------------------------------------


class TestContextPrecisionAtK:
    # --- Empty-set convention ---

    def test_empty_retrieved_returns_zero(self) -> None:
        """Convention: 0.0 when denominator is zero (no candidates in window)."""
        assert context_precision_at_k([], ["a", "b"], k=5) == 0.0

    def test_k_zero_returns_zero(self) -> None:
        """k=0 → top-k is empty → denominator 0 → 0.0."""
        assert context_precision_at_k(["a", "b"], ["a"], k=0) == 0.0

    def test_empty_expected_returns_zero(self) -> None:
        """No relevant items → 0 hits → 0.0 precision."""
        assert context_precision_at_k(["a", "b"], [], k=2) == 0.0

    # --- No overlap ---

    def test_no_overlap_returns_zero(self) -> None:
        assert context_precision_at_k(["x", "y"], ["a", "b"], k=2) == 0.0

    # --- Full overlap ---

    def test_full_overlap_all_retrieved_relevant(self) -> None:
        assert context_precision_at_k(["a", "b", "c"], ["a", "b", "c"], k=3) == 1.0

    def test_full_overlap_retrieved_subset_of_expected(self) -> None:
        # All 2 retrieved items are relevant out of 5 expected
        assert context_precision_at_k(["a", "b"], ["a", "b", "c", "d", "e"], k=2) == 1.0

    # --- Partial overlap ---

    def test_partial_overlap_half(self) -> None:
        result = context_precision_at_k(["a", "x"], ["a", "b"], k=2)
        assert result == pytest.approx(0.5)

    def test_partial_overlap_one_of_three(self) -> None:
        result = context_precision_at_k(["a", "x", "y"], ["a"], k=3)
        assert result == pytest.approx(1 / 3)

    # --- k truncation ---

    def test_k_truncates_retrieved(self) -> None:
        # "b" at index 1: not counted at k=1
        result_k1 = context_precision_at_k(["a", "b"], ["b"], k=1)
        result_k2 = context_precision_at_k(["a", "b"], ["b"], k=2)
        assert result_k1 == 0.0
        assert result_k2 == pytest.approx(0.5)

    def test_k_larger_than_retrieved_uses_full_list(self) -> None:
        # k=10 but only 3 items; denominator = 3
        result = context_precision_at_k(["a", "b", "c"], ["a", "b"], k=10)
        assert result == pytest.approx(2 / 3)

    # --- Return type ---

    def test_returns_float(self) -> None:
        assert isinstance(context_precision_at_k(["a"], ["a"], k=1), float)


# ---------------------------------------------------------------------------
# aggregate_scores
# ---------------------------------------------------------------------------


class TestAggregateScores:
    def _make_score(
        self,
        question_id: str,
        question_type: QuestionType,
        recall: float,
        precision: float,
    ) -> QuestionScore:
        return QuestionScore(
            question_id=question_id,
            question_type=question_type,
            recall=recall,
            precision=precision,
        )

    # --- Empty input ---

    def test_empty_input_returns_zero_means(self) -> None:
        summary = aggregate_scores([])
        assert summary.mean_recall == 0.0
        assert summary.mean_precision == 0.0
        assert summary.recall_by_type == {}
        assert summary.precision_by_type == {}
        assert summary.per_question == []

    def test_returns_score_summary(self) -> None:
        summary = aggregate_scores([])
        assert isinstance(summary, ScoreSummary)

    # --- Single question ---

    def test_single_question_overall_means_equal_that_question(self) -> None:
        score = self._make_score("q1", QuestionType.factual_lookup, recall=0.8, precision=0.6)
        summary = aggregate_scores([score])
        assert summary.mean_recall == pytest.approx(0.8)
        assert summary.mean_precision == pytest.approx(0.6)

    def test_single_question_per_type_matches_question(self) -> None:
        score = self._make_score("q1", QuestionType.factual_lookup, recall=0.8, precision=0.6)
        summary = aggregate_scores([score])
        assert summary.recall_by_type[QuestionType.factual_lookup] == pytest.approx(0.8)
        assert summary.precision_by_type[QuestionType.factual_lookup] == pytest.approx(0.6)

    # --- Multiple questions, single type ---

    def test_multiple_same_type_mean_computed(self) -> None:
        scores = [
            self._make_score("q1", QuestionType.factual_lookup, recall=1.0, precision=1.0),
            self._make_score("q2", QuestionType.factual_lookup, recall=0.0, precision=0.0),
        ]
        summary = aggregate_scores(scores)
        assert summary.mean_recall == pytest.approx(0.5)
        assert summary.mean_precision == pytest.approx(0.5)
        assert summary.recall_by_type[QuestionType.factual_lookup] == pytest.approx(0.5)

    # --- Multiple QuestionTypes ---

    def test_multiple_types_per_type_aggregates(self) -> None:
        scores = [
            self._make_score("q1", QuestionType.factual_lookup, recall=1.0, precision=1.0),
            self._make_score("q2", QuestionType.interpretive, recall=0.5, precision=0.5),
            self._make_score(
                "q3", QuestionType.multi_document_synthesis, recall=0.25, precision=0.75
            ),
        ]
        summary = aggregate_scores(scores)
        assert summary.recall_by_type[QuestionType.factual_lookup] == pytest.approx(1.0)
        assert summary.recall_by_type[QuestionType.interpretive] == pytest.approx(0.5)
        assert summary.recall_by_type[QuestionType.multi_document_synthesis] == pytest.approx(0.25)

    def test_multiple_types_overall_mean(self) -> None:
        scores = [
            self._make_score("q1", QuestionType.factual_lookup, recall=1.0, precision=1.0),
            self._make_score("q2", QuestionType.interpretive, recall=0.5, precision=0.0),
            self._make_score("q3", QuestionType.interpretive, recall=0.5, precision=1.0),
        ]
        summary = aggregate_scores(scores)
        assert summary.mean_recall == pytest.approx(2.0 / 3.0)
        assert summary.mean_precision == pytest.approx(2.0 / 3.0)
        assert summary.recall_by_type[QuestionType.interpretive] == pytest.approx(0.5)

    def test_missing_type_not_in_per_type_dicts(self) -> None:
        scores = [
            self._make_score("q1", QuestionType.factual_lookup, recall=1.0, precision=1.0),
        ]
        summary = aggregate_scores(scores)
        assert QuestionType.interpretive not in summary.recall_by_type
        assert QuestionType.multi_document_synthesis not in summary.precision_by_type

    # --- Stable type ordering ---

    def test_per_type_dict_ordering_is_stable(self) -> None:
        """recall_by_type and precision_by_type must be sorted by QuestionType value."""
        scores = [
            self._make_score(
                "q1", QuestionType.multi_document_synthesis, recall=0.1, precision=0.1
            ),
            self._make_score("q2", QuestionType.factual_lookup, recall=0.9, precision=0.9),
            self._make_score("q3", QuestionType.interpretive, recall=0.5, precision=0.5),
        ]
        summary = aggregate_scores(scores)
        expected_order = sorted(
            [
                QuestionType.factual_lookup,
                QuestionType.interpretive,
                QuestionType.multi_document_synthesis,
            ],
            key=lambda qt: qt.value,
        )
        assert list(summary.recall_by_type.keys()) == expected_order
        assert list(summary.precision_by_type.keys()) == expected_order

    # --- Determinism ---

    def test_same_input_produces_identical_output(self) -> None:
        scores = [
            self._make_score("q1", QuestionType.factual_lookup, recall=0.8, precision=0.7),
            self._make_score("q2", QuestionType.interpretive, recall=0.6, precision=0.4),
        ]
        summary_a = aggregate_scores(scores)
        summary_b = aggregate_scores(scores)
        assert summary_a.mean_recall == summary_b.mean_recall
        assert summary_a.mean_precision == summary_b.mean_precision
        assert summary_a.recall_by_type == summary_b.recall_by_type
        assert summary_a.precision_by_type == summary_b.precision_by_type
        assert summary_a.per_question == summary_b.per_question

    def test_per_question_preserves_input_order(self) -> None:
        scores = [
            self._make_score("q1", QuestionType.factual_lookup, recall=1.0, precision=1.0),
            self._make_score("q2", QuestionType.interpretive, recall=0.5, precision=0.5),
            self._make_score("q3", QuestionType.factual_lookup, recall=0.0, precision=0.0),
        ]
        summary = aggregate_scores(scores)
        assert [q.question_id for q in summary.per_question] == ["q1", "q2", "q3"]

    # --- QuestionScore is immutable (frozen dataclass) ---

    def test_question_score_is_frozen(self) -> None:
        score = self._make_score("q1", QuestionType.factual_lookup, recall=1.0, precision=1.0)
        with pytest.raises((AttributeError, TypeError)):
            score.recall = 0.0  # type: ignore[misc]
