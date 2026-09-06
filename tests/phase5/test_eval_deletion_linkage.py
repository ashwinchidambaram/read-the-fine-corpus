"""Phase 5 tests for M-086 eval-question deletion linkage.

These tests exercise the PRODUCTION deletion-linkage path: the document's real
chunk/segment IDs are collected from the index via a payload-filtered search
(``collect_document_segment_ids``) BEFORE ``delete_by_document`` removes the
points, and eval questions whose ``source_segment_ids`` intersect that set are
removed.  The same code path runs for the real QdrantAdapter and the test
FakeAdapter — there is deliberately no FakeAdapter-only ``.collections`` shortcut.

Because the collection is resolved through a real ``AliasRecord`` (exactly as in
production), every test seeds a genuine alias record rather than mocking the
repository.  A dedicated test asserts the honest degraded behaviour when no
alias record exists (no collection → nothing to resolve → nothing removed).

Test inventory:
  test_eval_questions_removed_via_production_path — M-086 core: removal happens
    through the search-based collection path (this FAILS against the old
    ``.collections``-only code once the alias resolves the collection).
  test_eval_questions_removed_on_doc_deletion — sibling doc's questions untouched.
  test_eval_questions_count_on_report — DeletionReport.eval_questions_removed count.
  test_audit_row_includes_eval_questions_removed — audit row carries the count.
  test_other_kb_questions_untouched — cross-KB isolation.
  test_no_eval_questions_yields_zero_count — no questions → zero.
  test_no_alias_record_removes_nothing — honest degraded path when unresolvable.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from finecorpus.control.audit import AuditLogRecord
from finecorpus.control.eval_store import EvalSetRepository
from finecorpus.control.metadata import AliasRepository, create_tables
from finecorpus.index.adapter import alias_name
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


def _coll_name(kb_id: str) -> str:
    return f"rtfc_{kb_id.replace('-', '').lower()}_00000001"


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


def _seed_alias(
    session: Session,
    *,
    kb_id: str = KB_ID,
    coll: str | None = None,
) -> str:
    """Seed a real promoted AliasRecord so the live collection resolves.

    This mirrors production: the deletion path reads ``AliasRecord.collection_name``
    to find the live collection and then searches it for the document's segments.
    """
    if coll is None:
        coll = _coll_name(kb_id)
    repo = AliasRepository(session)
    repo.create(alias=alias_name(kb_id), kb_id=kb_id, workspace_id=WS_ID)
    repo.promote(
        alias=alias_name(kb_id),
        new_collection=coll,
        new_build_id=1,
        embedding_provider="fake",
        embedding_model="fake-embed",
        embedding_dimensions=64,
        config_version="cfg-000",
        promoted_at=_NOW,
    )
    session.commit()
    return coll


def _make_adapter_with_doc(
    *,
    kb_id: str = KB_ID,
    doc_id: str = DOC_A,
    seg_ids: list[str] | None = None,
) -> FakeAdapter:
    """Create a FakeAdapter with points for doc_id carrying seg_ids as chunk_ids.

    The collection name matches ``_coll_name(kb_id)`` so it lines up with the
    promoted AliasRecord seeded by ``_seed_alias``.
    """
    if seg_ids is None:
        seg_ids = [CHK_A1, CHK_A2]

    adapter = FakeAdapter()
    alias = alias_name(kb_id)
    coll = _coll_name(kb_id)

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
    return adapter


# ---------------------------------------------------------------------------
# M-086 core: removal via the production search path
# ---------------------------------------------------------------------------


class TestEvalQuestionsRemovedOnDocDeletion:
    """M-086: delete a doc → its derived eval questions removed."""

    def test_eval_questions_removed_via_production_path(self, engine):
        """Removal must flow through the search-based collection path.

        The alias resolves the live collection; ``collect_document_segment_ids``
        searches it for DOC_A's real chunk IDs (CHK_A1/CHK_A2) BEFORE the points
        are deleted, and the eval store removes the matching questions.  This is
        the exact production path — no FakeAdapter-only ``.collections`` iteration.
        """
        with Session(engine) as session:
            _seed_kb_with_eval(
                session, kb_id=KB_ID, eval_set_id=EVAL_SET_A, doc_id=DOC_A, seg_ids=[CHK_A1, CHK_A2]
            )
            _seed_alias(session, kb_id=KB_ID)
            adapter = _make_adapter_with_doc(kb_id=KB_ID, doc_id=DOC_A, seg_ids=[CHK_A1, CHK_A2])

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

            assert report.eval_questions_removed == 2
            eval_repo = EvalSetRepository(session)
            assert len(eval_repo.get_questions(EVAL_SET_A)) == 0

    def test_eval_questions_removed_on_doc_deletion(self, engine):
        with Session(engine) as session:
            _seed_kb_with_eval(
                session, kb_id=KB_ID, eval_set_id=EVAL_SET_A, doc_id=DOC_A, seg_ids=[CHK_A1, CHK_A2]
            )
            # Sibling doc's questions — must NOT be removed
            _seed_kb_with_eval(
                session, kb_id=KB_ID, eval_set_id="es-del-002", doc_id=DOC_B, seg_ids=[CHK_B1]
            )
            _seed_alias(session, kb_id=KB_ID)
            adapter = _make_adapter_with_doc(kb_id=KB_ID, doc_id=DOC_A, seg_ids=[CHK_A1, CHK_A2])

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

            assert isinstance(report.eval_questions_removed, int)
            assert report.eval_questions_removed == 2

            eval_repo = EvalSetRepository(session)
            assert len(eval_repo.get_questions(EVAL_SET_A)) == 0
            # Sibling doc's question survives (its segment was not in the deleted doc).
            assert len(eval_repo.get_questions("es-del-002")) == 1

    def test_eval_questions_count_on_report(self, engine):
        """DeletionReport.eval_questions_removed reflects actual removed count."""
        with Session(engine) as session:
            _seed_kb_with_eval(
                session, kb_id=KB_ID, eval_set_id=EVAL_SET_A, doc_id=DOC_A, seg_ids=[CHK_A1, CHK_A2]
            )
            _seed_alias(session, kb_id=KB_ID)
            adapter = _make_adapter_with_doc(kb_id=KB_ID, doc_id=DOC_A, seg_ids=[CHK_A1, CHK_A2])

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
            _seed_alias(session, kb_id=KB_ID)
            adapter = _make_adapter_with_doc(kb_id=KB_ID, doc_id=DOC_A, seg_ids=[CHK_A1])

            delete_document(
                kb_id=KB_ID,
                document_id=DOC_A,
                purge=False,
                session=session,
                adapter=adapter,
                deleted_by="test-actor",
                deleted_at=_NOW,
            )

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
            _seed_kb_with_eval(
                session, kb_id=KB_ID, eval_set_id=EVAL_SET_A, doc_id=DOC_A, seg_ids=[CHK_A1]
            )
            # Other KB, same chunk ID — the KB scope must protect it.
            _seed_kb_with_eval(
                session,
                kb_id=KB_ID_OTHER,
                eval_set_id=EVAL_SET_OTHER,
                doc_id=DOC_A,
                seg_ids=[CHK_A1],
            )
            _seed_alias(session, kb_id=KB_ID)
            adapter = _make_adapter_with_doc(kb_id=KB_ID, doc_id=DOC_A, seg_ids=[CHK_A1])

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
            assert len(eval_repo.get_questions(EVAL_SET_A)) == 0
            assert len(eval_repo.get_questions(EVAL_SET_OTHER)) == 1

    def test_no_eval_questions_yields_zero_count(self, engine):
        """Deletion of a doc with no eval questions yields eval_questions_removed=0."""
        with Session(engine) as session:
            _seed_alias(session, kb_id=KB_ID)
            adapter = _make_adapter_with_doc(kb_id=KB_ID, doc_id=DOC_A, seg_ids=[CHK_A1])

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

    def test_no_alias_record_removes_nothing(self, engine):
        """Honest degraded path: with no alias record the live collection cannot
        be resolved, so no segment IDs are collected and nothing is removed.

        This documents the boundary — M-086 linkage depends on the index being
        resolvable; it does not silently claim success when it is not.
        """
        with Session(engine) as session:
            _seed_kb_with_eval(
                session, kb_id=KB_ID, eval_set_id=EVAL_SET_A, doc_id=DOC_A, seg_ids=[CHK_A1, CHK_A2]
            )
            # No _seed_alias — AliasRepository.get returns None.
            adapter = _make_adapter_with_doc(kb_id=KB_ID, doc_id=DOC_A, seg_ids=[CHK_A1, CHK_A2])

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
            eval_repo = EvalSetRepository(session)
            assert len(eval_repo.get_questions(EVAL_SET_A)) == 2
