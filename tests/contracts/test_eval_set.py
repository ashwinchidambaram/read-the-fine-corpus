"""Unit tests for EvalSet contract invariants.

See docs/contracts/eval-set.md.

Tests:
- EvalQuestion without review_status fails to construct (ValidationError).
- EvalQuestion with review_status constructs.
- EvalSet tenancy is required.
"""

from __future__ import annotations

from datetime import UTC

import pytest
from pydantic import ValidationError

from finecorpus.contracts.eval_set import (
    ConfidenceLevel,
    EvalQuestion,
    EvalSet,
    EvalSetOrigin,
    GenerationMethod,
    QuestionType,
    ReviewStatus,
)
from finecorpus.contracts.shared.blocks import (
    PermissionFidelity,
    PermissionMode,
    PermissionSource,
    TenancyBlock,
)


def make_tenancy() -> TenancyBlock:
    return TenancyBlock(
        workspace_id="01JWSPACE001",
        kb_id="01JKB000001",
        permission_mode=PermissionMode.public_to_kb,
        permission_principals=[],
        permission_source=PermissionSource.platform,
        permission_fidelity=PermissionFidelity.authoritative,
    )


class TestEvalQuestionReviewStatusRequired:
    """review_status has NO DEFAULT — omitting it is a ValidationError (§9.2, §12)."""

    def test_missing_review_status_raises_validation_error(self) -> None:
        """EvalQuestion without review_status MUST raise ValidationError."""
        with pytest.raises(ValidationError) as exc_info:
            EvalQuestion(
                question_id="01JQUESTION001",
                text="What is the procedure for lubricating bearings?",
                generation_method=GenerationMethod.generated_factual,
                # review_status intentionally omitted
                source_segment_ids=["01JSEG00010"],
                source_unknown=False,
                question_type=QuestionType.factual_lookup,
            )
        errors = exc_info.value.errors()
        fields = [e["loc"][0] for e in errors]
        assert "review_status" in fields, (
            "ValidationError should flag 'review_status' as missing "
            "(§9.2: provisional status inseparable from data)"
        )

    def test_explicit_review_status_constructs(self) -> None:
        """EvalQuestion with explicit review_status constructs successfully."""
        q = EvalQuestion(
            question_id="01JQUESTION001",
            text="What is the procedure for lubricating bearings?",
            generation_method=GenerationMethod.generated_factual,
            review_status=ReviewStatus.unreviewed,
            source_segment_ids=["01JSEG00010"],
            source_unknown=False,
            question_type=QuestionType.factual_lookup,
        )
        assert q.review_status == ReviewStatus.unreviewed

    def test_all_review_status_values_are_valid(self) -> None:
        """All four review_status enum values construct without error."""
        for status in ReviewStatus:
            q = EvalQuestion(
                question_id="01JQUESTION001",
                text="Test question",
                generation_method=GenerationMethod.generated_factual,
                review_status=status,
                source_segment_ids=[],
                source_unknown=True,
                question_type=QuestionType.factual_lookup,
            )
            assert q.review_status == status

    def test_no_default_review_status(self) -> None:
        """Verify that review_status has no default by checking the model field info."""
        field_info = EvalQuestion.model_fields["review_status"]
        # PydanticUndefined means no default was set
        from pydantic_core import PydanticUndefinedType

        assert isinstance(field_info.default, PydanticUndefinedType), (
            "review_status must have no default (§9.2)"
        )


class TestEvalSetConstruction:
    """EvalSet constructs correctly with valid data."""

    def test_eval_set_tenancy_required(self) -> None:
        """EvalSet without tenancy raises ValidationError (Phase 0 MUST)."""
        from datetime import datetime

        with pytest.raises(ValidationError) as exc_info:
            EvalSet(
                schema_version="1.0.0",
                # tenancy intentionally omitted
                eval_set_id="01JEVALSET001",
                origin=EvalSetOrigin.generated,
                created_at=datetime.now(tz=UTC),
                confidence_level=ConfidenceLevel.provisional,
                questions=[],
            )
        errors = exc_info.value.errors()
        fields = [e["loc"][0] for e in errors]
        assert "tenancy" in fields

    def test_valid_eval_set_constructs(self) -> None:
        """An EvalSet with all required fields constructs successfully."""
        from datetime import datetime

        q = EvalQuestion(
            question_id="01JQUESTION001",
            text="What is the lubrication interval?",
            generation_method=GenerationMethod.generated_factual,
            review_status=ReviewStatus.unreviewed,
            source_segment_ids=["01JSEG00010"],
            source_unknown=False,
            question_type=QuestionType.factual_lookup,
        )
        es = EvalSet(
            schema_version="1.0.0",
            tenancy=make_tenancy(),
            eval_set_id="01JEVALSET001",
            origin=EvalSetOrigin.generated,
            created_at=datetime.now(tz=UTC),
            confidence_level=ConfidenceLevel.provisional,
            questions=[q],
        )
        assert len(es.questions) == 1
        assert es.questions[0].review_status == ReviewStatus.unreviewed
