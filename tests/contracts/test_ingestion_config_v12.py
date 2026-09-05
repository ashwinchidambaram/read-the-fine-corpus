"""Tests for IngestionConfig schema_version 1.2.0 additions.

Covers:
- ClassDescription model: fields, extra=forbid.
- class_descriptions field on IngestionConfig.
- to_canonical_json: sorted keys, compact, trailing newline, stability.
- extra=forbid on IngestionConfig and submodels.
- M-032/M-033/M-034 flag fields (contract shape, defaults, accept/reject).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

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
    to_canonical_json,
)
from finecorpus.contracts.parse_result import LanguageShare
from finecorpus.contracts.shared.blocks import (
    PermissionFidelity,
    PermissionMode,
    PermissionSource,
    SalienceTier,
    SegmentType,
    TenancyBlock,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_tenancy() -> TenancyBlock:
    return TenancyBlock(
        workspace_id="ws-test-001",
        kb_id="kb-test-001",
        permission_mode=PermissionMode.public_to_kb,
        permission_principals=[],
        permission_source=PermissionSource.platform,
        permission_fidelity=PermissionFidelity.authoritative,
    )


def _make_transformation(
    *, tier3_enabled: bool = False, tier3_settings=None
) -> TransformationSettings:
    return TransformationSettings(
        tier1_enabled=True,
        tier1_operations=[],
        tier2_enabled=False,
        tier2_operations=[],
        tier3_enabled=tier3_enabled,
        tier3_settings=tier3_settings,
    )


def _make_chunking() -> ChunkingConfig:
    return ChunkingConfig(
        strategy=ChunkingStrategy.recursive_char,
        max_tokens=512,
        overlap_tokens=64,
        respect_headings=False,
    )


def _make_retrieval() -> RetrievalTreatment:
    return RetrievalTreatment(
        default_salience_filter=[SalienceTier.primary],
        rerank_eligible=False,
        strategy=RetrievalStrategy.dense,
    )


def _make_class_rule(segment_class: SegmentType = SegmentType.prose) -> ClassRule:
    return ClassRule(
        segment_class=segment_class,
        transformation=_make_transformation(),
        chunking=_make_chunking(),
        metadata_schema=[],
        retrieval_treatment=_make_retrieval(),
    )


def _make_minimal_config(**overrides) -> IngestionConfig:
    """Build a valid minimal IngestionConfig for testing."""
    defaults: dict = dict(
        schema_version="1.2.0",
        tenancy=_make_tenancy(),
        config_version="a" * 64,
        created_at=datetime(2026, 9, 1, tzinfo=UTC),
        naive_baseline=NaiveBaselineRef(reference_id="nb-v0", description="test baseline"),
        class_rules=[],
        default_rule=_make_class_rule(),
        embedding=EmbeddingConfig(
            provider="ollama", model="nomic-embed-text", dimensions=768, normalize=True
        ),
        retrieval_defaults=_make_retrieval(),
        language_support=LanguageSupportDecision(
            detected_languages=[], unsupported_languages=[], decision=LanguageDecision.proceed
        ),
        spreadsheet_triage=[],
        exclusions_confirmed=[],
        provenance=[
            RecommendationProvenance(
                target="/default_rule", basis=RecommendationBasis.heuristic, rationale="test"
            )
        ],
        class_descriptions=[],
        secret_free_attestation=True,
    )
    defaults.update(overrides)
    return IngestionConfig(**defaults)


# ---------------------------------------------------------------------------
# ClassDescription model
# ---------------------------------------------------------------------------


class TestClassDescription:
    """Tests for the new ClassDescription model (1.2.0)."""

    def test_valid_class_description(self) -> None:
        cd = ClassDescription(
            segment_class=SegmentType.prose,
            class_id="prose",
            description="Prose content for retrieval augmentation.",
        )
        assert cd.class_id == "prose"
        assert cd.description == "Prose content for retrieval augmentation."

    def test_extra_key_rejected(self) -> None:
        """extra=forbid: unknown keys must raise ValidationError."""
        with pytest.raises(ValidationError) as exc_info:
            ClassDescription(
                segment_class=SegmentType.prose,
                class_id="prose",
                description="desc",
                unknown_secret_field="sk-very-secret",  # type: ignore[call-arg]
            )
        assert (
            "extra" in str(exc_info.value)
            or "unexpected" in str(exc_info.value).lower()
            or any("unknown_secret_field" in str(e) for e in exc_info.value.errors())
        )

    def test_missing_required_field_rejected(self) -> None:
        """Required fields must be present."""
        with pytest.raises(ValidationError):
            ClassDescription(segment_class=SegmentType.prose, class_id="prose")  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# class_descriptions field on IngestionConfig
# ---------------------------------------------------------------------------


class TestClassDescriptionsField:
    """Tests for IngestionConfig.class_descriptions (1.2.0)."""

    def test_empty_class_descriptions_default(self) -> None:
        config = _make_minimal_config()
        assert config.class_descriptions == []

    def test_class_descriptions_accepted(self) -> None:
        cd = ClassDescription(
            segment_class=SegmentType.prose,
            class_id="prose",
            description="Prose narrative content.",
        )
        config = _make_minimal_config(class_descriptions=[cd])
        assert len(config.class_descriptions) == 1
        assert config.class_descriptions[0].class_id == "prose"

    def test_multiple_class_descriptions(self) -> None:
        descriptions = [
            ClassDescription(
                segment_class=SegmentType.prose, class_id="prose", description="Prose."
            ),
            ClassDescription(
                segment_class=SegmentType.table, class_id="table", description="Tabular data."
            ),
        ]
        config = _make_minimal_config(class_descriptions=descriptions)
        assert len(config.class_descriptions) == 2


# ---------------------------------------------------------------------------
# to_canonical_json
# ---------------------------------------------------------------------------


class TestCanonicalJson:
    """Tests for to_canonical_json (sorted keys, compact, trailing newline)."""

    def test_returns_string(self) -> None:
        config = _make_minimal_config()
        result = to_canonical_json(config)
        assert isinstance(result, str)

    def test_trailing_newline(self) -> None:
        config = _make_minimal_config()
        result = to_canonical_json(config)
        assert result.endswith("\n")

    def test_valid_json(self) -> None:
        config = _make_minimal_config()
        result = to_canonical_json(config)
        parsed = json.loads(result)
        assert isinstance(parsed, dict)

    def test_sorted_keys(self) -> None:
        config = _make_minimal_config()
        result = to_canonical_json(config)
        parsed = json.loads(result)
        keys = list(parsed.keys())
        assert keys == sorted(keys), "Top-level keys must be sorted"

    def test_no_insignificant_whitespace(self) -> None:
        config = _make_minimal_config()
        result = to_canonical_json(config)
        # No ": " or ", " with spaces (compact separators)
        assert ": " not in result.rstrip("\n")
        assert ", " not in result.rstrip("\n")

    def test_stability(self) -> None:
        """Same config → same canonical JSON every time."""
        config = _make_minimal_config()
        assert to_canonical_json(config) == to_canonical_json(config)

    def test_different_configs_differ(self) -> None:
        config_a = _make_minimal_config()
        config_b = _make_minimal_config(config_version="b" * 64)
        assert to_canonical_json(config_a) != to_canonical_json(config_b)

    def test_class_descriptions_in_canonical_output(self) -> None:
        cd = ClassDescription(
            segment_class=SegmentType.prose, class_id="prose", description="Prose."
        )
        config = _make_minimal_config(class_descriptions=[cd])
        result = to_canonical_json(config)
        parsed = json.loads(result)
        assert "class_descriptions" in parsed
        assert parsed["class_descriptions"][0]["description"] == "Prose."


# ---------------------------------------------------------------------------
# extra=forbid on IngestionConfig and submodels
# ---------------------------------------------------------------------------


class TestExtraForbid:
    """extra=forbid must reject unknown keys on IngestionConfig and submodels."""

    def test_ingestion_config_extra_key_rejected(self) -> None:
        base = _make_minimal_config().model_dump(mode="json")
        base["rogue_key"] = "should_be_rejected"
        with pytest.raises(ValidationError) as exc_info:
            IngestionConfig.model_validate(base)
        assert any("rogue_key" in str(e) for e in exc_info.value.errors())

    def test_chunking_config_extra_key_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ChunkingConfig(
                strategy=ChunkingStrategy.recursive_char,
                max_tokens=512,
                overlap_tokens=64,
                respect_headings=False,
                rogue_field="bad",  # type: ignore[call-arg]
            )

    def test_embedding_config_extra_key_rejected(self) -> None:
        with pytest.raises(ValidationError):
            EmbeddingConfig(
                provider="openai",
                model="text-embedding-3-small",
                dimensions=1536,
                normalize=True,
                rogue_field="bad",  # type: ignore[call-arg]
            )

    def test_transformation_settings_extra_key_rejected(self) -> None:
        with pytest.raises(ValidationError):
            TransformationSettings(
                tier1_enabled=True,
                tier1_operations=[],
                tier2_enabled=False,
                tier2_operations=[],
                tier3_enabled=False,
                rogue_field="bad",  # type: ignore[call-arg]
            )

    def test_class_rule_extra_key_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ClassRule(
                segment_class=SegmentType.prose,
                transformation=_make_transformation(),
                chunking=_make_chunking(),
                metadata_schema=[],
                retrieval_treatment=_make_retrieval(),
                rogue_field="bad",  # type: ignore[call-arg]
            )

    def test_retrieval_treatment_extra_key_rejected(self) -> None:
        with pytest.raises(ValidationError):
            RetrievalTreatment(
                default_salience_filter=[SalienceTier.primary],
                rerank_eligible=False,
                strategy=RetrievalStrategy.dense,
                rogue_field="bad",  # type: ignore[call-arg]
            )


# ---------------------------------------------------------------------------
# M-032/M-033/M-034 flag fields
# ---------------------------------------------------------------------------


class TestM032M033M034FlagFields:
    """M-032/M-033/M-034 flag fields exist with correct defaults."""

    def test_m032_retain_original_ref_default_false(self) -> None:
        t = _make_transformation()
        assert t.retain_original_ref is False

    def test_m033_diff_preview_required_default_false(self) -> None:
        t = _make_transformation()
        assert t.diff_preview_required is False

    def test_m034_mark_rewritten_chunks_default_false(self) -> None:
        t = _make_transformation()
        assert t.mark_rewritten_chunks is False

    def test_m032_can_be_set_true(self) -> None:
        t = _make_transformation(tier3_enabled=False)
        t2 = t.model_copy(update={"retain_original_ref": True})
        assert t2.retain_original_ref is True

    def test_flag_fields_in_canonical_json(self) -> None:
        config = _make_minimal_config()
        result = json.loads(to_canonical_json(config))
        # The default_rule transformation should carry the flag fields
        tr = result["default_rule"]["transformation"]
        assert "retain_original_ref" in tr
        assert "diff_preview_required" in tr
        assert "mark_rewritten_chunks" in tr


# ---------------------------------------------------------------------------
# Ruling 1: ClassDescription class_id/segment_class consistency
# ---------------------------------------------------------------------------


class TestClassDescriptionConsistency:
    """class_id must equal segment_class.value (Ruling 1)."""

    def test_mismatch_rejected(self) -> None:
        """class_id='prose' with segment_class=table must raise ValidationError."""
        with pytest.raises(ValidationError) as exc_info:
            ClassDescription(
                segment_class=SegmentType.table,
                class_id="prose",  # mismatch: should be "table"
                description="Should be rejected.",
            )
        errors = exc_info.value.errors()
        # At least one error should mention class_id/segment_class mismatch
        assert any(
            "class_id" in str(e) or "segment_class" in str(e) or "mismatch" in str(e).lower()
            for e in errors
        ), f"Expected class_id/segment_class error, got: {errors}"

    def test_mismatch_reversed_rejected(self) -> None:
        """class_id='table' with segment_class=prose must also raise ValidationError."""
        with pytest.raises(ValidationError):
            ClassDescription(
                segment_class=SegmentType.prose,
                class_id="table",  # mismatch
                description="Should be rejected.",
            )

    def test_match_accepted(self) -> None:
        """class_id matching segment_class.value must be accepted."""
        cd = ClassDescription(
            segment_class=SegmentType.prose,
            class_id="prose",
            description="Valid prose description.",
        )
        assert cd.class_id == "prose"
        assert cd.segment_class == SegmentType.prose

    def test_match_table_accepted(self) -> None:
        """class_id='table' with segment_class=table must be accepted."""
        cd = ClassDescription(
            segment_class=SegmentType.table,
            class_id="table",
            description="Tabular data.",
        )
        assert cd.class_id == "table"

    def test_match_code_accepted(self) -> None:
        """class_id='code' with segment_class=code must be accepted."""
        cd = ClassDescription(
            segment_class=SegmentType.code,
            class_id="code",
            description="Code blocks.",
        )
        assert cd.class_id == "code"


# ---------------------------------------------------------------------------
# Ruling 2: Duplicate class_id in class_descriptions on IngestionConfig
# ---------------------------------------------------------------------------


class TestDuplicateClassDescriptions:
    """IngestionConfig must reject duplicate class_id values in class_descriptions (Ruling 2)."""

    def test_duplicate_class_id_rejected(self) -> None:
        """Two ClassDescriptions with the same class_id must raise ValidationError."""
        cd1 = ClassDescription(
            segment_class=SegmentType.prose,
            class_id="prose",
            description="First prose description.",
        )
        cd2 = ClassDescription(
            segment_class=SegmentType.prose,
            class_id="prose",
            description="Second prose description — duplicate.",
        )
        with pytest.raises(ValidationError) as exc_info:
            _make_minimal_config(class_descriptions=[cd1, cd2])
        errors = exc_info.value.errors()
        assert any(
            "duplicate" in str(e).lower() or "class_id" in str(e) or "class_descriptions" in str(e)
            for e in errors
        ), f"Expected duplicate class_id error, got: {errors}"

    def test_distinct_class_ids_accepted(self) -> None:
        """Two ClassDescriptions with distinct class_ids must be accepted."""
        cd1 = ClassDescription(
            segment_class=SegmentType.prose,
            class_id="prose",
            description="Prose narrative content.",
        )
        cd2 = ClassDescription(
            segment_class=SegmentType.table,
            class_id="table",
            description="Tabular structured data.",
        )
        config = _make_minimal_config(class_descriptions=[cd1, cd2])
        assert len(config.class_descriptions) == 2

    def test_single_entry_accepted(self) -> None:
        """A single ClassDescription is trivially non-duplicate — must be accepted."""
        cd = ClassDescription(
            segment_class=SegmentType.code,
            class_id="code",
            description="Source code blocks.",
        )
        config = _make_minimal_config(class_descriptions=[cd])
        assert len(config.class_descriptions) == 1


# ---------------------------------------------------------------------------
# Ruling 3: extra=forbid probe tests for TenancyBlock and LanguageShare
# ---------------------------------------------------------------------------


class TestTenancyBlockExtraForbid:
    """TenancyBlock must reject extra keys (Ruling 3 — M-071 gap closed)."""

    def test_extra_key_in_tenancy_rejected(self) -> None:
        """An extra key in TenancyBlock must raise ValidationError."""
        with pytest.raises(ValidationError) as exc_info:
            TenancyBlock(
                workspace_id="ws-test",
                kb_id="kb-test",
                permission_mode=PermissionMode.public_to_kb,
                permission_principals=[],
                permission_source=PermissionSource.platform,
                permission_fidelity=PermissionFidelity.authoritative,
                rogue_extra_field="should_be_rejected",  # type: ignore[call-arg]
            )
        assert any(
            "rogue_extra_field" in str(e) or "extra" in str(e) for e in exc_info.value.errors()
        )

    def test_valid_tenancy_accepted(self) -> None:
        """A well-formed TenancyBlock must be accepted."""
        tb = TenancyBlock(
            workspace_id="ws-test",
            kb_id="kb-test",
            permission_mode=PermissionMode.public_to_kb,
            permission_principals=[],
            permission_source=PermissionSource.platform,
            permission_fidelity=PermissionFidelity.authoritative,
        )
        assert tb.workspace_id == "ws-test"


class TestLanguageShareExtraForbid:
    """LanguageShare must reject extra keys (Ruling 3 — M-071 gap closed)."""

    def test_extra_key_in_language_share_rejected(self) -> None:
        """An extra key in LanguageShare must raise ValidationError."""
        with pytest.raises(ValidationError) as exc_info:
            LanguageShare(
                language="en",
                fraction=0.95,
                rogue_extra_field="should_be_rejected",  # type: ignore[call-arg]
            )
        assert any(
            "rogue_extra_field" in str(e) or "extra" in str(e) for e in exc_info.value.errors()
        )

    def test_valid_language_share_accepted(self) -> None:
        """A well-formed LanguageShare must be accepted."""
        ls = LanguageShare(language="en", fraction=0.95)
        assert ls.language == "en"
        assert ls.fraction == pytest.approx(0.95)
