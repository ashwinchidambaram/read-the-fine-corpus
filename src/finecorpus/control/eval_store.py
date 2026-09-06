"""Phase 5 eval-store persistence — eval sets, questions, baselines, sweep runs.

Provides ORM models and repositories for the control-plane eval substrate:
  - EvalSetRecord / EvalSetRepository
  - EvalQuestionRecord  (part of EvalSetRepository)
  - EvalBaselineRecord / EvalBaselineRepository
  - SweepRunRecord / SweepRankingRow / SweepRunRepository

Tenancy: every query is kb_id-scoped; workspace_id stored but never used as
a cross-KB read axis. No cross-KB reads are ever performed.

Spec references:
  - contracts/eval-set.md — EvalSet/EvalQuestion contract (§9.1, §9.2, §12).
  - Invariant M-049: when a new baseline with a different reference_fingerprint
    is written, the prior current row is marked
    ``measured_against_reference_id=<old reference_id>`` rather than silently
    overwritten.
  - Invariant M-086 (Deletion linkage §17.1): questions derived from deleted
    documents are REMOVED.  ``remove_questions_for_segments`` deletes questions
    whose source_segment_ids intersect the supplied set; the caller wraps it in
    the same transaction as the tombstone write.

SQLite-compat: JSON columns use sqlalchemy JSON (JSONB on PG via the migration,
JSON on SQLite); follows the same pattern as Phase 4 tables.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    delete,
    select,
)
from sqlalchemy.orm import Mapped, Session, mapped_column

from finecorpus.control.metadata import Base

# ---------------------------------------------------------------------------
# ULID-lite helper (same style as jobs.py)
# ---------------------------------------------------------------------------


def _new_ulid() -> str:
    """Generate a sortable 26-char ULID-style ID (8-char hex ts + 18-char hex rand)."""
    ts = format(int(datetime.now(tz=UTC).timestamp()), "08x")
    rand = secrets.token_hex(9)
    return ts + rand


# ---------------------------------------------------------------------------
# ORM: EvalSetRecord
# ---------------------------------------------------------------------------


class EvalSetRecord(Base):
    """One eval set (Contract 6 root).

    One row per EvalSet; questions live in EvalQuestionRecord (FK back here).

    Columns mirror the EvalSet contract fields that are storage-relevant.
    ``baseline_ref`` stores the NaiveBaselineRef JSON blob (reference_id +
    description) as a nullable JSON column; structured scoring is in
    EvalBaselineRecord.
    """

    __tablename__ = "eval_sets"
    __table_args__ = (
        Index("ix_eval_sets_kb_id", "kb_id"),
        Index("ix_eval_sets_workspace_id", "workspace_id"),
    )

    eval_set_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    kb_id: Mapped[str] = mapped_column(String(64), nullable=False, index=False)
    workspace_id: Mapped[str] = mapped_column(String(64), nullable=False, index=False)
    schema_version: Mapped[str] = mapped_column(String(32), nullable=False)
    origin: Mapped[str] = mapped_column(String(64), nullable=False)
    confidence_level: Mapped[str] = mapped_column(String(64), nullable=False)
    # JSON blob: {reference_id, description} or null
    baseline_ref: Mapped[Any] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    def __repr__(self) -> str:
        return (
            f"EvalSetRecord("
            f"eval_set_id={self.eval_set_id!r}, "
            f"kb_id={self.kb_id!r}, "
            f"origin={self.origin!r})"
        )


# ---------------------------------------------------------------------------
# ORM: EvalQuestionRecord
# ---------------------------------------------------------------------------


class EvalQuestionRecord(Base):
    """One eval question (Contract 6 EvalQuestion) stored in the DB.

    Deletion linkage (M-086): when a document is deleted, the caller invokes
    ``EvalSetRepository.remove_questions_for_segments(kb_id, segment_ids)``
    which DELETEs rows whose source_segment_ids JSON array intersects the
    supplied set.  The method returns the count of removed rows.

    review_status has NO DEFAULT in the contract; the repository enforces that
    callers supply an explicit value.

    source_segment_ids and expected_segment_ids are stored as JSON arrays.
    """

    __tablename__ = "eval_questions"
    __table_args__ = (
        Index("ix_eval_questions_eval_set_id", "eval_set_id"),
        Index("ix_eval_questions_kb_id", "kb_id"),
    )

    question_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    eval_set_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("eval_sets.eval_set_id", name="fk_eq_eval_set"),
        nullable=False,
        index=False,
    )
    kb_id: Mapped[str] = mapped_column(String(64), nullable=False, index=False)
    text: Mapped[str] = mapped_column(Text(), nullable=False)
    question_type: Mapped[str] = mapped_column(String(64), nullable=False)
    generation_method: Mapped[str] = mapped_column(String(64), nullable=False)
    # NO DEFAULT — caller must supply
    review_status: Mapped[str] = mapped_column(String(64), nullable=False)
    # JSON arrays
    source_segment_ids: Mapped[Any] = mapped_column(JSON, nullable=False)
    expected_segment_ids: Mapped[Any] = mapped_column(JSON, nullable=True)
    source_unknown: Mapped[bool] = mapped_column(Boolean(), nullable=False)
    injection_suspicion: Mapped[float | None] = mapped_column(Float(), nullable=True)
    reviewed_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    class_description_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)

    def __repr__(self) -> str:
        return (
            f"EvalQuestionRecord("
            f"question_id={self.question_id!r}, "
            f"eval_set_id={self.eval_set_id!r}, "
            f"review_status={self.review_status!r})"
        )


# ---------------------------------------------------------------------------
# ORM: EvalBaselineRecord
# ---------------------------------------------------------------------------


class EvalBaselineRecord(Base):
    """One §9.3 naive-baseline measurement for a KB.

    M-049 reference-change marking: when a new baseline is written with a
    different reference_fingerprint than the existing current row, the prior
    current row's ``measured_against_reference_id`` is set to the old
    ``reference_id`` (documents the drift rather than silently overwriting).
    The new row then becomes is_current=True.

    ``measured_against_reference_id`` is null on the current row and on any
    prior rows that were superseded by a same-fingerprint update.
    """

    __tablename__ = "eval_baselines"
    __table_args__ = (
        Index("ix_eval_baselines_kb_id", "kb_id"),
        Index("ix_eval_baselines_reference_fingerprint", "reference_fingerprint"),
        Index("ix_eval_baselines_eval_set_id", "eval_set_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    kb_id: Mapped[str] = mapped_column(String(64), nullable=False, index=False)
    reference_id: Mapped[str] = mapped_column(String(255), nullable=False)
    reference_fingerprint: Mapped[str] = mapped_column(String(255), nullable=False, index=False)
    eval_set_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("eval_sets.eval_set_id", name="fk_eb_eval_set"),
        nullable=False,
        index=False,
    )
    recall: Mapped[float] = mapped_column(Float(), nullable=False)
    precision: Mapped[float] = mapped_column(Float(), nullable=False)
    scored_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    is_current: Mapped[bool] = mapped_column(Boolean(), nullable=False)
    # M-049: set to the old reference_id when this row was superseded by a
    # new baseline with a different reference_fingerprint.
    measured_against_reference_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    def __repr__(self) -> str:
        return (
            f"EvalBaselineRecord("
            f"id={self.id!r}, "
            f"kb_id={self.kb_id!r}, "
            f"is_current={self.is_current!r}, "
            f"reference_fingerprint={self.reference_fingerprint!r})"
        )


# ---------------------------------------------------------------------------
# ORM: SweepRunRecord
# ---------------------------------------------------------------------------


class SweepRunRecord(Base):
    """One eval-sweep run (a batch config-comparison experiment).

    Rankings are stored separately in SweepRankingRow (FK back here).
    ``total_cost_usd`` is the accumulated cost across all configurations tried.
    ``declined_reason`` is set when the operator rejects the sweep result.
    """

    __tablename__ = "sweep_runs"
    __table_args__ = (
        Index("ix_sweep_runs_kb_id", "kb_id"),
        Index("ix_sweep_runs_workspace_id", "workspace_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    kb_id: Mapped[str] = mapped_column(String(64), nullable=False, index=False)
    workspace_id: Mapped[str] = mapped_column(String(64), nullable=False, index=False)
    status: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    total_cost_usd: Mapped[float] = mapped_column(Float(), nullable=False, default=0.0)
    confirmed: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=False)
    declined_reason: Mapped[str | None] = mapped_column(Text(), nullable=True)

    def __repr__(self) -> str:
        return f"SweepRunRecord(id={self.id!r}, kb_id={self.kb_id!r}, status={self.status!r})"


# ---------------------------------------------------------------------------
# ORM: SweepRankingRow
# ---------------------------------------------------------------------------


class SweepRankingRow(Base):
    """One ranked configuration result within a sweep run.

    ``rank`` is 1-based; lower rank = better (rank 1 = best config).
    ``config_json`` holds the full ingestion-config diff/summary for this slot.
    ``recall_delta`` and ``precision_delta`` are relative to the current
    production baseline (positive = improvement).
    """

    __tablename__ = "sweep_ranking_rows"
    __table_args__ = (Index("ix_sweep_ranking_rows_sweep_run_id", "sweep_run_id"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    sweep_run_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("sweep_runs.id", name="fk_srr_sweep_run"),
        nullable=False,
        index=False,
    )
    rank: Mapped[int] = mapped_column(Integer(), nullable=False)
    config_json: Mapped[Any] = mapped_column(JSON, nullable=False)
    recall: Mapped[float] = mapped_column(Float(), nullable=False)
    precision: Mapped[float] = mapped_column(Float(), nullable=False)
    recall_delta: Mapped[float] = mapped_column(Float(), nullable=False)
    precision_delta: Mapped[float] = mapped_column(Float(), nullable=False)
    est_cost_usd: Mapped[float] = mapped_column(Float(), nullable=False)
    index_size: Mapped[int] = mapped_column(Integer(), nullable=False)
    ingestion_seconds: Mapped[float] = mapped_column(Float(), nullable=False)

    def __repr__(self) -> str:
        return (
            f"SweepRankingRow("
            f"id={self.id!r}, "
            f"sweep_run_id={self.sweep_run_id!r}, "
            f"rank={self.rank!r})"
        )


# ---------------------------------------------------------------------------
# Repository: EvalSetRepository
# ---------------------------------------------------------------------------


class EvalSetRepository:
    """Storage operations for eval sets and their questions.

    All queries are scoped to kb_id; no cross-KB reads are performed.

    Args:
        session: SQLAlchemy Session bound to the control-plane engine.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    # -- EvalSet CRUD --------------------------------------------------------

    def create(
        self,
        *,
        eval_set_id: str,
        kb_id: str,
        workspace_id: str,
        schema_version: str,
        origin: str,
        confidence_level: str,
        baseline_ref: dict[str, Any] | None = None,
        created_at: datetime | None = None,
    ) -> EvalSetRecord:
        """Persist a new eval set.

        Args:
            eval_set_id: ULID identity.
            kb_id: Owning KB.
            workspace_id: Owning workspace.
            schema_version: Contract semver string.
            origin: EvalSetOrigin value.
            confidence_level: ConfidenceLevel value.
            baseline_ref: Optional NaiveBaselineRef dict to store.
            created_at: Creation timestamp; defaults to UTC now.

        Returns:
            Newly created EvalSetRecord (added to session, not committed).
        """
        if created_at is None:
            created_at = datetime.now(tz=UTC)

        record = EvalSetRecord(
            eval_set_id=eval_set_id,
            kb_id=kb_id,
            workspace_id=workspace_id,
            schema_version=schema_version,
            origin=origin,
            confidence_level=confidence_level,
            baseline_ref=baseline_ref,
            created_at=created_at,
        )
        self._session.add(record)
        return record

    def get(self, eval_set_id: str) -> EvalSetRecord | None:
        """Fetch an eval set by ID (no KB scope — id is globally unique).

        Args:
            eval_set_id: ULID identity.

        Returns:
            EvalSetRecord or None.
        """
        stmt = select(EvalSetRecord).where(EvalSetRecord.eval_set_id == eval_set_id)
        return self._session.execute(stmt).scalar_one_or_none()

    def list_for_kb(self, kb_id: str) -> list[EvalSetRecord]:
        """List all eval sets for a given KB (ordered by created_at descending).

        Args:
            kb_id: Owning KB identity.

        Returns:
            List of EvalSetRecord rows (may be empty).
        """
        stmt = (
            select(EvalSetRecord)
            .where(EvalSetRecord.kb_id == kb_id)
            .order_by(EvalSetRecord.created_at.desc())
        )
        return list(self._session.execute(stmt).scalars())

    # -- EvalQuestion CRUD ---------------------------------------------------

    def add_question(
        self,
        *,
        question_id: str,
        eval_set_id: str,
        kb_id: str,
        text: str,
        question_type: str,
        generation_method: str,
        review_status: str,
        source_segment_ids: list[str],
        source_unknown: bool,
        expected_segment_ids: list[str] | None = None,
        injection_suspicion: float | None = None,
        reviewed_by: str | None = None,
        reviewed_at: datetime | None = None,
        class_description_ref: str | None = None,
    ) -> EvalQuestionRecord:
        """Persist a new eval question.

        review_status has no default in the contract; callers MUST supply it.

        Args:
            question_id: ULID identity.
            eval_set_id: Owning eval set.
            kb_id: Owning KB (denormalized for tenancy-scoped queries).
            text: Question text.
            question_type: QuestionType value.
            generation_method: GenerationMethod value.
            review_status: ReviewStatus value (NO DEFAULT — required).
            source_segment_ids: List of segment IDs this question was derived from.
            source_unknown: True when source provenance is unknowable.
            expected_segment_ids: Gold segment IDs for recall/precision, or None.
            injection_suspicion: Injection-pattern score 0..1, or None.
            reviewed_by: Reviewer identity when reviewed.
            reviewed_at: Review timestamp.
            class_description_ref: Class-description seeding reference, for audit.

        Returns:
            Newly created EvalQuestionRecord.
        """
        record = EvalQuestionRecord(
            question_id=question_id,
            eval_set_id=eval_set_id,
            kb_id=kb_id,
            text=text,
            question_type=question_type,
            generation_method=generation_method,
            review_status=review_status,
            source_segment_ids=source_segment_ids,
            expected_segment_ids=expected_segment_ids,
            source_unknown=source_unknown,
            injection_suspicion=injection_suspicion,
            reviewed_by=reviewed_by,
            reviewed_at=reviewed_at,
            class_description_ref=class_description_ref,
        )
        self._session.add(record)
        return record

    def get_questions(self, eval_set_id: str) -> list[EvalQuestionRecord]:
        """Fetch all questions for an eval set.

        Args:
            eval_set_id: Owning eval set ID.

        Returns:
            List of EvalQuestionRecord rows.
        """
        stmt = select(EvalQuestionRecord).where(EvalQuestionRecord.eval_set_id == eval_set_id)
        return list(self._session.execute(stmt).scalars())

    def set_question_review(
        self,
        question_id: str,
        *,
        status: str,
        reviewed_by: str,
        at: datetime,
    ) -> EvalQuestionRecord:
        """Update review_status, reviewed_by, and reviewed_at for a question.

        Called by the review CLI.  The question must exist.

        Args:
            question_id: Question ULID.
            status: New ReviewStatus value.
            reviewed_by: Reviewer identity.
            at: Review timestamp.

        Returns:
            Updated EvalQuestionRecord.

        Raises:
            KeyError: If the question does not exist.
        """
        record = self._get_question_or_raise(question_id)
        record.review_status = status
        record.reviewed_by = reviewed_by
        record.reviewed_at = at
        return record

    def remove_questions_for_segments(
        self,
        kb_id: str,
        segment_ids: set[str],
    ) -> int:
        """Delete questions whose source_segment_ids intersect ``segment_ids``.

        M-086 Deletion linkage: questions derived from deleted documents are
        REMOVED (not flagged).  A question is derived from the deleted document
        if any of its source_segment_ids belong to that document.

        This method performs a Python-level filter for SQLite compatibility:
        it loads all questions for the KB and deletes those with an intersection.
        On PostgreSQL the caller may prefer a raw JSON query, but this approach
        is correct on both backends and is the canonical Phase 5 implementation.

        Must be called within the same transaction as the tombstone write so
        the deletion is atomic.

        Args:
            kb_id: KB to scope the deletion (tenancy guard).
            segment_ids: Set of segment IDs that were deleted.

        Returns:
            Number of questions removed.
        """
        if not segment_ids:
            return 0

        # Load all questions for this KB; filter in Python (JSON intersection).
        stmt = select(EvalQuestionRecord).where(EvalQuestionRecord.kb_id == kb_id)
        rows = list(self._session.execute(stmt).scalars())

        to_delete = []
        for row in rows:
            stored: list[str] = row.source_segment_ids or []
            if set(stored) & segment_ids:
                to_delete.append(row.question_id)

        if not to_delete:
            return 0

        del_stmt = delete(EvalQuestionRecord).where(EvalQuestionRecord.question_id.in_(to_delete))
        from sqlalchemy.engine import CursorResult  # noqa: PLC0415

        result: CursorResult[Any] = self._session.execute(del_stmt)  # type: ignore[assignment]
        return result.rowcount

    # -- Helpers -------------------------------------------------------------

    def _get_question_or_raise(self, question_id: str) -> EvalQuestionRecord:
        stmt = select(EvalQuestionRecord).where(EvalQuestionRecord.question_id == question_id)
        record = self._session.execute(stmt).scalar_one_or_none()
        if record is None:
            raise KeyError(f"question_id {question_id!r} not found")
        return record


# ---------------------------------------------------------------------------
# Repository: EvalBaselineRepository
# ---------------------------------------------------------------------------


class EvalBaselineRepository:
    """Storage operations for §9.3 naive baseline measurements.

    M-049 invariant is enforced in ``upsert_current``: when the new baseline
    has a different reference_fingerprint from the existing current row, the
    prior current row's ``measured_against_reference_id`` is set to the old
    ``reference_id`` before the new row is inserted.

    Args:
        session: SQLAlchemy Session bound to the control-plane engine.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def upsert_current(
        self,
        *,
        kb_id: str,
        reference_id: str,
        reference_fingerprint: str,
        eval_set_id: str,
        recall: float,
        precision: float,
        scored_at: datetime | None = None,
    ) -> EvalBaselineRecord:
        """Write a new current baseline for a KB, applying M-049 marking.

        Steps:
        1. Load the existing ``is_current=True`` row for this kb_id (if any).
        2. Set its ``is_current=False``.
        3. If the existing row's ``reference_fingerprint`` differs from the
           new one, also set ``measured_against_reference_id`` on the existing
           row to the OLD ``reference_id`` (M-049 reference-change marking).
        4. Insert a new row with ``is_current=True``.

        Args:
            kb_id: KB to scope (tenancy guard).
            reference_id: Identity of the reference config being measured against.
            reference_fingerprint: Content fingerprint of the reference config.
            eval_set_id: The eval set used for scoring.
            recall: Recall score [0..1].
            precision: Precision score [0..1].
            scored_at: Scoring timestamp; defaults to UTC now.

        Returns:
            Newly created EvalBaselineRecord (is_current=True).
        """
        if scored_at is None:
            scored_at = datetime.now(tz=UTC)

        # Step 1: find the existing current row (kb_id scoped)
        existing = self.get_current(kb_id)

        if existing is not None:
            # Step 2: demote existing current
            existing.is_current = False
            # Step 3: M-049 — if fingerprint changed, mark the old row
            if existing.reference_fingerprint != reference_fingerprint:
                existing.measured_against_reference_id = existing.reference_id

        # Step 4: insert new current
        new_id = _new_ulid()
        new_row = EvalBaselineRecord(
            id=new_id,
            kb_id=kb_id,
            reference_id=reference_id,
            reference_fingerprint=reference_fingerprint,
            eval_set_id=eval_set_id,
            recall=recall,
            precision=precision,
            scored_at=scored_at,
            is_current=True,
            measured_against_reference_id=None,
        )
        self._session.add(new_row)
        return new_row

    def get_current(self, kb_id: str) -> EvalBaselineRecord | None:
        """Fetch the current (is_current=True) baseline for a KB.

        Args:
            kb_id: KB identity.

        Returns:
            EvalBaselineRecord or None if no baseline has been scored yet.
        """
        stmt = (
            select(EvalBaselineRecord)
            .where(EvalBaselineRecord.kb_id == kb_id)
            .where(EvalBaselineRecord.is_current.is_(True))
            .limit(1)
        )
        return self._session.execute(stmt).scalar_one_or_none()

    def list_for_kb(self, kb_id: str) -> list[EvalBaselineRecord]:
        """List all baseline rows for a KB (newest first by scored_at).

        Args:
            kb_id: KB identity.

        Returns:
            List of EvalBaselineRecord rows (may be empty).
        """
        stmt = (
            select(EvalBaselineRecord)
            .where(EvalBaselineRecord.kb_id == kb_id)
            .order_by(EvalBaselineRecord.scored_at.desc())
        )
        return list(self._session.execute(stmt).scalars())


# ---------------------------------------------------------------------------
# Repository: SweepRunRepository
# ---------------------------------------------------------------------------


class SweepRunRepository:
    """Storage operations for eval-sweep runs and their rankings.

    Args:
        session: SQLAlchemy Session bound to the control-plane engine.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(
        self,
        *,
        kb_id: str,
        workspace_id: str,
        status: str,
        created_at: datetime | None = None,
    ) -> SweepRunRecord:
        """Create a new sweep run.

        Args:
            kb_id: Owning KB.
            workspace_id: Owning workspace.
            status: Initial status string (e.g. ``"pending"``).
            created_at: Creation timestamp; defaults to UTC now.

        Returns:
            Newly created SweepRunRecord.
        """
        if created_at is None:
            created_at = datetime.now(tz=UTC)

        record = SweepRunRecord(
            id=_new_ulid(),
            kb_id=kb_id,
            workspace_id=workspace_id,
            status=status,
            created_at=created_at,
            total_cost_usd=0.0,
            confirmed=False,
            declined_reason=None,
        )
        self._session.add(record)
        return record

    def get(self, sweep_id: str) -> SweepRunRecord | None:
        """Fetch a sweep run by ID.

        Args:
            sweep_id: Sweep run ULID.

        Returns:
            SweepRunRecord or None.
        """
        stmt = select(SweepRunRecord).where(SweepRunRecord.id == sweep_id)
        return self._session.execute(stmt).scalar_one_or_none()

    def list_for_kb(self, kb_id: str) -> list[SweepRunRecord]:
        """List all sweep runs for a KB (newest first).

        Args:
            kb_id: Owning KB identity.

        Returns:
            List of SweepRunRecord rows.
        """
        stmt = (
            select(SweepRunRecord)
            .where(SweepRunRecord.kb_id == kb_id)
            .order_by(SweepRunRecord.created_at.desc())
        )
        return list(self._session.execute(stmt).scalars())

    def set_status(
        self,
        sweep_id: str,
        *,
        status: str,
        total_cost_usd: float | None = None,
        confirmed: bool | None = None,
        declined_reason: str | None = None,
    ) -> SweepRunRecord:
        """Update the status (and optional fields) of a sweep run.

        Args:
            sweep_id: Sweep run ULID.
            status: New status string.
            total_cost_usd: Final cost, if known.
            confirmed: Whether the operator confirmed the result.
            declined_reason: Reason string when the operator declined.

        Returns:
            Updated SweepRunRecord.

        Raises:
            KeyError: If the sweep run does not exist.
        """
        record = self._get_or_raise(sweep_id)
        record.status = status
        if total_cost_usd is not None:
            record.total_cost_usd = total_cost_usd
        if confirmed is not None:
            record.confirmed = confirmed
        if declined_reason is not None:
            record.declined_reason = declined_reason
        return record

    def add_ranking_row(
        self,
        *,
        sweep_run_id: str,
        rank: int,
        config_json: dict[str, Any],
        recall: float,
        precision: float,
        recall_delta: float,
        precision_delta: float,
        est_cost_usd: float,
        index_size: int,
        ingestion_seconds: float,
    ) -> SweepRankingRow:
        """Append a ranking row to a sweep run.

        Args:
            sweep_run_id: Parent sweep run ID.
            rank: 1-based rank (1 = best).
            config_json: Full config diff/summary JSON for this slot.
            recall: Recall score [0..1].
            precision: Precision score [0..1].
            recall_delta: Recall delta vs. current production baseline.
            precision_delta: Precision delta vs. current production baseline.
            est_cost_usd: Estimated cost to deploy this config.
            index_size: Number of index entries for this config.
            ingestion_seconds: Wall-clock ingestion time for this config.

        Returns:
            Newly created SweepRankingRow.
        """
        row = SweepRankingRow(
            id=_new_ulid(),
            sweep_run_id=sweep_run_id,
            rank=rank,
            config_json=config_json,
            recall=recall,
            precision=precision,
            recall_delta=recall_delta,
            precision_delta=precision_delta,
            est_cost_usd=est_cost_usd,
            index_size=index_size,
            ingestion_seconds=ingestion_seconds,
        )
        self._session.add(row)
        return row

    def get_ranking(self, sweep_run_id: str) -> list[SweepRankingRow]:
        """Fetch all ranking rows for a sweep run, ordered by rank ascending.

        Args:
            sweep_run_id: Parent sweep run ULID.

        Returns:
            List of SweepRankingRow rows.
        """
        stmt = (
            select(SweepRankingRow)
            .where(SweepRankingRow.sweep_run_id == sweep_run_id)
            .order_by(SweepRankingRow.rank.asc())
        )
        return list(self._session.execute(stmt).scalars())

    # -- Helpers -------------------------------------------------------------

    def _get_or_raise(self, sweep_id: str) -> SweepRunRecord:
        record = self.get(sweep_id)
        if record is None:
            raise KeyError(f"sweep_id {sweep_id!r} not found")
        return record


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    "EvalSetRecord",
    "EvalQuestionRecord",
    "EvalBaselineRecord",
    "SweepRunRecord",
    "SweepRankingRow",
    "EvalSetRepository",
    "EvalBaselineRepository",
    "SweepRunRepository",
]
