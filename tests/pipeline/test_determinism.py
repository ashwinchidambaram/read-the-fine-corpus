"""Determinism tests for the pipeline.

Tests that two runs with the same run_id inputs produce byte-identical
inventory artifacts.

mtime caveat: source_modified_at and source_created_at are read from file
metadata and may change between test runs if test fixtures are re-generated.
discovered_at (= collected_at) is passed explicitly by the caller and fixed.

Strategy:
  - Compare all inventory item fields EXCEPT those that are inherently volatile
    across runs: source_created_at, source_modified_at, discovered_at.
  - These volatile fields are explicitly excluded from the byte-identical comparison.
  - The core content: document_id, content_hash, source_path, size_bytes, media_type
    must be identical across runs.

The collected_at field is fixed by passing it to CollectStage explicitly.
"""

from __future__ import annotations

import pathlib
import shutil
from datetime import UTC, datetime

from finecorpus.pipeline.artifact_store import ArtifactStore
from finecorpus.pipeline.collect import CollectStage

FIXTURE_CORPUS = pathlib.Path(__file__).parent.parent / "fixtures" / "golden" / "corpus"

# Fields excluded from byte-identical comparison (volatile across runs).
_VOLATILE_ITEM_FIELDS = {"source_created_at", "source_modified_at", "discovered_at"}
# Top-level inventory fields that are volatile
_VOLATILE_TOP_FIELDS = {"collected_at"}


def _strip_volatile(inventory_dict: dict) -> dict:
    """Remove volatile fields from an inventory dict for comparison."""
    result = {k: v for k, v in inventory_dict.items() if k not in _VOLATILE_TOP_FIELDS}
    result["items"] = [
        {k: v for k, v in item.items() if k not in _VOLATILE_ITEM_FIELDS}
        for item in result.get("items", [])
    ]
    return result


class TestInventoryDeterminism:
    """Two runs with same inputs produce byte-identical inventory (modulo volatile fields)."""

    def test_two_runs_produce_identical_inventory(self, tmp_path):
        """Running Collect twice with the same collected_at gives identical inventory."""
        # Use a small subset — just 3 files — for speed
        source_dir = tmp_path / "source"
        source_dir.mkdir()

        # Copy 3 fixture files
        fixture_files = sorted(FIXTURE_CORPUS.glob("*.pdf"))[:3]
        for f in fixture_files:
            shutil.copy2(f, source_dir / f.name)

        fixed_time = datetime(2026, 9, 1, 0, 0, 0, tzinfo=UTC)

        # Run 1
        store1 = ArtifactStore(artifacts_root=tmp_path / "run1", run_id="det-run")
        stage1 = CollectStage(
            source_dir=source_dir,
            workspace_id="ws-det",
            kb_id="kb-det",
            collected_at=fixed_time,
        )
        stage1.run(input_data=None, store=store1)
        inv1 = store1.load("collect")

        # Run 2 (same inputs)
        store2 = ArtifactStore(artifacts_root=tmp_path / "run2", run_id="det-run")
        stage2 = CollectStage(
            source_dir=source_dir,
            workspace_id="ws-det",
            kb_id="kb-det",
            collected_at=fixed_time,
        )
        stage2.run(input_data=None, store=store2)
        inv2 = store2.load("collect")

        # Strip volatile fields and compare
        stripped1 = _strip_volatile(inv1)
        stripped2 = _strip_volatile(inv2)

        assert stripped1 == stripped2, (
            "Inventory outputs differ between runs with identical inputs. "
            "Determinism invariant violated."
        )

    def test_document_ids_are_stable_across_runs(self, tmp_path):
        """document_id for each file is identical across two runs."""
        source_dir = tmp_path / "source"
        source_dir.mkdir()

        fixture_files = sorted(FIXTURE_CORPUS.glob("*.pdf"))[:3]
        for f in fixture_files:
            shutil.copy2(f, source_dir / f.name)

        fixed_time = datetime(2026, 9, 1, 0, 0, 0, tzinfo=UTC)

        store1 = ArtifactStore(artifacts_root=tmp_path / "run-a", run_id="det-a")
        CollectStage(
            source_dir=source_dir,
            workspace_id="ws-x",
            kb_id="kb-x",
            collected_at=fixed_time,
        ).run(input_data=None, store=store1)
        inv1 = store1.load("collect")

        store2 = ArtifactStore(artifacts_root=tmp_path / "run-b", run_id="det-b")
        CollectStage(
            source_dir=source_dir,
            workspace_id="ws-x",
            kb_id="kb-x",
            collected_at=fixed_time,
        ).run(input_data=None, store=store2)
        inv2 = store2.load("collect")

        ids1 = {item["source_path"]: item["document_id"] for item in inv1["items"]}
        ids2 = {item["source_path"]: item["document_id"] for item in inv2["items"]}

        for path, doc_id in ids1.items():
            assert ids2.get(path) == doc_id, (
                f"document_id changed between runs for {path}: {doc_id} vs {ids2.get(path)}"
            )

    def test_content_hashes_are_deterministic(self, tmp_path):
        """sha256 content hashes are identical across runs for the same files."""
        source_dir = tmp_path / "source"
        source_dir.mkdir()

        fixture_files = sorted(FIXTURE_CORPUS.glob("*.pdf"))[:3]
        for f in fixture_files:
            shutil.copy2(f, source_dir / f.name)

        fixed_time = datetime(2026, 9, 1, 0, 0, 0, tzinfo=UTC)

        store1 = ArtifactStore(artifacts_root=tmp_path / "run-c", run_id="hash-c")
        CollectStage(
            source_dir=source_dir,
            workspace_id="ws-h",
            kb_id="kb-h",
            collected_at=fixed_time,
        ).run(input_data=None, store=store1)
        inv1 = store1.load("collect")

        store2 = ArtifactStore(artifacts_root=tmp_path / "run-d", run_id="hash-d")
        CollectStage(
            source_dir=source_dir,
            workspace_id="ws-h",
            kb_id="kb-h",
            collected_at=fixed_time,
        ).run(input_data=None, store=store2)
        inv2 = store2.load("collect")

        hashes1 = {item["source_path"]: item["content_hash"] for item in inv1["items"]}
        hashes2 = {item["source_path"]: item["content_hash"] for item in inv2["items"]}

        assert hashes1 == hashes2, "Content hashes changed between runs"
