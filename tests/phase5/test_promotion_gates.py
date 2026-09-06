"""Phase 5 tests for pre-promotion gates 3 and 4 (M-054, eval_baseline, regression_threshold).

Tests:
  - test_gate_eval_baseline_skipped_when_no_eval_configured: SKIPPED visible, not FAIL.
  - test_gate_eval_baseline_passes_when_score_provided: PASSED with injected score.
  - test_gate_eval_baseline_fails_when_scoring_errors: FAILED when eval configured but no score.
  - test_gate_regression_threshold_blocks: shadow score below baseline beyond threshold → FAILED.
  - test_gate_passes_within_threshold: regression within tolerance → PASSED.
  - test_gate_regression_threshold_skipped_when_no_baseline: SKIPPED when no live baseline.
  - test_gate_regression_threshold_skipped_when_no_shadow_score: SKIPPED when no shadow score.
  - test_existing_gates_unchanged: phase-4 gates still pass/fail correctly with new params.
  - test_both_gates_visible_in_gate_results: gates 3+4 always appear in gate_results.

Also verifies backward-compat: validate_shadow called without new params behaves as before.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from finecorpus.index.adapter import CollectionNotFoundError
from finecorpus.index.lifecycle import GateStatus, validate_shadow

# ---------------------------------------------------------------------------
# Helpers (mirrors phase4/test_promotion_gates.py)
# ---------------------------------------------------------------------------


def _make_adapter(count: int, raise_not_found: bool = False):
    adapter = MagicMock()
    if raise_not_found:
        adapter.count_points.side_effect = CollectionNotFoundError("shadow")
    else:
        adapter.count_points.return_value = count
    adapter.get_collection_metadata.return_value = {}
    return adapter


# ---------------------------------------------------------------------------
# Gate 3: eval_baseline
# ---------------------------------------------------------------------------


class TestGate3EvalBaseline:
    """M-054 Gate 3: eval_baseline gate semantics."""

    def test_gate_eval_baseline_skipped_when_no_eval_configured(self):
        """SKIPPED (not FAIL) when eval is not configured for the KB.

        eval_configured defaults to False → gate is SKIPPED.
        The gate must be visible in gate_results and SKIPPED, not FAILED.
        """
        adapter = _make_adapter(100)
        result = validate_shadow(
            adapter=adapter,
            shadow_collection="shadow",
            expected_min_chunks=1,
            # eval_configured not supplied → defaults to False
        )
        assert result.passed, "validation should pass when eval not configured"
        gate_map = {g["gate"]: g for g in result.gate_results}
        assert "eval_baseline" in gate_map
        assert gate_map["eval_baseline"]["status"] == str(GateStatus.SKIPPED)
        assert (
            "not configured" in gate_map["eval_baseline"]["reason"].lower()
            or "no eval set" in gate_map["eval_baseline"]["reason"].lower()
        )

    def test_gate_eval_baseline_passes_when_score_provided(self):
        """PASSED when shadow_eval_score is injected and eval_configured=True."""
        adapter = _make_adapter(100)
        result = validate_shadow(
            adapter=adapter,
            shadow_collection="shadow",
            expected_min_chunks=1,
            shadow_eval_score=0.85,
            eval_configured=True,
        )
        assert result.passed
        gate_map = {g["gate"]: g for g in result.gate_results}
        assert gate_map["eval_baseline"]["status"] == str(GateStatus.PASSED)
        assert "0.8500" in gate_map["eval_baseline"]["reason"]

    def test_gate_eval_baseline_fails_when_scoring_errors(self):
        """FAILED when eval_configured=True but shadow_eval_score is None.

        This happens when scoring failed (or was never run before promotion).
        """
        adapter = _make_adapter(100)
        result = validate_shadow(
            adapter=adapter,
            shadow_collection="shadow",
            expected_min_chunks=1,
            eval_configured=True,
            shadow_eval_score=None,
            scoring_error="provider timeout",
        )
        assert not result.passed
        assert result.gate == "eval_baseline"
        assert "provider timeout" in result.detail

    def test_gate_eval_baseline_fails_without_error_message(self):
        """FAILED when eval_configured=True and no score, even without scoring_error."""
        adapter = _make_adapter(100)
        result = validate_shadow(
            adapter=adapter,
            shadow_collection="shadow",
            expected_min_chunks=1,
            eval_configured=True,
            shadow_eval_score=None,
        )
        assert not result.passed
        assert result.gate == "eval_baseline"

    def test_gate_eval_baseline_visible_in_gate_results(self):
        """eval_baseline gate always appears in gate_results (SKIPPED when not configured)."""
        adapter = _make_adapter(100)
        result = validate_shadow(
            adapter=adapter,
            shadow_collection="shadow",
            expected_min_chunks=1,
        )
        gate_names = {g["gate"] for g in result.gate_results}
        assert "eval_baseline" in gate_names


# ---------------------------------------------------------------------------
# Gate 4: regression_threshold
# ---------------------------------------------------------------------------


class TestGate4RegressionThreshold:
    """M-054 Gate 4: regression_threshold gate semantics (§10.4)."""

    def test_gate_regression_threshold_blocks(self):
        """Promotion blocked when shadow score is below live baseline beyond threshold.

        live=0.80, shadow=0.70, threshold=0.05 → regression=0.10 > 0.05 → FAILED.
        """
        adapter = _make_adapter(100)
        result = validate_shadow(
            adapter=adapter,
            shadow_collection="shadow",
            expected_min_chunks=1,
            shadow_eval_score=0.70,
            live_baseline_score=0.80,
            eval_regression_threshold=0.05,
            eval_configured=True,
        )
        assert not result.passed
        assert result.gate == "regression_threshold"
        assert "0.1000" in result.detail or "0.10" in result.detail

    def test_gate_passes_within_threshold(self):
        """PASSED when regression is within the threshold.

        live=0.80, shadow=0.77, threshold=0.05 → regression=0.03 ≤ 0.05 → PASSED.
        """
        adapter = _make_adapter(100)
        result = validate_shadow(
            adapter=adapter,
            shadow_collection="shadow",
            expected_min_chunks=1,
            shadow_eval_score=0.77,
            live_baseline_score=0.80,
            eval_regression_threshold=0.05,
            eval_configured=True,
        )
        assert result.passed
        gate_map = {g["gate"]: g for g in result.gate_results}
        assert gate_map["regression_threshold"]["status"] == str(GateStatus.PASSED)

    def test_gate_passes_when_shadow_better_than_baseline(self):
        """PASSED when shadow is BETTER than the live baseline (improvement)."""
        adapter = _make_adapter(100)
        result = validate_shadow(
            adapter=adapter,
            shadow_collection="shadow",
            expected_min_chunks=1,
            shadow_eval_score=0.90,
            live_baseline_score=0.80,
            eval_regression_threshold=0.05,
            eval_configured=True,
        )
        assert result.passed
        gate_map = {g["gate"]: g for g in result.gate_results}
        assert gate_map["regression_threshold"]["status"] == str(GateStatus.PASSED)

    def test_gate_regression_threshold_skipped_when_no_baseline(self):
        """SKIPPED when live_baseline_score is None (no baseline exists yet)."""
        adapter = _make_adapter(100)
        result = validate_shadow(
            adapter=adapter,
            shadow_collection="shadow",
            expected_min_chunks=1,
            shadow_eval_score=0.85,
            live_baseline_score=None,
            eval_regression_threshold=0.05,
            eval_configured=True,
        )
        assert result.passed
        gate_map = {g["gate"]: g for g in result.gate_results}
        assert gate_map["regression_threshold"]["status"] == str(GateStatus.SKIPPED)
        assert "no live baseline" in gate_map["regression_threshold"]["reason"].lower()

    def test_gate_regression_threshold_skipped_when_no_shadow_score(self):
        """SKIPPED when shadow_eval_score is None and eval not configured.

        (When eval_configured=False, Gate 3 is SKIPPED and Gate 4 also SKIPPED.)
        """
        adapter = _make_adapter(100)
        result = validate_shadow(
            adapter=adapter,
            shadow_collection="shadow",
            expected_min_chunks=1,
            # No shadow_eval_score, no eval_configured
            live_baseline_score=0.80,
            eval_regression_threshold=0.05,
        )
        assert result.passed
        gate_map = {g["gate"]: g for g in result.gate_results}
        assert gate_map["regression_threshold"]["status"] == str(GateStatus.SKIPPED)

    def test_gate_regression_threshold_visible_in_gate_results(self):
        """regression_threshold gate always appears in gate_results."""
        adapter = _make_adapter(100)
        result = validate_shadow(
            adapter=adapter,
            shadow_collection="shadow",
            expected_min_chunks=1,
        )
        gate_names = {g["gate"] for g in result.gate_results}
        assert "regression_threshold" in gate_names

    def test_gate_regression_default_threshold_used_when_not_specified(self):
        """Default threshold (0.05) is used when eval_regression_threshold is None.

        live=0.80, shadow=0.74, default_threshold=0.05 → regression=0.06 > 0.05 → FAILED.
        """
        adapter = _make_adapter(100)
        result = validate_shadow(
            adapter=adapter,
            shadow_collection="shadow",
            expected_min_chunks=1,
            shadow_eval_score=0.74,
            live_baseline_score=0.80,
            eval_configured=True,
            # eval_regression_threshold not supplied → defaults to 0.05
        )
        assert not result.passed
        assert result.gate == "regression_threshold"

    def test_regression_below_threshold_passes(self):
        """Threshold check uses strict greater-than (>): regression < threshold → PASSED.

        The gate FAILS only when regression STRICTLY EXCEEDS the threshold.
        Use a value that is unambiguously within threshold (0.04 < 0.05 → PASSED).

        IEEE-754 hazard note — exact-threshold-apart scores are float-hazardous:
            (0.80 - 0.75) evaluates to 0.050000000000000044 in IEEE-754, which
            is STRICTLY GREATER THAN 0.05 and would therefore BLOCK promotion.
        Callers must keep headroom (e.g. shadow_score = baseline - threshold + epsilon)
        rather than relying on exact floating-point equality at the boundary.
        The strict ``>`` check is correct per spec (§10.4) and is kept as-is.
        """
        adapter = _make_adapter(100)
        result = validate_shadow(
            adapter=adapter,
            shadow_collection="shadow",
            expected_min_chunks=1,
            shadow_eval_score=0.76,
            live_baseline_score=0.80,
            eval_regression_threshold=0.05,
            eval_configured=True,
        )
        # 0.80 - 0.76 = 0.04, which is NOT > 0.05 → PASSED
        assert result.passed
        gate_map = {g["gate"]: g for g in result.gate_results}
        assert gate_map["regression_threshold"]["status"] == str(GateStatus.PASSED)

    def test_ieee754_hazard_exact_threshold_apart_blocks(self):
        """IEEE-754 hazard: (0.80 - 0.75) == 0.050000000000000044 > 0.05 → BLOCKS.

        This documents the float-hazardous exact-threshold boundary case:
        scores that are EXACTLY threshold apart in real arithmetic may still
        trigger the gate due to IEEE-754 floating-point representation.

        Callers MUST keep headroom between shadow_score and baseline rather than
        relying on exact-threshold equality.  The strict ``>`` check is correct
        per spec (§10.4) — this test simply makes the hazard observable.
        """
        adapter = _make_adapter(100)
        result = validate_shadow(
            adapter=adapter,
            shadow_collection="shadow",
            expected_min_chunks=1,
            shadow_eval_score=0.75,
            live_baseline_score=0.80,
            eval_regression_threshold=0.05,
            eval_configured=True,
        )
        # In IEEE-754: 0.80 - 0.75 = 0.050000000000000044, which IS > 0.05.
        # The gate correctly BLOCKS promotion even though the intended regression
        # is exactly at the threshold. Callers must keep headroom to avoid this.
        assert not result.passed
        assert result.gate == "regression_threshold"


# ---------------------------------------------------------------------------
# Backward compatibility and both gates visible
# ---------------------------------------------------------------------------


class TestBackwardCompatAndVisibility:
    """Verify that new params don't break existing callers and both gates are visible."""

    def test_both_gates_visible_in_gate_results(self):
        """Both eval_baseline and regression_threshold appear in gate_results."""
        adapter = _make_adapter(100)
        result = validate_shadow(
            adapter=adapter,
            shadow_collection="shadow",
            expected_min_chunks=1,
        )
        gate_names = {g["gate"] for g in result.gate_results}
        assert "eval_baseline" in gate_names
        assert "regression_threshold" in gate_names

    def test_backward_compat_no_new_params(self):
        """Calling validate_shadow without new params behaves like Phase 4 (gates SKIPPED)."""
        adapter = _make_adapter(100)
        result = validate_shadow(
            adapter=adapter,
            shadow_collection="shadow",
            expected_min_chunks=1,
        )
        assert result.passed
        gate_map = {g["gate"]: g for g in result.gate_results}
        # Both gates SKIPPED when no eval info provided
        assert gate_map["eval_baseline"]["status"] == str(GateStatus.SKIPPED)
        assert gate_map["regression_threshold"]["status"] == str(GateStatus.SKIPPED)

    def test_existing_gates_unchanged(self):
        """Phase-4 gates (Gate 1+2) still work correctly with new params added."""
        adapter = _make_adapter(50)
        result = validate_shadow(
            adapter=adapter,
            shadow_collection="shadow",
            expected_min_chunks=1,
            previous_chunk_count=100,
            chunk_count_tolerance_pct=20.0,
            # New params present but neutral
            eval_configured=False,
        )
        # Gate 1 should fail (50/100 = 50% below, > 20% tolerance)
        assert not result.passed
        assert result.gate == "chunk_count_tolerance"

    def test_gate2_still_blocks_with_new_params(self):
        """Gate 2 (class regression) still blocks when class goes to zero."""
        adapter = _make_adapter(100)
        result = validate_shadow(
            adapter=adapter,
            shadow_collection="shadow",
            expected_min_chunks=1,
            previous_chunks_by_class={"prose": 50, "table": 20},
            current_chunks_by_class={"prose": 60, "table": 0},
            # New eval params present but neutral
            shadow_eval_score=0.90,
            eval_configured=True,
            eval_regression_threshold=0.05,
        )
        assert not result.passed
        assert result.gate == "no_class_zero_regression"

    def test_all_gates_pass_with_full_eval_context(self):
        """When all gates pass including eval gates, result.passed=True."""
        adapter = _make_adapter(100)
        result = validate_shadow(
            adapter=adapter,
            shadow_collection="shadow",
            expected_min_chunks=1,
            previous_chunk_count=100,
            chunk_count_tolerance_pct=20.0,
            previous_chunks_by_class={"prose": 50},
            current_chunks_by_class={"prose": 55},
            shadow_eval_score=0.85,
            live_baseline_score=0.82,
            eval_regression_threshold=0.05,
            eval_configured=True,
        )
        assert result.passed
        gate_map = {g["gate"]: g for g in result.gate_results}
        assert gate_map["eval_baseline"]["status"] == str(GateStatus.PASSED)
        assert gate_map["regression_threshold"]["status"] == str(GateStatus.PASSED)
