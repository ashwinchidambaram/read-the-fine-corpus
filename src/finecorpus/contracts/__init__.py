"""finecorpus.contracts — Data contract models for the Read The Fine Corpus pipeline.

Public API: import from this module for all contract types, shared blocks, chunk-ID
derivation, and contract versioning. All names re-exported here are the stable public API.

See docs/contracts/README.md for the authoritative design documentation.

Pipeline contracts (in stage order):
1. Inventory     — Collect → Assess          (docs/contracts/inventory.md)
2. ParseResult   — Assess → Decompose        (docs/contracts/parse-result.md)
3. SegmentSet    — Decompose → Plan          (docs/contracts/segment-set.md)
4. IngestionConfig — Plan → Build            (docs/contracts/ingestion-config.md)
5. Chunk         — Build → Serve (the spine) (docs/contracts/chunk.md)
6. EvalSet       — ⟷ Plan, Serve            (docs/contracts/eval-set.md)
7. RetrievalResponse — Serve → caller        (docs/contracts/retrieval-response.md)
"""

# ---------------------------------------------------------------------------
# Shared blocks
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Contract 5 — Chunk
# ---------------------------------------------------------------------------
from finecorpus.contracts.chunk import (
    Augmentation,
    Chunk,
    EmbeddingRef,
)

# ---------------------------------------------------------------------------
# Chunk ID derivation
# ---------------------------------------------------------------------------
from finecorpus.contracts.chunk_id import (
    canonical_string,
    derive_chunk_id,
    derive_point_id,
)

# ---------------------------------------------------------------------------
# Contract 6 — Eval set
# ---------------------------------------------------------------------------
from finecorpus.contracts.eval_set import (
    ConfidenceLevel,
    EvalQuestion,
    EvalSet,
    EvalSetOrigin,
    GenerationMethod,
    QuestionType,
    ReviewStatus,
)

# ---------------------------------------------------------------------------
# Contract 4 — Ingestion config
# ---------------------------------------------------------------------------
from finecorpus.contracts.ingestion_config import (
    ChunkingConfig,
    ChunkingStrategy,
    ClassRule,
    EmbeddingConfig,
    ExclusionDecision,
    ExclusionDecisionReason,
    IngestionConfig,
    LanguageDecision,
    LanguageSupportDecision,
    MetadataField,
    MetadataFieldSource,
    MetadataFieldType,
    NaiveBaselineRef,
    RecommendationBasis,
    RecommendationProvenance,
    RetrievalStrategy,
    RetrievalTreatment,
    SpreadsheetDisposition,
    SpreadsheetKind,
    SpreadsheetTriage,
    Tier1Operation,
    Tier2Operation,
    Tier3Settings,
    TransformationSettings,
    TriageSource,
)

# ---------------------------------------------------------------------------
# Contract 1 — Inventory
# ---------------------------------------------------------------------------
from finecorpus.contracts.inventory import (
    CollectStatus,
    DedupRole,
    DocumentStatus,
    DuplicateGroup,
    FetchPolicy,
    FetchStatus,
    Inventory,
    InventoryItem,
    LinkRecord,
    PrimacyBasis,
    SourceKind,
    SourcePermissions,
    SourceRun,
    VersionFamily,
)

# ---------------------------------------------------------------------------
# Contract 2 — Parse result
# ---------------------------------------------------------------------------
from finecorpus.contracts.parse_result import (
    BoilerplateCandidate,
    BoilerplateKind,
    DocumentKind,
    EncodingIssue,
    EncodingIssueKind,
    ExtractStatus,
    Finding,
    FindingSeverity,
    InvisibleContentDetection,
    IssueSeverity,
    LanguageShare,
    PageResult,
    ParseResult,
    ParserRef,
    ParseStatus,
    QualityScore,
    RegionClassHint,
    RegionResult,
    TableStructureRetained,
)

# ---------------------------------------------------------------------------
# Contract 7 — Retrieval response
# ---------------------------------------------------------------------------
from finecorpus.contracts.retrieval_response import (
    AppliedFilter,
    ErrorCode,
    ErrorEnvelope,
    ExplainBlock,
    ExplainCandidate,
    ExplainExclusion,
    FilterOrigin,
    RequestEcho,
    ResultStatus,
    RetrievalResponse,
    RetrievalResult,
    Scores,
)

# ---------------------------------------------------------------------------
# Contract 3 — Segment set
# ---------------------------------------------------------------------------
from finecorpus.contracts.segment_set import (
    CrossReference,
    CrossReferenceResolution,
    ExclusionReason,
    ExclusionRecord,
    ReassemblyMethod,
    ReassemblyRecord,
    Segment,
    SegmentSet,
)
from finecorpus.contracts.shared.blocks import (
    AppliedBy,
    AuditRecord,
    BreakGlassGrant,
    InvisibleContentKind,
    LocatorKind,
    PermissionFidelity,
    PermissionMode,
    PermissionSource,
    Provenance,
    SalienceSignal,
    SalienceSignalKind,
    SalienceTier,
    SegmentType,
    SensitivityFlag,
    SourceLocation,
    TenancyBlock,
    TransformationRecord,
    TransformationTier,
    TrustLevel,
)

# ---------------------------------------------------------------------------
# Contract versioning
# ---------------------------------------------------------------------------
from finecorpus.contracts.versions import (
    SUPPORTED_CHUNK,
    SUPPORTED_EVAL_SET,
    SUPPORTED_INGESTION_CONFIG,
    SUPPORTED_INVENTORY,
    SUPPORTED_PARSE_RESULT,
    SUPPORTED_RETRIEVAL_RESPONSE,
    SUPPORTED_SEGMENT_SET,
    ContractVersionError,
    SpecRange,
    check_version,
)

__all__ = [
    # Shared blocks
    "TenancyBlock",
    "PermissionMode",
    "PermissionSource",
    "PermissionFidelity",
    "SourceLocation",
    "LocatorKind",
    "TransformationRecord",
    "TransformationTier",
    "AppliedBy",
    "Provenance",
    "SalienceSignal",
    "SalienceSignalKind",
    "SalienceTier",
    "SegmentType",
    "InvisibleContentKind",
    "SensitivityFlag",
    "TrustLevel",
    "BreakGlassGrant",
    "AuditRecord",
    # Versioning
    "ContractVersionError",
    "SpecRange",
    "check_version",
    "SUPPORTED_INVENTORY",
    "SUPPORTED_PARSE_RESULT",
    "SUPPORTED_SEGMENT_SET",
    "SUPPORTED_INGESTION_CONFIG",
    "SUPPORTED_CHUNK",
    "SUPPORTED_EVAL_SET",
    "SUPPORTED_RETRIEVAL_RESPONSE",
    # Chunk ID derivation
    "canonical_string",
    "derive_chunk_id",
    "derive_point_id",
    # Contract 1 — Inventory
    "Inventory",
    "InventoryItem",
    "SourceRun",
    "SourceKind",
    "DedupRole",
    "CollectStatus",
    "DocumentStatus",
    "SourcePermissions",
    "LinkRecord",
    "FetchPolicy",
    "FetchStatus",
    "DuplicateGroup",
    "VersionFamily",
    "PrimacyBasis",
    # Contract 2 — Parse result
    "ParseResult",
    "ParserRef",
    "ParseStatus",
    "DocumentKind",
    "QualityScore",
    "TableStructureRetained",
    "PageResult",
    "InvisibleContentDetection",
    "RegionResult",
    "ExtractStatus",
    "RegionClassHint",
    "BoilerplateCandidate",
    "BoilerplateKind",
    "EncodingIssue",
    "EncodingIssueKind",
    "IssueSeverity",
    "LanguageShare",
    "Finding",
    "FindingSeverity",
    # Contract 3 — Segment set
    "SegmentSet",
    "Segment",
    "ExclusionReason",
    "ExclusionRecord",
    "ReassemblyRecord",
    "ReassemblyMethod",
    "CrossReference",
    "CrossReferenceResolution",
    # Contract 4 — Ingestion config
    "IngestionConfig",
    "NaiveBaselineRef",
    "ClassRule",
    "TransformationSettings",
    "Tier1Operation",
    "Tier2Operation",
    "Tier3Settings",
    "ChunkingConfig",
    "ChunkingStrategy",
    "EmbeddingConfig",
    "RetrievalTreatment",
    "RetrievalStrategy",
    "LanguageSupportDecision",
    "LanguageDecision",
    "SpreadsheetTriage",
    "SpreadsheetKind",
    "SpreadsheetDisposition",
    "TriageSource",
    "ExclusionDecision",
    "ExclusionDecisionReason",
    "RecommendationProvenance",
    "RecommendationBasis",
    "MetadataField",
    "MetadataFieldType",
    "MetadataFieldSource",
    # Contract 5 — Chunk
    "Chunk",
    "Augmentation",
    "EmbeddingRef",
    # Contract 6 — Eval set
    "EvalSet",
    "EvalQuestion",
    "EvalSetOrigin",
    "ConfidenceLevel",
    "GenerationMethod",
    "ReviewStatus",
    "QuestionType",
    # Contract 7 — Retrieval response
    "RetrievalResponse",
    "RequestEcho",
    "AppliedFilter",
    "FilterOrigin",
    "ResultStatus",
    "RetrievalResult",
    "Scores",
    "ErrorEnvelope",
    "ErrorCode",
    "ExplainBlock",
    "ExplainCandidate",
    "ExplainExclusion",
]
