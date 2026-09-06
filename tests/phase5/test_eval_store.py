"""Phase 5 eval-store tests.

Test inventory:
  test_eval_set_roundtrip — create + get for EvalSetRecord.
  test_eval_question_roundtrip — add_question + get_questions roundtrip.
  test_eval_baseline_roundtrip — upsert_current + get_current roundtrip.
  test_sweep_run_roundtrip — create + get + add_ranking_row + get_ranking.
  test_eval_baseline_reference_fingerprint_marking — M-049: writing a
    new-fingerprint baseline marks the prior current row's
    measured_against_reference_id; doesn't silently overwrite.
  test_eval_baseline_same_fingerprint_no_marking — M-049 negative: same
    fingerprint → prior row NOT marked with measured_against_reference_id.
  test_remove_questions_for_segments — M-086: only intersecting questions
    deleted, count correct, others untouched.
  test_eval_set_tenancy_scoped — list_for_kb never returns another KB's rows.
  test_set_question_review_transition — review CLI path mutates status/reviewer.
  test_create_tables_creates_eval_tables — create_tables() (SQLite) creates all
    five Phase 5 tables.
  test_sweep_run_set_status — set_status updates status, cost, confirmed.
  test_eval_baseline_list_for_kb — list_for_kb returns all rows (newest first).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import Session

from finecorpus.control.eval_store import (
    EvalBaselineRepository,
    EvalSetRepository,
    SweepRunRepository,
)
from finecorpus.control.metadata import create_tables

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def engine():
    """Fresh in-memory SQLite engine with all tables created."""
    eng = create_engine("sqlite://")
    create_tables(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def session(engine):
    with Session(engine) as s:
        yield s


@pytest.fixture
def eval_repo(session):
    return EvalSetRepository(session)


@pytest.fixture
def baseline_repo(session):
    return EvalBaselineRepository(session)


@pytest.fixture
def sweep_repo(session):
    return SweepRunRepository(session)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 9, 5, 12, 0, 0, tzinfo=UTC)


def _make_eval_set(repo: EvalSetRepository, *, eval_set_id="es-001", kb_id="kb-A"):
    return repo.create(
        eval_set_id=eval_set_id,
        kb_id=kb_id,
        workspace_id="ws-1",
        schema_version="1.0.0",
        origin="generated",
        confidence_level="provisional",
        baseline_ref=None,
        created_at=_NOW,
    )


def _make_question(
    repo: EvalSetRepository,
    *,
    question_id="q-001",
    eval_set_id="es-001",
    kb_id="kb-A",
    source_segment_ids=None,
    review_status="unreviewed",
):
    return repo.add_question(
        question_id=question_id,
        eval_set_id=eval_set_id,
        kb_id=kb_id,
        text="What is the retention policy?",
        question_type="factual_lookup",
        generation_method="generated_factual",
        review_status=review_status,
        source_segment_ids=source_segment_ids or ["seg-1", "seg-2"],
        source_unknown=False,
        expected_segment_ids=["seg-1"],
        injection_suspicion=0.0,
        reviewed_by=None,
        reviewed_at=None,
        class_description_ref="cls-policies",
    )


# ---------------------------------------------------------------------------
# Tests — basic roundtrips
# ---------------------------------------------------------------------------


def test_eval_set_roundtrip(eval_repo, session):
    """create + get returns the same record."""
    _make_eval_set(eval_repo)
    session.flush()

    record = eval_repo.get("es-001")
    assert record is not None
    assert record.eval_set_id == "es-001"
    assert record.kb_id == "kb-A"
    assert record.origin == "generated"
    assert record.confidence_level == "provisional"
    assert record.schema_version == "1.0.0"
    assert record.workspace_id == "ws-1"
    assert record.baseline_ref is None
    # SQLite strips tzinfo; compare naive representation
    assert record.created_at.replace(tzinfo=None) == _NOW.replace(tzinfo=None)


def test_eval_set_with_baseline_ref(eval_repo, session):
    """baseline_ref JSON blob is round-tripped."""
    ref = {"reference_id": "ref-001", "description": "§9.3 naive BM25 baseline"}
    eval_repo.create(
        eval_set_id="es-002",
        kb_id="kb-A",
        workspace_id="ws-1",
        schema_version="1.0.0",
        origin="generated",
        confidence_level="provisional",
        baseline_ref=ref,
        created_at=_NOW,
    )
    session.flush()

    record = eval_repo.get("es-002")
    assert record is not None
    assert record.baseline_ref == ref


def test_eval_question_roundtrip(eval_repo, session):
    """add_question + get_questions returns the persisted question."""
    _make_eval_set(eval_repo)
    session.flush()

    _make_question(eval_repo)
    session.flush()

    questions = eval_repo.get_questions("es-001")
    assert len(questions) == 1
    q = questions[0]
    assert q.question_id == "q-001"
    assert q.eval_set_id == "es-001"
    assert q.kb_id == "kb-A"
    assert q.text == "What is the retention policy?"
    assert q.question_type == "factual_lookup"
    assert q.generation_method == "generated_factual"
    assert q.review_status == "unreviewed"
    assert q.source_segment_ids == ["seg-1", "seg-2"]
    assert q.expected_segment_ids == ["seg-1"]
    assert q.source_unknown is False
    assert q.injection_suspicion == 0.0
    assert q.reviewed_by is None
    assert q.reviewed_at is None
    assert q.class_description_ref == "cls-policies"


def test_eval_baseline_roundtrip(baseline_repo, eval_repo, session):
    """upsert_current (first write) → get_current returns the new row."""
    _make_eval_set(eval_repo)
    session.flush()

    row = baseline_repo.upsert_current(
        kb_id="kb-A",
        reference_id="ref-001",
        reference_fingerprint="fp-abc",
        eval_set_id="es-001",
        recall=0.82,
        precision=0.79,
        scored_at=_NOW,
    )
    session.flush()

    current = baseline_repo.get_current("kb-A")
    assert current is not None
    assert current.id == row.id
    assert current.kb_id == "kb-A"
    assert current.reference_id == "ref-001"
    assert current.reference_fingerprint == "fp-abc"
    assert current.eval_set_id == "es-001"
    assert current.recall == pytest.approx(0.82)
    assert current.precision == pytest.approx(0.79)
    assert current.is_current is True
    assert current.measured_against_reference_id is None


def test_sweep_run_roundtrip(sweep_repo, eval_repo, session):
    """create + get + add_ranking_row + get_ranking roundtrip."""
    _make_eval_set(eval_repo)
    session.flush()

    run = sweep_repo.create(
        kb_id="kb-A",
        workspace_id="ws-1",
        status="pending",
        created_at=_NOW,
    )
    session.flush()

    fetched = sweep_repo.get(run.id)
    assert fetched is not None
    assert fetched.kb_id == "kb-A"
    assert fetched.status == "pending"
    assert fetched.confirmed is False
    assert fetched.total_cost_usd == pytest.approx(0.0)

    # Add ranking rows
    sweep_repo.add_ranking_row(
        sweep_run_id=run.id,
        rank=1,
        config_json={"chunking_strategy": "recursive_char", "chunk_size": 512},
        recall=0.91,
        precision=0.88,
        recall_delta=0.05,
        precision_delta=0.03,
        est_cost_usd=12.5,
        index_size=50000,
        ingestion_seconds=120.0,
    )
    sweep_repo.add_ranking_row(
        sweep_run_id=run.id,
        rank=2,
        config_json={"chunking_strategy": "semantic", "chunk_size": 256},
        recall=0.87,
        precision=0.84,
        recall_delta=0.01,
        precision_delta=-0.01,
        est_cost_usd=18.0,
        index_size=60000,
        ingestion_seconds=180.0,
    )
    session.flush()

    ranking = sweep_repo.get_ranking(run.id)
    assert len(ranking) == 2
    assert ranking[0].rank == 1
    assert ranking[0].recall == pytest.approx(0.91)
    assert ranking[1].rank == 2
    assert ranking[1].recall == pytest.approx(0.87)


# ---------------------------------------------------------------------------
# Tests — M-049: reference fingerprint marking
# ---------------------------------------------------------------------------


def test_eval_baseline_reference_fingerprint_marking(baseline_repo, eval_repo, session):
    """M-049: writing a new-fingerprint baseline marks the prior current row.

    The prior row's measured_against_reference_id must be set to the old
    reference_id (documents the drift).  The new row has
    measured_against_reference_id=None and is_current=True.
    """
    _make_eval_set(eval_repo)
    session.flush()

    # First baseline
    first = baseline_repo.upsert_current(
        kb_id="kb-A",
        reference_id="ref-v1",
        reference_fingerprint="fp-v1",
        eval_set_id="es-001",
        recall=0.80,
        precision=0.75,
        scored_at=_NOW,
    )
    session.flush()

    # Second baseline with a DIFFERENT fingerprint
    second = baseline_repo.upsert_current(
        kb_id="kb-A",
        reference_id="ref-v2",
        reference_fingerprint="fp-v2",  # changed
        eval_set_id="es-001",
        recall=0.85,
        precision=0.80,
        scored_at=_NOW,
    )
    session.flush()

    # The first row must be marked
    session.expire(first)  # reload from DB
    assert first.is_current is False
    # M-049: measured_against_reference_id is set to the OLD reference_id
    assert first.measured_against_reference_id == "ref-v1"

    # The new current row is unmarked
    assert second.is_current is True
    assert second.measured_against_reference_id is None
    assert second.reference_fingerprint == "fp-v2"

    # get_current returns the new row
    current = baseline_repo.get_current("kb-A")
    assert current is not None
    assert current.id == second.id


def test_eval_baseline_same_fingerprint_no_marking(baseline_repo, eval_repo, session):
    """M-049 negative: same fingerprint → prior row NOT marked with measured_against_reference_id.

    When the reference fingerprint is unchanged, upsert_current demotes the old
    row (is_current=False) but does NOT set measured_against_reference_id on it
    (no reference drift occurred).
    """
    _make_eval_set(eval_repo)
    session.flush()

    first = baseline_repo.upsert_current(
        kb_id="kb-A",
        reference_id="ref-v1",
        reference_fingerprint="fp-stable",
        eval_set_id="es-001",
        recall=0.80,
        precision=0.75,
        scored_at=_NOW,
    )
    session.flush()

    # Second baseline with the SAME fingerprint (re-scored, not a reference drift)
    baseline_repo.upsert_current(
        kb_id="kb-A",
        reference_id="ref-v1",
        reference_fingerprint="fp-stable",  # unchanged
        eval_set_id="es-001",
        recall=0.81,
        precision=0.76,
        scored_at=_NOW,
    )
    session.flush()

    session.expire(first)
    # Demoted but NOT marked — no reference change occurred
    assert first.is_current is False
    assert first.measured_against_reference_id is None


# ---------------------------------------------------------------------------
# Tests — M-086: deletion linkage
# ---------------------------------------------------------------------------


def test_remove_questions_for_segments(eval_repo, session):
    """M-086: only intersecting questions deleted; count correct; others untouched."""
    _make_eval_set(eval_repo)
    session.flush()

    # Q1 — derived from seg-1, seg-2 (should be deleted when seg-1 deleted)
    _make_question(
        eval_repo,
        question_id="q-001",
        source_segment_ids=["seg-1", "seg-2"],
    )
    # Q2 — derived from seg-3 only (should NOT be deleted)
    _make_question(
        eval_repo,
        question_id="q-002",
        source_segment_ids=["seg-3"],
    )
    # Q3 — derived from seg-1 (should be deleted)
    _make_question(
        eval_repo,
        question_id="q-003",
        source_segment_ids=["seg-1"],
    )
    session.flush()

    # Delete seg-1 — questions touching seg-1 must be removed
    count = eval_repo.remove_questions_for_segments("kb-A", {"seg-1"})

    assert count == 2, f"Expected 2 deleted, got {count}"

    # Verify: only q-002 remains
    remaining = eval_repo.get_questions("es-001")
    remaining_ids = {q.question_id for q in remaining}
    assert remaining_ids == {"q-002"}


def test_remove_questions_empty_segment_set(eval_repo, session):
    """Empty segment_ids → no deletion, returns 0."""
    _make_eval_set(eval_repo)
    session.flush()
    _make_question(eval_repo)
    session.flush()

    count = eval_repo.remove_questions_for_segments("kb-A", set())
    assert count == 0
    assert len(eval_repo.get_questions("es-001")) == 1


def test_remove_questions_tenancy_scoped(eval_repo, session):
    """remove_questions_for_segments is KB-scoped; other KB's questions are untouched."""
    # KB-A eval set + question
    _make_eval_set(eval_repo, eval_set_id="es-001", kb_id="kb-A")
    session.flush()
    _make_question(
        eval_repo,
        question_id="q-a",
        eval_set_id="es-001",
        kb_id="kb-A",
        source_segment_ids=["seg-1"],
    )

    # KB-B eval set + question (same segment ID)
    eval_repo.create(
        eval_set_id="es-002",
        kb_id="kb-B",
        workspace_id="ws-1",
        schema_version="1.0.0",
        origin="generated",
        confidence_level="provisional",
        created_at=_NOW,
    )
    eval_repo.add_question(
        question_id="q-b",
        eval_set_id="es-002",
        kb_id="kb-B",
        text="Another question",
        question_type="factual_lookup",
        generation_method="generated_factual",
        review_status="unreviewed",
        source_segment_ids=["seg-1"],
        source_unknown=False,
    )
    session.flush()

    # Delete seg-1 from KB-A only
    count = eval_repo.remove_questions_for_segments("kb-A", {"seg-1"})
    assert count == 1

    # KB-B question must still exist
    kb_b_questions = eval_repo.get_questions("es-002")
    assert len(kb_b_questions) == 1
    assert kb_b_questions[0].question_id == "q-b"


# ---------------------------------------------------------------------------
# Tests — tenancy isolation
# ---------------------------------------------------------------------------


def test_eval_set_tenancy_scoped(eval_repo, session):
    """list_for_kb never returns another KB's rows."""
    # KB-A
    _make_eval_set(eval_repo, eval_set_id="es-A1", kb_id="kb-A")
    _make_eval_set(eval_repo, eval_set_id="es-A2", kb_id="kb-A")
    # KB-B
    eval_repo.create(
        eval_set_id="es-B1",
        kb_id="kb-B",
        workspace_id="ws-1",
        schema_version="1.0.0",
        origin="imported_eval_set",
        confidence_level="reviewed",
        created_at=_NOW,
    )
    session.flush()

    kb_a_sets = eval_repo.list_for_kb("kb-A")
    assert {r.eval_set_id for r in kb_a_sets} == {"es-A1", "es-A2"}

    kb_b_sets = eval_repo.list_for_kb("kb-B")
    assert {r.eval_set_id for r in kb_b_sets} == {"es-B1"}

    # No cross-KB leakage
    assert all(r.kb_id == "kb-A" for r in kb_a_sets)
    assert all(r.kb_id == "kb-B" for r in kb_b_sets)


def test_eval_baseline_tenancy_scoped(baseline_repo, eval_repo, session):
    """get_current and list_for_kb never return another KB's baselines."""
    _make_eval_set(eval_repo, eval_set_id="es-001", kb_id="kb-A")
    eval_repo.create(
        eval_set_id="es-002",
        kb_id="kb-B",
        workspace_id="ws-1",
        schema_version="1.0.0",
        origin="generated",
        confidence_level="provisional",
        created_at=_NOW,
    )
    session.flush()

    baseline_repo.upsert_current(
        kb_id="kb-A",
        reference_id="ref-a",
        reference_fingerprint="fp-a",
        eval_set_id="es-001",
        recall=0.80,
        precision=0.75,
        scored_at=_NOW,
    )
    baseline_repo.upsert_current(
        kb_id="kb-B",
        reference_id="ref-b",
        reference_fingerprint="fp-b",
        eval_set_id="es-002",
        recall=0.70,
        precision=0.65,
        scored_at=_NOW,
    )
    session.flush()

    current_a = baseline_repo.get_current("kb-A")
    assert current_a is not None
    assert current_a.kb_id == "kb-A"
    assert current_a.reference_id == "ref-a"

    current_b = baseline_repo.get_current("kb-B")
    assert current_b is not None
    assert current_b.kb_id == "kb-B"

    assert current_a.id != current_b.id


# ---------------------------------------------------------------------------
# Tests — review transition
# ---------------------------------------------------------------------------


def test_set_question_review_transition(eval_repo, session):
    """set_question_review mutates status, reviewed_by, reviewed_at."""
    _make_eval_set(eval_repo)
    session.flush()
    _make_question(eval_repo, review_status="unreviewed")
    session.flush()

    review_at = datetime(2026, 9, 5, 14, 0, 0, tzinfo=UTC)
    eval_repo.set_question_review(
        "q-001",
        status="reviewed_kept",
        reviewed_by="alice@example.com",
        at=review_at,
    )
    session.flush()

    # Re-fetch
    questions = eval_repo.get_questions("es-001")
    assert len(questions) == 1
    q = questions[0]
    assert q.review_status == "reviewed_kept"
    assert q.reviewed_by == "alice@example.com"
    # SQLite strips tzinfo; compare naive representation
    assert q.reviewed_at.replace(tzinfo=None) == review_at.replace(tzinfo=None)


def test_set_question_review_not_found(eval_repo, session):
    """set_question_review raises KeyError for a missing question."""
    with pytest.raises(KeyError, match="q-missing"):
        eval_repo.set_question_review(
            "q-missing",
            status="reviewed_kept",
            reviewed_by="alice",
            at=_NOW,
        )


# ---------------------------------------------------------------------------
# Tests — sweep run status
# ---------------------------------------------------------------------------


def test_sweep_run_set_status(sweep_repo, session):
    """set_status updates status, total_cost_usd, confirmed, declined_reason."""
    run = sweep_repo.create(kb_id="kb-A", workspace_id="ws-1", status="pending")
    session.flush()

    sweep_repo.set_status(
        run.id,
        status="completed",
        total_cost_usd=24.75,
        confirmed=True,
    )
    session.flush()

    fetched = sweep_repo.get(run.id)
    assert fetched is not None
    assert fetched.status == "completed"
    assert fetched.total_cost_usd == pytest.approx(24.75)
    assert fetched.confirmed is True
    assert fetched.declined_reason is None


def test_sweep_run_declined(sweep_repo, session):
    """declined_reason is stored when operator declines."""
    run = sweep_repo.create(kb_id="kb-A", workspace_id="ws-1", status="pending")
    session.flush()

    sweep_repo.set_status(
        run.id,
        status="declined",
        confirmed=False,
        declined_reason="Cost exceeds budget",
    )
    session.flush()

    fetched = sweep_repo.get(run.id)
    assert fetched is not None
    assert fetched.status == "declined"
    assert fetched.declined_reason == "Cost exceeds budget"


def test_sweep_run_list_for_kb(sweep_repo, session):
    """list_for_kb returns only that KB's runs."""
    sweep_repo.create(kb_id="kb-A", workspace_id="ws-1", status="pending")
    sweep_repo.create(kb_id="kb-A", workspace_id="ws-1", status="completed")
    sweep_repo.create(kb_id="kb-B", workspace_id="ws-1", status="pending")
    session.flush()

    kb_a_runs = sweep_repo.list_for_kb("kb-A")
    assert len(kb_a_runs) == 2
    assert all(r.kb_id == "kb-A" for r in kb_a_runs)

    kb_b_runs = sweep_repo.list_for_kb("kb-B")
    assert len(kb_b_runs) == 1
    assert kb_b_runs[0].kb_id == "kb-B"


# ---------------------------------------------------------------------------
# Tests — create_tables creates all five Phase 5 tables
# ---------------------------------------------------------------------------


def test_create_tables_creates_eval_tables(engine):
    """create_tables (SQLite path) creates all five Phase 5 eval tables."""
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    assert "eval_sets" in table_names
    assert "eval_questions" in table_names
    assert "eval_baselines" in table_names
    assert "sweep_runs" in table_names
    assert "sweep_ranking_rows" in table_names


def test_eval_baseline_list_for_kb(baseline_repo, eval_repo, session):
    """list_for_kb returns all baseline rows for a KB, newest first."""
    _make_eval_set(eval_repo)
    session.flush()

    t1 = datetime(2026, 9, 1, 0, 0, 0, tzinfo=UTC)
    t2 = datetime(2026, 9, 3, 0, 0, 0, tzinfo=UTC)
    t3 = datetime(2026, 9, 5, 0, 0, 0, tzinfo=UTC)

    # Write three baselines in sequence; each demotes the previous
    baseline_repo.upsert_current(
        kb_id="kb-A",
        reference_id="ref-1",
        reference_fingerprint="fp-1",
        eval_set_id="es-001",
        recall=0.70,
        precision=0.65,
        scored_at=t1,
    )
    session.flush()

    baseline_repo.upsert_current(
        kb_id="kb-A",
        reference_id="ref-2",
        reference_fingerprint="fp-2",
        eval_set_id="es-001",
        recall=0.75,
        precision=0.70,
        scored_at=t2,
    )
    session.flush()

    baseline_repo.upsert_current(
        kb_id="kb-A",
        reference_id="ref-3",
        reference_fingerprint="fp-3",
        eval_set_id="es-001",
        recall=0.80,
        precision=0.75,
        scored_at=t3,
    )
    session.flush()

    all_rows = baseline_repo.list_for_kb("kb-A")
    assert len(all_rows) == 3

    # Newest first — compare naive (SQLite strips tzinfo)
    def _naive(dt):
        return dt.replace(tzinfo=None)

    assert _naive(all_rows[0].scored_at) == _naive(t3)
    assert _naive(all_rows[1].scored_at) == _naive(t2)
    assert _naive(all_rows[2].scored_at) == _naive(t1)

    # Only the last one is current
    assert sum(1 for r in all_rows if r.is_current) == 1
    assert all_rows[0].is_current is True
