"""Stage 4 — Plan (Phase 0 skeleton / Phase 1 unchanged).

Produces a minimal but contract-valid IngestionConfig:
  - default_rule covers all segment classes.
  - All recommendations provenance is 'heuristic' (labelled per §6.4 MUST).
  - secret_free_attestation=True (structural guarantee, §14.2).
  - No class_rules (skeleton — only default_rule).

The config_version is derived deterministically from a sha256 of the
build-affecting config fields (as required by §10.5).

D-26 resolution: now version-checks the consumed SegmentSetBatch via
SUPPORTED_SEGMENT_SET_BATCH (previously opted out with consumed_version_range=None).

Phase 2+ replaces _produce with real per-class planning.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

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
    Tier1Operation,
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
from finecorpus.contracts.versions import SUPPORTED_SEGMENT_SET_BATCH
from finecorpus.pipeline.stage import Stage

_INGESTION_CONFIG_SCHEMA_VERSION = "1.1.0"

# Fixed naive baseline reference (§9.3).
_NAIVE_BASELINE = NaiveBaselineRef(
    reference_id="naive-baseline-v0",
    description="Phase 0 skeleton: recursive_char chunking, dense retrieval, no augmentation.",
)


def _make_default_rule() -> ClassRule:
    """Build a minimal but fully-valid default ClassRule for all segment types."""
    return ClassRule(
        segment_class=SegmentType.prose,  # default_rule applies to any unmatched class
        transformation=TransformationSettings(
            tier1_enabled=True,
            tier1_operations=[Tier1Operation.whitespace_repair],
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
            atomic_rows=None,
            repeat_headers_on_split=None,
            split_boundaries=None,
        ),
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


def _make_default_embedding() -> EmbeddingConfig:
    """Build a minimal EmbeddingConfig (skeleton — no real model configured yet)."""
    return EmbeddingConfig(
        provider="ollama",
        model="nomic-embed-text",
        dimensions=768,
        normalize=True,
        supports_languages=["en"],
    )


def _derive_config_version(default_rule: ClassRule, embedding: EmbeddingConfig) -> str:
    """Derive a deterministic config_version from build-affecting fields (§10.5)."""
    # Serialize only the build-affecting fields (not retrieval-time-only fields).
    build_affecting = {
        "default_rule_chunking": default_rule.chunking.model_dump(mode="json"),
        "default_rule_transformation": default_rule.transformation.model_dump(mode="json"),
        "embedding": embedding.model_dump(mode="json"),
    }
    canonical = json.dumps(build_affecting, sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


class PlanStage(Stage):
    """Stage 4 — Plan (skeleton; D-26: now version-checks SegmentSetBatch).

    Consumes SegmentSetBatch, emits IngestionConfig.

    D-26 resolution: consumed_version_range is now SUPPORTED_SEGMENT_SET_BATCH
    instead of None — every stage boundary is now version-checked.

    Args:
        run_started_at: Single run timestamp threaded from the orchestrator.
            All stages share this timestamp so no stage calls wall-clock.
            Defaults to the collected_at value already established at run start.
    """

    name = "plan"
    consumed_contract = "segment_set_batch"
    consumed_version_range = SUPPORTED_SEGMENT_SET_BATCH  # D-26: version-checked now
    produced_contract = "ingestion_config"
    output_model = IngestionConfig

    def __init__(self, run_started_at: datetime | None = None) -> None:
        self._run_started_at = run_started_at or datetime.now(tz=UTC)

    def _produce(self, input_data: dict[str, Any] | None) -> dict[str, Any]:
        """Produce a minimal valid IngestionConfig."""
        assert input_data is not None, "Plan requires SegmentSetBatch input"

        # Extract tenancy from first segment set if available
        segment_sets = input_data.get("segment_sets", [])
        if segment_sets:
            tenancy_raw = segment_sets[0].get("tenancy", {})
        else:
            tenancy_raw = {}

        tenancy = TenancyBlock(
            workspace_id=tenancy_raw.get("workspace_id", ""),
            kb_id=tenancy_raw.get("kb_id", ""),
            permission_mode=tenancy_raw.get("permission_mode", PermissionMode.public_to_kb),
            permission_principals=tenancy_raw.get("permission_principals", []),
            permission_source=tenancy_raw.get("permission_source", PermissionSource.platform),
            permission_fidelity=tenancy_raw.get(
                "permission_fidelity", PermissionFidelity.authoritative
            ),
            permission_resolved_at=None,
        )

        default_rule = _make_default_rule()
        embedding = _make_default_embedding()
        config_version = _derive_config_version(default_rule, embedding)

        config = IngestionConfig(
            schema_version=_INGESTION_CONFIG_SCHEMA_VERSION,
            tenancy=tenancy,
            config_version=config_version,
            created_at=self._run_started_at,
            naive_baseline=_NAIVE_BASELINE,
            class_rules=[],  # No per-class rules in skeleton
            default_rule=default_rule,
            embedding=embedding,
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
                    rationale=(
                        "Phase 0 skeleton: default rule is a heuristic baseline "
                        "(recursive_char chunking, dense retrieval). "
                        "No configuration sweep has run yet."
                    ),
                )
            ],
            secret_free_attestation=True,
        )
        return config.model_dump(mode="json")
