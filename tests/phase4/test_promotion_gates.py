"""Tests for pre-promotion gates (M-054, D-16, D-20).

Tests:
  - test_four_gates_m054: each M-054 gate individually blocks/passes.
  - test_gates_3_4_stubbed_named: Gates 3+4 report SKIPPED status visibly.
  - test_tier_shift_warning_d20: D-20 warns, never blocks.
  - test_d16_unacked_permission_gap_fails_closed: unacknowledged gap → StageError.
  - test_d16_acked_proceeds_with_audit: acknowledged gap → proceeds with audit row.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from finecorpus.index.adapter import CollectionNotFoundError
from finecorpus.index.lifecycle import GateStatus, ValidationResult, validate_shadow
from finecorpus.pipeline.plan.stage import StageError, check_permission_gap_gate


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_adapter(count: int, raise_not_found: bool = False):
    """Create a minimal IndexAdapter mock."""
    adapter = MagicMock()
    if raise_not_found:
        adapter.count_points.side_effect = CollectionNotFoundError("shadow")
    else:
        adapter.count_points.return_value = count
    adapter.get_collection_metadata.return_value = {}
    return adapter


# ---------------------------------------------------------------------------
# test_four_gates_m054
# ---------------------------------------------------------------------------


class TestFourGatesM054:
    """M-054: each gate individually blocks or passes."""

    def test_gate1_chunk_count_tolerance_fails_when_too_low(self):
        """Gate 1 (chunk_count_tolerance): fails when count is too far below previous."""
        adapter = _make_adapter(50)  # 50 chunks
        result = validate_shadow(
            adapter=adapter,
            shadow_collection="shadow",
            expected_min_chunks=1,
            previous_chunk_count=100,  # 50 is 50% below → exceeds 20% tolerance
            chunk_count_tolerance_pct=20.0,
        )
        assert not result.passed
        assert result.gate == "chunk_count_tolerance"

    def test_gate1_chunk_count_tolerance_passes_within_range(self):
        """Gate 1: passes when count is within tolerance."""
        adapter = _make_adapter(90)  # 90/100 = 10% below → within 20% tolerance
        result = validate_shadow(
            adapter=adapter,
            shadow_collection="shadow",
            expected_min_chunks=1,
            previous_chunk_count=100,
            chunk_count_tolerance_pct=20.0,
        )
        assert result.passed
        gate_names = {g["gate"]: g["status"] for g in result.gate_results}
        assert gate_names["chunk_count_tolerance"] == str(GateStatus.PASSED)

    def test_gate1_chunk_count_tolerance_skipped_when_no_previous(self):
        """Gate 1 skips when no previous_chunk_count is provided."""
        adapter = _make_adapter(50)
        result = validate_shadow(
            adapter=adapter,
            shadow_collection="shadow",
            expected_min_chunks=1,
        )
        assert result.passed
        gate_names = {g["gate"]: g["status"] for g in result.gate_results}
        assert gate_names["chunk_count_tolerance"] == str(GateStatus.SKIPPED)

    def test_gate2_no_class_zero_regression_fails(self):
        """Gate 2 (no_class_zero_regression): fails when a class goes to zero."""
        adapter = _make_adapter(100)
        result = validate_shadow(
            adapter=adapter,
            shadow_collection="shadow",
            expected_min_chunks=1,
            previous_chunks_by_class={"prose": 50, "table": 20},
            current_chunks_by_class={"prose": 60, "table": 0},  # table → 0
        )
        assert not result.passed
        assert result.gate == "no_class_zero_regression"
        assert "table" in result.detail

    def test_gate2_no_class_zero_regression_passes(self):
        """Gate 2: passes when all previously-producing classes still produce."""
        adapter = _make_adapter(100)
        result = validate_shadow(
            adapter=adapter,
            shadow_collection="shadow",
            expected_min_chunks=1,
            previous_chunks_by_class={"prose": 50, "table": 20},
            current_chunks_by_class={"prose": 60, "table": 10},
        )
        assert result.passed
        gate_names = {g["gate"]: g["status"] for g in result.gate_results}
        assert gate_names["no_class_zero_regression"] == str(GateStatus.PASSED)

    def test_gate2_skipped_when_no_previous_classes(self):
        """Gate 2 skips when no previous_chunks_by_class provided."""
        adapter = _make_adapter(100)
        result = validate_shadow(
            adapter=adapter,
            shadow_collection="shadow",
            expected_min_chunks=1,
        )
        assert result.passed
        gate_names = {g["gate"]: g["status"] for g in result.gate_results}
        assert gate_names["no_class_zero_regression"] == str(GateStatus.SKIPPED)

    def test_collection_not_found_fails_immediately(self):
        """collection_exists gate blocks if shadow does not exist."""
        adapter = _make_adapter(0, raise_not_found=True)
        result = validate_shadow(
            adapter=adapter,
            shadow_collection="shadow",
        )
        assert not result.passed
        assert result.gate == "collection_exists"

    def test_non_empty_gate_fails_when_zero_chunks(self):
        """non_empty gate blocks when count is 0 and not declared_empty."""
        adapter = _make_adapter(0)
        result = validate_shadow(
            adapter=adapter,
            shadow_collection="shadow",
            declared_empty=False,
        )
        assert not result.passed
        assert result.gate == "non_empty"

    def test_declared_empty_passes_non_empty_gate(self):
        """declared_empty=True allows zero chunks to pass the non_empty gate."""
        adapter = _make_adapter(0)
        result = validate_shadow(
            adapter=adapter,
            shadow_collection="shadow",
            declared_empty=True,
        )
        assert result.passed


# ---------------------------------------------------------------------------
# test_gates_3_4_stubbed_named
# ---------------------------------------------------------------------------


class TestGates34Stubbed:
    """M-054 Gates 3+4 are stubbed — must report SKIPPED with phase5-eval-integration."""

    def test_gate3_eval_baseline_is_skipped(self):
        """Gate 3 (eval_baseline) reports SKIPPED with phase5-eval-integration."""
        adapter = _make_adapter(100)
        result = validate_shadow(
            adapter=adapter,
            shadow_collection="shadow",
            expected_min_chunks=1,
        )
        assert result.passed
        gate_names = {g["gate"]: g for g in result.gate_results}
        g3 = gate_names.get("eval_baseline")
        assert g3 is not None
        assert g3["status"] == str(GateStatus.SKIPPED)
        assert "phase5-eval-integration" in g3["reason"]

    def test_gate4_regression_threshold_is_skipped(self):
        """Gate 4 (regression_threshold) reports SKIPPED with phase5-eval-integration."""
        adapter = _make_adapter(100)
        result = validate_shadow(
            adapter=adapter,
            shadow_collection="shadow",
            expected_min_chunks=1,
        )
        assert result.passed
        gate_names = {g["gate"]: g for g in result.gate_results}
        g4 = gate_names.get("regression_threshold")
        assert g4 is not None
        assert g4["status"] == str(GateStatus.SKIPPED)
        assert "phase5-eval-integration" in g4["reason"]

    def test_both_stubs_visible_in_gate_results(self):
        """Both gate 3 and gate 4 are visible in gate_results."""
        adapter = _make_adapter(100)
        result = validate_shadow(
            adapter=adapter,
            shadow_collection="shadow",
            expected_min_chunks=1,
        )
        gate_names_set = {g["gate"] for g in result.gate_results}
        assert "eval_baseline" in gate_names_set
        assert "regression_threshold" in gate_names_set


# ---------------------------------------------------------------------------
# test_tier_shift_warning_d20
# ---------------------------------------------------------------------------


class TestTierShiftWarningD20:
    """D-20: non-blocking tier-distribution-shift WARNING gate."""

    def test_d20_warns_on_large_count_shift(self):
        """D-20 emits a warning when chunk count shifts by more than 30%."""
        adapter = _make_adapter(20)  # was 100, now 20 = 80% drop
        result = validate_shadow(
            adapter=adapter,
            shadow_collection="shadow",
            expected_min_chunks=1,
            previous_chunk_count=100,
            chunk_count_tolerance_pct=100.0,  # disable tolerance gate for this test
        )
        # D-20 is a WARNING — must NOT block (passed=True)
        assert result.passed, "D-20 must never block promotion"
        assert len(result.warnings) > 0, "D-20 must emit a warning"
        assert any("D-20" in w for w in result.warnings)

    def test_d20_no_warning_on_small_shift(self):
        """D-20 does not warn when shift is small."""
        adapter = _make_adapter(95)  # 5% drop from 100
        result = validate_shadow(
            adapter=adapter,
            shadow_collection="shadow",
            expected_min_chunks=1,
            previous_chunk_count=100,
            chunk_count_tolerance_pct=100.0,
        )
        assert result.passed
        # No D-20 warning for small shift
        d20_warnings = [w for w in result.warnings if "D-20" in w]
        assert len(d20_warnings) == 0

    def test_d20_skipped_when_no_previous(self):
        """D-20 gate skips when no previous_chunk_count is available."""
        adapter = _make_adapter(100)
        result = validate_shadow(
            adapter=adapter,
            shadow_collection="shadow",
            expected_min_chunks=1,
        )
        gate_names = {g["gate"]: g for g in result.gate_results}
        d20 = gate_names.get("tier_shift_warning")
        assert d20 is not None
        assert d20["status"] == str(GateStatus.SKIPPED)

    def test_d20_warning_gate_result_visible(self):
        """D-20 warning appears in gate_results with WARNING status."""
        adapter = _make_adapter(10)  # 90% drop → warning
        result = validate_shadow(
            adapter=adapter,
            shadow_collection="shadow",
            expected_min_chunks=1,
            previous_chunk_count=100,
            chunk_count_tolerance_pct=100.0,  # disable tolerance gate
        )
        gate_names = {g["gate"]: g for g in result.gate_results}
        d20 = gate_names.get("tier_shift_warning")
        assert d20 is not None
        assert d20["status"] == str(GateStatus.WARNING)


# ---------------------------------------------------------------------------
# test_d16_unacked_permission_gap_fails_closed
# ---------------------------------------------------------------------------


class TestD16PermissionGate:
    """D-16: fail-closed permission gap gate."""

    def _make_segment_sets(self, fidelities):
        """Create segment_sets list with given permission_fidelity values."""
        sets = []
        for i, fidelity in enumerate(fidelities):
            sets.append({
                "document_id": f"doc-{i}",
                "tenancy": {"permission_fidelity": fidelity},
            })
        return sets

    def test_d16_unacked_fails_closed(self):
        """Unacknowledged permission_fidelity=unavailable → StageError (D-16)."""
        segment_sets = self._make_segment_sets(["unavailable", "authoritative"])
        with pytest.raises(StageError, match="D-16 fail-closed"):
            check_permission_gap_gate(
                segment_sets,
                acknowledged_permission_gap=False,
            )

    def test_d16_unacked_names_documents_in_error(self):
        """Error message names the offending document IDs."""
        segment_sets = self._make_segment_sets(["unavailable"])
        segment_sets[0]["document_id"] = "my-sensitive-doc"
        with pytest.raises(StageError, match="my-sensitive-doc"):
            check_permission_gap_gate(
                segment_sets,
                acknowledged_permission_gap=False,
            )

    def test_d16_acked_proceeds(self):
        """acknowledged_permission_gap=True → no exception."""
        segment_sets = self._make_segment_sets(["unavailable"])
        # Should not raise
        check_permission_gap_gate(
            segment_sets,
            acknowledged_permission_gap=True,
        )

    def test_d16_acked_emits_audit_row_when_session_available(self):
        """Acknowledged gap emits an audit row when session is provided."""
        from sqlalchemy import create_engine, select
        from sqlalchemy.orm import Session as OrmSession

        from finecorpus.control.audit import AuditAction, AuditLogRecord
        from finecorpus.control.metadata import create_tables

        engine = create_engine("sqlite:///:memory:")
        create_tables(engine)
        with OrmSession(engine) as session:
            segment_sets = self._make_segment_sets(["unavailable"])
            # Should not raise and should write audit row
            check_permission_gap_gate(
                segment_sets,
                acknowledged_permission_gap=True,
                session=session,
            )
            rows = list(session.execute(select(AuditLogRecord)).scalars())
            assert any(r.entry_type == str(AuditAction.permission_gap_ack) for r in rows)

    def test_d16_authoritative_passes_without_ack(self):
        """authoritative fidelity → no gate failure (no unavailable)."""
        segment_sets = self._make_segment_sets(["authoritative", "best_effort"])
        check_permission_gap_gate(
            segment_sets,
            acknowledged_permission_gap=False,
        )  # Should not raise

    def test_d16_no_segment_sets_passes(self):
        """Empty segment_sets list → no gate failure."""
        check_permission_gap_gate(
            [],
            acknowledged_permission_gap=False,
        )  # Should not raise
