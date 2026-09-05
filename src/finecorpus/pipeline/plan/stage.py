"""Stage 4 — Plan (Phase 3: real heuristic recommendation engine).

Produces a complete IngestionConfig:
  - Per-class ClassRules encoding the §6.4 content matrix (corpus_stats + recommender).
  - All recommendation provenance labelled heuristic (M-025), or class_description when
    a class description influenced the value.
  - Language support decision (M-041): warns before ingestion when the configured embedding
    model does not declare a detected language, producing LanguageDecision.warned_proceed.
  - Exclusion decisions with per-group remediation text (M-040).
  - class_descriptions carried from the optional class-descriptions YAML.
  - secret_free_attestation=True (structural guarantee, §14.2).

The config_version is derived deterministically from the build-affecting fields
(§10.5 — M-015: only embedding/chunking changes rotate the version, not retrieval fields).

D-26 resolution: now version-checks the consumed SegmentSetBatch via
SUPPORTED_SEGMENT_SET_BATCH.

Phase 3 replaces the skeleton _produce with real per-class planning.
"""

from __future__ import annotations

import pathlib
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from finecorpus.contracts.ingestion_config import (
    ChunkingConfig,
    ChunkingStrategy,
    ClassDescription,
    ClassRule,
    EmbeddingConfig,
    ExclusionDecision,
    ExclusionDecisionReason,
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
from finecorpus.contracts.parse_result import LanguageShare
from finecorpus.contracts.shared.blocks import (
    PermissionFidelity,
    PermissionMode,
    PermissionSource,
    SalienceTier,
    SegmentType,
    TenancyBlock,
)
from finecorpus.contracts.versions import (
    INGESTION_CONFIG_SCHEMA_VERSION,
    SUPPORTED_SEGMENT_SET_BATCH,
)
from finecorpus.pipeline.plan.class_descriptions import (
    load_class_descriptions,
)
from finecorpus.pipeline.plan.config_version import derive_config_version
from finecorpus.pipeline.plan.corpus_stats import CorpusStats, compute_corpus_stats
from finecorpus.pipeline.plan.recommender import RecommendationResult, recommend
from finecorpus.pipeline.stage import Stage

if TYPE_CHECKING:
    from finecorpus.embedding.base import ProviderCapabilities

# Fixed naive baseline reference (§9.3).
_NAIVE_BASELINE = NaiveBaselineRef(
    reference_id="naive-baseline-v0",
    description="Phase 0 skeleton: recursive_char chunking, dense retrieval, no augmentation.",
)

# Languages considered "meaningful" for M-041 language warning.
# Languages below this share of corpus segments are not warned about.
_LANGUAGE_WARN_SHARE_FLOOR = 0.02  # 2% of corpus segments


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


def _compute_language_support(
    corpus_stats: CorpusStats,
    embedding: EmbeddingConfig,
    provider_capabilities: ProviderCapabilities | None,
) -> LanguageSupportDecision:
    """Compute the LanguageSupportDecision (M-041).

    Compares detected language distribution against the configured embedding
    provider's declared supported languages.  Unsupported languages with
    meaningful share (>= _LANGUAGE_WARN_SHARE_FLOOR) → warned_proceed.

    Args:
        corpus_stats: Corpus statistics from compute_corpus_stats.
        embedding: The EmbeddingConfig being used.
        provider_capabilities: Optional ProviderCapabilities from the configured
            embedding provider. When None, the check falls back to
            embedding.supports_languages from the EmbeddingConfig.

    Returns:
        LanguageSupportDecision with decision=proceed or warned_proceed.
    """
    # Determine which languages the model supports
    declared: list[str] | str
    if provider_capabilities is not None:
        declared = provider_capabilities.supported_languages
        cross_lingual = provider_capabilities.cross_lingual
    else:
        declared = embedding.supports_languages or []
        cross_lingual = None

    # Build language shares from corpus_stats
    total_segs = corpus_stats.total_segments
    lang_counts = corpus_stats.detected_languages

    if total_segs == 0 or not lang_counts:
        return LanguageSupportDecision(
            detected_languages=[],
            unsupported_languages=[],
            decision=LanguageDecision.proceed,
            cross_lingual_supported=cross_lingual,
        )

    # Build LanguageShare list (excluding 'und' / undetermined from support check)
    detected: list[LanguageShare] = []
    for lang, count in sorted(lang_counts.items(), key=lambda x: x[1], reverse=True):
        fraction = count / total_segs
        detected.append(LanguageShare(language=lang, fraction=round(fraction, 4)))

    # Determine unsupported languages with meaningful share
    unsupported: list[str] = []

    if declared is None:
        # No declared languages — cannot determine support; proceed without warning
        pass
    elif declared == "*":
        # Universal coverage — all languages supported
        pass
    else:
        supported_set = set(declared)
        for ls in detected:
            lang = ls.language
            fraction = ls.fraction
            if lang == "und":
                continue  # undetermined — skip support check
            if lang not in supported_set and fraction >= _LANGUAGE_WARN_SHARE_FLOOR:
                unsupported.append(lang)

    if unsupported:
        decision = LanguageDecision.warned_proceed
    else:
        decision = LanguageDecision.proceed

    return LanguageSupportDecision(
        detected_languages=detected,
        unsupported_languages=sorted(unsupported),
        decision=decision,
        cross_lingual_supported=cross_lingual,
    )


def _build_exclusion_decisions(
    batch: dict[str, Any],
) -> list[ExclusionDecision]:
    """Build ExclusionDecision list from SegmentSetBatch exclusion records.

    Maps ExclusionRecord.reason → ExclusionDecisionReason, with per-reason
    remediation text readable by a non-technical user (M-040).

    The completeness invariant: every ExclusionRecord present in the batch
    appears here. Nothing is silently dropped.
    """
    decisions: list[ExclusionDecision] = []
    seen_ids: set[str] = set()

    # ExclusionRecord reason → ExclusionDecisionReason mapping
    _reason_map: dict[str, ExclusionDecisionReason] = {
        "unservable_content": ExclusionDecisionReason.unservable_content,
        "spreadsheet_database": ExclusionDecisionReason.spreadsheet_database,
        "spreadsheet_model": ExclusionDecisionReason.spreadsheet_model,
        "encrypted": ExclusionDecisionReason.encrypted,
        "empty_region": ExclusionDecisionReason.empty_region,
        "superseded_version": ExclusionDecisionReason.superseded_version,
        "duplicate": ExclusionDecisionReason.duplicate,
        "parse_failed": ExclusionDecisionReason.parse_failed,
        "too_short": ExclusionDecisionReason.other,  # too_short maps to other
        "other": ExclusionDecisionReason.other,
    }

    for ss in batch.get("segment_sets", []):
        doc_id = ss.get("document_id", "")
        for exc in ss.get("exclusions", []):
            exc_id = exc.get("exclusion_id", "")
            if exc_id in seen_ids:
                continue
            seen_ids.add(exc_id)

            raw_reason = exc.get("reason", "other")
            decision_reason = _reason_map.get(raw_reason, ExclusionDecisionReason.other)
            remediation = _remediation_for_reason(raw_reason)

            decisions.append(
                ExclusionDecision(
                    document_id=doc_id,
                    reason=decision_reason,
                    remediation=remediation,
                )
            )

    return decisions


def _remediation_for_reason(reason: str) -> str:
    """Return plain-language remediation guidance for an exclusion reason (M-040).

    Includes the honest 'nothing to do — this is correct behavior' where applicable.
    """
    _remediation: dict[str, str] = {
        "unservable_content": (
            "Nothing to do — this content type (e.g. audio, video, CAD, image-only PDF) "
            "cannot be usefully indexed by a text embedding system. This is the correct "
            "outcome. If you need this content indexed, convert it to a text-bearing "
            "format before ingestion."
        ),
        "spreadsheet_database": (
            "Nothing to do — this spreadsheet is classified as a row-oriented database. "
            "Vectorizing tabular records produces confident nonsense because similarity is "
            "computed over cell values rather than semantic content. This is the correct outcome. "
            "If this is actually a narrative report, reclassify it via spreadsheet_triage override "
            "in the IngestionConfig (see corpus config diff / import commands)."
        ),
        "spreadsheet_model": (
            "Nothing to do — this spreadsheet is classified as a formula-driven model. "
            "Ingesting a values snapshot creates a stale artifact that looks authoritative "
            "but is actually a single point-in-time read. This is the correct outcome. "
            "If the spreadsheet contains significant narrative text, reclassify it via "
            "spreadsheet_triage override in the IngestionConfig."
        ),
        "encrypted": (
            "Decrypt the file and re-run the pipeline, or remove the document from the "
            "source directory if it should not be indexed. Encrypted files cannot be parsed "
            "and must be handled before ingestion."
        ),
        "empty_region": (
            "Nothing to do — this region contained no extractable text. "
            "This is the correct outcome for blank sections, image-only pages, or "
            "regions the OCR pipeline could not process. No action is required."
        ),
        "superseded_version": (
            "Nothing to do — this document is a near-duplicate of a newer version in the "
            "same version family. Only the newest version is indexed by default. "
            "Set index_superseded_versions=true in IngestionConfig to include older versions "
            "in the index."
        ),
        "duplicate": (
            "Remove exact duplicate files from the source directory, or nothing — "
            "only one copy will be indexed. Exact duplicates (same content hash) are "
            "deduplicated automatically; no user action is required."
        ),
        "parse_failed": (
            "Investigate why the document could not be parsed. Common causes: "
            "password protection (decrypt first), file corruption (check file integrity), "
            "unsupported encoding (convert to UTF-8), or missing OCR dependencies "
            "(install tesseract for scanned PDFs). Re-run after fixing the root cause."
        ),
        "too_short": (
            "Nothing to do — this content span is below the minimum segment length threshold "
            "and would produce a low-quality chunk. Short trailing paragraphs, post-heading "
            "remainders, and other brief spans are excluded to maintain chunk quality. "
            "This is the correct outcome."
        ),
        "other": (
            "Review the exclusion report (corpus report) for more detail on this specific "
            "exclusion. If the content should be indexed, contact the corpus administrator."
        ),
    }
    return _remediation.get(reason, _remediation["other"])


class PlanStage(Stage):
    """Stage 4 — Plan (Phase 3: real heuristic recommendation engine).

    Consumes SegmentSetBatch, emits IngestionConfig.

    D-26 resolution: consumed_version_range is now SUPPORTED_SEGMENT_SET_BATCH
    instead of None — every stage boundary is now version-checked.

    The real implementation computes corpus statistics from the SegmentSetBatch,
    runs the heuristic recommender to produce per-class ClassRules encoding the
    §6.4 content matrix, performs the M-041 language support check, and assembles
    the complete IngestionConfig.

    Args:
        run_started_at: Single run timestamp threaded from the orchestrator.
            All stages share this timestamp so no stage calls wall-clock.
            Defaults to now() when None.
        class_descriptions_path: Optional path to a YAML file containing per-class
            description text (see pipeline/plan/class_descriptions.py for schema).
            When provided, descriptions influence Tier 2 class_context augmentation
            and the provenance basis for affected fields.
        provider_capabilities: Optional ProviderCapabilities from the configured
            embedding provider, used for the M-041 language support check.
            When None, the check falls back to EmbeddingConfig.supports_languages.
    """

    name = "plan"
    consumed_contract = "segment_set_batch"
    consumed_version_range = SUPPORTED_SEGMENT_SET_BATCH  # D-26: version-checked now
    produced_contract = "ingestion_config"
    output_model = IngestionConfig

    def __init__(
        self,
        run_started_at: datetime | None = None,
        class_descriptions_path: str | pathlib.Path | None = None,
        provider_capabilities: ProviderCapabilities | None = None,
    ) -> None:
        self._run_started_at = run_started_at or datetime.now(tz=UTC)
        self._class_descriptions_path = (
            pathlib.Path(class_descriptions_path) if class_descriptions_path is not None else None
        )
        self._provider_capabilities = provider_capabilities

    def _produce(self, input_data: dict[str, Any] | None) -> dict[str, Any]:
        """Produce a complete IngestionConfig via heuristic corpus analysis."""
        assert input_data is not None, "Plan requires SegmentSetBatch input"

        # --- Extract tenancy from first segment set ---
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

        # --- Load class descriptions (optional) ---
        class_descriptions: list[ClassDescription] = []
        if self._class_descriptions_path is not None:
            class_descriptions = load_class_descriptions(self._class_descriptions_path)

        # --- Compute corpus statistics ---
        corpus_stats: CorpusStats = compute_corpus_stats(input_data)

        # --- Run heuristic recommender ---
        rec: RecommendationResult = recommend(corpus_stats, class_descriptions)

        # --- Embedding config ---
        embedding = _make_default_embedding()

        # --- Language support decision (M-041) ---
        language_support = _compute_language_support(
            corpus_stats, embedding, self._provider_capabilities
        )

        # --- Exclusion decisions (M-040) ---
        exclusions_confirmed = _build_exclusion_decisions(input_data)

        # --- Default rule ---
        default_rule = _make_default_rule()

        # --- Build provenance list ---
        # Start with recommender provenance, add default_rule provenance, add language warning
        all_provenance: list[RecommendationProvenance] = list(rec.provenance)

        all_provenance.append(
            RecommendationProvenance(
                target="/default_rule",
                basis=RecommendationBasis.heuristic,
                sweep_run_id=None,
                rationale=(
                    "Heuristic: default_rule is a conservative baseline "
                    "(recursive_char chunking, dense retrieval, Tier 1 whitespace repair). "
                    "It applies to any segment class not explicitly listed in class_rules. "
                    "No configuration sweep has run — all recommendations are heuristic (M-025)."
                ),
            )
        )

        # Language warning provenance (M-041)
        if language_support.decision == LanguageDecision.warned_proceed:
            lang_list = ", ".join(language_support.unsupported_languages)
            all_provenance.append(
                RecommendationProvenance(
                    target="/language_support",
                    basis=RecommendationBasis.heuristic,
                    sweep_run_id=None,
                    rationale=(
                        f"M-041 language warning: the configured embedding model "
                        f"({embedding.provider}/{embedding.model}) declares support for "
                        f"{embedding.supports_languages} but the corpus contains segments "
                        f"in: {lang_list}. "
                        "Embedding unsupported languages produces vectors that are quietly "
                        "meaningless — retrieval quality for these languages will be degraded. "
                        "Decision: warned_proceed. To resolve: choose an embedding model that "
                        "declares support for these languages, or accept the degraded retrieval "
                        "quality for unsupported-language content."
                    ),
                )
            )

        # --- Assemble partial config for config_version derivation ---
        partial_config = IngestionConfig(
            schema_version=INGESTION_CONFIG_SCHEMA_VERSION,
            tenancy=tenancy,
            config_version="0" * 64,  # placeholder; will be replaced below
            created_at=self._run_started_at,
            naive_baseline=_NAIVE_BASELINE,
            class_rules=rec.class_rules,
            default_rule=default_rule,
            embedding=embedding,
            retrieval_defaults=RetrievalTreatment(
                default_salience_filter=[SalienceTier.primary, SalienceTier.supporting],
                salience_weights=None,
                rerank_eligible=False,
                strategy=RetrievalStrategy.dense,
                confidence_floor=None,
            ),
            language_support=language_support,
            spreadsheet_triage=[],
            exclusions_confirmed=exclusions_confirmed,
            provenance=all_provenance,
            class_descriptions=class_descriptions,
            secret_free_attestation=True,
        )
        config_version = derive_config_version(partial_config)

        # --- Assemble final config ---
        config = IngestionConfig(
            schema_version=INGESTION_CONFIG_SCHEMA_VERSION,
            tenancy=tenancy,
            config_version=config_version,
            created_at=self._run_started_at,
            naive_baseline=_NAIVE_BASELINE,
            class_rules=rec.class_rules,
            default_rule=default_rule,
            embedding=embedding,
            retrieval_defaults=RetrievalTreatment(
                default_salience_filter=[SalienceTier.primary, SalienceTier.supporting],
                salience_weights=None,
                rerank_eligible=False,
                strategy=RetrievalStrategy.dense,
                confidence_floor=None,
            ),
            language_support=language_support,
            spreadsheet_triage=[],
            exclusions_confirmed=exclusions_confirmed,
            provenance=all_provenance,
            class_descriptions=class_descriptions,
            secret_free_attestation=True,
        )
        return config.model_dump(mode="json")
