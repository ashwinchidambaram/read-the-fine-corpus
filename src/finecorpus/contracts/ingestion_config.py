"""Contract 4 — Ingestion config.

Stage boundary: Plan → Build.
See docs/contracts/ingestion-config.md for the authoritative spec
(§6.4, §12, §14.2, §10.5).

The Ingestion config fully determines Build output (§6.4, §12). It has no implicit
defaults resolved at build time (§12). It is secret-free by construction (§14.2):
no secret-typed fields exist, so it can be committed to version control. Its
config_version participates in chunk identity (§10.5).

Schema version history:
- 1.0.0: initial contract.
- 1.1.0: added ChunkingConfig.tokenizer (MINOR — backward-compatible).
- 1.2.0: added ClassDescription / class_descriptions; M-032/M-033/M-034 flag fields;
  extra="forbid" on all models; Tier-3 structural validators (M-031/M-035);
  config_version derivation moved to pipeline.plan.config_version (MINOR).
"""

from __future__ import annotations

import json
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from finecorpus.contracts.parse_result import LanguageShare
from finecorpus.contracts.shared.blocks import SalienceTier, SegmentType, TenancyBlock

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class ChunkingStrategy(StrEnum):
    """Splitter type for chunking (§6.4 content matrix).

    See docs/contracts/ingestion-config.md ChunkingStrategy.strategy.
    """

    recursive_char = "recursive_char"
    structure_aware = "structure_aware"
    table_atomic = "table_atomic"
    code_syntax = "code_syntax"
    semantic = "semantic"


class RetrievalStrategy(StrEnum):
    """Default retrieval mode for a class.

    See docs/contracts/ingestion-config.md RetrievalTreatment.strategy.
    """

    dense = "dense"
    sparse = "sparse"
    hybrid = "hybrid"


class LanguageDecision(StrEnum):
    """Pre-ingestion decision for language support (§7.6).

    See docs/contracts/ingestion-config.md LanguageSupportDecision.decision.
    """

    proceed = "proceed"
    warned_proceed = "warned_proceed"
    """Requires a recorded acknowledgement (§7.6)."""
    blocked = "blocked"


class SpreadsheetKind(StrEnum):
    """Fixed triage for a spreadsheet (§6.4).

    See docs/contracts/ingestion-config.md SpreadsheetTriage.kind.
    """

    report = "report"
    """Report kind — ingested."""
    database = "database"
    """Database kind — excluded as unservable."""
    model = "model"
    """Model kind — excluded as unservable."""


class SpreadsheetDisposition(StrEnum):
    """What to do with a spreadsheet after triage (§6.4).

    See docs/contracts/ingestion-config.md SpreadsheetTriage.disposition.
    """

    ingest = "ingest"
    exclude_unservable = "exclude_unservable"


class TriageSource(StrEnum):
    """How the triage was determined — visible and overridable (§6.4).

    See docs/contracts/ingestion-config.md SpreadsheetTriage.source.
    """

    detected = "detected"
    user_override = "user_override"


class ExclusionDecisionReason(StrEnum):
    """Why content was excluded — matches SegmentSet exclusion reasons.

    See docs/contracts/ingestion-config.md ExclusionDecision.reason.
    """

    unservable_content = "unservable_content"
    spreadsheet_database = "spreadsheet_database"
    spreadsheet_model = "spreadsheet_model"
    encrypted = "encrypted"
    empty_region = "empty_region"
    superseded_version = "superseded_version"
    duplicate = "duplicate"
    parse_failed = "parse_failed"
    other = "other"


class RecommendationBasis(StrEnum):
    """Evidence class for a recommendation (§6.4).

    See docs/contracts/ingestion-config.md RecommendationProvenance.basis.
    heuristic MUST be labelled as such (§6.4).
    """

    heuristic = "heuristic"
    sweep_backed = "sweep_backed"
    user_set = "user_set"
    class_description = "class_description"


class MetadataFieldType(StrEnum):
    """Type of a metadata field.

    See docs/contracts/ingestion-config.md MetadataField.type.
    """

    str_ = "str"
    int_ = "int"
    float_ = "float"
    bool_ = "bool"
    datetime_ = "datetime"
    list_str = "list_str"


class MetadataFieldSource(StrEnum):
    """Where the metadata field value comes from.

    See docs/contracts/ingestion-config.md MetadataField.source.
    """

    provenance = "provenance"
    source_metadata = "source_metadata"
    derived = "derived"


class Tier1Operation(StrEnum):
    """Tier 1 structure normalization operations.

    See docs/contracts/ingestion-config.md TransformationSettings.tier1_operations.
    Note: boilerplate_strip is NOT in this list — boilerplate is handled structurally
    (a boilerplate-typed segment tier-filtered at retrieval), never by removing bytes
    from another chunk's text (R6, C-R1/C-R10).
    """

    ocr_cleanup = "ocr_cleanup"
    table_to_markdown = "table_to_markdown"
    whitespace_repair = "whitespace_repair"
    header_inference = "header_inference"


class Tier2Operation(StrEnum):
    """Tier 2 contextual augmentation operations.

    See docs/contracts/ingestion-config.md TransformationSettings.tier2_operations.
    Augmentation goes in separate fields, never merged into chunk text.
    """

    breadcrumb_augment = "breadcrumb_augment"
    table_description = "table_description"
    class_context = "class_context"


# ---------------------------------------------------------------------------
# Sub-models
# ---------------------------------------------------------------------------


class MetadataField(BaseModel):
    """Declares an extra payload field carried by chunks of a segment class.

    See docs/contracts/ingestion-config.md MetadataField.
    Provenance fields are always present regardless.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="Field name.")
    type: MetadataFieldType = Field(description="Value type.")
    filterable: bool = Field(description="Whether this field is indexed as a retrieval filter.")
    source: MetadataFieldSource = Field(description="Where the value comes from.")


class Tier3Settings(BaseModel):
    """Tier 3 rewriting settings (§7.2 MUSTs).

    See docs/contracts/ingestion-config.md Tier3Settings.
    Required when tier3_enabled=True. model_ref is a name, never a secret.
    """

    model_config = ConfigDict(extra="forbid")

    model_ref: str = Field(
        description="Model identity for the rewriter. A name, not a secret (§14.2)."
    )
    opt_in_ack: Literal[True] = Field(
        description="Explicit opt-in acknowledgement required (§7.2 Tier 3 MUST). Must be True."
    )
    diff_preview_required: bool = Field(
        description="Whether a diff preview is required before accepting (§7.2)."
    )


class NaiveBaselineRef(BaseModel):
    """Reference to the fixed §9.3 naive baseline.

    See docs/contracts/ingestion-config.md NaiveBaselineRef.
    Pins the fixed §9.3 reference config; if it ever changes, prior baselines are
    marked against the old reference (§9.3).
    """

    model_config = ConfigDict(extra="forbid")

    reference_id: str = Field(description="Identity of the reference config.")
    description: str = Field(description="Human-readable description of the baseline.")


class TransformationSettings(BaseModel):
    """Which transformation tiers are on and their parameters.

    See docs/contracts/ingestion-config.md TransformationSettings.
    All three tiers are explicit — no implicit set.

    M-031/M-035 structural invariants (validated here):
    - tier3_enabled=True requires tier3_settings.opt_in_ack=True.
    - tier3_enabled=False requires tier3_settings=None.

    M-032/M-033/M-034 flag fields (contract shape; behavior is Phase 7):
    - retain_original_ref: whether the original-retained reference must be preserved.
    - diff_preview_required: whether a diff preview must be confirmed before accepting.
    - mark_rewritten_chunks: whether rewritten chunks carry a provenance flag.
    """

    model_config = ConfigDict(extra="forbid")

    tier1_enabled: bool = Field(description="Structure normalization (default on, §7.2).")
    tier1_operations: list[Tier1Operation] = Field(
        description=(
            "Which Tier 1 ops are enabled. Explicit — no implicit set. "
            "No boilerplate_strip: boilerplate is handled structurally (R6, C-R1/C-R10)."
        )
    )
    tier2_enabled: bool = Field(
        description=(
            "Contextual augmentation (default on, §7.2). "
            "Augmentation goes in separate fields, never merged into chunk text."
        )
    )
    tier2_operations: list[Tier2Operation] = Field(description="Which Tier 2 ops are enabled.")
    tier3_enabled: bool = Field(
        description="Full rewriting (default off, §7.2). Per-class opt-in only."
    )
    tier3_settings: Tier3Settings | None = Field(
        default=None,
        description=(
            "Required when tier3_enabled; carries the opt-in acknowledgement and model ref."
        ),
    )
    # M-032: original-retained reference field (contract shape; behavior Phase 7)
    retain_original_ref: bool = Field(
        default=False,
        description=(
            "M-032: whether the original-retained reference is preserved for Tier 3 chunks. "
            "Contract field only — behavior implemented in Phase 7."
        ),
    )
    # M-033: diff-preview-required (contract shape; behavior Phase 7)
    diff_preview_required: bool = Field(
        default=False,
        description=(
            "M-033: whether a diff preview must be confirmed before accepting Tier 3 output. "
            "Contract field only — behavior implemented in Phase 7."
        ),
    )
    # M-034: rewritten-chunk flag (contract shape; behavior Phase 7)
    mark_rewritten_chunks: bool = Field(
        default=False,
        description=(
            "M-034: whether rewritten chunks carry a provenance flag in their payload. "
            "Contract field only — behavior implemented in Phase 7."
        ),
    )

    @model_validator(mode="after")
    def _validate_tier3_structural(self) -> TransformationSettings:
        """M-031: tier3_enabled=True requires tier3_settings with opt_in_ack=True (structural).

        This is a Pydantic validator, not a runtime check — the shape itself
        enforces the invariant so it cannot be silently bypassed.
        """
        if self.tier3_enabled:
            if self.tier3_settings is None:
                raise ValueError(
                    "tier3_enabled=True requires tier3_settings to be provided "
                    "(M-031: Tier 3 per-class opt-in requires explicit acknowledgement). "
                    "Set tier3_settings with opt_in_ack=True."
                )
            # opt_in_ack is Literal[True] on Tier3Settings — already enforced by Pydantic.
            # This assert is belt-and-braces documentation.
            assert self.tier3_settings.opt_in_ack is True  # noqa: S101
        else:
            if self.tier3_settings is not None:
                raise ValueError(
                    "tier3_enabled=False but tier3_settings is set. "
                    "Set tier3_settings=None when tier3 is disabled."
                )
        return self


class ChunkingConfig(BaseModel):
    """Chunking strategy and parameters (§6.4).

    See docs/contracts/ingestion-config.md ChunkingStrategy.
    """

    model_config = ConfigDict(extra="forbid")

    strategy: ChunkingStrategy = Field(description="Splitter type (§6.4 content matrix).")
    max_tokens: int = Field(description="Target chunk size (tokens, whitespace-word proxy).")
    overlap_tokens: int = Field(
        description="Overlap between adjacent chunks (tokens, whitespace-word proxy)."
    )
    tokenizer: Literal["whitespace_word"] = Field(
        default="whitespace_word",
        description=(
            "Token-counting method used by the chunker. "
            "Currently fixed to 'whitespace_word' (len(text.split())), the Phase 1 naive "
            "baseline (§9.3). Encoded here so that any future tokenizer change forces a "
            "config_version change → full rebuild (§10.5). "
            "WARNING: switching tokenizers requires changing this field, which changes "
            "config_version, which invalidates every existing chunk at existing point_ids. "
            "Without the config_version change, re-indexing would silently corrupt chunk "
            "text at existing point_ids."
        ),
    )
    respect_headings: bool = Field(description="Structure-aware boundary respect (prose, §6.4).")
    atomic_rows: bool | None = Field(
        default=None,
        description="Tables: never split rows from headers (§6.4).",
    )
    repeat_headers_on_split: bool | None = Field(
        default=None,
        description="Tables too large to keep atomic: repeat headers into each fragment (§6.4).",
    )
    split_boundaries: list[str] | None = Field(
        default=None,
        description="Code: function/class boundaries (§6.4).",
    )


class EmbeddingConfig(BaseModel):
    """Embedding model identity and parameters.

    See docs/contracts/ingestion-config.md EmbeddingConfig.
    Reference by name; the credential lives elsewhere (§14.2), never here.
    """

    model_config = ConfigDict(extra="forbid")

    provider: str = Field(
        description="e.g. openai, ollama. Reference by name; credential lives elsewhere (§14.2)."
    )
    model: str = Field(
        description="Model identity (§8 chunk provenance ties to it; §15 mismatch fails closed)."
    )
    dimensions: int = Field(
        description="Vector dimensionality, for index sizing and mismatch detection."
    )
    normalize: bool = Field(description="Whether vectors are normalized.")
    supports_languages: list[str] | None = Field(
        default=None,
        description="Languages the model supports (BCP-47), for the §7.6 capability check.",
    )


class RetrievalTreatment(BaseModel):
    """Default retrieval treatment; class rules refine per class.

    See docs/contracts/ingestion-config.md RetrievalTreatment.
    Retrieval-treatment fields do NOT affect config_version (they are retrieval-time only).
    """

    model_config = ConfigDict(extra="forbid")

    default_salience_filter: list[SalienceTier] = Field(
        description="Which salience tiers are returned by default."
    )
    salience_weights: dict[str, float] | None = Field(
        default=None,
        description="Per-tier score weighting.",
    )
    rerank_eligible: bool = Field(description="Whether this class participates in reranking.")
    strategy: RetrievalStrategy = Field(description="Default retrieval mode for this class.")
    confidence_floor: Annotated[float, Field(ge=0.0, le=1.0)] | None = Field(
        default=None,
        description=(
            "Optional minimum confidence/OCR-confidence for default inclusion "
            "(§6.4 low-confidence down-weighting)."
        ),
    )


class ClassRule(BaseModel):
    """Per segment class: transformation tiers, chunking, embedding override, metadata, retrieval.

    See docs/contracts/ingestion-config.md ClassRule.
    Complete — no field is optional-with-implicit-build-default (§12).
    """

    model_config = ConfigDict(extra="forbid")

    segment_class: SegmentType = Field(description="The class this rule governs.")
    transformation: TransformationSettings = Field(
        description="Which tiers are on and their parameters."
    )
    chunking: ChunkingConfig = Field(description="Strategy + parameters (§6.4).")
    embedding_override: EmbeddingConfig | None = Field(
        default=None,
        description="Overrides the KB default embedding for this class, if any.",
    )
    metadata_schema: list[MetadataField] = Field(
        description=(
            "The metadata fields chunks of this class carry, beyond mandatory provenance (§6.4)."
        )
    )
    retrieval_treatment: RetrievalTreatment = Field(
        description="Default salience weighting, filters, rerank eligibility for this class (§6.4)."
    )


class LanguageSupportDecision(BaseModel):
    """Pre-ingestion language support decision (§7.6).

    See docs/contracts/ingestion-config.md LanguageSupportDecision.
    The platform MUST warn before embedding unsupported languages (§7.6).
    """

    model_config = ConfigDict(extra="forbid")

    detected_languages: list[LanguageShare] = Field(
        description="From the Parse-result aggregate (§7.6)."
    )
    unsupported_languages: list[str] = Field(
        description="Detected languages the chosen model does not support (§7.6)."
    )
    decision: LanguageDecision = Field(
        description=(
            "The pre-ingestion decision. warned_proceed requires a recorded acknowledgement — "
            "the platform MUST warn before embedding unsupported languages (§7.6)."
        )
    )
    cross_lingual_supported: bool | None = Field(
        default=None,
        description=(
            "Whether the model supports cross-lingual retrieval "
            "(reported, not added by the platform, §7.6)."
        ),
    )


class SpreadsheetTriage(BaseModel):
    """Per-spreadsheet report/database/model classification and disposition (§6.4).

    See docs/contracts/ingestion-config.md SpreadsheetTriage.
    Triage is visible and overridable (§6.4).
    """

    model_config = ConfigDict(extra="forbid")

    document_id: str = Field(description="The spreadsheet.")
    kind: SpreadsheetKind = Field(description="The fixed triage (§6.4). Only report is ingested.")
    disposition: SpreadsheetDisposition = Field(
        description="database/model → exclude_unservable (§6.4)."
    )
    source: TriageSource = Field(description="Triage is visible and overridable (§6.4).")
    reason: str = Field(description="Plain-language basis, for the report.")


class ExclusionDecision(BaseModel):
    """Confirmed unservable/excluded content, feeding the exclusion report (§7.5).

    See docs/contracts/ingestion-config.md ExclusionDecision.
    """

    model_config = ConfigDict(extra="forbid")

    document_id: str = Field(description="Excluded document (or segment scope).")
    reason: ExclusionDecisionReason = Field(description="Why (§7.5).")
    remediation: str = Field(
        description=("What the user could do — including 'nothing, and that is correct' (§7.5).")
    )


class RecommendationProvenance(BaseModel):
    """Provenance for each recommendation in this config (§6.4).

    See docs/contracts/ingestion-config.md RecommendationProvenance.
    heuristic values MUST be labelled as such (§6.4).
    """

    model_config = ConfigDict(extra="forbid")

    target: str = Field(
        description="Which field the recommendation set (JSON pointer into this config)."
    )
    basis: RecommendationBasis = Field(
        description="Evidence class. heuristic MUST be labelled as such (§6.4)."
    )
    sweep_run_id: str | None = Field(
        default=None,
        description="The §9.3 sweep that backs it, when sweep_backed.",
    )
    rationale: str = Field(description="Plain-language why (the 'why' affordance, §3.1).")


# ---------------------------------------------------------------------------
# ClassDescription (1.2.0)
# ---------------------------------------------------------------------------


class ClassDescription(BaseModel):
    """Per-class description text used by Tier 2 class_context and salience classification.

    See docs/contracts/ingestion-config.md config_version derivation.
    The description text is a build-affecting input (§6.5, S-R14, attack 8): editing it
    changes embedding_input → changes produced vectors → must rotate config_version.
    Therefore class_descriptions are folded into the config_version hash.

    Added in schema_version 1.2.0 (M-005, M-026).

    Invariant: class_id must equal segment_class.value. Enforced by _validate_class_id_match.
    """

    model_config = ConfigDict(extra="forbid")

    segment_class: SegmentType = Field(description="The segment class this description covers.")
    class_id: str = Field(
        description=(
            "Stable class identifier (matches the segment_class enum value). "
            "Included in hash so class-identity changes rotate config_version."
        )
    )
    description: str = Field(
        description=(
            "Human-readable description of this class, consumed by Tier 2 class_context "
            "augmentation and the §4.1 LLM salience classifier. "
            "This text is a hashed input (attack 8, S-R14): editing it changes embedding_input "
            "and produced vectors, so it rotates config_version and triggers a full rebuild."
        )
    )

    @model_validator(mode="after")
    def _validate_class_id_match(self) -> ClassDescription:
        """class_id must equal segment_class.value (consistency invariant).

        A mismatch would cause the config_version hash to collide with a correctly-formed
        config (class_id is included in the hash alongside segment_class), making the hash
        ambiguous and the Tier-2 class_context source untraceable.
        """
        if self.class_id != self.segment_class.value:
            raise ValueError(
                f"class_id {self.class_id!r} does not match "
                f"segment_class {self.segment_class.value!r}. "
                "class_id must equal segment_class.value — they identify the same class. "
                "A mismatch would corrupt the config_version hash and make Tier-2 "
                "class_context untraceable to the correct segment class."
            )
        return self


# ---------------------------------------------------------------------------
# IngestionConfig (root)
# ---------------------------------------------------------------------------


class IngestionConfig(BaseModel):
    """Contract 4 root model — produced by Plan, consumed by Build.

    See docs/contracts/ingestion-config.md IngestionConfig (root).

    Secret-free by construction (§14.2): no secret-typed fields exist.
    config_version participates in chunk identity (§10.5).

    Invariants:
    - Fully determines Build: every value Build reads is present; default_rule guarantees
      totality so no implicit default is resolved at build time (§12).
    - Secret-free by construction: no field holds a credential; providers/models are
      referenced by name only (§14.2).
    - config_version is derived deterministically from every input that affects a chunk's
      text or embedding_input (§10.5).
    - Recommendation provenance: every recommender-set value has a RecommendationProvenance
      entry; heuristic values are labelled heuristic (§6.4).
    - tenancy present (Phase 0 MUST).
    - extra="forbid": unknown keys rejected on import (secret-free/reject by construction,
      M-071). This means hand-edited files with typos or secret-bearing extra keys are
      rejected at parse time.
    - default_rule must not have tier3_enabled=True (M-035: never global).
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: str = Field(
        description="Contract version (the shape). Distinct from config_version."
    )
    tenancy: TenancyBlock = Field(description="Owning workspace/KB.")
    config_version: str = Field(
        description=(
            "Content hash (sha256 hex) of all build-affecting fields. "
            "Participates in chunk identity (§10.5). Changing any build-affecting field "
            "changes it and invalidates every chunk (§10.3 config-change trigger, §10.5)."
        )
    )
    created_at: datetime = Field(description="When this config was produced/approved (UTC).")
    naive_baseline: NaiveBaselineRef = Field(
        description=(
            "The fixed reference configuration deltas are measured against (§9.3). "
            "Recorded so reported deltas are interpretable."
        )
    )
    class_rules: list[ClassRule] = Field(
        description=(
            "Per-segment-class routing. Every segment class present in the corpus "
            "MUST have a rule (completeness invariant)."
        )
    )
    default_rule: ClassRule = Field(
        description=(
            "Fallback for any class not explicitly listed — makes the config total, "
            "so Build never resolves an implicit default (§12)."
        )
    )
    embedding: EmbeddingConfig = Field(
        description=(
            "Embedding model identity and parameters (KB-wide default; "
            "a class rule may override per §6.4)."
        )
    )
    retrieval_defaults: RetrievalTreatment = Field(
        description="KB-level default retrieval treatment; class rules refine per class."
    )
    language_support: LanguageSupportDecision = Field(
        description=(
            "Which detected languages the chosen embedding model supports, "
            "and the pre-ingestion decision (§7.6)."
        )
    )
    spreadsheet_triage: list[SpreadsheetTriage] = Field(
        description=(
            "Per-spreadsheet report/database/model classification and disposition (§6.4). "
            "Visible and overridable."
        )
    )
    exclusions_confirmed: list[ExclusionDecision] = Field(
        description=("Confirmed unservable/excluded content, feeding the exclusion report (§7.5).")
    )
    provenance: list[RecommendationProvenance] = Field(
        description=(
            "For each recommendation in this config: heuristic vs sweep-backed, "
            "with evidence pointer (§6.4)."
        )
    )
    # 1.2.0: class descriptions (M-005, M-026)
    class_descriptions: list[ClassDescription] = Field(
        default_factory=list,
        description=(
            "Per-class description text for Tier 2 class_context augmentation and §4.1 LLM "
            "salience classification. Description text is a build-affecting input (attack 8, "
            "S-R14): folded into config_version hash. Added in schema_version 1.2.0."
        ),
    )
    secret_free_attestation: Literal[True] = Field(
        description=(
            "Structural guarantee no secrets are present (§14.2); "
            "the model forbids secret-bearing fields by construction. Must be True."
        )
    )

    @model_validator(mode="after")
    def _validate_default_rule_no_tier3(self) -> IngestionConfig:
        """M-035: default_rule must never have tier3_enabled=True (never global).

        Tier 3 is per-class opt-in only. A global default with tier3_enabled would
        silently enable LLM rewriting for every unmatched class — structurally forbidden.
        """
        if self.default_rule.transformation.tier3_enabled:
            raise ValueError(
                "default_rule.transformation.tier3_enabled must be False (M-035). "
                "Tier 3 is per-class opt-in only — enabling it on the default_rule would "
                "make it a global default, which is structurally forbidden. "
                "Use class_rules to opt individual classes into Tier 3."
            )
        return self

    @model_validator(mode="after")
    def _validate_no_duplicate_class_descriptions(self) -> IngestionConfig:
        """class_descriptions must not contain duplicate class_id values.

        Duplicate class_ids make the Tier-2 class_context source ambiguous — the Build
        stage cannot determine which description to use for a given segment class.
        """
        seen: set[str] = set()
        duplicates: list[str] = []
        for cd in self.class_descriptions:
            if cd.class_id in seen:
                duplicates.append(cd.class_id)
            seen.add(cd.class_id)
        if duplicates:
            raise ValueError(
                "class_descriptions contains duplicate class_id values: "
                f"{sorted(set(duplicates))}. "
                "Each class_id must appear at most once — duplicates make the Tier-2 "
                "class_context source ambiguous."
            )
        return self


# ---------------------------------------------------------------------------
# Canonical JSON (shared by export and config_version hash)
# ---------------------------------------------------------------------------


def to_canonical_json(config: IngestionConfig) -> str:
    """Serialize config to canonical JSON: sorted keys, no insignificant whitespace, UTF-8.

    This is the ONE canonicalization function used by both:
    - config export (M-026: exportable/diffable/re-importable)
    - config_version derivation (the hash input; see pipeline.plan.config_version)

    Returns a string ending with a trailing newline.

    Args:
        config: The IngestionConfig to serialize.

    Returns:
        Canonical JSON string (UTF-8, sorted keys, compact, trailing newline).
    """
    data = config.model_dump(mode="json")
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"


__all__ = [
    "ChunkingStrategy",
    "RetrievalStrategy",
    "LanguageDecision",
    "SpreadsheetKind",
    "SpreadsheetDisposition",
    "TriageSource",
    "ExclusionDecisionReason",
    "RecommendationBasis",
    "MetadataFieldType",
    "MetadataFieldSource",
    "Tier1Operation",
    "Tier2Operation",
    "MetadataField",
    "Tier3Settings",
    "NaiveBaselineRef",
    "TransformationSettings",
    "ChunkingConfig",
    "EmbeddingConfig",
    "RetrievalTreatment",
    "ClassRule",
    "LanguageSupportDecision",
    "SpreadsheetTriage",
    "ExclusionDecision",
    "RecommendationProvenance",
    "ClassDescription",
    "IngestionConfig",
    "to_canonical_json",
]
