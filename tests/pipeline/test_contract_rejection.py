"""Contract rejection tests.

Tests that:
  1. Feeding a stage an artifact with schema_version "99.0.0" raises ContractVersionError.
  2. Feeding a stage malformed JSON raises ArtifactStoreError loudly.
  3. Feeding a stage an artifact missing schema_version raises ArtifactStoreError.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from finecorpus.contracts.versions import ContractVersionError
from finecorpus.pipeline.artifact_store import ArtifactStore, ArtifactStoreError
from finecorpus.pipeline.assess import AssessStage
from finecorpus.pipeline.build import BuildStage


def _make_store(tmp_path: pathlib.Path, run_id: str = "test-rejection") -> ArtifactStore:
    return ArtifactStore(artifacts_root=tmp_path, run_id=run_id)


def _make_bad_ingestion_config(schema_version: str = "99.0.0") -> dict:
    """Build a minimal IngestionConfig-shaped dict with an unsupported schema_version."""
    return {
        "schema_version": schema_version,
        "tenancy": {
            "workspace_id": "ws-test",
            "kb_id": "kb-test",
            "permission_mode": "public_to_kb",
            "permission_principals": [],
            "permission_source": "platform",
            "permission_fidelity": "authoritative",
            "permission_resolved_at": None,
        },
        "config_version": "abc123",
        "created_at": "2026-09-01T00:00:00+00:00",
        "naive_baseline": {
            "reference_id": "naive-baseline-v0",
            "description": "test",
        },
        "class_rules": [],
        "default_rule": {
            "segment_class": "prose",
            "transformation": {
                "tier1_enabled": True,
                "tier1_operations": ["whitespace_repair"],
                "tier2_enabled": False,
                "tier2_operations": [],
                "tier3_enabled": False,
                "tier3_settings": None,
            },
            "chunking": {
                "strategy": "recursive_char",
                "max_tokens": 512,
                "overlap_tokens": 64,
                "respect_headings": False,
                "atomic_rows": None,
                "repeat_headers_on_split": None,
                "split_boundaries": None,
            },
            "embedding_override": None,
            "metadata_schema": [],
            "retrieval_treatment": {
                "default_salience_filter": ["primary"],
                "salience_weights": None,
                "rerank_eligible": False,
                "strategy": "dense",
                "confidence_floor": None,
            },
        },
        "embedding": {
            "provider": "ollama",
            "model": "nomic-embed-text",
            "dimensions": 768,
            "normalize": True,
            "supports_languages": ["en"],
        },
        "retrieval_defaults": {
            "default_salience_filter": ["primary"],
            "salience_weights": None,
            "rerank_eligible": False,
            "strategy": "dense",
            "confidence_floor": None,
        },
        "language_support": {
            "detected_languages": [],
            "unsupported_languages": [],
            "decision": "proceed",
            "cross_lingual_supported": None,
        },
        "spreadsheet_triage": [],
        "exclusions_confirmed": [],
        "provenance": [],
        "secret_free_attestation": True,
    }


class TestBuildStageContractVersionRejection:
    """BuildStage MUST reject IngestionConfig with unsupported schema_version (§12).

    IngestionConfig is a real §12 contract at the Plan→Build boundary;
    BuildStage declares consumed_version_range = SUPPORTED_INGESTION_CONFIG.
    """

    def test_build_rejects_unsupported_ingestion_config_version_through_load_run(self, tmp_path):
        """BuildStage raises ContractVersionError for schema_version 99.0.0 via load+run path.

        The artifact is saved to the store (simulating how Plan would persist it),
        then loaded and fed to BuildStage.run() — the real production path.
        """
        store = _make_store(tmp_path, run_id="test-build-rejection")

        bad_config = _make_bad_ingestion_config(schema_version="99.0.0")

        # Persist the bad artifact as if Plan had written it (save bypasses version checks)
        store.save("plan", bad_config)
        loaded_config = store.load("plan")

        stage = BuildStage()
        with pytest.raises(ContractVersionError) as exc_info:
            stage.run(input_data=loaded_config, store=store)

        err = exc_info.value
        assert "99.0.0" in str(err), f"Error should mention the bad version: {err}"
        assert "ingestion_config" in str(err).lower(), (
            f"Error should mention the contract name: {err}"
        )

    def test_build_accepts_supported_ingestion_config_version(self, tmp_path):
        """BuildStage accepts schema_version 1.0.0 without raising."""
        store = _make_store(tmp_path, run_id="test-build-accept")

        good_config = _make_bad_ingestion_config(schema_version="1.0.0")
        store.save("plan", good_config)
        loaded_config = store.load("plan")

        stage = BuildStage()
        result = stage.run(input_data=loaded_config, store=store)
        assert result["schema_version"] == "1.0.0"
        assert result["chunk_count"] == 0


class TestContractVersionRejection:
    """Stage MUST reject a contract version it does not declare support for (§12)."""

    def test_assess_rejects_unsupported_inventory_version(self, tmp_path):
        """AssessStage must raise ContractVersionError for schema_version 99.0.0."""
        store = _make_store(tmp_path)

        # Build a valid-looking Inventory dict but with an unsupported schema_version
        bad_inventory = {
            "schema_version": "99.0.0",  # <-- unsupported major
            "tenancy": {
                "workspace_id": "ws-test",
                "kb_id": "kb-test",
                "permission_mode": "public_to_kb",
                "permission_principals": [],
                "permission_source": "platform",
                "permission_fidelity": "authoritative",
                "permission_resolved_at": None,
            },
            "collected_at": "2026-09-01T00:00:00Z",
            "source_run": {
                "source_kind": "directory",
                "connector_id": None,
                "incremental_cursor": None,
                "acknowledged_permission_gap": None,
            },
            "items": [],
            "links": [],
            "duplicate_groups": [],
            "version_families": [],
        }

        stage = AssessStage()
        with pytest.raises(ContractVersionError) as exc_info:
            stage.run(input_data=bad_inventory, store=store)

        err = exc_info.value
        assert "99.0.0" in str(err), f"Error should mention the bad version: {err}"
        assert "inventory" in str(err).lower(), f"Error should mention the contract: {err}"

    def test_assess_accepts_supported_inventory_version(self, tmp_path):
        """AssessStage accepts schema_version 1.0.0 without raising."""
        store = _make_store(tmp_path)

        valid_inventory = {
            "schema_version": "1.0.0",
            "tenancy": {
                "workspace_id": "ws-test",
                "kb_id": "kb-test",
                "permission_mode": "public_to_kb",
                "permission_principals": [],
                "permission_source": "platform",
                "permission_fidelity": "authoritative",
                "permission_resolved_at": None,
            },
            "collected_at": "2026-09-01T00:00:00Z",
            "source_run": {
                "source_kind": "directory",
                "connector_id": None,
                "incremental_cursor": None,
                "acknowledged_permission_gap": None,
            },
            "items": [],
            "links": [],
            "duplicate_groups": [],
            "version_families": [],
        }

        stage = AssessStage()
        result = stage.run(input_data=valid_inventory, store=store)
        assert result["schema_version"] == "1.0.0"

    def test_assess_rejects_wrong_major(self, tmp_path):
        """AssessStage must reject major version 2 (different breaking version)."""
        store = _make_store(tmp_path)

        bad_inventory = {
            "schema_version": "2.0.0",
            "tenancy": {
                "workspace_id": "ws-test",
                "kb_id": "kb-test",
                "permission_mode": "public_to_kb",
                "permission_principals": [],
                "permission_source": "platform",
                "permission_fidelity": "authoritative",
                "permission_resolved_at": None,
            },
            "collected_at": "2026-09-01T00:00:00Z",
            "source_run": {"source_kind": "directory"},
            "items": [],
            "links": [],
            "duplicate_groups": [],
            "version_families": [],
        }

        stage = AssessStage()
        with pytest.raises(ContractVersionError):
            stage.run(input_data=bad_inventory, store=store)


class TestMalformedInputRejection:
    """Malformed JSON artifacts must raise loudly (§18.2)."""

    def test_load_malformed_json_raises(self, tmp_path):
        """ArtifactStore.load() raises ArtifactStoreError on invalid JSON."""
        store = _make_store(tmp_path)

        # Write garbage to the artifact file
        artifact_path = store.artifact_path("collect")
        artifact_path.write_text("{ this is not valid JSON !!!", encoding="utf-8")

        with pytest.raises(ArtifactStoreError) as exc_info:
            store.load("collect")

        assert "not valid JSON" in str(exc_info.value)

    def test_load_missing_schema_version_raises(self, tmp_path):
        """ArtifactStore.load() raises ArtifactStoreError when schema_version missing."""
        store = _make_store(tmp_path)

        artifact_path = store.artifact_path("collect")
        artifact_path.write_text(
            json.dumps({"some_field": "value", "no_schema_version": True}),
            encoding="utf-8",
        )

        with pytest.raises(ArtifactStoreError) as exc_info:
            store.load("collect")

        assert "schema_version" in str(exc_info.value)

    def test_load_nonexistent_artifact_raises(self, tmp_path):
        """ArtifactStore.load() raises ArtifactStoreError when file does not exist."""
        store = _make_store(tmp_path)

        with pytest.raises(ArtifactStoreError) as exc_info:
            store.load("nonexistent_stage")

        assert "not found" in str(exc_info.value).lower()

    def test_load_non_object_json_raises(self, tmp_path):
        """ArtifactStore.load() raises ArtifactStoreError when JSON root is not an object."""
        store = _make_store(tmp_path)

        artifact_path = store.artifact_path("collect")
        artifact_path.write_text(json.dumps(["list", "not", "object"]), encoding="utf-8")

        with pytest.raises(ArtifactStoreError) as exc_info:
            store.load("collect")

        assert "not a JSON object" in str(exc_info.value)

    def test_empty_run_id_raises(self, tmp_path):
        """ArtifactStore raises ArtifactStoreError when run_id is empty."""
        with pytest.raises(ArtifactStoreError):
            ArtifactStore(artifacts_root=tmp_path, run_id="")

    def test_missing_schema_version_in_input_raises(self, tmp_path):
        """AssessStage raises ContractVersionError when schema_version field is absent."""
        store = _make_store(tmp_path)

        no_version = {
            # No schema_version key
            "tenancy": {
                "workspace_id": "ws-test",
                "kb_id": "kb-test",
                "permission_mode": "public_to_kb",
                "permission_principals": [],
                "permission_source": "platform",
                "permission_fidelity": "authoritative",
                "permission_resolved_at": None,
            },
            "items": [],
        }

        stage = AssessStage()
        with pytest.raises(ContractVersionError):
            stage.run(input_data=no_version, store=store)
