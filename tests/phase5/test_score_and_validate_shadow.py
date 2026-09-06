"""Tests for services/promotion.py::score_and_validate_shadow.

Acceptance-critical characterization tests proving the M-054/§10.4 guard:
  failed gate blocks promotion — promote() is NOT called when validation fails.

Cases:
  (a) Gate 4 regression beyond threshold → result.passed False AND promote() NOT called.
  (b) Gate 3 fail (eval_configured, scoring raises → scoring_error set,
      shadow_eval_score None) → promote() NOT called.
  (c) do_promote=True + all gates pass → promote() IS called exactly once.
  (d) do_promote=False → promote() never called regardless of gate outcome.
  (e) No live baseline (get_current returns None) → Gate 4 SKIPPED, promotion
      proceeds if other gates pass.

Tests (a) and (b) explicitly assert promote was NOT called.

Spec references: M-054, §10.4, §9.4.
"""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from finecorpus.contracts.eval_set import (
    ConfidenceLevel,
    EvalQuestion,
    GenerationMethod,
    QuestionType,
    ReviewStatus,
)
from finecorpus.index.adapter import IndexAdapter, ModelIdentity, alias_name, collection_name
from finecorpus.index.lifecycle import (
    BuildContext,
    BuildState,
    GateStatus,
)
from finecorpus.services.eval_scoring import ScoreResult
from finecorpus.services.promotion import score_and_validate_shadow

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

KB_ID = "kb-promo-test"
BUILD_ID = 42
SHADOW_COLL = collection_name(KB_ID, BUILD_ID)
ALIAS = alias_name(KB_ID)
MODEL_ID = "fake-embed-v1"
DIMENSIONS = 64

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ctx() -> BuildContext:
    """Build a minimal BuildContext for test use."""
    return BuildContext(
        kb_id=KB_ID,
        workspace_id="ws-test",
        build_id=BUILD_ID,
        shadow_collection=SHADOW_COLL,
        alias=ALIAS,
        model_identity=ModelIdentity(
            provider="fake",
            model=MODEL_ID,
            dimensions=DIMENSIONS,
            config_version="cfgv1",
        ),
        state=BuildState.VALIDATING,
    )


def _make_adapter(point_count: int = 100) -> MagicMock:
    """Build a minimal adapter mock that passes collection_exists and count_points gates."""
    adapter = MagicMock(spec=IndexAdapter)
    adapter.collection_exists.return_value = True
    adapter.count_points.return_value = point_count
    adapter.get_collection_metadata.return_value = {}
    return adapter


def _make_question(question_id: str = "q1") -> EvalQuestion:
    """Build a minimal reviewed EvalQuestion."""
    return EvalQuestion(
        question_id=question_id,
        text="What is the primary content?",
        generation_method=GenerationMethod.generated_factual,
        review_status=ReviewStatus.reviewed_kept,
        source_segment_ids=[],
        source_unknown=False,
        question_type=QuestionType.factual_lookup,
        expected_segment_ids=["chk_a"],
    )


def _make_score_result(recall: float = 0.85) -> ScoreResult:
    """Build a ScoreResult with the given recall."""
    from finecorpus.pipeline.evaluation.metrics import ScoreSummary

    summary = ScoreSummary(
        mean_recall=recall,
        mean_precision=recall,
        recall_by_type={},
        precision_by_type={},
    )
    return ScoreResult(
        summary=summary,
        confidence_level=ConfidenceLevel.reviewed,
        k=10,
        n_scored=1,
        n_total=1,
    )


def _make_baseline_record(recall: float) -> MagicMock:
    """Build a fake baseline record with the given recall."""
    record = MagicMock()
    record.recall = recall
    return record


@contextmanager
def _patch_promotion_internals(
    *,
    score_result: ScoreResult | None = None,
    score_raises: Exception | None = None,
    baseline_recall: float | None = None,
    baseline_raises: Exception | None = None,
):
    """Context manager that patches score_eval_set, EvalBaselineRepository,
    and promote within the finecorpus.services.promotion module.

    Yields a dict with keys:
        mock_score_eval_set: Mock for score_eval_set.
        mock_promote: Mock for promote.
        mock_baseline_repo: Mock EvalBaselineRepository instance.
    """
    fake_session = MagicMock()

    with (
        patch("finecorpus.services.promotion.score_eval_set") as mock_score,
        patch("finecorpus.services.promotion.EvalBaselineRepository") as MockBaselineRepo,
        patch("finecorpus.services.promotion.promote") as mock_promote,
    ):
        # Configure score_eval_set mock.
        if score_raises is not None:
            mock_score.side_effect = score_raises
        elif score_result is not None:
            mock_score.return_value = score_result
        else:
            mock_score.return_value = _make_score_result(recall=0.85)

        # Configure EvalBaselineRepository mock.
        mock_repo_instance = MagicMock()
        MockBaselineRepo.return_value = mock_repo_instance
        if baseline_raises is not None:
            mock_repo_instance.get_current.side_effect = baseline_raises
        elif baseline_recall is not None:
            mock_repo_instance.get_current.return_value = _make_baseline_record(baseline_recall)
        else:
            mock_repo_instance.get_current.return_value = None  # no baseline yet

        yield {
            "mock_score_eval_set": mock_score,
            "mock_promote": mock_promote,
            "mock_baseline_repo": mock_repo_instance,
            "fake_session": fake_session,
        }


# ---------------------------------------------------------------------------
# Case (a): Gate 4 regression beyond threshold → passed=False, promote NOT called
# ---------------------------------------------------------------------------


class TestGate4RegressionBlocksPromotion:
    """Case (a): Gate 4 blocks promotion when regression exceeds threshold."""

    def test_gate4_regression_blocks_promote_not_called(self):
        """Gate 4 regression beyond threshold → result.passed False AND promote() NOT called.

        shadow_recall=0.70, live_baseline=0.80, threshold=0.05
        → regression=0.10 > 0.05 → FAILED → promote() must NOT be called.

        This is acceptance-critical: the M-054/§10.4 guard MUST refuse promote()
        when any gate fails, regardless of do_promote flag.
        """
        ctx = _make_ctx()
        adapter = _make_adapter(point_count=100)

        with _patch_promotion_internals(
            score_result=_make_score_result(recall=0.70),
            baseline_recall=0.80,
        ) as mocks:
            result = score_and_validate_shadow(
                ctx=ctx,
                adapter=adapter,
                session=mocks["fake_session"],
                eval_set_questions=[_make_question()],
                provider=MagicMock(),
                k=10,
                eval_regression_threshold=0.05,
                do_promote=True,
            )

        # Gate 4 must have blocked: validation did not pass.
        assert not result.passed, (
            "expected result.passed=False when regression (0.10) exceeds threshold (0.05)"
        )

        # CRITICAL: promote() must NOT have been called.
        mocks["mock_promote"].assert_not_called()

    def test_gate4_regression_result_gate_is_regression_threshold(self):
        """Gate name in failed result is 'regression_threshold' when Gate 4 blocks."""
        ctx = _make_ctx()
        adapter = _make_adapter(point_count=100)

        with _patch_promotion_internals(
            score_result=_make_score_result(recall=0.70),
            baseline_recall=0.80,
        ) as mocks:
            result = score_and_validate_shadow(
                ctx=ctx,
                adapter=adapter,
                session=mocks["fake_session"],
                eval_set_questions=[_make_question()],
                provider=MagicMock(),
                k=10,
                eval_regression_threshold=0.05,
                do_promote=True,
            )

        assert result.gate == "regression_threshold"
        mocks["mock_promote"].assert_not_called()


# ---------------------------------------------------------------------------
# Case (b): Gate 3 fail (scoring raises) → scoring_error set, promote NOT called
# ---------------------------------------------------------------------------


class TestGate3ScoringFailureBlocksPromotion:
    """Case (b): scoring raises → Gate 3 fails → promote() NOT called."""

    def test_gate3_scoring_error_blocks_promote_not_called(self):
        """eval_configured=True + scoring raises → Gate 3 FAILED → promote() NOT called.

        When score_eval_set raises an exception (provider timeout, network error, etc.),
        the scoring_error is captured, shadow_eval_score is None, and validate_shadow
        sees eval_configured=True + shadow_eval_score=None → Gate 3 FAIL.
        The M-054 guard must refuse promote().

        This is acceptance-critical: the fail-closed path must block promotion.
        promote() must NOT be called.
        """
        ctx = _make_ctx()
        adapter = _make_adapter(point_count=100)

        with _patch_promotion_internals(
            score_raises=RuntimeError("provider timeout"),
            baseline_recall=None,
        ) as mocks:
            result = score_and_validate_shadow(
                ctx=ctx,
                adapter=adapter,
                session=mocks["fake_session"],
                eval_set_questions=[_make_question()],
                provider=MagicMock(),
                k=10,
                do_promote=True,
            )

        # Gate 3 must have failed: eval configured but score unavailable.
        assert not result.passed, (
            "expected result.passed=False when eval is configured but scoring failed"
        )
        assert result.gate == "eval_baseline", (
            f"expected failing gate 'eval_baseline', got '{result.gate}'"
        )

        # CRITICAL: promote() must NOT have been called.
        mocks["mock_promote"].assert_not_called()

    def test_gate3_scoring_error_appears_in_detail(self):
        """The scoring error message must appear in result.detail for operator visibility."""
        ctx = _make_ctx()
        adapter = _make_adapter(point_count=100)

        with _patch_promotion_internals(
            score_raises=RuntimeError("provider timeout"),
            baseline_recall=None,
        ) as mocks:
            result = score_and_validate_shadow(
                ctx=ctx,
                adapter=adapter,
                session=mocks["fake_session"],
                eval_set_questions=[_make_question()],
                provider=MagicMock(),
                k=10,
                do_promote=True,
            )

        assert "provider timeout" in result.detail
        mocks["mock_promote"].assert_not_called()


# ---------------------------------------------------------------------------
# Case (c): do_promote=True + all gates pass → promote() IS called exactly once
# ---------------------------------------------------------------------------


class TestAllGatesPassPromoteCalled:
    """Case (c): all gates pass + do_promote=True → promote() called exactly once."""

    def test_promote_called_once_when_all_gates_pass(self):
        """When all gates pass and do_promote=True, promote() is called exactly once."""
        ctx = _make_ctx()
        adapter = _make_adapter(point_count=100)

        with _patch_promotion_internals(
            score_result=_make_score_result(recall=0.90),
            baseline_recall=0.80,  # regression = 0.0 → PASSED (improvement)
        ) as mocks:
            result = score_and_validate_shadow(
                ctx=ctx,
                adapter=adapter,
                session=mocks["fake_session"],
                eval_set_questions=[_make_question()],
                provider=MagicMock(),
                k=10,
                eval_regression_threshold=0.05,
                do_promote=True,
            )

        assert result.passed, f"expected result.passed=True, got gate={result.gate!r}"
        mocks["mock_promote"].assert_called_once()

    def test_promote_called_once_no_eval_configured(self):
        """promote() is called once when eval is not configured and all structural gates pass."""
        ctx = _make_ctx()
        adapter = _make_adapter(point_count=100)

        with _patch_promotion_internals(baseline_recall=None) as mocks:
            result = score_and_validate_shadow(
                ctx=ctx,
                adapter=adapter,
                session=mocks["fake_session"],
                eval_set_questions=None,
                provider=None,
                k=10,
                do_promote=True,
            )

        assert result.passed
        mocks["mock_promote"].assert_called_once()

    def test_promote_receives_correct_ctx(self):
        """promote() is called with the ctx from score_and_validate_shadow."""
        ctx = _make_ctx()
        adapter = _make_adapter(point_count=100)

        with _patch_promotion_internals(
            score_result=_make_score_result(recall=0.85),
            baseline_recall=None,
        ) as mocks:
            score_and_validate_shadow(
                ctx=ctx,
                adapter=adapter,
                session=mocks["fake_session"],
                eval_set_questions=[_make_question()],
                provider=MagicMock(),
                k=10,
                do_promote=True,
            )

        call_kwargs = mocks["mock_promote"].call_args
        assert call_kwargs.kwargs["ctx"] is ctx


# ---------------------------------------------------------------------------
# Case (d): do_promote=False → promote() never called regardless of gates
# ---------------------------------------------------------------------------


class TestDoPromoteFalse:
    """Case (d): do_promote=False → promote() never called regardless of gate outcome."""

    def test_promote_not_called_when_do_promote_false_and_gates_pass(self):
        """promote() is NOT called when do_promote=False even if all gates pass."""
        ctx = _make_ctx()
        adapter = _make_adapter(point_count=100)

        with _patch_promotion_internals(
            score_result=_make_score_result(recall=0.90),
            baseline_recall=0.80,
        ) as mocks:
            result = score_and_validate_shadow(
                ctx=ctx,
                adapter=adapter,
                session=mocks["fake_session"],
                eval_set_questions=[_make_question()],
                provider=MagicMock(),
                k=10,
                eval_regression_threshold=0.05,
                do_promote=False,  # key: False
            )

        assert result.passed  # gates passed
        mocks["mock_promote"].assert_not_called()

    def test_promote_not_called_when_do_promote_false_and_gate_fails(self):
        """promote() is NOT called when do_promote=False and gates fail."""
        ctx = _make_ctx()
        adapter = _make_adapter(point_count=100)

        with _patch_promotion_internals(
            score_result=_make_score_result(recall=0.70),
            baseline_recall=0.80,
        ) as mocks:
            result = score_and_validate_shadow(
                ctx=ctx,
                adapter=adapter,
                session=mocks["fake_session"],
                eval_set_questions=[_make_question()],
                provider=MagicMock(),
                k=10,
                eval_regression_threshold=0.05,
                do_promote=False,
            )

        assert not result.passed
        mocks["mock_promote"].assert_not_called()

    def test_do_promote_false_is_default(self):
        """do_promote defaults to False — promotion must not occur without explicit opt-in."""
        ctx = _make_ctx()
        adapter = _make_adapter(point_count=100)

        with _patch_promotion_internals(
            score_result=_make_score_result(recall=0.85),
            baseline_recall=None,
        ) as mocks:
            score_and_validate_shadow(
                ctx=ctx,
                adapter=adapter,
                session=mocks["fake_session"],
                eval_set_questions=[_make_question()],
                provider=MagicMock(),
                k=10,
                # do_promote not supplied → defaults to False
            )

        mocks["mock_promote"].assert_not_called()


# ---------------------------------------------------------------------------
# Case (e): No live baseline → Gate 4 SKIPPED, promotion proceeds if other gates pass
# ---------------------------------------------------------------------------


class TestNoLiveBaselineGate4Skipped:
    """Case (e): no live baseline → Gate 4 SKIPPED, other passing gates allow promotion."""

    def test_gate4_skipped_when_no_baseline_promotion_proceeds(self):
        """When get_current returns None, Gate 4 is SKIPPED and promotion proceeds.

        This is the first-promotion case: no historical baseline exists yet.
        The regression gate must be SKIPPED (not FAILED) — blocking promotion
        when no comparison is available would be incorrect (§10.4 semantics).
        """
        ctx = _make_ctx()
        adapter = _make_adapter(point_count=100)

        with _patch_promotion_internals(
            score_result=_make_score_result(recall=0.85),
            baseline_recall=None,  # → get_current returns None
        ) as mocks:
            result = score_and_validate_shadow(
                ctx=ctx,
                adapter=adapter,
                session=mocks["fake_session"],
                eval_set_questions=[_make_question()],
                provider=MagicMock(),
                k=10,
                eval_regression_threshold=0.05,
                do_promote=True,
            )

        assert result.passed, (
            f"expected result.passed=True when no baseline exists; gate={result.gate!r}"
        )

        gate_map = {g["gate"]: g for g in result.gate_results}
        assert gate_map["regression_threshold"]["status"] == str(GateStatus.SKIPPED), (
            "Gate 4 must be SKIPPED when no live baseline exists"
        )
        mocks["mock_promote"].assert_called_once()

    def test_gate4_skipped_when_baseline_fetch_raises(self):
        """When baseline fetch raises, Gate 4 is SKIPPED (treated as no baseline).

        A transient DB error during baseline fetch must not block promotion
        (the baseline fetch is best-effort; Gate 4 requires both scores to run).
        """
        ctx = _make_ctx()
        adapter = _make_adapter(point_count=100)

        with _patch_promotion_internals(
            score_result=_make_score_result(recall=0.85),
            baseline_raises=Exception("db connection lost"),
        ) as mocks:
            result = score_and_validate_shadow(
                ctx=ctx,
                adapter=adapter,
                session=mocks["fake_session"],
                eval_set_questions=[_make_question()],
                provider=MagicMock(),
                k=10,
                eval_regression_threshold=0.05,
                do_promote=True,
            )

        # With no live_baseline_score, Gate 4 is SKIPPED (not FAILED).
        gate_map = {g["gate"]: g for g in result.gate_results}
        assert gate_map["regression_threshold"]["status"] == str(GateStatus.SKIPPED)
        assert result.passed
        mocks["mock_promote"].assert_called_once()

    def test_gate4_skipped_no_eval_configured_no_baseline(self):
        """When no eval configured AND no baseline, both Gate 3 and Gate 4 are SKIPPED."""
        ctx = _make_ctx()
        adapter = _make_adapter(point_count=100)

        with _patch_promotion_internals(baseline_recall=None) as mocks:
            result = score_and_validate_shadow(
                ctx=ctx,
                adapter=adapter,
                session=mocks["fake_session"],
                eval_set_questions=None,
                provider=None,
                k=10,
                do_promote=True,
            )

        assert result.passed
        gate_map = {g["gate"]: g for g in result.gate_results}
        assert gate_map["eval_baseline"]["status"] == str(GateStatus.SKIPPED)
        assert gate_map["regression_threshold"]["status"] == str(GateStatus.SKIPPED)
        mocks["mock_promote"].assert_called_once()
