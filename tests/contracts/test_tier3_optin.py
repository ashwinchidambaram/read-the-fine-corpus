"""Tests for M-031/M-035 Tier 3 structural invariants.

M-031: tier3_enabled=True on a TransformationSettings requires tier3_settings with opt_in_ack=True.
       This is enforced structurally by a Pydantic model_validator, not a runtime check.

M-035: default_rule on IngestionConfig must never have tier3_enabled=True (never global).
       This is enforced structurally by a Pydantic model_validator on IngestionConfig.

These tests verify:
1. Enabling tier3 globally (on default_rule) is structurally impossible.
2. Per-class tier3 without opt_in_ack is rejected.
3. Per-class tier3 with opt_in_ack=True is accepted.
4. tier3_enabled=False with tier3_settings set is rejected.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from finecorpus.contracts.ingestion_config import (
    ChunkingConfig,
    ChunkingStrategy,
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
    Tier3Settings,
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


def _make_class_rule_with_tier3(*, tier3_settings: Tier3Settings) -> ClassRule:
    """Build a ClassRule with tier3 enabled and the given settings."""
    return ClassRule(
        segment_class=SegmentType.prose,
        transformation=TransformationSettings(
            tier1_enabled=True,
            tier1_operations=[],
            tier2_enabled=False,
            tier2_operations=[],
            tier3_enabled=True,
            tier3_settings=tier3_settings,
        ),
        chunking=_make_chunking(),
        metadata_schema=[],
        retrieval_treatment=_make_retrieval(),
    )


def _make_class_rule_no_tier3() -> ClassRule:
    """Build a ClassRule with tier3 disabled."""
    return ClassRule(
        segment_class=SegmentType.prose,
        transformation=TransformationSettings(
            tier1_enabled=True,
            tier1_operations=[],
            tier2_enabled=False,
            tier2_operations=[],
            tier3_enabled=False,
            tier3_settings=None,
        ),
        chunking=_make_chunking(),
        metadata_schema=[],
        retrieval_treatment=_make_retrieval(),
    )


def _minimal_ingestion_config(
    default_rule: ClassRule, class_rules: list[ClassRule] | None = None
) -> IngestionConfig:
    return IngestionConfig(
        schema_version="1.2.0",
        tenancy=_make_tenancy(),
        config_version="a" * 64,
        created_at=datetime(2026, 9, 1, tzinfo=UTC),
        naive_baseline=NaiveBaselineRef(reference_id="nb-v0", description="test"),
        class_rules=class_rules or [],
        default_rule=default_rule,
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
                target="/default_rule",
                basis=RecommendationBasis.heuristic,
                rationale="test",
            )
        ],
        class_descriptions=[],
        secret_free_attestation=True,
    )


# ---------------------------------------------------------------------------
# M-035: default_rule must never have tier3_enabled=True
# ---------------------------------------------------------------------------


class TestM035DefaultRuleNeverTier3:
    """M-035: enabling tier3 globally (on default_rule) is structurally impossible."""

    def test_default_rule_tier3_enabled_rejected(self) -> None:
        """IngestionConfig must reject a default_rule with tier3_enabled=True."""
        tier3 = Tier3Settings(model_ref="gpt-4o", opt_in_ack=True, diff_preview_required=False)
        global_rule = _make_class_rule_with_tier3(tier3_settings=tier3)

        with pytest.raises(ValidationError) as exc_info:
            _minimal_ingestion_config(default_rule=global_rule)

        errors = exc_info.value.errors()
        error_messages = " ".join(str(e) for e in errors)
        assert (
            "M-035" in error_messages
            or "default_rule" in error_messages
            or "never global" in error_messages
        ), f"Expected M-035 / default_rule / never-global in error, got: {errors}"

    def test_default_rule_tier3_disabled_accepted(self) -> None:
        """IngestionConfig accepts a default_rule with tier3_enabled=False."""
        safe_default = _make_class_rule_no_tier3()
        config = _minimal_ingestion_config(default_rule=safe_default)
        assert config.default_rule.transformation.tier3_enabled is False

    def test_per_class_rule_tier3_allowed(self) -> None:
        """class_rules entries MAY have tier3_enabled=True (per-class opt-in is fine)."""
        tier3 = Tier3Settings(model_ref="gpt-4o", opt_in_ack=True, diff_preview_required=False)
        per_class_rule = ClassRule(
            segment_class=SegmentType.table,  # Different class from default_rule
            transformation=TransformationSettings(
                tier1_enabled=True,
                tier1_operations=[],
                tier2_enabled=False,
                tier2_operations=[],
                tier3_enabled=True,
                tier3_settings=tier3,
            ),
            chunking=_make_chunking(),
            metadata_schema=[],
            retrieval_treatment=_make_retrieval(),
        )
        safe_default = _make_class_rule_no_tier3()
        # Should not raise — only default_rule is prohibited from tier3.
        config = _minimal_ingestion_config(default_rule=safe_default, class_rules=[per_class_rule])
        assert config.class_rules[0].transformation.tier3_enabled is True


# ---------------------------------------------------------------------------
# M-031: tier3_enabled=True requires tier3_settings with opt_in_ack=True
# ---------------------------------------------------------------------------


class TestM031Tier3PerClassOptIn:
    """M-031: TransformationSettings validator enforces tier3 opt-in structurally."""

    def test_tier3_enabled_without_settings_rejected(self) -> None:
        """tier3_enabled=True without tier3_settings must be rejected."""
        with pytest.raises(ValidationError) as exc_info:
            TransformationSettings(
                tier1_enabled=True,
                tier1_operations=[],
                tier2_enabled=False,
                tier2_operations=[],
                tier3_enabled=True,
                tier3_settings=None,  # Missing — should fail
            )
        errors = exc_info.value.errors()
        error_text = " ".join(str(e) for e in errors)
        assert (
            "tier3" in error_text.lower() or "opt_in" in error_text.lower() or "M-031" in error_text
        ), f"Expected tier3/opt-in error, got: {errors}"

    def test_tier3_enabled_with_opt_in_ack_accepted(self) -> None:
        """tier3_enabled=True with tier3_settings.opt_in_ack=True is valid."""
        tier3 = Tier3Settings(model_ref="gpt-4o", opt_in_ack=True, diff_preview_required=False)
        t = TransformationSettings(
            tier1_enabled=True,
            tier1_operations=[],
            tier2_enabled=False,
            tier2_operations=[],
            tier3_enabled=True,
            tier3_settings=tier3,
        )
        assert t.tier3_enabled is True
        assert t.tier3_settings is not None
        assert t.tier3_settings.opt_in_ack is True

    def test_tier3_disabled_with_settings_rejected(self) -> None:
        """tier3_enabled=False with tier3_settings set must be rejected (M-031 consistency)."""
        tier3 = Tier3Settings(model_ref="gpt-4o", opt_in_ack=True, diff_preview_required=False)
        with pytest.raises(ValidationError) as exc_info:
            TransformationSettings(
                tier1_enabled=True,
                tier1_operations=[],
                tier2_enabled=False,
                tier2_operations=[],
                tier3_enabled=False,
                tier3_settings=tier3,  # Should fail — can't have settings when disabled
            )
        errors = exc_info.value.errors()
        assert errors, "Expected ValidationError when tier3_settings set but tier3_enabled=False"

    def test_tier3_opt_in_ack_false_rejected(self) -> None:
        """Tier3Settings with opt_in_ack=False must be rejected at the field level."""
        with pytest.raises(ValidationError):
            Tier3Settings(
                model_ref="gpt-4o",
                opt_in_ack=False,  # type: ignore[arg-type]
                diff_preview_required=False,
            )

    def test_per_class_rule_with_tier3_requires_ack(self) -> None:
        """ClassRule with tier3 settings must have opt_in_ack=True to be valid."""
        # This is enforced by Tier3Settings.opt_in_ack: Literal[True] — False is rejected.
        with pytest.raises(ValidationError):
            ClassRule(
                segment_class=SegmentType.prose,
                transformation=TransformationSettings(
                    tier1_enabled=True,
                    tier1_operations=[],
                    tier2_enabled=True,
                    tier2_operations=[],
                    tier3_enabled=True,
                    tier3_settings=Tier3Settings(
                        model_ref="gpt-4o",
                        opt_in_ack=False,  # type: ignore[arg-type]
                        diff_preview_required=False,
                    ),
                ),
                chunking=_make_chunking(),
                metadata_schema=[],
                retrieval_treatment=_make_retrieval(),
            )
