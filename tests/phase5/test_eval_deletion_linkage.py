"""Phase 5 tests for M-086 eval-question deletion linkage.

Test inventory:
  test_eval_questions_removed_on_doc_deletion — M-086: delete a doc → its
    derived eval questions removed, count on report, audit row written;
    other docs' questions untouched.
  test_eval_questions_count_on_report — DeletionReport.eval_questions_removed
    carries the correct count.
  test_other_kb_questions_untouched — questions for a different KB are not
    removed when a doc is deleted in another KB.
  test_no_eval_questions_yields_zero_count — deletion of a doc with no
    eval questions yields eval_questions_removed=0.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from finecorpus.control.audit import AuditLogRecord
from finecorpus.control.eval_store import EvalSetRepository
from finecorpus.control.metadata import create_tables
from finecorpus.pipeline.deletion import delete_document
from tests.retrieval.helpers import FakeAdapter

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

KB_ID = "kb-deletion-test"
KB_ID_OTHER = "kb-other"
WS_ID = "ws-del-test"
DOC_A = "doc-a"
DOC_B = "doc-b"
CHK_A1 = "seg-a-chunk-1"
CHK_A2 = "seg-a-chunk-2"
CHK_B1 = "seg-b-chunk-1"
EVAL_SET_A = "es-del-001"
EVAL_SET_OTHER = "es-del-other"
_NOW = datetime(2026, 9, 6, 12, 0, 0, tzinfo=UTC)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def engine():
    eng = create_engine("sqlite://")
    create_tables(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def session(engine):
    with Session(engine) as s:
        yield s


def _seed_kb_with_eval(
    session: Session,
    *,
    kb_id: str = KB_ID,
    eval_set_id: str = EVAL_SET_A,
    doc_id: str = DOC_A,
    seg_ids: list[str] | None = None,
) -> None:
    """Seed an eval set with questions derived from doc_id's segments."""
    if seg_ids is None:
        seg_ids = [CHK_A1, CHK_A2]

    repo = EvalSetRepository(session)
    repo.create(
        eval_set_id=eval_set_id,
        kb_id=kb_id,
        workspace_id=WS_ID,
        schema_version="1.0.0",
        origin="generated",
        confidence_level="reviewed",
        baseline_ref=None,
        created_at=_NOW,
    )

    for i, seg_id in enumerate(seg_ids):
        repo.add_question(
            question_id=f"q-del-{eval_set_id}-{i:03d}",
            eval_set_id=eval_set_id,
            kb_id=kb_id,
            text=f"Question derived from segment {seg_id}",
            question_type="factual_lookup",
            generation_method="generated_factual",
            review_status="reviewed_kept",
            source_segment_ids=[seg_id],
            source_unknown=False,
            expected_segment_ids=[seg_id],
        )
    session.commit()


def _make_adapter_with_doc(
    *,
    kb_id: str = KB_ID,
    doc_id: str = DOC_A,
    seg_ids: list[str] | None = None,
) -> FakeAdapter:
    """Create a FakeAdapter with points for doc_id carrying seg_ids as chunk_ids."""
    from finecorpus.index.adapter import alias_name  # noqa: PLC0415

    if seg_ids is None:
        seg_ids = [CHK_A1, CHK_A2]

    adapter = FakeAdapter()
    alias = alias_name(kb_id)
    coll = f"rtfc_{kb_id.replace('-', '').lower()}_00000001"

    points = []
    for seg_id in seg_ids:
        points.append(
            {
                "id": seg_id,
                "score": 0.9,
                "payload": {
                    "chunk_id": seg_id,
                    "text": f"Chunk text for {seg_id}",
                    "tenancy": {"kb_id": kb_id, "workspace_id": WS_ID},
                    "provenance": {
                        "source_document_id": doc_id,
                        "segment_path": seg_id,
                    },
                },
            }
        )

    adapter.collections[coll] = {"points": points}
    adapter.aliases[alias] = coll

    # Register alias record so the AliasRepository mock returns it
    return adapter


def _patch_alias_none(session: Session):
    """Patch AliasRepository to return None (no live collection)."""
    from unittest.mock import MagicMock  # noqa: PLC0415

    mock_repo = MagicMock()
    mock_repo.get.return_value = None
    return patch("finecorpus.pipeline.deletion.AliasRepository", return_value=mock_repo)


# ---------------------------------------------------------------------------
# test_eval_questions_removed_on_doc_deletion (M-086)
# ---------------------------------------------------------------------------


class TestEvalQuestionsRemovedOnDocDeletion:
    """M-086: delete a doc → its derived eval questions removed."""

    def test_eval_questions_removed_on_doc_deletion(self, engine):
        with Session(engine) as session:
            # Seed eval questions derived from DOC_A's segments
            _seed_kb_with_eval(
                session, kb_id=KB_ID, eval_set_id=EVAL_SET_A, doc_id=DOC_A, seg_ids=[CHK_A1, CHK_A2]
            )

            # Seed eval questions for DOC_B — should NOT be removed
            _seed_kb_with_eval(
                session, kb_id=KB_ID, eval_set_id="es-del-002", doc_id=DOC_B, seg_ids=[CHK_B1]
            )

            adapter = _make_adapter_with_doc(kb_id=KB_ID, doc_id=DOC_A, seg_ids=[CHK_A1, CHK_A2])

            with _patch_alias_none(session):
                report = delete_document(
                    kb_id=KB_ID,
                    document_id=DOC_A,
                    purge=False,
                    session=session,
                    adapter=adapter,
                    deleted_by="test-actor",
                    reason="test deletion",
                    deleted_at=_NOW,
                )

            # Check eval_questions_removed on report
            assert isinstance(report.eval_questions_removed, int)
            assert report.eval_questions_removed >= 2  # CHK_A1 and CHK_A2 derived questions

            # Verify DOC_A's questions are gone
            eval_repo = EvalSetRepository(session)
            remaining_a = eval_repo.get_questions(EVAL_SET_A)
            assert len(remaining_a) == 0

            # Verify DOC_B's questions are still present
            remaining_b = eval_repo.get_questions("es-del-002")
            assert len(remaining_b) == 1

    def test_eval_questions_count_on_report(self, engine):
        """DeletionReport.eval_questions_removed reflects actual removed count."""
        with Session(engine) as session:
            _seed_kb_with_eval(
                session, kb_id=KB_ID, eval_set_id=EVAL_SET_A, doc_id=DOC_A, seg_ids=[CHK_A1, CHK_A2]
            )

            adapter = _make_adapter_with_doc(kb_id=KB_ID, doc_id=DOC_A, seg_ids=[CHK_A1, CHK_A2])

            with _patch_alias_none(session):
                report = delete_document(
                    kb_id=KB_ID,
                    document_id=DOC_A,
                    purge=False,
                    session=session,
                    adapter=adapter,
                    deleted_by="test-actor",
                    deleted_at=_NOW,
                )

            assert report.eval_questions_removed == 2

    def test_audit_row_includes_eval_questions_removed(self, engine):
        """The deletion audit row must include eval_questions_removed in details."""
        with Session(engine) as session:
            _seed_kb_with_eval(
                session, kb_id=KB_ID, eval_set_id=EVAL_SET_A, doc_id=DOC_A, seg_ids=[CHK_A1]
            )

            adapter = _make_adapter_with_doc(kb_id=KB_ID, doc_id=DOC_A, seg_ids=[CHK_A1])

            with _patch_alias_none(session):
                delete_document(
                    kb_id=KB_ID,
                    document_id=DOC_A,
                    purge=False,
                    session=session,
                    adapter=adapter,
                    deleted_by="test-actor",
                    deleted_at=_NOW,
                )

            # Find the deletion audit row
            from finecorpus.control.audit import AuditAction  # noqa: PLC0415

            audit_rows = list(
                session.execute(
                    select(AuditLogRecord).where(
                        AuditLogRecord.entry_type == str(AuditAction.deletion),
                        AuditLogRecord.target_kb_id == KB_ID,
                    )
                ).scalars()
            )
            assert len(audit_rows) >= 1
            audit = audit_rows[0]
            assert "eval_questions_removed" in audit.details
            assert audit.details["eval_questions_removed"] == 1

    def test_other_kb_questions_untouched(self, engine):
        """Questions in a different KB must not be removed."""
        with Session(engine) as session:
            # Seed KB_ID eval questions
            _seed_kb_with_eval(
                session, kb_id=KB_ID, eval_set_id=EVAL_SET_A, doc_id=DOC_A, seg_ids=[CHK_A1]
            )
            # Seed other KB eval questions with same chunk ID
            _seed_kb_with_eval(
                session,
                kb_id=KB_ID_OTHER,
                eval_set_id=EVAL_SET_OTHER,
                doc_id=DOC_A,
                seg_ids=[CHK_A1],
            )

            adapter = _make_adapter_with_doc(kb_id=KB_ID, doc_id=DOC_A, seg_ids=[CHK_A1])

            with _patch_alias_none(session):
                delete_document(
                    kb_id=KB_ID,
                    document_id=DOC_A,
                    purge=False,
                    session=session,
                    adapter=adapter,
                    deleted_by="test-actor",
                    deleted_at=_NOW,
                )

            eval_repo = EvalSetRepository(session)
            # KB_ID questions should be gone
            remaining_own = eval_repo.get_questions(EVAL_SET_A)
            assert len(remaining_own) == 0

            # Other KB questions should still be present
            remaining_other = eval_repo.get_questions(EVAL_SET_OTHER)
            assert len(remaining_other) == 1

    def test_no_eval_questions_yields_zero_count(self, engine):
        """Deletion of a doc with no eval questions yields eval_questions_removed=0."""
        with Session(engine) as session:
            # No eval questions seeded
            adapter = _make_adapter_with_doc(kb_id=KB_ID, doc_id=DOC_A, seg_ids=[CHK_A1])

            with _patch_alias_none(session):
                report = delete_document(
                    kb_id=KB_ID,
                    document_id=DOC_A,
                    purge=False,
                    session=session,
                    adapter=adapter,
                    deleted_by="test-actor",
                    deleted_at=_NOW,
                )

            assert report.eval_questions_removed == 0
