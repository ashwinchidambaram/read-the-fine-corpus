"""Tests for PR #4 validator findings (F1–F5).

Covers the new model-level invariants enforced in blocks.py and ingestion_config.py:
- F1: TransformationRecord tier=2 with changed_text=True is rejected.
- F2: TenancyBlock source_mirrored mode requires connector source.
- F3: Tier3Settings.opt_in_ack=False rejected;
      IngestionConfig.secret_free_attestation=False rejected.
- F4: Provenance.source_document_id empty string rejected.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from finecorpus.contracts.ingestion_config import (
    Tier3Settings,
)
from finecorpus.contracts.shared.blocks import (
    AppliedBy,
    LocatorKind,
    PermissionFidelity,
    PermissionMode,
    PermissionSource,
    Provenance,
    SalienceSignal,
    SalienceSignalKind,
    SalienceTier,
    SegmentType,
    SourceLocation,
    TenancyBlock,
    TransformationRecord,
    TransformationTier,
    TrustLevel,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_source_location() -> SourceLocation:
    return SourceLocation(locator_kind=LocatorKind.page, page_start=1, page_end=1)


def make_provenance(**overrides) -> Provenance:
    base = dict(
        source_document_id="01JDOC00001",
        source_document_version="9f2c8b1e77a4d0c3e5b19a24d8f6c0b2e4a7913d5c8f0a2b4d6e8f1a3c5b7d9e0",
        source_location=make_source_location(),
        structural_path=[],
        transformations=[],
        confidence=1.0,
        ocr_confidence=None,
        segment_type=SegmentType.prose,
        salience_tier=SalienceTier.primary,
        salience_basis=SalienceSignalKind.segment_type_prior,
        salience_signals=[
            SalienceSignal(
                kind=SalienceSignalKind.segment_type_prior,
                implied_tier=SalienceTier.primary,
                won=True,
            )
        ],
        language="en",
        injection_suspicion=0.0,
        invisible_content_flags=[],
        sensitivity_flags=[],
        trust_level=TrustLevel.untrusted_ingested,
    )
    base.update(overrides)
    return Provenance(**base)


def make_tenancy(**overrides) -> TenancyBlock:
    base = dict(
        workspace_id="01JWSPACE001",
        kb_id="01JKB000001",
        permission_mode=PermissionMode.public_to_kb,
        permission_principals=[],
        permission_source=PermissionSource.platform,
        permission_fidelity=PermissionFidelity.authoritative,
    )
    base.update(overrides)
    return TenancyBlock(**base)


# ---------------------------------------------------------------------------
# F1 — Tier 2 changed_text=True rejected
# ---------------------------------------------------------------------------


class TestTier2ChangedTextRejected:
    """F1: TransformationRecord with tier=2 and changed_text=True must raise ValidationError."""

    def test_tier2_changed_text_true_rejected(self) -> None:
        """Tier 2 never touches text — changed_text=True must be rejected (§7.2)."""
        with pytest.raises(ValidationError) as exc_info:
            TransformationRecord(
                tier=TransformationTier.tier_2,
                operation="breadcrumb_augment",
                applied_by=AppliedBy.model,
                model_ref="openai/gpt-4o-mini",
                changed_text=True,
            )
        errors = exc_info.value.errors()
        assert any("changed_text" in str(e) or "tier" in str(e) for e in errors), (
            f"Expected error mentioning changed_text or tier, got: {errors}"
        )

    def test_tier2_changed_text_false_accepted(self) -> None:
        """Tier 2 with changed_text=False is valid."""
        rec = TransformationRecord(
            tier=TransformationTier.tier_2,
            operation="breadcrumb_augment",
            applied_by=AppliedBy.model,
            model_ref="openai/gpt-4o-mini",
            changed_text=False,
        )
        assert rec.changed_text is False

    def test_tier1_changed_text_true_accepted(self) -> None:
        """Tier 1 may set changed_text=True (D-11, §7.2 Tier 1 may alter text)."""
        rec = TransformationRecord(
            tier=TransformationTier.tier_1,
            operation="whitespace_repair",
            applied_by=AppliedBy.deterministic,
            changed_text=True,
        )
        assert rec.changed_text is True

    def test_tier3_changed_text_true_accepted(self) -> None:
        """Tier 3 rewrite may set changed_text=True."""
        rec = TransformationRecord(
            tier=TransformationTier.tier_3,
            operation="rewrite",
            applied_by=AppliedBy.model,
            model_ref="openai/gpt-4o",
            changed_text=True,
        )
        assert rec.changed_text is True

    def test_tier3_changed_text_false_accepted(self) -> None:
        """Tier 3 with changed_text=False is also valid (no-op rewrite or inspection pass)."""
        rec = TransformationRecord(
            tier=TransformationTier.tier_3,
            operation="rewrite",
            applied_by=AppliedBy.model,
            model_ref="openai/gpt-4o",
            changed_text=False,
        )
        assert rec.changed_text is False


# ---------------------------------------------------------------------------
# F2 — source_mirrored requires connector
# ---------------------------------------------------------------------------


class TestSourceMirroredRequiresConnector:
    """F2: permission_mode=source_mirrored requires permission_source=connector."""

    def test_source_mirrored_platform_rejected(self) -> None:
        """source_mirrored + platform source must be rejected."""
        with pytest.raises(ValidationError) as exc_info:
            make_tenancy(
                permission_mode=PermissionMode.source_mirrored,
                permission_source=PermissionSource.platform,
            )
        errors = exc_info.value.errors()
        assert any("source_mirrored" in str(e) or "connector" in str(e) for e in errors), (
            f"Expected error mentioning source_mirrored or connector, got: {errors}"
        )

    def test_source_mirrored_manual_rejected(self) -> None:
        """source_mirrored + manual source must be rejected."""
        with pytest.raises(ValidationError):
            make_tenancy(
                permission_mode=PermissionMode.source_mirrored,
                permission_source=PermissionSource.manual,
            )

    def test_source_mirrored_connector_accepted(self) -> None:
        """source_mirrored + connector source is valid."""
        tb = make_tenancy(
            permission_mode=PermissionMode.source_mirrored,
            permission_source=PermissionSource.connector,
        )
        assert tb.permission_mode == PermissionMode.source_mirrored
        assert tb.permission_source == PermissionSource.connector

    def test_public_to_kb_platform_accepted(self) -> None:
        """Other permission_mode values remain unconstrained on permission_source."""
        tb = make_tenancy(
            permission_mode=PermissionMode.public_to_kb,
            permission_source=PermissionSource.platform,
        )
        assert tb.permission_mode == PermissionMode.public_to_kb


# ---------------------------------------------------------------------------
# F3 — Literal[True] fields
# ---------------------------------------------------------------------------


class TestLiteralTrueFields:
    """F3: opt_in_ack and secret_free_attestation must be Literal[True] — False is rejected."""

    def test_opt_in_ack_false_rejected(self) -> None:
        """Tier3Settings with opt_in_ack=False must be rejected."""
        with pytest.raises(ValidationError):
            Tier3Settings(
                model_ref="openai/gpt-4o",
                opt_in_ack=False,  # type: ignore[arg-type]
                diff_preview_required=True,
            )

    def test_opt_in_ack_true_accepted(self) -> None:
        """Tier3Settings with opt_in_ack=True is valid."""
        t3 = Tier3Settings(
            model_ref="openai/gpt-4o",
            opt_in_ack=True,
            diff_preview_required=True,
        )
        assert t3.opt_in_ack is True

    def test_secret_free_attestation_false_rejected(self) -> None:
        """IngestionConfig with secret_free_attestation=False must be rejected at field level."""
        # Validate directly that the field type rejects False — use model_validate on a dict
        # that would otherwise be valid, and confirm the error is on this field.
        from datetime import UTC, datetime

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
            TransformationSettings,
        )
        from finecorpus.contracts.parse_result import LanguageShare
        from finecorpus.contracts.shared.blocks import SalienceTier, SegmentType

        tenancy = make_tenancy()
        default_rule = ClassRule(
            segment_class=SegmentType.unknown,
            transformation=TransformationSettings(
                tier1_enabled=True,
                tier1_operations=[],
                tier2_enabled=False,
                tier2_operations=[],
                tier3_enabled=False,
            ),
            chunking=ChunkingConfig(
                strategy=ChunkingStrategy.recursive_char,
                max_tokens=512,
                overlap_tokens=0,
                respect_headings=False,
            ),
            metadata_schema=[],
            retrieval_treatment=RetrievalTreatment(
                default_salience_filter=[SalienceTier.primary],
                rerank_eligible=False,
                strategy=RetrievalStrategy.dense,
            ),
        )
        with pytest.raises(ValidationError) as exc_info:
            IngestionConfig(
                schema_version="1.0.0",
                tenancy=tenancy,
                config_version="a" * 64,
                created_at=datetime(2026, 9, 1, tzinfo=UTC),
                naive_baseline=NaiveBaselineRef(
                    reference_id="baseline-v1", description="Test baseline"
                ),
                class_rules=[],
                default_rule=default_rule,
                embedding=EmbeddingConfig(
                    provider="openai",
                    model="text-embedding-3-large",
                    dimensions=3072,
                    normalize=True,
                ),
                retrieval_defaults=RetrievalTreatment(
                    default_salience_filter=[SalienceTier.primary],
                    rerank_eligible=False,
                    strategy=RetrievalStrategy.dense,
                ),
                language_support=LanguageSupportDecision(
                    detected_languages=[LanguageShare(language="en", fraction=1.0)],
                    unsupported_languages=[],
                    decision=LanguageDecision.proceed,
                ),
                spreadsheet_triage=[],
                exclusions_confirmed=[],
                provenance=[
                    RecommendationProvenance(
                        target="/embedding/model",
                        basis=RecommendationBasis.heuristic,
                        rationale="Default model.",
                    )
                ],
                secret_free_attestation=False,  # type: ignore[arg-type]
            )
        errors = exc_info.value.errors()
        fields = [e["loc"][-1] for e in errors]
        assert "secret_free_attestation" in fields, (
            f"Expected secret_free_attestation in error fields, got: {fields}"
        )


# ---------------------------------------------------------------------------
# F4 — min_length=1 on identity fields
# ---------------------------------------------------------------------------


class TestProvenanceIdentityFieldMinLength:
    """F4: source_document_id (and source_document_version) empty string must be rejected."""

    def test_empty_source_document_id_rejected(self) -> None:
        """source_document_id='' must raise ValidationError (min_length=1)."""
        with pytest.raises(ValidationError) as exc_info:
            make_provenance(source_document_id="")
        errors = exc_info.value.errors()
        fields = [e["loc"][-1] for e in errors]
        assert "source_document_id" in fields, (
            f"Expected source_document_id in error fields, got: {fields}"
        )

    def test_empty_source_document_version_rejected(self) -> None:
        """source_document_version='' must raise ValidationError (min_length=1)."""
        with pytest.raises(ValidationError) as exc_info:
            make_provenance(source_document_version="")
        errors = exc_info.value.errors()
        fields = [e["loc"][-1] for e in errors]
        assert "source_document_version" in fields, (
            f"Expected source_document_version in error fields, got: {fields}"
        )

    def test_nonempty_source_document_id_accepted(self) -> None:
        """Nonempty source_document_id is accepted."""
        prov = make_provenance(source_document_id="01JDOC00001")
        assert prov.source_document_id == "01JDOC00001"
