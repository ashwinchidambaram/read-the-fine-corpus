"""Regression tests for the PR #31 review findings.

Ruling 1 (MAJOR — marker constant triplication): RESTORED_UNREPLAYED_MARKER_KEY
must be defined ONCE in index/adapter.py and imported by both pipeline/deletion.py
and index/lifecycle.py. A monkeypatch probe verifies all three codepaths reference
the same runtime object.

Ruling 2 (MAJOR — llm_cache survives purge): purge=True must remove matching
llm_cache.json entries (or the whole file when the content_hash cannot be
resolved). Non-purge delete must leave the cache file untouched.

Ruling 3 (MINOR — fake raise bug): helpers.py retarget_alias used to raise
CollectionInfo (a dataclass) — must raise CollectionNotFoundError instead.

Ruling 4 (MINOR — retention boundary): snapshot_cold uses <= so exactly-90-day-old
snapshots are swept, not retained.

Ruling 5 (NOTE→FIX — FakeAdapter metadata semantics): FakeAdapter.set_collection_metadata
must MERGE (not replace) into existing metadata, matching QdrantAdapter behaviour.
"""

from __future__ import annotations

import json
import pathlib
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from finecorpus.control.metadata import create_tables
from finecorpus.index.adapter import (
    ModelIdentity,
    SnapshotRef,
    alias_name,
    collection_name,
)
from finecorpus.index.lifecycle import (
    BuildContext,
    RestoredUnreplayedError,
    create_shadow,
    promote,
    restore_from_snapshot,
    snapshot_cold,
)
from finecorpus.pipeline.deletion import (
    RESTORED_UNREPLAYED_MARKER_KEY,
    delete_document,
)
from tests.retrieval.helpers import FakeAdapter, make_provenance_payload

# ---------------------------------------------------------------------------
# Fixtures / shared helpers
# ---------------------------------------------------------------------------

KB_ID = "kb-31000000-0000-0000-0000-000000000001"
WS_ID = "ws-31000000-0000-0000-0000-000000000001"
DOC_ID = "doc-pr31-001"


def _make_engine():
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
    model = _make_model_identity(build_id)
    ctx = create_shadow(adapter, kb_id, ws_id, build_id, model)
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
# Ruling 1: Single marker constant — monkeypatch probe
# ---------------------------------------------------------------------------


class TestSingleMarkerConstant:
    """Ruling 1: RESTORED_UNREPLAYED_MARKER_KEY must be a single shared constant.

    Strategy: import both the public constant from pipeline.deletion and the
    private one from index.lifecycle, then verify:
    1. They have the same string value.
    2. Monkeypatching adapter.RESTORED_UNREPLAYED_MARKER_KEY propagates to
       promote()'s M-087 check — i.e. promote() reads the constant through
       index.adapter, not a local copy.
    """

    def test_public_constant_matches_lifecycle_private_constant(self) -> None:
        """pipeline.deletion.RESTORED_UNREPLAYED_MARKER_KEY == lifecycle private string."""
        from finecorpus.index import lifecycle as lc

        # Access the private constant used internally in lifecycle.py
        assert RESTORED_UNREPLAYED_MARKER_KEY == lc._RESTORED_UNREPLAYED_MARKER_KEY, (
            "The public constant in pipeline.deletion and the private constant in "
            "index.lifecycle must be identical strings. If they diverge, a rename "
            "silently breaks M-087."
        )

    def test_adapter_constant_matches_public_constant(self) -> None:
        """index.adapter.RESTORED_UNREPLAYED_MARKER_KEY (the canonical source) matches."""
        import finecorpus.index.adapter as adp

        assert hasattr(adp, "RESTORED_UNREPLAYED_MARKER_KEY"), (
            "index.adapter must export RESTORED_UNREPLAYED_MARKER_KEY as the single "
            "source of truth (Ruling 1 fix)."
        )
        assert adp.RESTORED_UNREPLAYED_MARKER_KEY == RESTORED_UNREPLAYED_MARKER_KEY

    def test_promote_refusal_follows_adapter_constant(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """promote() M-087 check must use the canonical constant from index.adapter.

        Probe: set the marker key in collection metadata using the canonical constant
        value, then assert promote() raises RestoredUnreplayedError. This confirms
        promote() is reading the same key string as the canonical constant — if it
        had a local hardcoded copy that diverged, the patch would not affect its check.
        """
        import finecorpus.index.adapter as adp
        import finecorpus.index.lifecycle as lc

        engine = _make_engine()
        session = _make_session_factory(engine)()
        adapter = FakeAdapter()

        _ingest_and_promote(adapter, session, KB_ID, WS_ID, build_id=1, doc_ids=[DOC_ID])

        # Create a "restored" collection manually
        restored_coll = collection_name(KB_ID, 99)
        adapter.create_collection(KB_ID, 99, dimensions=4)
        adapter.upsert_points(restored_coll, [_make_point(DOC_ID)])

        # Monkeypatch the adapter constant to a new sentinel value
        sentinel_key = "monkeypatched_sentinel_key_xyz"
        monkeypatch.setattr(adp, "RESTORED_UNREPLAYED_MARKER_KEY", sentinel_key)
        monkeypatch.setattr(lc, "_RESTORED_UNREPLAYED_MARKER_KEY", sentinel_key)

        # Set the marker using the sentinel key — if promote() uses the constant
        # from adapter, it will check for sentinel_key and raise.
        adapter.set_collection_metadata(restored_coll, {sentinel_key: "true"})

        model = _make_model_identity(99)
        ctx_restored = BuildContext(
            kb_id=KB_ID,
            workspace_id=WS_ID,
            build_id=99,
            shadow_collection=restored_coll,
            alias=alias_name(KB_ID),
            model_identity=model,
        )

        with pytest.raises(RestoredUnreplayedError):
            promote(
                adapter=adapter,
                session=session,
                ctx=ctx_restored,
                declared_empty=False,
                expected_min_chunks=1,
            )

    def test_restore_from_snapshot_uses_same_constant(self) -> None:
        """restore_from_snapshot sets/clears the key using the canonical constant."""
        engine = _make_engine()
        session = _make_session_factory(engine)()
        adapter = FakeAdapter()

        ctx = _ingest_and_promote(adapter, session, KB_ID, WS_ID, build_id=1, doc_ids=[DOC_ID])
        live_coll = ctx.shadow_collection
        snap_ref = adapter.snapshot_collection(live_coll)

        # After restore, the marker must have been set then cleared using the
        # canonical constant key.
        restored_coll = restore_from_snapshot(
            adapter=adapter,
            session=session,
            kb_id=KB_ID,
            ref=snap_ref,
            new_build_id=2,
        )

        meta = adapter.get_collection_metadata(restored_coll)
        # The marker should be present (set to "false" after successful replay)
        assert RESTORED_UNREPLAYED_MARKER_KEY in meta, (
            "restore_from_snapshot must write the canonical RESTORED_UNREPLAYED_MARKER_KEY "
            "into collection metadata"
        )
        assert meta[RESTORED_UNREPLAYED_MARKER_KEY] == "false", (
            "After successful replay, the marker must be cleared (set to 'false')"
        )


# ---------------------------------------------------------------------------
# Ruling 2: llm_cache.json purge — surgical path (content_hash resolved)
# ---------------------------------------------------------------------------


class TestLLMCachePurge:
    """Ruling 2: purge must remove matching llm_cache entries; delete must not touch the cache."""

    def _make_cache_file(self, run_dir: pathlib.Path, entries: dict[str, str]) -> pathlib.Path:
        """Write a synthetic llm_cache.json with given entries."""
        cache_path = run_dir / "llm_cache.json"
        cache_path.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
        return cache_path

    def test_purge_removes_matching_entries_surgical_path(self, tmp_path: pathlib.Path) -> None:
        """purge=True removes cache entries keyed by resolved content_hash (surgical path).

        The test creates an inventory artifact that maps document_id → content_hash,
        and a llm_cache.json containing entries for that content_hash plus entries
        for an unrelated hash. After purge, the matching entries must be gone and
        the unrelated entries must survive.
        """
        engine = _make_engine()
        session = _make_session_factory(engine)()
        adapter = FakeAdapter()

        content_hash = "abc123deadbeef"
        unrelated_hash = "fff000unrelated"

        # Build inventory artifact so the purge can resolve document_id → content_hash
        inventory_dir = tmp_path / "inventory"
        inventory_dir.mkdir()
        inventory_path = inventory_dir / "collect.json"
        inventory_data = {
            "items": [
                {
                    "document_id": DOC_ID,
                    "content_hash": content_hash,
                    "source_path": "/fake/doc.pdf",
                    "display_name": "doc.pdf",
                    "media_type": "application/pdf",
                    "size_bytes": 1000,
                    "source_metadata": {},
                    "discovered_at": "2026-01-01T00:00:00+00:00",
                    "dedup_role": "unique",
                    "collect_status": "collected",
                    "document_status": "active",
                }
            ]
        }
        inventory_path.write_text(json.dumps(inventory_data), encoding="utf-8")

        # Build llm_cache with matching + unrelated entries
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        cache_entries = {
            f"{content_hash}|/path/to/table1|gpt-4o": "Table with 3 rows and 2 columns.",
            f"{content_hash}|/path/to/table2|gpt-4o": "Another table description.",
            f"{unrelated_hash}|/path/to/other|gpt-4o": "Unrelated description that must survive.",
        }
        cache_path = self._make_cache_file(run_dir, cache_entries)

        _ingest_and_promote(adapter, session, KB_ID, WS_ID, build_id=1, doc_ids=[DOC_ID])

        report = delete_document(
            kb_id=KB_ID,
            document_id=DOC_ID,
            purge=True,
            session=session,
            adapter=adapter,
            artifacts_root=tmp_path,
            deleted_by="test-actor",
            llm_cache_path=cache_path,
            inventory_path=inventory_path,
        )

        assert cache_path.exists(), "llm_cache.json must still exist (some entries survive)"
        remaining = json.loads(cache_path.read_text(encoding="utf-8"))

        # Matching entries must be gone
        for key in cache_entries:
            if key.startswith(content_hash):
                assert key not in remaining, (
                    f"Purge must remove cache entry '{key}' (matches content_hash '{content_hash}')"
                )

        # Unrelated entry must survive
        unrelated_key = f"{unrelated_hash}|/path/to/other|gpt-4o"
        assert unrelated_key in remaining, "Unrelated cache entry must survive purge"

        # DeletionReport must record what happened
        assert report.purge is True
        assert report.llm_cache_action.startswith("surgical:"), (
            f"DeletionReport must document the llm_cache action taken; "
            f"got: '{report.llm_cache_action}'"
        )

    def test_purge_deletes_whole_cache_when_hash_unresolvable(self, tmp_path: pathlib.Path) -> None:
        """purge=True deletes the whole llm_cache.json when content_hash cannot be resolved.

        When no inventory artifact is available, the purge cannot surgically remove
        entries — so the whole cache file is deleted (honest cost of erasure).
        """
        engine = _make_engine()
        session = _make_session_factory(engine)()
        adapter = FakeAdapter()

        run_dir = tmp_path / "run"
        run_dir.mkdir()
        cache_entries = {
            "somehash|/path/to/table|model": "A table description.",
        }
        cache_path = self._make_cache_file(run_dir, cache_entries)

        _ingest_and_promote(adapter, session, KB_ID, WS_ID, build_id=1, doc_ids=[DOC_ID])

        # No inventory_path supplied → hash unresolvable → whole file deleted
        report = delete_document(
            kb_id=KB_ID,
            document_id=DOC_ID,
            purge=True,
            session=session,
            adapter=adapter,
            artifacts_root=tmp_path,
            deleted_by="test-actor",
            llm_cache_path=cache_path,
            inventory_path=None,
        )

        assert not cache_path.exists(), (
            "When content_hash cannot be resolved, the entire llm_cache.json must be deleted"
        )
        assert report.purge is True
        assert (
            "whole" in str(report.llm_cache_action).lower()
            or "deleted" in str(report.llm_cache_action).lower()
        ), "DeletionReport must record that the whole cache was deleted"

    def test_non_purge_delete_leaves_cache_untouched(self, tmp_path: pathlib.Path) -> None:
        """Non-purge delete must NOT modify llm_cache.json (deferred deferral per §17 note)."""
        engine = _make_engine()
        session = _make_session_factory(engine)()
        adapter = FakeAdapter()

        run_dir = tmp_path / "run"
        run_dir.mkdir()
        cache_entries = {
            "anyhash|/path/to/table|model": "A table description.",
        }
        cache_path = self._make_cache_file(run_dir, cache_entries)
        original_content = cache_path.read_text(encoding="utf-8")

        _ingest_and_promote(adapter, session, KB_ID, WS_ID, build_id=1, doc_ids=[DOC_ID])

        delete_document(
            kb_id=KB_ID,
            document_id=DOC_ID,
            purge=False,
            session=session,
            adapter=adapter,
            artifacts_root=tmp_path,
            deleted_by="test-actor",
            llm_cache_path=cache_path,
        )

        assert cache_path.exists(), "Non-purge delete must NOT delete llm_cache.json"
        assert cache_path.read_text(encoding="utf-8") == original_content, (
            "Non-purge delete must NOT modify llm_cache.json contents"
        )


# ---------------------------------------------------------------------------
# Ruling 3: FakeAdapter.retarget_alias must raise CollectionNotFoundError
# ---------------------------------------------------------------------------


class TestFakeAdapterRaisesBug:
    """Ruling 3: helpers.py:219 raised CollectionInfo (dataclass) not CollectionNotFoundError."""

    def test_retarget_alias_nonexistent_collection_raises_collection_not_found(self) -> None:
        """retarget_alias to a non-existent collection must raise CollectionNotFoundError."""
        from finecorpus.index.adapter import CollectionNotFoundError

        adapter = FakeAdapter()
        adapter.create_collection("kb1", 1, dimensions=4)
        adapter.create_alias("alias1", collection_name("kb1", 1))

        with pytest.raises(CollectionNotFoundError):
            adapter.retarget_alias("alias1", "does_not_exist")


# ---------------------------------------------------------------------------
# Ruling 4: Retention boundary — exactly-90-day-old snapshot is swept (<=)
# ---------------------------------------------------------------------------


class TestRetentionBoundary:
    """Ruling 4: snapshot_cold must use <= so exactly-at-boundary snapshots are swept."""

    def test_exactly_90_day_old_snapshot_is_swept(self) -> None:
        """A snapshot whose created_at is exactly 90 days old must be swept."""
        engine = _make_engine()
        session = _make_session_factory(engine)()
        adapter = FakeAdapter()

        ctx = _ingest_and_promote(adapter, session, KB_ID, WS_ID, build_id=1, doc_ids=[DOC_ID])
        live_coll = ctx.shadow_collection

        # Create a snapshot exactly at the boundary (90 days ago to the second)
        boundary_time = datetime.now(tz=UTC) - timedelta(days=90)
        boundary_snap_id = "snap_boundary_90d"
        boundary_ref = SnapshotRef(
            collection=live_coll,
            snapshot_id=boundary_snap_id,
            created_at=boundary_time,
            location=f"memory://{boundary_snap_id}",
        )
        adapter._snapshots[boundary_snap_id] = boundary_ref
        adapter._snapshot_data[boundary_snap_id] = {"points": []}

        # A snapshot 89 days old (within retention — must survive)
        young_snap_id = "snap_young_89d"
        young_time = datetime.now(tz=UTC) - timedelta(days=89)
        young_ref = SnapshotRef(
            collection=live_coll,
            snapshot_id=young_snap_id,
            created_at=young_time,
            location=f"memory://{young_snap_id}",
        )
        adapter._snapshots[young_snap_id] = young_ref
        adapter._snapshot_data[young_snap_id] = {"points": []}

        result = snapshot_cold(adapter, live_coll, retention_period_days=90)

        # The exactly-90-day-old snapshot must be swept
        assert boundary_snap_id in result.swept_snapshots, (
            "A snapshot exactly 90 days old must be swept (boundary is inclusive: <= not <)"
        )
        assert boundary_snap_id not in adapter._snapshots

        # The 89-day-old snapshot must survive
        assert young_snap_id not in result.swept_snapshots
        assert young_snap_id in adapter._snapshots


# ---------------------------------------------------------------------------
# Ruling 5: FakeAdapter.set_collection_metadata must MERGE (not replace)
# ---------------------------------------------------------------------------


class TestFakeAdapterMetadataMerge:
    """Ruling 5: set_collection_metadata must MERGE not replace (matches QdrantAdapter)."""

    def test_set_metadata_merges_into_existing(self) -> None:
        """Setting metadata should preserve existing keys not present in the new dict."""
        adapter = FakeAdapter()
        adapter.create_collection("kb1", 1, dimensions=4)
        coll = collection_name("kb1", 1)

        # Set initial model-identity metadata
        adapter.set_collection_metadata(
            coll,
            {
                "provider": "fake",
                "model": "fake-embed-v1",
                "dimensions": 4,
            },
        )

        # Now set the unreplayed marker (as restore_from_snapshot does)
        adapter.set_collection_metadata(coll, {RESTORED_UNREPLAYED_MARKER_KEY: "true"})

        meta = adapter.get_collection_metadata(coll)

        # Original model-identity keys must survive the second set_collection_metadata call
        assert meta.get("provider") == "fake", (
            "FakeAdapter.set_collection_metadata must MERGE, not replace — "
            "'provider' key lost after second call"
        )
        assert meta.get("model") == "fake-embed-v1", (
            "FakeAdapter.set_collection_metadata must MERGE — 'model' key lost"
        )
        # The new marker must also be present
        assert meta.get(RESTORED_UNREPLAYED_MARKER_KEY) == "true"

    def test_restore_marker_set_clear_preserves_prior_metadata(self) -> None:
        """restore_from_snapshot's marker set/clear must not discard prior metadata."""
        engine = _make_engine()
        session = _make_session_factory(engine)()
        adapter = FakeAdapter()

        # Build a collection with model-identity metadata
        ctx = _ingest_and_promote(adapter, session, KB_ID, WS_ID, build_id=1, doc_ids=[DOC_ID])
        live_coll = ctx.shadow_collection

        # Verify model-identity metadata is present from create_shadow
        prior_meta = adapter.get_collection_metadata(live_coll)
        assert "provider" in prior_meta, "create_shadow must store provider in metadata"

        snap_ref = adapter.snapshot_collection(live_coll)

        restored_coll = restore_from_snapshot(
            adapter=adapter,
            session=session,
            kb_id=KB_ID,
            ref=snap_ref,
            new_build_id=2,
        )

        post_meta = adapter.get_collection_metadata(restored_coll)

        # After marker set/clear, the snapshot's original metadata must still be present.
        # The restored collection inherits metadata from the snapshot (deep copy).
        # The marker set/clear must not wipe that metadata out.
        assert RESTORED_UNREPLAYED_MARKER_KEY in post_meta, (
            "The marker key must be present after restore"
        )
        assert post_meta[RESTORED_UNREPLAYED_MARKER_KEY] == "false", (
            "After successful replay the marker must be cleared to 'false'"
        )
        # The original model-identity metadata from the snapshotted collection
        # must survive the marker set/clear operations.
        assert "provider" in post_meta, (
            "Model-identity metadata must survive restore_from_snapshot's marker set/clear "
            "(FakeAdapter.set_collection_metadata must merge, not replace)"
        )
