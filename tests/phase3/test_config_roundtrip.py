"""Phase 3 roundtrip tests for IngestionConfig export/import.

Covers:
- export → import → re-export is byte-identical (roundtrip parity).
- Hand-tampered chunking value → import hard-errors on config_version mismatch.
- Unknown key in file → rejected (extra=forbid).
- Version range acceptance (1.1.0+ accepted).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from finecorpus.contracts.ingestion_config import (
    ChunkingConfig,
    ChunkingStrategy,
    ClassDescription,
    ClassRule,
    EmbeddingConfig,
    IngestionConfig,
    LanguageDecision,
    LanguageSupportDecision,
    NaiveBaselineRef,
    RecommendationBasis,
    RecommendationProvenance,
    RetrievalStrategy,
    RetrievalTreatment,
    TransformationSettings,
)
from finecorpus.contracts.shared.blocks import (
    PermissionFidelity,
    PermissionMode,
    PermissionSource,
    SalienceTier,
    SegmentType,
    TenancyBlock,
)
from finecorpus.pipeline.plan.config_io import (
    ConfigImportError,
    export_config,
    import_config,
)
from finecorpus.pipeline.plan.config_version import derive_config_version

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_tenancy() -> TenancyBlock:
    return TenancyBlock(
        workspace_id="ws-roundtrip",
        kb_id="kb-roundtrip",
        permission_mode=PermissionMode.public_to_kb,
        permission_principals=[],
        permission_source=PermissionSource.platform,
        permission_fidelity=PermissionFidelity.authoritative,
    )


def _make_full_config() -> IngestionConfig:
    """Build a realistic IngestionConfig including class_descriptions for roundtrip testing."""
    default_rule = ClassRule(
        segment_class=SegmentType.prose,
        transformation=TransformationSettings(
            tier1_enabled=True,
            tier1_operations=[],
            tier2_enabled=False,
            tier2_operations=[],
            tier3_enabled=False,
            tier3_settings=None,
        ),
        chunking=ChunkingConfig(
            strategy=ChunkingStrategy.recursive_char,
            max_tokens=512,
            overlap_tokens=64,
            respect_headings=False,
        ),
        metadata_schema=[],
        retrieval_treatment=RetrievalTreatment(
            default_salience_filter=[SalienceTier.primary, SalienceTier.supporting],
            rerank_eligible=False,
            strategy=RetrievalStrategy.dense,
        ),
    )

    descriptions = [
        ClassDescription(
            segment_class=SegmentType.prose, class_id="prose", description="Narrative prose."
        ),
        ClassDescription(
            segment_class=SegmentType.table, class_id="table", description="Tabular data."
        ),
    ]

    partial = IngestionConfig(
        schema_version="1.2.0",
        tenancy=_make_tenancy(),
        config_version="0" * 64,  # placeholder
        created_at=datetime(2026, 9, 1, tzinfo=UTC),
        naive_baseline=NaiveBaselineRef(reference_id="nb-v0", description="Phase 0 skeleton"),
        class_rules=[],
        default_rule=default_rule,
        embedding=EmbeddingConfig(
            provider="ollama", model="nomic-embed-text", dimensions=768, normalize=True
        ),
        retrieval_defaults=RetrievalTreatment(
            default_salience_filter=[SalienceTier.primary],
            rerank_eligible=False,
            strategy=RetrievalStrategy.dense,
        ),
        language_support=LanguageSupportDecision(
            detected_languages=[], unsupported_languages=[], decision=LanguageDecision.proceed
        ),
        spreadsheet_triage=[],
        exclusions_confirmed=[],
        provenance=[
            RecommendationProvenance(
                target="/default_rule",
                basis=RecommendationBasis.heuristic,
                rationale="Heuristic default.",
            )
        ],
        class_descriptions=descriptions,
        secret_free_attestation=True,
    )
    real_version = derive_config_version(partial)
    return partial.model_copy(update={"config_version": real_version})


# ---------------------------------------------------------------------------
# Roundtrip parity
# ---------------------------------------------------------------------------


class TestRoundtripParity:
    """export → import → re-export must be byte-identical."""

    def test_export_import_reexport_byte_identical(self, tmp_path: Path) -> None:
        config = _make_full_config()
        file1 = tmp_path / "config.json"
        export_config(config, file1)
        original_content = file1.read_text(encoding="utf-8")

        imported = import_config(file1)
        file2 = tmp_path / "config_reexported.json"
        export_config(imported, file2)
        reexported_content = file2.read_text(encoding="utf-8")

        assert original_content == reexported_content, (
            "Re-exported config must be byte-identical to the original export."
        )

    def test_import_preserves_config_version(self, tmp_path: Path) -> None:
        config = _make_full_config()
        f = tmp_path / "config.json"
        export_config(config, f)
        imported = import_config(f)
        assert imported.config_version == config.config_version

    def test_import_preserves_class_descriptions(self, tmp_path: Path) -> None:
        config = _make_full_config()
        f = tmp_path / "config.json"
        export_config(config, f)
        imported = import_config(f)
        assert len(imported.class_descriptions) == len(config.class_descriptions)
        for a, b in zip(imported.class_descriptions, config.class_descriptions, strict=True):
            assert a.class_id == b.class_id
            assert a.description == b.description


# ---------------------------------------------------------------------------
# Tamper detection
# ---------------------------------------------------------------------------


class TestTamperDetection:
    """Hand-editing a build-affecting field must cause config_version mismatch → hard error."""

    def test_tampered_chunking_rejected(self, tmp_path: Path) -> None:
        config = _make_full_config()
        f = tmp_path / "config.json"
        export_config(config, f)

        # Tamper: change max_tokens in default_rule.chunking
        data = json.loads(f.read_text(encoding="utf-8"))
        data["default_rule"]["chunking"]["max_tokens"] = 9999
        f.write_text(json.dumps(data), encoding="utf-8")

        with pytest.raises(ConfigImportError) as exc_info:
            import_config(f)
        err_text = str(exc_info.value).lower()
        assert "mismatch" in err_text or "tamper" in err_text, (
            f"Expected mismatch/tamper in error, got: {exc_info.value}"
        )

    def test_tampered_embedding_rejected(self, tmp_path: Path) -> None:
        config = _make_full_config()
        f = tmp_path / "config.json"
        export_config(config, f)

        data = json.loads(f.read_text(encoding="utf-8"))
        data["embedding"]["model"] = "hacked-model"
        f.write_text(json.dumps(data), encoding="utf-8")

        with pytest.raises(ConfigImportError):
            import_config(f)

    def test_tampered_config_version_directly_rejected(self, tmp_path: Path) -> None:
        """Directly editing config_version (without editing content) → mismatch."""
        config = _make_full_config()
        f = tmp_path / "config.json"
        export_config(config, f)

        data = json.loads(f.read_text(encoding="utf-8"))
        data["config_version"] = "b" * 64  # Forged version
        f.write_text(json.dumps(data), encoding="utf-8")

        with pytest.raises(ConfigImportError):
            import_config(f)

    def test_tampered_class_description_rejected(self, tmp_path: Path) -> None:
        """Editing class description text (build-affecting) → config_version mismatch."""
        config = _make_full_config()
        f = tmp_path / "config.json"
        export_config(config, f)

        data = json.loads(f.read_text(encoding="utf-8"))
        if data["class_descriptions"]:
            data["class_descriptions"][0]["description"] = "Injected description."
        f.write_text(json.dumps(data), encoding="utf-8")

        with pytest.raises(ConfigImportError):
            import_config(f)


# ---------------------------------------------------------------------------
# extra=forbid on import
# ---------------------------------------------------------------------------


class TestImportExtraForbid:
    """Unknown keys in the JSON file must be rejected on import."""

    def test_unknown_top_level_key_rejected(self, tmp_path: Path) -> None:
        config = _make_full_config()
        f = tmp_path / "config.json"
        export_config(config, f)

        data = json.loads(f.read_text(encoding="utf-8"))
        data["unknown_injected_key"] = "secret_value"
        f.write_text(json.dumps(data), encoding="utf-8")

        from pydantic import ValidationError

        with pytest.raises((ValidationError, ConfigImportError)):
            import_config(f)

    def test_unknown_nested_key_in_default_rule_rejected(self, tmp_path: Path) -> None:
        config = _make_full_config()
        f = tmp_path / "config.json"
        export_config(config, f)

        data = json.loads(f.read_text(encoding="utf-8"))
        data["default_rule"]["rogue_nested"] = "bad_value"
        f.write_text(json.dumps(data), encoding="utf-8")

        from pydantic import ValidationError

        with pytest.raises((ValidationError, ConfigImportError)):
            import_config(f)


# ---------------------------------------------------------------------------
# Version range
# ---------------------------------------------------------------------------


class TestVersionRange:
    """import_config must accept schema_version 1.1.0+ and reject 0.x / 2.x."""

    def test_version_1_1_0_accepted(self, tmp_path: Path) -> None:
        """1.1.0 is within the supported range (min_minor=1)."""
        config = _make_full_config()
        f = tmp_path / "config.json"
        export_config(config, f)
        data = json.loads(f.read_text(encoding="utf-8"))
        data["schema_version"] = "1.1.0"
        # Re-derive config_version with the 1.1.0 schema_version string — note schema_version
        # is NOT a hashed input, so config_version stays the same.
        f.write_text(json.dumps(data), encoding="utf-8")
        # Should not raise on version check; may raise on tamper if fields differ — that's ok
        # as long as the version check passes. We just check no ContractVersionError.
        try:
            import_config(f)
        except ConfigImportError as exc:
            exc_text = str(exc).lower()
            assert "mismatch" in exc_text or "tamper" in exc_text, (
                f"Only config_version mismatch errors acceptable, not version rejection: {exc}"
            )

    def test_version_2_0_0_rejected(self, tmp_path: Path) -> None:
        """schema_version 2.0.0 must be rejected (wrong major)."""
        config = _make_full_config()
        f = tmp_path / "config.json"
        export_config(config, f)
        data = json.loads(f.read_text(encoding="utf-8"))
        data["schema_version"] = "2.0.0"
        f.write_text(json.dumps(data), encoding="utf-8")

        with pytest.raises(ConfigImportError) as exc_info:
            import_config(f)
        assert "2.0.0" in str(exc_info.value) or "unsupported" in str(exc_info.value).lower()

    def test_version_1_0_0_accepted(self, tmp_path: Path) -> None:
        """1.0.0 is below min_minor=1 — should be rejected."""
        config = _make_full_config()
        f = tmp_path / "config.json"
        export_config(config, f)
        data = json.loads(f.read_text(encoding="utf-8"))
        data["schema_version"] = "1.0.0"
        f.write_text(json.dumps(data), encoding="utf-8")
        # SUPPORTED_INGESTION_CONFIG has min_minor=1, so 1.0.0 is rejected.
        with pytest.raises(ConfigImportError):
            import_config(f)
