"""Tests for the IndexAdapter snapshot surface (§10.2, §17.1).

Covers:
- FakeAdapter: snapshot → mutate → restore-to-new-collection → original state present.
- FakeAdapter: list snapshots (sorted), delete snapshot.
- FakeAdapter: restore into existing collection raises IndexError.
- FakeAdapter: snapshot of non-existent collection raises CollectionNotFoundError.
- FakeAdapter: delete non-existent snapshot raises SnapshotError.
- BackendCapabilities.snapshots flag present and True.
- Qdrant integration test (gated; follows container-gated pattern from test_integration.py).
"""

from __future__ import annotations

import time

import pytest

from finecorpus.index.adapter import (
    BackendCapabilities,
    CollectionNotFoundError,
    SnapshotError,
    SnapshotRef,
)
from tests.retrieval.helpers import FakeAdapter

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

QDRANT_URL = "http://localhost:6333"


def _qdrant_reachable() -> bool:
    try:
        from qdrant_client import QdrantClient

        c = QdrantClient(url=QDRANT_URL, timeout=2)
        c.get_collections()
        return True
    except Exception:
        return False


qdrant_snapshot_mark = pytest.mark.skipif(
    not _qdrant_reachable(),
    reason="Qdrant not reachable at localhost:6333 — skipping snapshot integration tests",
)


def _seed_collection(adapter: FakeAdapter, coll: str, n_points: int = 3) -> list[dict]:
    """Seed a fake collection with n_points and return them."""
    points = [
        {
            "id": f"pt_{i}",
            "score": 1.0,
            "payload": {
                "chunk_id": f"chk_{i:03d}",
                "text": f"text chunk {i}",
                "provenance": {"source_document_id": f"doc_{i}", "source_document_version": "v1"},
                "tenancy": {"kb_id": "kb-test", "workspace_id": "ws-test"},
            },
        }
        for i in range(n_points)
    ]
    adapter.collections[coll] = {"points": list(points)}
    return points


# ---------------------------------------------------------------------------
# BackendCapabilities.snapshots flag
# ---------------------------------------------------------------------------


class TestBackendCapabilitiesSnapshotsFlag:
    def test_default_capabilities_has_snapshots_true(self) -> None:
        """Default BackendCapabilities.snapshots must be True (Phase 4)."""
        caps = BackendCapabilities()
        assert caps.snapshots is True

    def test_fake_adapter_capabilities_snapshots(self) -> None:
        """FakeAdapter.capabilities() must declare snapshots=True."""
        adapter = FakeAdapter()
        caps = adapter.capabilities()
        assert caps.snapshots is True


# ---------------------------------------------------------------------------
# FakeAdapter snapshot unit tests
# ---------------------------------------------------------------------------


class TestFakeAdapterSnapshotCreate:
    def test_snapshot_returns_ref(self) -> None:
        adapter = FakeAdapter()
        coll = "rtfc_test_00000001"
        _seed_collection(adapter, coll)
        ref = adapter.snapshot_collection(coll)
        assert isinstance(ref, SnapshotRef)
        assert ref.collection == coll
        assert ref.snapshot_id.startswith("snap_")
        assert ref.location.startswith("memory://")
        assert ref.created_at is not None

    def test_snapshot_nonexistent_collection_raises(self) -> None:
        adapter = FakeAdapter()
        with pytest.raises(CollectionNotFoundError):
            adapter.snapshot_collection("does_not_exist")

    def test_multiple_snapshots_same_collection(self) -> None:
        adapter = FakeAdapter()
        coll = "rtfc_test_00000001"
        _seed_collection(adapter, coll, n_points=2)
        ref1 = adapter.snapshot_collection(coll)
        ref2 = adapter.snapshot_collection(coll)
        assert ref1.snapshot_id != ref2.snapshot_id


class TestFakeAdapterSnapshotRestore:
    def test_restore_to_new_collection_has_original_data(self) -> None:
        """Snapshot → mutate original → restore into new → new has original state."""
        adapter = FakeAdapter()
        coll = "rtfc_test_00000001"
        original_points = _seed_collection(adapter, coll, n_points=3)

        # Take a snapshot of the original state
        ref = adapter.snapshot_collection(coll)

        # Mutate the original collection (simulate a document deletion)
        adapter.collections[coll]["points"] = adapter.collections[coll]["points"][:1]
        assert len(adapter.collections[coll]["points"]) == 1

        # Restore into a NEW collection
        new_coll = "rtfc_test_00000002"
        adapter.restore_snapshot(ref, new_coll)

        # The new collection must have the original 3 points
        assert new_coll in adapter.collections
        restored_points = adapter.collections[new_coll]["points"]
        assert len(restored_points) == 3
        restored_ids = {p["id"] for p in restored_points}
        assert restored_ids == {p["id"] for p in original_points}

    def test_restore_into_existing_collection_raises(self) -> None:
        """Restoring into an existing collection must raise IndexError."""
        adapter = FakeAdapter()
        coll = "rtfc_test_00000001"
        _seed_collection(adapter, coll)
        ref = adapter.snapshot_collection(coll)

        # Create a second collection that already exists
        existing = "rtfc_test_00000002"
        adapter.collections[existing] = {"points": []}

        from finecorpus.index.adapter import IndexError as AdapterIndexError

        with pytest.raises(AdapterIndexError, match="already exists"):
            adapter.restore_snapshot(ref, existing)

    def test_restore_invalid_snapshot_id_raises(self) -> None:
        """Restoring a ref with an unknown snapshot_id must raise SnapshotError."""
        adapter = FakeAdapter()
        phantom_ref = SnapshotRef(
            collection="rtfc_test_00000001",
            snapshot_id="snap_does_not_exist",
            created_at=None,
            location="memory://snap_does_not_exist",
        )
        with pytest.raises(SnapshotError):
            adapter.restore_snapshot(phantom_ref, "rtfc_test_00000002")

    def test_restore_is_deep_copy(self) -> None:
        """Mutating the restored collection must not affect the snapshot store."""
        adapter = FakeAdapter()
        coll = "rtfc_test_00000001"
        _seed_collection(adapter, coll, n_points=2)
        ref = adapter.snapshot_collection(coll)

        new_coll = "rtfc_test_00000002"
        adapter.restore_snapshot(ref, new_coll)

        # Mutate the restored collection
        adapter.collections[new_coll]["points"] = []

        # A second restore must still yield 2 points (snapshot data is independent)
        new_coll2 = "rtfc_test_00000003"
        adapter.restore_snapshot(ref, new_coll2)
        assert len(adapter.collections[new_coll2]["points"]) == 2


class TestFakeAdapterListSnapshots:
    def test_list_snapshots_empty(self) -> None:
        adapter = FakeAdapter()
        coll = "rtfc_test_00000001"
        _seed_collection(adapter, coll)
        assert adapter.list_snapshots(coll) == []

    def test_list_snapshots_returns_refs_for_collection(self) -> None:
        adapter = FakeAdapter()
        coll = "rtfc_test_00000001"
        other_coll = "rtfc_other_00000001"
        _seed_collection(adapter, coll, n_points=2)
        _seed_collection(adapter, other_coll, n_points=1)

        ref_a = adapter.snapshot_collection(coll)
        ref_b = adapter.snapshot_collection(other_coll)
        ref_c = adapter.snapshot_collection(coll)

        listed = adapter.list_snapshots(coll)
        listed_ids = {r.snapshot_id for r in listed}
        assert ref_a.snapshot_id in listed_ids
        assert ref_c.snapshot_id in listed_ids
        # The other-collection snapshot must NOT appear
        assert ref_b.snapshot_id not in listed_ids

    def test_list_snapshots_sorted_oldest_first(self) -> None:
        """list_snapshots must return entries sorted by created_at (oldest first)."""
        adapter = FakeAdapter()
        coll = "rtfc_test_00000001"
        _seed_collection(adapter, coll, n_points=2)

        ref1 = adapter.snapshot_collection(coll)
        time.sleep(0.01)  # ensure distinct timestamps
        ref2 = adapter.snapshot_collection(coll)

        listed = adapter.list_snapshots(coll)
        assert len(listed) == 2
        # oldest first: ref1 was created before ref2
        if ref1.created_at and ref2.created_at:
            assert listed[0].created_at <= listed[1].created_at

    def test_list_snapshots_nonexistent_collection_raises(self) -> None:
        adapter = FakeAdapter()
        with pytest.raises(CollectionNotFoundError):
            adapter.list_snapshots("does_not_exist")


class TestFakeAdapterDeleteSnapshot:
    def test_delete_snapshot_removes_from_list(self) -> None:
        adapter = FakeAdapter()
        coll = "rtfc_test_00000001"
        _seed_collection(adapter, coll)
        ref = adapter.snapshot_collection(coll)

        adapter.delete_snapshot(ref)
        assert adapter.list_snapshots(coll) == []

    def test_delete_snapshot_prevents_restore(self) -> None:
        adapter = FakeAdapter()
        coll = "rtfc_test_00000001"
        _seed_collection(adapter, coll)
        ref = adapter.snapshot_collection(coll)

        adapter.delete_snapshot(ref)
        with pytest.raises(SnapshotError):
            adapter.restore_snapshot(ref, "rtfc_test_00000002")

    def test_delete_nonexistent_snapshot_raises(self) -> None:
        adapter = FakeAdapter()
        phantom = SnapshotRef(
            collection="rtfc_x",
            snapshot_id="snap_ghost",
            created_at=None,
            location="memory://snap_ghost",
        )
        with pytest.raises(SnapshotError):
            adapter.delete_snapshot(phantom)


# ---------------------------------------------------------------------------
# Full round-trip: snapshot → mutate → restore → verify original in new collection
# ---------------------------------------------------------------------------


class TestFakeAdapterSnapshotRoundTrip:
    def test_full_round_trip(self) -> None:
        """Canonical snapshot scenario: take snapshot, mutate, restore, verify."""
        adapter = FakeAdapter()
        coll = "rtfc_kb_test_00000001"
        _seed_collection(adapter, coll, n_points=4)

        # Bind alias to the collection
        adapter.aliases["rtfc_kb_test"] = coll

        # 1. Take a snapshot (cold backup before doc deletion)
        ref = adapter.snapshot_collection(coll)
        assert ref.collection == coll

        # 2. Simulate a document deletion from the live collection
        adapter.collections[coll]["points"] = adapter.collections[coll]["points"][2:]
        assert len(adapter.collections[coll]["points"]) == 2

        # 3. Restore the snapshot into a new collection
        restored_coll = "rtfc_kb_test_00000001_restored"
        adapter.restore_snapshot(ref, restored_coll)

        # 4. New collection has the original 4 points (pre-deletion state)
        assert len(adapter.collections[restored_coll]["points"]) == 4

        # 5. Live collection still has the post-deletion 2 points
        assert len(adapter.collections[coll]["points"]) == 2

        # 6. list_snapshots returns the ref
        listed = adapter.list_snapshots(coll)
        assert any(r.snapshot_id == ref.snapshot_id for r in listed)

        # 7. delete the snapshot
        adapter.delete_snapshot(ref)
        assert adapter.list_snapshots(coll) == []


# ---------------------------------------------------------------------------
# Qdrant integration tests (gated — skipped unless Qdrant is reachable)
# ---------------------------------------------------------------------------


@qdrant_snapshot_mark
class TestQdrantSnapshotIntegration:
    """Integration tests for QdrantAdapter snapshot methods.

    Limitation documented: restore_snapshot uses recover_snapshot with
    file:// path — this works only when Qdrant runs locally with its default
    snapshot directory mounted.  In cloud/S3 deployments callers must
    provide an externally reachable URL.
    """

    @pytest.fixture(scope="class")
    def qdrant_adapter(self):
        from finecorpus.index.qdrant.backend import QdrantAdapter

        return QdrantAdapter(url=QDRANT_URL, timeout=15)

    def _make_point(self, adapter, coll: str, idx: int) -> dict:
        """Build a minimal point for upsert."""
        import uuid

        return {
            "id": uuid.uuid4(),
            "vector": [0.1 * (idx + 1)] * 4,
            "payload": {
                "chunk_id": f"chk_{idx:03d}",
                "text": f"text {idx}",
                "provenance": {
                    "source_document_id": f"doc_{idx}",
                    "source_document_version": "v1",
                },
                "tenancy": {"kb_id": "kb-snap-test", "workspace_id": "ws-snap-test"},
            },
        }

    def test_snapshot_and_list(self, qdrant_adapter) -> None:
        """Create a snapshot and verify it appears in list_snapshots."""
        import uuid

        kb = uuid.uuid4().hex[:8]
        coll = f"rtfc_{kb}_00000001"
        try:
            qdrant_adapter.create_collection(kb, 1, 4)
            qdrant_adapter.upsert_points(coll, [self._make_point(qdrant_adapter, coll, 0)])

            ref = qdrant_adapter.snapshot_collection(coll)
            assert ref.collection == coll
            assert ref.snapshot_id != ""
            assert ref.location.startswith("file://")

            listed = qdrant_adapter.list_snapshots(coll)
            snap_ids = [r.snapshot_id for r in listed]
            assert ref.snapshot_id in snap_ids

            # Cleanup snapshot
            qdrant_adapter.delete_snapshot(ref)
            listed_after = qdrant_adapter.list_snapshots(coll)
            assert ref.snapshot_id not in [r.snapshot_id for r in listed_after]
        finally:
            if qdrant_adapter.collection_exists(coll):
                qdrant_adapter.drop_collection(coll)

    def test_snapshot_nonexistent_collection(self, qdrant_adapter) -> None:
        with pytest.raises(CollectionNotFoundError):
            qdrant_adapter.snapshot_collection("rtfc_does_not_exist_00000001")

    def test_list_snapshots_nonexistent_collection(self, qdrant_adapter) -> None:
        with pytest.raises(CollectionNotFoundError):
            qdrant_adapter.list_snapshots("rtfc_does_not_exist_00000001")

    def test_restore_into_existing_collection_raises(self, qdrant_adapter) -> None:
        """restore_snapshot with an already-existing target raises IndexError."""
        import uuid

        from finecorpus.index.adapter import IndexError as AdapterIndexError

        kb = uuid.uuid4().hex[:8]
        coll = f"rtfc_{kb}_00000001"
        coll2 = f"rtfc_{kb}_00000002"
        try:
            qdrant_adapter.create_collection(kb, 1, 4)
            qdrant_adapter.create_collection(kb, 2, 4)
            ref = qdrant_adapter.snapshot_collection(coll)
            with pytest.raises(AdapterIndexError, match="already exists"):
                qdrant_adapter.restore_snapshot(ref, coll2)
        finally:
            for c in (coll, coll2):
                if qdrant_adapter.collection_exists(c):
                    qdrant_adapter.drop_collection(c)
            # Best-effort snapshot cleanup
            try:
                qdrant_adapter.delete_snapshot(ref)  # type: ignore[possibly-undefined]
            except Exception:
                pass
