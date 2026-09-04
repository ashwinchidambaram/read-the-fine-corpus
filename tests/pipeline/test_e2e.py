"""E2E pipeline tests over the golden fixture corpus.

Acceptance criteria (spec Phase 0):
  - run_pipeline over tests/fixtures/golden/corpus/ → all five artifacts exist
    and re-load as valid contracts.
  - Inventory covers all 21 fixture files.
  - Correct sha256 for 3 spot-checked files (from manifest.yaml).
  - Duplicates detected if any.
  - Nothing dropped (inventory count == file count).
"""

from __future__ import annotations

import pathlib

import pytest
import yaml

from finecorpus.contracts.ingestion_config import IngestionConfig
from finecorpus.contracts.inventory import Inventory
from finecorpus.contracts.parse_result_batch import ParseResultBatch
from finecorpus.contracts.segment_set_batch import SegmentSetBatch
from finecorpus.pipeline import run_pipeline
from finecorpus.pipeline.artifact_store import ArtifactStore
from finecorpus.pipeline.build.stage import BuildResult

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

FIXTURE_CORPUS = pathlib.Path(__file__).parent.parent / "fixtures" / "golden" / "corpus"
MANIFEST = pathlib.Path(__file__).parent.parent / "fixtures" / "golden" / "manifest.yaml"

EXPECTED_FILE_COUNT = 21  # from manifest.yaml


def _load_manifest() -> dict:
    with MANIFEST.open() as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# E2E: full pipeline over the golden corpus
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def pipeline_run(tmp_path_factory):
    """Run the pipeline once over the golden corpus; share across tests in this module."""
    artifacts_root = tmp_path_factory.mktemp("artifacts")
    run_id = "test-e2e-golden"

    artifact_paths = run_pipeline(
        source_dir=FIXTURE_CORPUS,
        artifacts_root=artifacts_root,
        run_id=run_id,
        workspace_id="ws-test-golden",
        kb_id="kb-test-golden",
    )
    store = ArtifactStore(artifacts_root=artifacts_root, run_id=run_id)
    return {"paths": artifact_paths, "store": store}


class TestAllArtifactsExistAndValidate:
    """All five artifacts exist and re-load as valid contracts."""

    def test_collect_artifact_exists(self, pipeline_run):
        store: ArtifactStore = pipeline_run["store"]
        assert store.exists("collect"), "Collect artifact missing"

    def test_assess_artifact_exists(self, pipeline_run):
        store: ArtifactStore = pipeline_run["store"]
        assert store.exists("assess"), "Assess artifact missing"

    def test_decompose_artifact_exists(self, pipeline_run):
        store: ArtifactStore = pipeline_run["store"]
        assert store.exists("decompose"), "Decompose artifact missing"

    def test_plan_artifact_exists(self, pipeline_run):
        store: ArtifactStore = pipeline_run["store"]
        assert store.exists("plan"), "Plan artifact missing"

    def test_build_artifact_exists(self, pipeline_run):
        store: ArtifactStore = pipeline_run["store"]
        assert store.exists("build"), "Build artifact missing"

    def test_collect_loads_as_inventory(self, pipeline_run):
        store: ArtifactStore = pipeline_run["store"]
        inv = store.load_with_model_validation("collect", Inventory)
        assert inv.schema_version == "1.0.0"

    def test_assess_loads_as_parse_result_batch(self, pipeline_run):
        store: ArtifactStore = pipeline_run["store"]
        batch = store.load_with_model_validation("assess", ParseResultBatch)
        assert batch.schema_version == "1.0.0"
        # Phase 1: real implementation — skeleton is None (not a skeleton pass-through)
        assert batch.skeleton is not True, (
            "Assess stage should be a real implementation in Phase 1 (skeleton=None)"
        )

    def test_decompose_loads_as_segment_set_batch(self, pipeline_run):
        store: ArtifactStore = pipeline_run["store"]
        batch = store.load_with_model_validation("decompose", SegmentSetBatch)
        assert batch.schema_version == "1.0.0"
        # Phase 1: real implementation — skeleton is None (not a skeleton pass-through)
        assert batch.skeleton is not True, (
            "Decompose stage should be a real implementation in Phase 1 (skeleton=None)"
        )

    def test_plan_loads_as_ingestion_config(self, pipeline_run):
        store: ArtifactStore = pipeline_run["store"]
        config = store.load_with_model_validation("plan", IngestionConfig)
        assert config.schema_version == "1.1.0"
        assert config.secret_free_attestation is True

    def test_build_loads_as_build_result(self, pipeline_run):
        store: ArtifactStore = pipeline_run["store"]
        result = store.load_with_model_validation("build", BuildResult)
        assert result.schema_version == "1.0.0"
        assert result.chunk_count == 0
        assert result.skeleton is True


class TestInventoryCorrectness:
    """Inventory must cover all files and have correct hashes."""

    def test_inventory_covers_all_fixture_files(self, pipeline_run):
        """Inventory source_path set equals actual file set in corpus directory (§6 rule 6)."""
        store: ArtifactStore = pipeline_run["store"]
        inv = store.load_with_model_validation("collect", Inventory)

        # Build set of actual files and compare with inventory source_path set
        actual_files = {str(p) for p in FIXTURE_CORPUS.rglob("*") if p.is_file()}
        inventory_paths = {item.source_path for item in inv.items}
        assert inventory_paths == actual_files, (
            f"Inventory source_paths do not match corpus files. "
            f"Missing from inventory: {actual_files - inventory_paths}. "
            f"Extra in inventory: {inventory_paths - actual_files}. "
            "Nothing must be silently dropped (§6 rule 6)."
        )

    def test_inventory_has_expected_count(self, pipeline_run):
        """Inventory count matches expected fixture count from manifest."""
        store: ArtifactStore = pipeline_run["store"]
        inv = store.load_with_model_validation("collect", Inventory)
        assert len(inv.items) == EXPECTED_FILE_COUNT, (
            f"Expected {EXPECTED_FILE_COUNT} files, got {len(inv.items)}"
        )

    @pytest.mark.parametrize(
        "fixture_file,expected_sha256",
        [
            # Spot-check 3 files against manifest.yaml
            (
                "clean_native.pdf",
                "ff5829d59ef3af5697e631604c4b359c2f980c791da6bef2a623be54fd6a41bc",
            ),
            (
                "scanned_poor.pdf",
                "0f2f1aae07bca02a8c0e4ee93153957c75b06215156efd80064a52eac239917b",
            ),
            ("adversarial.pdf", "ff04be503f7c2c2e8cfa65c19cedf0714d4a80893b921e412dd54c0bfbe20927"),
        ],
    )
    def test_sha256_spot_check(self, pipeline_run, fixture_file, expected_sha256):
        """Spot-check content hashes against manifest.yaml."""
        store: ArtifactStore = pipeline_run["store"]
        inv = store.load_with_model_validation("collect", Inventory)

        # Find the item matching this fixture file
        matching = [
            item for item in inv.items if pathlib.Path(item.source_path).name == fixture_file
        ]
        assert len(matching) == 1, (
            f"Expected exactly 1 item for {fixture_file}, found {len(matching)}"
        )
        assert matching[0].content_hash == expected_sha256, (
            f"sha256 mismatch for {fixture_file}: "
            f"got {matching[0].content_hash}, expected {expected_sha256}"
        )

    def test_every_item_has_document_id(self, pipeline_run):
        """Every inventory item has a stable, non-empty document_id."""
        store: ArtifactStore = pipeline_run["store"]
        inv = store.load_with_model_validation("collect", Inventory)
        for item in inv.items:
            assert item.document_id, f"Item {item.source_path} has empty document_id"

    def test_every_item_has_content_hash(self, pipeline_run):
        """Every inventory item has a non-empty content_hash (sha256 hex)."""
        store: ArtifactStore = pipeline_run["store"]
        inv = store.load_with_model_validation("collect", Inventory)
        for item in inv.items:
            assert item.content_hash, f"Item {item.source_path} has empty content_hash"
            assert len(item.content_hash) == 64, (
                f"Item {item.source_path} has non-sha256 content_hash "
                f"(len={len(item.content_hash)})"
            )

    def test_duplicate_detection(self, pipeline_run):
        """If any exact duplicates exist, they are detected and listed."""
        store: ArtifactStore = pipeline_run["store"]
        inv = store.load_with_model_validation("collect", Inventory)

        # Check consistency: items in duplicate_groups must have dedup_role != unique
        for group in inv.duplicate_groups:
            for doc_id in group.member_document_ids:
                matching = [i for i in inv.items if i.document_id == doc_id]
                assert len(matching) == 1
                assert matching[0].dedup_role in ("exact_duplicate", "primary"), (
                    f"Item in duplicate_group has unexpected role: {matching[0].dedup_role}"
                )

    def test_nothing_silently_dropped(self, pipeline_run):
        """Nothing is silently dropped — unreadable files have collect_status set."""
        store: ArtifactStore = pipeline_run["store"]
        inv = store.load_with_model_validation("collect", Inventory)

        for item in inv.items:
            # Every item must have a collect_status (not None/empty)
            assert item.collect_status, (
                f"Item {item.source_path} has no collect_status (silent drop violated §6 rule 6)"
            )

    def test_tenancy_present(self, pipeline_run):
        """Inventory carries tenancy block (Phase 0 MUST — §19)."""
        store: ArtifactStore = pipeline_run["store"]
        inv = store.load_with_model_validation("collect", Inventory)
        assert inv.tenancy.workspace_id == "ws-test-golden"
        assert inv.tenancy.kb_id == "kb-test-golden"

    def test_assess_has_same_count_as_inventory(self, pipeline_run):
        """Assess output has one ParseResult per inventory item (nothing dropped)."""
        store: ArtifactStore = pipeline_run["store"]
        inv = store.load_with_model_validation("collect", Inventory)
        batch = store.load_with_model_validation("assess", ParseResultBatch)
        assert len(batch.results) == len(inv.items), (
            f"Assess has {len(batch.results)} results but inventory has {len(inv.items)} items"
        )

    def test_decompose_has_same_count_as_inventory(self, pipeline_run):
        """Decompose output has one SegmentSet per inventory item (nothing dropped)."""
        store: ArtifactStore = pipeline_run["store"]
        inv = store.load_with_model_validation("collect", Inventory)
        batch = store.load_with_model_validation("decompose", SegmentSetBatch)
        assert len(batch.segment_sets) == len(inv.items), (
            f"Decompose has {len(batch.segment_sets)} segment_sets but inventory has "
            f"{len(inv.items)} items (nothing must be silently dropped)"
        )

    def test_plan_has_secret_free_attestation(self, pipeline_run):
        """IngestionConfig carries secret_free_attestation=True (§14.2)."""
        store: ArtifactStore = pipeline_run["store"]
        config = store.load_with_model_validation("plan", IngestionConfig)
        assert config.secret_free_attestation is True

    def test_build_has_zero_chunks(self, pipeline_run):
        """Build result has 0 chunks (skeleton phase)."""
        store: ArtifactStore = pipeline_run["store"]
        result = store.load_with_model_validation("build", BuildResult)
        assert result.chunk_count == 0
        assert result.chunks == []
