"""Phase 5 enum tests.

Covers:
- AuditAction.drift_detected present with value "drift_detected"
- JobType.eval_sweep present with value "eval_sweep"
- JobType.eval_drift present with value "eval_drift"
- StrEnum round-trip: str(member) == member.value
"""

from __future__ import annotations

from finecorpus.control.audit import AuditAction
from finecorpus.control.jobs import JobType


class TestAuditActionDriftDetected:
    def test_drift_detected_member_exists(self) -> None:
        assert hasattr(AuditAction, "drift_detected")

    def test_drift_detected_value(self) -> None:
        assert AuditAction.drift_detected == "drift_detected"

    def test_drift_detected_str_round_trip(self) -> None:
        assert str(AuditAction.drift_detected) == "drift_detected"

    def test_drift_detected_in_enum_values(self) -> None:
        assert "drift_detected" in [m.value for m in AuditAction]

    def test_all_original_members_still_present(self) -> None:
        expected = {
            "break_glass_grant",
            "break_glass_read",
            "break_glass_expiry",
            "config_change",
            "promotion",
            "rollback",
            "permission_change",
            "permission_gap_ack",
            "confidence_floor_lowered",
            "deletion",
            "purge",
            "budget_cap_hit",
            "key_issued",
            "key_revoked",
        }
        values = {m.value for m in AuditAction}
        assert expected.issubset(values)


class TestJobTypeEvalTypes:
    def test_eval_sweep_member_exists(self) -> None:
        assert hasattr(JobType, "eval_sweep")

    def test_eval_sweep_value(self) -> None:
        assert JobType.eval_sweep == "eval_sweep"

    def test_eval_sweep_str_round_trip(self) -> None:
        assert str(JobType.eval_sweep) == "eval_sweep"

    def test_eval_drift_member_exists(self) -> None:
        assert hasattr(JobType, "eval_drift")

    def test_eval_drift_value(self) -> None:
        assert JobType.eval_drift == "eval_drift"

    def test_eval_drift_str_round_trip(self) -> None:
        assert str(JobType.eval_drift) == "eval_drift"

    def test_all_original_job_types_still_present(self) -> None:
        expected = {
            "ingest",
            "reindex_full",
            "reindex_incremental",
            "restore",
            "purge",
        }
        values = {m.value for m in JobType}
        assert expected.issubset(values)

    def test_eval_sweep_and_eval_drift_in_enum_values(self) -> None:
        values = {m.value for m in JobType}
        assert "eval_sweep" in values
        assert "eval_drift" in values
