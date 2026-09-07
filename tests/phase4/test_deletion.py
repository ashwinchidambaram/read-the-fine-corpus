"""Phase 4 deletion tests — T-08 unit tier.

Tests:
- test_t08_deletion_completeness_unit: Full T-08 scenario with FakeAdapter.
  ingest → promote → re-ingest (N-1 exists) → snapshot → delete doc
  → doc absent from live AND N-1 → restore snapshot to shadow → replay ran
  → doc absent in restored shadow → promote → query returns empty for doc.
- test_tombstone_appended_before_index_mutation: Crash between steps leaves
  durable intent.
- test_purge_destroys_snapshots_d05: purge=True destroys all snapshots.
- test_delete_vs_purge_report_m089: M-089 summary distinction.
- test_restore_unreplayed_not_promotable_m087: Structural precondition probe.
- test_retention_sweep_deletes_aged_snapshots: snapshot_cold sweeps aged snapshots.
- test_orphan_scan_counts: scan_orphans returns correct orphan set.
"""

from __future__ import annotations

import pathlib
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from finecorpus.control.audit import AuditAction, AuditLogRepository
from finecorpus.control.metadata import AliasRepository, create_tables
from finecorpus.control.tombstone import TombstoneRepository
from finecorpus.index.adapter import (
    ModelIdentity,
    SnapshotRef,
    alias_name,
    collection_name,
)
from finecorpus.index.lifecycle import (
    _RESTORED_UNREPLAYED_MARKER_KEY,
    BuildContext,
    BuildState,
    RestoredUnreplayedError,
    SnapshotLifecycleError,
    create_shadow,
    promote,
    restore_from_snapshot,
    snapshot_cold,
)
from finecorpus.pipeline.deletion import (
    delete_document,
    scan_orphans,
)
from tests.retrieval.helpers import FakeAdapter, make_provenance_payload

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

KB_ID = "kb-00000000-0000-0000-0000-000000000001"
WS_ID = "ws-00000000-0000-0000-0000-000000000001"
DOC_ID = "doc-test-001"
DOC_ID_2 = "doc-test-002"


def _make_engine():
    """Create an in-memory SQLite engine with all Phase 4 tables."""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    create_tables(engine)
    return engine


def _make_session_factory(engine):
    return sessionmaker(bind=engine)


def _make_model_identity(build_id: int = 1) -> ModelIdentity:
    return ModelIdentity(
        provider="fake",
        model="fake-embed-v1",
        dimensions=4,
        config_version=f"cv-{build_id}",
    )


def _make_point(doc_id: str, chunk_suffix: str = "a") -> dict[str, Any]:
    """Build a minimal Qdrant point dict for testing."""
    return {
        "id": f"pt-{doc_id}-{chunk_suffix}",
        "score": 1.0,
        "payload": {
            "chunk_id": f"chk-{doc_id}-{chunk_suffix}",
            "text": f"text from {doc_id}",
            "tenancy": {"kb_id": KB_ID, "workspace_id": WS_ID},
            "provenance": make_provenance_payload(source_document_id=doc_id),
        },
    }


def _ingest_and_promote(
    adapter: FakeAdapter,
    session,
    kb_id: str,
    ws_id: str,
    build_id: int,
    doc_ids: list[str],
) -> BuildContext:
    """Helper: create a shadow collection, seed points, promote."""
    model = _make_model_identity(build_id)
    ctx = create_shadow(adapter, kb_id, ws_id, build_id, model)

    # Seed one point per doc
    points = [_make_point(doc_id) for doc_id in doc_ids]
    adapter.upsert_points(ctx.shadow_collection, points)

    promote(
        adapter=adapter,
        session=session,
        ctx=ctx,
        declared_empty=False,
        expected_min_chunks=1,
    )
    return ctx


# ---------------------------------------------------------------------------
# T-08: Full deletion completeness scenario
# ---------------------------------------------------------------------------


def test_t08_deletion_completeness_unit(tmp_path: pathlib.Path) -> None:
    """T-08 unit test: end-to-end deletion completeness with FakeAdapter.

    Scenario:
    1. Ingest build 1 (live) with doc-test-001 + doc-test-002.
    2. Promote build 1.
    3. Ingest build 2 (re-ingest) — N-1 = build 1, live = build 2.
    4. Promote build 2.
    5. Snapshot the live collection (build 2).
    6. delete_document(doc-test-001) — removes from live AND N-1.
    7. Assert doc-test-001 absent from live AND N-1 (direct scroll).
    8. restore_from_snapshot → shadow collection (build 3).
    9. Assert tombstone was replayed (doc-test-001 absent in restored shadow).
    10. Promote the restored shadow (build 3).
    11. Query via search — doc-test-001 not in results.
    """
    engine = _make_engine()
    SessionLocal = _make_session_factory(engine)
    adapter = FakeAdapter()

    with SessionLocal() as session:
        # --- Step 1+2: First build --- #
        ctx1 = _ingest_and_promote(
            adapter, session, KB_ID, WS_ID, build_id=1, doc_ids=[DOC_ID, DOC_ID_2]
        )
        live_coll_1 = ctx1.shadow_collection  # now live

        # --- Step 3+4: Second build (N-1 = build 1, live = build 2) --- #
        ctx2 = _ingest_and_promote(
            adapter, session, KB_ID, WS_ID, build_id=2, doc_ids=[DOC_ID, DOC_ID_2]
        )
        live_coll_2 = ctx2.shadow_collection  # now live
        # Verify N-1 is set
        alias_repo = AliasRepository(session)
        als = alias_name(KB_ID)
        record = alias_repo.get(als)
        assert record is not None
        assert record.collection_name == live_coll_2
        assert record.previous_collection == live_coll_1

        # --- Step 5: Snapshot live collection --- #
        snap_result = snapshot_cold(adapter, live_coll_2)
        snap_ref = snap_result.ref
        assert snap_ref.snapshot_id in adapter._snapshots

        # Pre-condition: doc exists in both live and N-1
        live_pts = adapter.collections[live_coll_2]["points"]
        n1_pts = adapter.collections[live_coll_1]["points"]
        assert any(p["payload"]["provenance"]["source_document_id"] == DOC_ID for p in live_pts), (
            "doc should be in live before delete"
        )
        assert any(p["payload"]["provenance"]["source_document_id"] == DOC_ID for p in n1_pts), (
            "doc should be in N-1 before delete"
        )

        # --- Step 6: delete_document --- #
        report = delete_document(
            kb_id=KB_ID,
            document_id=DOC_ID,
            purge=False,
            session=session,
            adapter=adapter,
            artifacts_root=tmp_path,
            deleted_by="test-actor",
        )

        # --- Step 7: Assert doc absent from live AND N-1 --- #
        live_pts = adapter.collections[live_coll_2]["points"]
        n1_pts = adapter.collections[live_coll_1]["points"]
        assert not any(
            p["payload"]["provenance"]["source_document_id"] == DOC_ID for p in live_pts
        ), "doc should be absent from live after delete"
        assert not any(
            p["payload"]["provenance"]["source_document_id"] == DOC_ID for p in n1_pts
        ), "doc should be absent from N-1 after delete"

        assert report.live_chunks_removed >= 1
        assert report.tombstone_entry_id != ""
        assert "deleted from service" in report.summary

        # DOC_ID_2 still present
        assert any(
            p["payload"]["provenance"]["source_document_id"] == DOC_ID_2 for p in live_pts
        ), "other doc should still be in live"

        # --- Step 8: restore_from_snapshot → shadow collection (build 3) --- #
        restored_coll = restore_from_snapshot(
            adapter=adapter,
            session=session,
            kb_id=KB_ID,
            ref=snap_ref,
            new_build_id=3,
        )
        assert restored_coll == collection_name(KB_ID, 3)
        assert adapter.collection_exists(restored_coll)

        # --- Step 9: doc-test-001 absent in restored shadow (replay ran) --- #
        restored_pts = adapter.collections[restored_coll]["points"]
        assert not any(
            p["payload"]["provenance"]["source_document_id"] == DOC_ID for p in restored_pts
        ), "doc should be absent in restored shadow after tombstone replay"

        # DOC_ID_2 still present in restored shadow
        assert any(
            p["payload"]["provenance"]["source_document_id"] == DOC_ID_2 for p in restored_pts
        ), "other doc should still be in restored shadow"

        # Verify unreplayed marker is cleared
        meta = adapter.get_collection_metadata(restored_coll)
        assert meta.get(_RESTORED_UNREPLAYED_MARKER_KEY) == "false"

        # --- Step 10: Promote the restored shadow (build 3) --- #
        model3 = _make_model_identity(3)
        ctx3 = BuildContext(
            kb_id=KB_ID,
            workspace_id=WS_ID,
            build_id=3,
            shadow_collection=restored_coll,
            alias=alias_name(KB_ID),
            model_identity=model3,
        )
        promote(
            adapter=adapter,
            session=session,
            ctx=ctx3,
            declared_empty=False,
            expected_min_chunks=1,
        )
        assert ctx3.state == BuildState.LIVE

        # --- Step 11: Query returns no results for doc-test-001 --- #
        als_str = alias_name(KB_ID)
        results = adapter.search(
            alias=als_str,
            query_vector=[0.0, 0.0, 0.0, 0.0],
            top_k=100,
            payload_filter={"provenance.source_document_id": DOC_ID},
        )
        assert len(results) == 0, "query for deleted doc must return no results"


# ---------------------------------------------------------------------------
# Tombstone appended before index mutation
# ---------------------------------------------------------------------------


def test_tombstone_appended_before_index_mutation() -> None:
    """Crash between tombstone append and index mutation leaves durable intent.

    Simulate by making delete_by_document raise after the tombstone is committed.
    Assert the tombstone exists in the DB even though the index mutation failed.
    """
    engine = _make_engine()
    SessionLocal = _make_session_factory(engine)
    adapter = FakeAdapter()

    with SessionLocal() as session:
        # Ingest + promote
        ctx = _ingest_and_promote(adapter, session, KB_ID, WS_ID, build_id=1, doc_ids=[DOC_ID])
        live_coll = ctx.shadow_collection

        # Verify the doc is there before
        assert adapter.count_points(live_coll) == 1

        # Patch delete_by_document to raise AFTER tombstone is committed
        original_dbd = adapter.delete_by_document
        call_count = {"n": 0}

        def _raising_dbd(collection: str, doc_id: str) -> int:
            call_count["n"] += 1
            raise RuntimeError("Simulated crash during index mutation")

        adapter.delete_by_document = _raising_dbd  # type: ignore[method-assign]

        from finecorpus.pipeline.deletion import DeletionError

        with pytest.raises(DeletionError, match="Index mutation failed"):
            delete_document(
                kb_id=KB_ID,
                document_id=DOC_ID,
                purge=False,
                session=session,
                adapter=adapter,
                deleted_by="test-actor",
            )

        # Tombstone MUST exist in DB (durable intent committed before the crash)
        tomb_repo = TombstoneRepository(session)
        entries = tomb_repo.list_for_kb(KB_ID)
        assert len(entries) == 1, "tombstone must be present after crash"
        assert entries[0].document_id == DOC_ID
        assert entries[0].kind == "delete"

        # Restore the real method
        adapter.delete_by_document = original_dbd  # type: ignore[method-assign]


# ---------------------------------------------------------------------------
# Purge destroys snapshots (D-05)
# ---------------------------------------------------------------------------


def test_purge_destroys_snapshots_d05() -> None:
    """purge=True destroys all snapshots for the KB's collections (D-05)."""
    engine = _make_engine()
    SessionLocal = _make_session_factory(engine)
    adapter = FakeAdapter()

    with SessionLocal() as session:
        ctx = _ingest_and_promote(adapter, session, KB_ID, WS_ID, build_id=1, doc_ids=[DOC_ID])
        live_coll = ctx.shadow_collection

        # Create two snapshots
        ref1 = adapter.snapshot_collection(live_coll)
        ref2 = adapter.snapshot_collection(live_coll)

        assert len(adapter._snapshots) == 2

        report = delete_document(
            kb_id=KB_ID,
            document_id=DOC_ID,
            purge=True,
            session=session,
            adapter=adapter,
            deleted_by="test-actor",
        )

        assert len(adapter._snapshots) == 0, "all snapshots should be destroyed by purge"
        assert len(report.snapshots_destroyed) == 2
        assert ref1.snapshot_id in report.snapshots_destroyed
        assert ref2.snapshot_id in report.snapshots_destroyed
        assert "purged from all copies" in report.summary

        # Tombstone kind = purge
        tomb_repo = TombstoneRepository(session)
        entries = tomb_repo.list_for_kb(KB_ID)
        assert entries[0].kind == "purge"
        assert set(entries[0].snapshots_destroyed) == {ref1.snapshot_id, ref2.snapshot_id}


# ---------------------------------------------------------------------------
# M-089 delete vs purge report distinction
# ---------------------------------------------------------------------------


def test_delete_vs_purge_report_m089() -> None:
    """DeletionReport.summary verbatim distinguishes delete from purge (M-089)."""
    engine = _make_engine()
    SessionLocal = _make_session_factory(engine)
    adapter = FakeAdapter()

    with SessionLocal() as session:
        _ingest_and_promote(adapter, session, KB_ID, WS_ID, build_id=1, doc_ids=[DOC_ID])

        delete_report = delete_document(
            kb_id=KB_ID,
            document_id=DOC_ID,
            purge=False,
            session=session,
            adapter=adapter,
            deleted_by="test-actor",
        )
        assert "deleted from service" in delete_report.summary
        assert "purged from all copies" not in delete_report.summary
        assert delete_report.purge is False

    # New engine + adapter for purge test
    engine2 = _make_engine()
    SessionLocal2 = _make_session_factory(engine2)
    adapter2 = FakeAdapter()
    with SessionLocal2() as session2:
        _ingest_and_promote(adapter2, session2, KB_ID, WS_ID, build_id=1, doc_ids=[DOC_ID])

        purge_report = delete_document(
            kb_id=KB_ID,
            document_id=DOC_ID,
            purge=True,
            session=session2,
            adapter=adapter2,
            deleted_by="test-actor",
        )
        assert "purged from all copies" in purge_report.summary
        assert "deleted from service" not in purge_report.summary
        assert purge_report.purge is True


# ---------------------------------------------------------------------------
# M-087: Restored collection not promotable until replay
# ---------------------------------------------------------------------------


def test_restore_unreplayed_not_promotable_m087() -> None:
    """A restored collection with unreplayed marker must not be promotable (M-087).

    We manually set the marker in metadata and verify promote() raises
    RestoredUnreplayedError.
    """
    engine = _make_engine()
    SessionLocal = _make_session_factory(engine)
    adapter = FakeAdapter()

    with SessionLocal() as session:
        _ingest_and_promote(adapter, session, KB_ID, WS_ID, build_id=1, doc_ids=[DOC_ID])

        # Create a "restored" collection by hand (simulate restore without replay)
        restored_coll = collection_name(KB_ID, 99)
        adapter.create_collection(KB_ID, 99, dimensions=4)
        # Set the unreplayed marker
        adapter.set_collection_metadata(restored_coll, {_RESTORED_UNREPLAYED_MARKER_KEY: "true"})

        model = _make_model_identity(99)
        ctx_restored = BuildContext(
            kb_id=KB_ID,
            workspace_id=WS_ID,
            build_id=99,
            shadow_collection=restored_coll,
            alias=alias_name(KB_ID),
            model_identity=model,
        )
        # Add a point so validation gate passes
        adapter.upsert_points(restored_coll, [_make_point(DOC_ID)])

        with pytest.raises(RestoredUnreplayedError, match="restored from a snapshot"):
            promote(
                adapter=adapter,
                session=session,
                ctx=ctx_restored,
                declared_empty=False,
                expected_min_chunks=1,
            )

        # After clearing the marker, promote should succeed
        adapter.set_collection_metadata(restored_coll, {_RESTORED_UNREPLAYED_MARKER_KEY: "false"})
        promote(
            adapter=adapter,
            session=session,
            ctx=ctx_restored,
            declared_empty=False,
            expected_min_chunks=1,
        )
        assert ctx_restored.state == BuildState.LIVE


# ---------------------------------------------------------------------------
# Retention sweep deletes aged snapshots
# ---------------------------------------------------------------------------


def test_retention_sweep_deletes_aged_snapshots() -> None:
    """snapshot_cold sweeps snapshots older than retention_period_days."""
    engine = _make_engine()
    SessionLocal = _make_session_factory(engine)
    adapter = FakeAdapter()

    with SessionLocal() as session:
        ctx = _ingest_and_promote(adapter, session, KB_ID, WS_ID, build_id=1, doc_ids=[DOC_ID])
        live_coll = ctx.shadow_collection

    # Create old snapshots manually with past timestamps
    past_time = datetime.now(tz=UTC) - timedelta(days=100)
    old_snap_id = "snap_old_001"
    old_ref = SnapshotRef(
        collection=live_coll,
        snapshot_id=old_snap_id,
        created_at=past_time,
        location=f"memory://{old_snap_id}",
    )
    adapter._snapshots[old_snap_id] = old_ref
    adapter._snapshot_data[old_snap_id] = {"points": []}

    recent_snap_id = "snap_recent_001"
    recent_time = datetime.now(tz=UTC) - timedelta(days=10)
    recent_ref = SnapshotRef(
        collection=live_coll,
        snapshot_id=recent_snap_id,
        created_at=recent_time,
        location=f"memory://{recent_snap_id}",
    )
    adapter._snapshots[recent_snap_id] = recent_ref
    adapter._snapshot_data[recent_snap_id] = {"points": []}

    assert len(adapter._snapshots) == 2

    # snapshot_cold with 90-day retention: old snap should be swept
    result = snapshot_cold(adapter, live_coll, retention_period_days=90)

    # New snapshot created
    assert result.ref.snapshot_id not in (old_snap_id, recent_snap_id)
    # Old snapshot swept
    assert old_snap_id in result.swept_snapshots
    assert old_snap_id not in adapter._snapshots
    # Recent snapshot retained
    assert recent_snap_id not in result.swept_snapshots
    assert recent_snap_id in adapter._snapshots
    # New snapshot retained
    assert result.ref.snapshot_id in adapter._snapshots

    # Total: new + recent = 2
    assert len(adapter._snapshots) == 2


# ---------------------------------------------------------------------------
# Orphan scan
# ---------------------------------------------------------------------------


def test_orphan_scan_counts() -> None:
    """scan_orphans returns document IDs in index that are not in known_document_ids."""
    engine = _make_engine()
    SessionLocal = _make_session_factory(engine)
    adapter = FakeAdapter()

    with SessionLocal() as session:
        ctx = _ingest_and_promote(
            adapter, session, KB_ID, WS_ID, build_id=1, doc_ids=[DOC_ID, DOC_ID_2]
        )
        live_coll = ctx.shadow_collection

    # Known docs: only DOC_ID_2 — DOC_ID is "orphan"
    known = {DOC_ID_2}
    orphans = scan_orphans(adapter, live_coll, known_document_ids=known)

    assert DOC_ID in orphans, "DOC_ID should be detected as orphan"
    assert DOC_ID_2 not in orphans, "DOC_ID_2 is in known_document_ids, not an orphan"

    # When all docs are known, no orphans
    all_known = {DOC_ID, DOC_ID_2}
    no_orphans = scan_orphans(adapter, live_coll, known_document_ids=all_known)
    assert no_orphans == []


# ---------------------------------------------------------------------------
# Audit log records correct action types
# ---------------------------------------------------------------------------


def test_audit_log_records_deletion_and_purge_actions() -> None:
    """Audit log appends deletion action for delete, purge action for purge."""
    engine = _make_engine()
    SessionLocal = _make_session_factory(engine)
    adapter = FakeAdapter()

    with SessionLocal() as session:
        _ingest_and_promote(adapter, session, KB_ID, WS_ID, build_id=1, doc_ids=[DOC_ID])

        delete_document(
            kb_id=KB_ID,
            document_id=DOC_ID,
            purge=False,
            session=session,
            adapter=adapter,
            deleted_by="test-actor",
        )

        audit_repo = AuditLogRepository(session)
        entries = audit_repo.list_for_kb(KB_ID)
        assert any(e.entry_type == str(AuditAction.deletion) for e in entries)

    engine2 = _make_engine()
    SessionLocal2 = _make_session_factory(engine2)
    adapter2 = FakeAdapter()
    with SessionLocal2() as session2:
        _ingest_and_promote(adapter2, session2, KB_ID, WS_ID, build_id=1, doc_ids=[DOC_ID])

        delete_document(
            kb_id=KB_ID,
            document_id=DOC_ID,
            purge=True,
            session=session2,
            adapter=adapter2,
            deleted_by="test-actor",
        )

        audit_repo2 = AuditLogRepository(session2)
        entries2 = audit_repo2.list_for_kb(KB_ID)
        assert any(e.entry_type == str(AuditAction.purge) for e in entries2)


# ---------------------------------------------------------------------------
# restore_from_snapshot: replay clears marker, doc absent
# ---------------------------------------------------------------------------


def test_restore_from_snapshot_replay_and_marker() -> None:
    """restore_from_snapshot replays tombstones and clears the unreplayed marker."""
    engine = _make_engine()
    SessionLocal = _make_session_factory(engine)
    adapter = FakeAdapter()

    with SessionLocal() as session:
        ctx = _ingest_and_promote(
            adapter, session, KB_ID, WS_ID, build_id=1, doc_ids=[DOC_ID, DOC_ID_2]
        )
        live_coll = ctx.shadow_collection

        # Snapshot the live collection (before delete)
        snap_ref = adapter.snapshot_collection(live_coll)

        # Delete DOC_ID — tombstone gets appended
        delete_document(
            kb_id=KB_ID,
            document_id=DOC_ID,
            purge=False,
            session=session,
            adapter=adapter,
            deleted_by="test-actor",
        )

        # Restore the snapshot (which still has DOC_ID in it)
        restored_coll = restore_from_snapshot(
            adapter=adapter,
            session=session,
            kb_id=KB_ID,
            ref=snap_ref,
            new_build_id=2,
        )

        # The restored collection must NOT have DOC_ID (replay ran)
        pts = adapter.collections[restored_coll]["points"]
        assert not any(p["payload"]["provenance"]["source_document_id"] == DOC_ID for p in pts), (
            "DOC_ID must be absent after tombstone replay"
        )
        assert any(p["payload"]["provenance"]["source_document_id"] == DOC_ID_2 for p in pts), (
            "DOC_ID_2 must still be present"
        )

        # Marker must be cleared (set to "false")
        meta = adapter.get_collection_metadata(restored_coll)
        assert meta.get(_RESTORED_UNREPLAYED_MARKER_KEY) == "false"

        # replay record must exist in tombstone_replays
        tomb_repo = TombstoneRepository(session)
        entries = tomb_repo.list_for_kb(KB_ID)
        assert len(entries) == 1
        # The tombstone should have a replay record for the restored collection
        unreplayed = tomb_repo.unreplayed_for(KB_ID, restored_coll)
        assert len(unreplayed) == 0, "all tombstones must be replayed"


def test_restore_fails_closed_when_marker_write_fails_m087(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MINOR-1 / M-087: if the unreplayed marker cannot be recorded after a
    restore, restore_from_snapshot must FAIL (raise) rather than warn-and-proceed.

    The marker is the only structural gate keeping a freshly-restored collection
    out of promote() until tombstones are replayed; a restore that cannot record
    it must not silently yield a promotable collection.
    """
    engine = _make_engine()
    SessionLocal = _make_session_factory(engine)
    adapter = FakeAdapter()

    with SessionLocal() as session:
        ctx = _ingest_and_promote(
            adapter, session, KB_ID, WS_ID, build_id=1, doc_ids=[DOC_ID, DOC_ID_2]
        )
        snap_ref = adapter.snapshot_collection(ctx.shadow_collection)

        # Make the unreplayed-marker write fail.  (restore_snapshot itself must
        # already have succeeded, so only fail set_collection_metadata.)
        def _boom(collection: str, metadata: dict[str, Any]) -> None:
            raise RuntimeError("simulated metadata backend failure")

        monkeypatch.setattr(adapter, "set_collection_metadata", _boom, raising=True)

        with pytest.raises(SnapshotLifecycleError, match="unreplayed marker"):
            restore_from_snapshot(
                adapter=adapter,
                session=session,
                kb_id=KB_ID,
                ref=snap_ref,
                new_build_id=2,
            )
