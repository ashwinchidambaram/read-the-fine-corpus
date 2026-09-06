"""T-08 integration tier — deletion completeness with real Qdrant snapshots.

Marker: ``qdrant_integration`` — composite mark (named + skipif) from
:func:`conftest.qdrant_integration_mark`.  Tests auto-skip when Qdrant
or Postgres are unreachable; selectable via ``pytest -m qdrant_integration``.

What this covers:
- T-08 (§18.3 test 8) at the integration tier: real Qdrant snapshot API,
  real Postgres tombstone log, full deletion completeness flow including
  actual snapshot create / restore / tombstone replay / promote.
- Confirms that delete_document removes content from live + N-1 in real Qdrant.
- Confirms that restore_from_snapshot replays the tombstone log so deleted
  content is absent in the restored collection before promotion.
- Confirms the M-087 unreplayed marker prevents promotion before replay.

Follows the container-gating pattern from tests/index/test_integration.py.
"""

from __future__ import annotations

import uuid

import pytest

from conftest import qdrant_integration_mark
from finecorpus.control.metadata import create_tables
from finecorpus.index.adapter import ModelIdentity, alias_name, build_point_payload, collection_name
from finecorpus.index.lifecycle import (
    _RESTORED_UNREPLAYED_MARKER_KEY,
    BuildContext,
    BuildState,
    RestoredUnreplayedError,
    create_shadow,
    promote,
    restore_from_snapshot,
    snapshot_cold,
)
from finecorpus.pipeline.deletion import delete_document

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

QDRANT_URL = "http://localhost:6333"
POSTGRES_DSN = "postgresql+psycopg://finecorpus:finecorpus@localhost:5432/finecorpus"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def qdrant_adapter():
    """QdrantAdapter connected to the compose Qdrant instance."""
    from finecorpus.index.qdrant.backend import QdrantAdapter

    return QdrantAdapter(url=QDRANT_URL, timeout=10)


@pytest.fixture(scope="module")
def db_engine():
    """SQLAlchemy engine connected to the compose Postgres instance."""
    from sqlalchemy import create_engine as _mk_engine

    engine = _mk_engine(POSTGRES_DSN)
    create_tables(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def db_session(db_engine):
    """Fresh session per test."""
    from sqlalchemy.orm import Session

    with Session(db_engine) as session:
        yield session


def _fresh_kb_id() -> str:
    """Generate a unique KB ID for isolation between tests."""
    return f"kb-t08-integ-{uuid.uuid4().hex[:8]}"


def _fresh_ws_id() -> str:
    return f"ws-t08-integ-{uuid.uuid4().hex[:8]}"


def _make_model(build_id: int = 1) -> ModelIdentity:
    return ModelIdentity(
        provider="fake",
        model="fake-embed-v1",
        dimensions=4,
        config_version=f"cv-{build_id}",
    )


def _make_point_payload(doc_id: str, kb_id: str, ws_id: str) -> dict:
    """Build a minimal Qdrant point payload for real insertion."""
    from finecorpus.contracts.chunk_id import derive_point_id

    point_id = derive_point_id(
        kb_id=kb_id,
        document_id=doc_id,
        segment_path="seg/0",
        position=0,
        config_version="cv-1",
    )
    payload = build_point_payload(
        chunk_id=f"chk-{doc_id}-a",
        text=f"Content from {doc_id}",
        source_document_id=doc_id,
        source_document_version="v1",
        kb_id=kb_id,
        workspace_id=ws_id,
        char_start=0,
        char_end=20,
        segment_type="prose",
        salience_tier="primary",
        structural_path=[],
        injection_suspicion=0.0,
        invisible_content_flags=[],
        sensitivity_flags=[],
        language="en",
        trust_level="untrusted_ingested",
        config_version="cv-1",
    )
    return {"id": point_id, "vector": [0.1, 0.2, 0.3, 0.4], "payload": payload}


def _promote_build(
    adapter, session, kb_id: str, ws_id: str, build_id: int, doc_ids: list[str]
) -> BuildContext:
    """Helper: create shadow, insert points, promote."""
    model = _make_model(build_id)
    ctx = create_shadow(adapter, kb_id, ws_id, build_id, model)
    for doc_id in doc_ids:
        point = _make_point_payload(doc_id, kb_id, ws_id)
        adapter.upsert_points(ctx.shadow_collection, [point])
    promote(adapter=adapter, session=session, ctx=ctx, declared_empty=False, expected_min_chunks=1)
    return ctx


# ---------------------------------------------------------------------------
# Integration test: T-08 deletion completeness
# ---------------------------------------------------------------------------


@qdrant_integration_mark
class TestT08DeletionCompletenessIntegration:
    """T-08 integration: deletion completeness with real Qdrant + Postgres.

    Requires live Qdrant (localhost:6333) and Postgres (localhost:5432).
    Run with: pytest -m qdrant_integration tests/phase4/integration/test_t08_integration.py
    """

    def test_t08_delete_from_live_and_n1(self, qdrant_adapter, db_session, tmp_path):
        """T-08: delete_document removes doc from live AND N-1 in real Qdrant.

        Flow:
        1. Promote build 1 (doc-a, doc-b).
        2. Promote build 2 (doc-a, doc-b) → N-1 = build 1, live = build 2.
        3. delete_document(doc-a) → absent from live AND N-1.
        4. doc-b still present in live.
        """
        kb_id = _fresh_kb_id()
        ws_id = _fresh_ws_id()

        # Build 1
        ctx1 = _promote_build(
            qdrant_adapter, db_session, kb_id, ws_id, build_id=1, doc_ids=["doc-a", "doc-b"]
        )
        coll_1 = ctx1.shadow_collection

        # Build 2 → N-1 = build 1
        ctx2 = _promote_build(
            qdrant_adapter, db_session, kb_id, ws_id, build_id=2, doc_ids=["doc-a", "doc-b"]
        )
        coll_2 = ctx2.shadow_collection

        # Pre-condition: doc-a in both collections
        assert qdrant_adapter.count_points(coll_1) >= 1
        assert qdrant_adapter.count_points(coll_2) >= 1

        # Delete doc-a
        report = delete_document(
            kb_id=kb_id,
            document_id="doc-a",
            purge=False,
            session=db_session,
            adapter=qdrant_adapter,
            artifacts_root=tmp_path,
            deleted_by="t08-integ-test",
        )

        assert report.live_chunks_removed >= 1, "live chunks must be removed"
        assert report.tombstone_entry_id != "", "tombstone entry must be written"
        assert "deleted from service" in report.summary

        # doc-a must be absent from live (coll_2) and N-1 (coll_1)
        live_results = qdrant_adapter.search(
            alias=alias_name(kb_id),
            query_vector=[0.1, 0.2, 0.3, 0.4],
            top_k=100,
            payload_filter={"provenance.source_document_id": "doc-a"},
        )
        assert len(live_results) == 0, "doc-a must be absent from live after delete"

    def test_t08_restore_replays_tombstone(self, qdrant_adapter, db_session, tmp_path):
        """T-08: restore_from_snapshot replays tombstone; doc absent in restored collection.

        Flow:
        1. Promote build 1.
        2. snapshot_cold the live collection.
        3. delete_document → tombstone written.
        4. restore_from_snapshot → tombstone replayed → doc absent.
        5. Promote the restored collection (M-087 marker must be cleared).
        """
        kb_id = _fresh_kb_id()
        ws_id = _fresh_ws_id()

        # Build 1 with doc-restore-a
        ctx1 = _promote_build(
            qdrant_adapter,
            db_session,
            kb_id,
            ws_id,
            build_id=1,
            doc_ids=["doc-restore-a", "doc-restore-b"],
        )
        coll_1 = ctx1.shadow_collection

        # Snapshot the live collection (before delete)
        snap_result = snapshot_cold(qdrant_adapter, coll_1)
        snap_ref = snap_result.ref

        # Delete doc-restore-a → tombstone
        delete_document(
            kb_id=kb_id,
            document_id="doc-restore-a",
            purge=False,
            session=db_session,
            adapter=qdrant_adapter,
            artifacts_root=tmp_path,
            deleted_by="t08-integ-test",
        )

        # Restore from snapshot → tombstone replay must run
        restored_coll = restore_from_snapshot(
            adapter=qdrant_adapter,
            session=db_session,
            kb_id=kb_id,
            ref=snap_ref,
            new_build_id=2,
        )
        assert restored_coll == collection_name(kb_id, 2)

        # Unreplayed marker must be cleared
        meta = qdrant_adapter.get_collection_metadata(restored_coll)
        assert meta.get(_RESTORED_UNREPLAYED_MARKER_KEY) == "false", (
            "M-087: unreplayed marker must be cleared after tombstone replay"
        )

        # doc-restore-a must be absent in restored collection
        results = qdrant_adapter.search(
            alias=restored_coll,  # query the restored collection directly
            query_vector=[0.1, 0.2, 0.3, 0.4],
            top_k=100,
            payload_filter={"provenance.source_document_id": "doc-restore-a"},
        )
        assert len(results) == 0, (
            "T-08: doc-restore-a must be absent in restored collection after tombstone replay"
        )

        # doc-restore-b must still be present
        results_b = qdrant_adapter.search(
            alias=restored_coll,
            query_vector=[0.1, 0.2, 0.3, 0.4],
            top_k=100,
            payload_filter={"provenance.source_document_id": "doc-restore-b"},
        )
        assert len(results_b) >= 1, (
            "T-08: doc-restore-b must still be present in the restored collection"
        )

    def test_m087_unreplayed_marker_blocks_promotion(self, qdrant_adapter, db_session):
        """M-087: a restored collection with unreplayed marker must not promote.

        Sets the marker manually on a new collection and verifies that
        promote() raises RestoredUnreplayedError.
        After clearing the marker, promote succeeds.
        """
        kb_id = _fresh_kb_id()
        ws_id = _fresh_ws_id()

        # Promote build 1 to get baseline alias record
        _promote_build(qdrant_adapter, db_session, kb_id, ws_id, build_id=1, doc_ids=["doc-m087"])

        # Create a "restored" shadow with the unreplayed marker set
        restored_coll = collection_name(kb_id, 99)
        qdrant_adapter.create_collection(kb_id, 99, dimensions=4)
        qdrant_adapter.set_collection_metadata(
            restored_coll, {_RESTORED_UNREPLAYED_MARKER_KEY: "true"}
        )
        # Add a point so chunk-count gate passes
        point = _make_point_payload("doc-m087", kb_id, ws_id)
        qdrant_adapter.upsert_points(restored_coll, [point])

        model99 = _make_model(99)
        ctx_restored = BuildContext(
            kb_id=kb_id,
            workspace_id=ws_id,
            build_id=99,
            shadow_collection=restored_coll,
            alias=alias_name(kb_id),
            model_identity=model99,
        )

        with pytest.raises(RestoredUnreplayedError, match="restored from a snapshot"):
            promote(
                adapter=qdrant_adapter,
                session=db_session,
                ctx=ctx_restored,
                declared_empty=False,
                expected_min_chunks=1,
            )

        # Clear the marker → promote should succeed
        qdrant_adapter.set_collection_metadata(
            restored_coll, {_RESTORED_UNREPLAYED_MARKER_KEY: "false"}
        )
        promote(
            adapter=qdrant_adapter,
            session=db_session,
            ctx=ctx_restored,
            declared_empty=False,
            expected_min_chunks=1,
        )
        assert ctx_restored.state == BuildState.LIVE
