"""Contract 6 — Eval set.

Boundary: ⟷ Plan, Serve (bidirectional cross-cutting; §5, §9).
See docs/contracts/eval-set.md for the authoritative spec (§9.1, §9.2, §12, §18.1).

Every question carries its generation method, review status, and source segments (§12).
Provisional status is inseparable from the data (§9.2, §12): review_status is a required
enum with NO DEFAULT, so a question cannot exist without declaring whether it was reviewed.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field

from finecorpus.contracts.ingestion_config import NaiveBaselineRef
from finecorpus.contracts.shared.blocks import TenancyBlock

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class EvalSetOrigin(StrEnum):
    """Where an eval set came from (§9.2).

    See docs/contracts/eval-set.md EvalSet.origin.
    Imported origins supersede generation (§9.2).
    """

    generated = "generated"
    imported_eval_set = "imported_eval_set"
    imported_query_log = "imported_query_log"


class ConfidenceLevel(StrEnum):
    """Set-level confidence for an eval set (§9.2).

    See docs/contracts/eval-set.md EvalSet.confidence_level.
    provisional when any question is unreviewed and origin is generated.
    Stated explicitly and prominently (§9.2).
    """

    provisional = "provisional"
    reviewed = "reviewed"
    production_derived = "production_derived"


class GenerationMethod(StrEnum):
    """How a question was produced (§12).

    See docs/contracts/eval-set.md EvalQuestion.generation_method.
    Generated questions are stratified across types (§9.1).
    """

    generated_factual = "generated_factual"
    generated_interpretive = "generated_interpretive"
    generated_synthesis = "generated_synthesis"
    imported = "imported"
    query_log = "query_log"


class ReviewStatus(StrEnum):
    """Provisional status for an eval question (§9.2, §12).

    See docs/contracts/eval-set.md EvalQuestion.review_status.

    IMPORTANT: This field has NO DEFAULT. A question cannot be constructed without
    an explicit value. Omitting it is a construction/validation error.
    Provisional status is therefore inseparable from the data (§9.2, §12).

    Note per R7 (resolving W-6/W-7): there is NO source_deleted enum value.
    Questions derived from deleted documents are REMOVED (deleted), not marked
    with a flag. See eval-set.md Invariants (Deletion linkage).
    """

    unreviewed = "unreviewed"
    reviewed_kept = "reviewed_kept"
    reviewed_edited = "reviewed_edited"
    reviewed_rejected = "reviewed_rejected"


class QuestionType(StrEnum):
    """Stratification class for an eval question (§9.1).

    See docs/contracts/eval-set.md EvalQuestion.question_type.
    """

    factual_lookup = "factual_lookup"
    interpretive = "interpretive"
    multi_document_synthesis = "multi_document_synthesis"


# ---------------------------------------------------------------------------
# EvalQuestion
# ---------------------------------------------------------------------------


class EvalQuestion(BaseModel):
    """One evaluation question with inseparable review status (§9.2, §12).

    See docs/contracts/eval-set.md EvalQuestion.

    CRITICAL INVARIANT: review_status has NO DEFAULT (§9.2, §12).
    Constructing an EvalQuestion without specifying review_status raises ValidationError.
    Provisional status is thereby inseparable from the data.

    Deletion linkage (§17.1 MUST): eval questions derived from a deleted document
    are REMOVED when that document is deleted — not retained with a flag. A question
    is derived from the deleted document if any of its source_segment_ids belong to
    that document. source_segment_ids makes this traceable.
    """

    question_id: str = Field(description="Identity (ULID).")
    text: str = Field(description="The question text.")
    generation_method: GenerationMethod = Field(
        description=(
            "How this question was produced (§12). "
            "Generated questions are stratified across types (§9.1)."
        )
    )
    # NO DEFAULT — omitting this field at construction time is a ValidationError (§9.2, §12)
    review_status: ReviewStatus = Field(
        description=(
            "Provisional status inseparable from data (§9.2). "
            "A question cannot be constructed without an explicit value; there is no default. "
            "Pydantic field with no default → construction error if omitted."
        )
    )
    source_segment_ids: list[str] = Field(
        description=(
            "Source segments this question was derived from (§12). "
            "Empty only for query_log/imported where provenance is unknown; "
            "then source_unknown=True."
        )
    )
    source_unknown: bool = Field(
        description=(
            "True when source segments are not knowable (imported/query-log). "
            "Makes 'no sources' explicit rather than an empty-list ambiguity."
        )
    )
    question_type: QuestionType = Field(description="Stratification class (§9.1).")
    expected_segment_ids: list[str] | None = Field(
        default=None,
        description=(
            "Gold segments expected in retrieval, when known "
            "(used for context recall/precision, §9.3)."
        ),
    )
    reviewed_by: str | None = Field(
        default=None,
        description="Reviewer identity when review_status is a reviewed_* value.",
    )
    reviewed_at: datetime | None = Field(
        default=None,
        description="When reviewed.",
    )
    class_description_ref: str | None = Field(
        default=None,
        description="The class description (§6.5) that seeded generation, for auditability.",
    )


# ---------------------------------------------------------------------------
# EvalSet (root)
# ---------------------------------------------------------------------------


class EvalSet(BaseModel):
    """Contract 6 root model — used by Plan and Serve bidirectionally.

    See docs/contracts/eval-set.md EvalSet (root).

    Invariants:
    - Every question carries generation_method, review_status, and source_segment_ids
      (or explicit source_unknown) — the §12 triple.
    - review_status has no default — omitting it is a construction/validation error.
    - confidence_level reflects the set-level provisional status, prominently (§9.2).
    - Import supersedes generation (§9.2).
    - Deletion linkage (§17.1 MUST): questions derived from deleted documents are REMOVED.
    - tenancy present (Phase 0 MUST).
    """

    schema_version: str = Field(description="Contract version (semver).")
    tenancy: TenancyBlock = Field(description="Owning workspace/KB.")
    eval_set_id: str = Field(description="Identity (ULID).")
    origin: EvalSetOrigin = Field(
        description="Where this set came from (§9.2). Imported origins supersede generation."
    )
    provenance_note: str | None = Field(
        default=None,
        description="For imported sets: what was imported and when (audit).",
    )
    created_at: datetime = Field(description="Creation/import time (UTC).")
    confidence_level: ConfidenceLevel = Field(
        description=(
            "Set-level confidence, stated explicitly and prominently (§9.2). "
            "provisional when any question is unreviewed and origin is generated; "
            "production_derived for query-log imports."
        )
    )
    questions: list[EvalQuestion] = Field(description="The questions.")
    baseline_ref: NaiveBaselineRef | None = Field(
        default=None,
        description="The §9.3 naive baseline these scores are measured against, when scored.",
    )


__all__ = [
    "EvalSetOrigin",
    "ConfidenceLevel",
    "GenerationMethod",
    "ReviewStatus",
    "QuestionType",
    "EvalQuestion",
    "EvalSet",
]
