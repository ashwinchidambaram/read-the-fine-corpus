"""Tests for config_version scope (M-015).

Verifies the pinned-scope contract:
- Editing a RetrievalTreatment value does NOT rotate config_version.
- Editing chunking max_tokens DOES rotate config_version.
- Editing the embedding model DOES rotate config_version.
- Editing a class description text DOES rotate config_version.

Checks both the derive_config_version function and the IngestionConfig
model-level derivation (via the stage or directly).
"""

from __future__ import annotations

from datetime import UTC, datetime

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
from finecorpus.contracts.versions import INGESTION_CONFIG_SCHEMA_VERSION
from finecorpus.pipeline.plan.config_version import derive_config_version

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_tenancy() -> TenancyBlock:
    return TenancyBlock(
        workspace_id="ws-test",
        kb_id="kb-test",
        permission_mode=PermissionMode.public_to_kb,
        permission_principals=[],
        permission_source=PermissionSource.platform,
        permission_fidelity=PermissionFidelity.authoritative,
    )


def _make_transformation() -> TransformationSettings:
    return TransformationSettings(
        tier1_enabled=True,
        tier1_operations=[],
        tier2_enabled=False,
        tier2_operations=[],
        tier3_enabled=False,
        tier3_settings=None,
    )


def _make_chunking(max_tokens: int = 512) -> ChunkingConfig:
    return ChunkingConfig(
        strategy=ChunkingStrategy.recursive_char,
        max_tokens=max_tokens,
        overlap_tokens=64,
        respect_headings=False,
        atomic_rows=None,
        repeat_headers_on_split=None,
        split_boundaries=None,
    )


def _make_class_rule(max_tokens: int = 512) -> ClassRule:
    return ClassRule(
        segment_class=SegmentType.prose,
        transformation=_make_transformation(),
        chunking=_make_chunking(max_tokens=max_tokens),
        embedding_override=None,
        metadata_schema=[],
        retrieval_treatment=RetrievalTreatment(
            default_salience_filter=[SalienceTier.primary, SalienceTier.supporting],
            salience_weights=None,
            rerank_eligible=False,
            strategy=RetrievalStrategy.dense,
            confidence_floor=None,
        ),
    )


def _make_embedding(model: str = "nomic-embed-text") -> EmbeddingConfig:
    return EmbeddingConfig(
        provider="ollama",
        model=model,
        dimensions=768,
        normalize=True,
        supports_languages=["en"],
    )


def _make_config(
    *,
    max_tokens: int = 512,
    embedding_model: str = "nomic-embed-text",
    class_descriptions: list[ClassDescription] | None = None,
    rerank_eligible: bool = False,
    confidence_floor: float | None = None,
) -> IngestionConfig:
    """Build a minimal valid IngestionConfig for testing config_version scope."""
    class_rule = _make_class_rule(max_tokens=max_tokens)
    # Override retrieval_treatment on the class rule for the retrieval-field test
    if rerank_eligible or confidence_floor is not None:
        class_rule = ClassRule(
            segment_class=SegmentType.prose,
            transformation=_make_transformation(),
            chunking=_make_chunking(max_tokens=max_tokens),
            embedding_override=None,
            metadata_schema=[],
            retrieval_treatment=RetrievalTreatment(
                default_salience_filter=[SalienceTier.primary, SalienceTier.supporting],
                salience_weights=None,
                rerank_eligible=rerank_eligible,
                strategy=RetrievalStrategy.dense,
                confidence_floor=confidence_floor,
            ),
        )

    return IngestionConfig(
        schema_version=INGESTION_CONFIG_SCHEMA_VERSION,
        tenancy=_make_tenancy(),
        config_version="0" * 64,  # placeholder; not used in scope tests
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        naive_baseline=NaiveBaselineRef(
            reference_id="naive-baseline-v0",
            description="Test baseline.",
        ),
        class_rules=[class_rule],
        default_rule=_make_class_rule(),
        embedding=_make_embedding(model=embedding_model),
        retrieval_defaults=RetrievalTreatment(
            default_salience_filter=[SalienceTier.primary, SalienceTier.supporting],
            salience_weights=None,
            rerank_eligible=False,
            strategy=RetrievalStrategy.dense,
            confidence_floor=None,
        ),
        language_support=LanguageSupportDecision(
            detected_languages=[],
            unsupported_languages=[],
            decision=LanguageDecision.proceed,
            cross_lingual_supported=None,
        ),
        spreadsheet_triage=[],
        exclusions_confirmed=[],
        provenance=[
            RecommendationProvenance(
                target="/default_rule",
                basis=RecommendationBasis.heuristic,
                sweep_run_id=None,
                rationale="Test provenance.",
            )
        ],
        class_descriptions=class_descriptions or [],
        secret_free_attestation=True,
    )


# ---------------------------------------------------------------------------
# M-015 scope tests
# ---------------------------------------------------------------------------


class TestRetrievalFieldsDoNotRotateVersion:
    """Editing a RetrievalTreatment value does NOT rotate config_version (M-015)."""

    def test_rerank_eligible_change_does_not_rotate(self):
        config_a = _make_config(rerank_eligible=False)
        config_b = _make_config(rerank_eligible=True)
        version_a = derive_config_version(config_a)
        version_b = derive_config_version(config_b)
        assert version_a == version_b, (
            "Changing rerank_eligible (retrieval-only field) must NOT rotate config_version"
        )

    def test_confidence_floor_change_does_not_rotate(self):
        config_a = _make_config(confidence_floor=None)
        config_b = _make_config(confidence_floor=0.75)
        version_a = derive_config_version(config_a)
        version_b = derive_config_version(config_b)
        assert version_a == version_b, (
            "Changing confidence_floor (retrieval-only field) must NOT rotate config_version"
        )

    def test_retrieval_strategy_change_does_not_rotate(self):
        """RetrievalTreatment.strategy is retrieval-only and must not affect config_version."""
        # Both configs have the same build-affecting fields; only retrieval fields differ
        config_a = _make_config()
        # Manually change retrieval_defaults.strategy (retrieval-only, not in class_rules)
        raw_b = config_a.model_dump(mode="json")
        raw_b["retrieval_defaults"]["strategy"] = "sparse"
        # Rebuild from dict
        config_b = IngestionConfig.model_validate(raw_b)
        version_a = derive_config_version(config_a)
        version_b = derive_config_version(config_b)
        assert version_a == version_b, (
            "Changing retrieval_defaults.strategy must NOT rotate config_version"
        )


class TestChunkingChangesRotateVersion:
    """Editing chunking max_tokens DOES rotate config_version."""

    def test_max_tokens_change_rotates_version(self):
        config_a = _make_config(max_tokens=512)
        config_b = _make_config(max_tokens=1024)
        version_a = derive_config_version(config_a)
        version_b = derive_config_version(config_b)
        assert version_a != version_b, (
            "Changing max_tokens (build-affecting field) MUST rotate config_version"
        )

    def test_chunking_strategy_change_rotates_version(self):
        config_a = _make_config()
        # Change strategy in class rule
        raw_b = config_a.model_dump(mode="json")
        raw_b["class_rules"][0]["chunking"]["strategy"] = "code_syntax"
        config_b = IngestionConfig.model_validate(raw_b)
        version_a = derive_config_version(config_a)
        version_b = derive_config_version(config_b)
        assert version_a != version_b, "Changing chunking strategy MUST rotate config_version"


class TestEmbeddingChangesRotateVersion:
    """Editing the embedding model DOES rotate config_version."""

    def test_embedding_model_change_rotates_version(self):
        config_a = _make_config(embedding_model="nomic-embed-text")
        config_b = _make_config(embedding_model="text-embedding-3-small")
        version_a = derive_config_version(config_a)
        version_b = derive_config_version(config_b)
        assert version_a != version_b, "Changing embedding model MUST rotate config_version"


class TestClassDescriptionChangesRotateVersion:
    """Editing a class description text DOES rotate config_version."""

    def test_class_description_text_change_rotates_version(self):
        desc_a = ClassDescription(
            segment_class=SegmentType.prose,
            class_id="prose",
            description="Policy narrative text.",
        )
        desc_b = ClassDescription(
            segment_class=SegmentType.prose,
            class_id="prose",
            description="Legal contract clauses.",
        )
        config_a = _make_config(class_descriptions=[desc_a])
        config_b = _make_config(class_descriptions=[desc_b])
        version_a = derive_config_version(config_a)
        version_b = derive_config_version(config_b)
        assert version_a != version_b, (
            "Changing class description text MUST rotate config_version (attack 8, S-R14)"
        )

    def test_adding_class_description_rotates_version(self):
        config_a = _make_config(class_descriptions=[])
        desc = ClassDescription(
            segment_class=SegmentType.prose,
            class_id="prose",
            description="Policy text.",
        )
        config_b = _make_config(class_descriptions=[desc])
        version_a = derive_config_version(config_a)
        version_b = derive_config_version(config_b)
        assert version_a != version_b, "Adding a class description MUST rotate config_version"

    def test_same_class_description_same_version(self):
        desc = ClassDescription(
            segment_class=SegmentType.prose,
            class_id="prose",
            description="Policy text.",
        )
        config_a = _make_config(class_descriptions=[desc])
        config_b = _make_config(class_descriptions=[desc])
        version_a = derive_config_version(config_a)
        version_b = derive_config_version(config_b)
        assert version_a == version_b, (
            "Identical class descriptions must produce the same config_version"
        )
