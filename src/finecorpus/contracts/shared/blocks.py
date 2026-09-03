"""Shared building-block models for all data contracts.

See docs/contracts/README.md for the authoritative field tables for:
- TenancyBlock
- SourceLocation
- TransformationRecord
- Provenance

These blocks are defined once here and embedded by reference in every contract
so that a field means the same thing at every pipeline stage.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum, StrEnum
from typing import Annotated, Any

from pydantic import BaseModel, Field, model_validator

# ---------------------------------------------------------------------------
# Enums — TenancyBlock
# ---------------------------------------------------------------------------


class PermissionMode(StrEnum):
    """How permission_principals is interpreted.

    See docs/contracts/README.md TenancyBlock.permission_mode.
    """

    public_to_kb = "public_to_kb"
    """Any KB member may retrieve."""
    restricted = "restricted"
    """Only listed principals may retrieve."""
    source_mirrored = "source_mirrored"
    """Mirrored from a connector's source ACLs (§14.3)."""


class PermissionSource(StrEnum):
    """Where permission facts came from.

    See docs/contracts/README.md TenancyBlock.permission_source.
    """

    platform = "platform"
    """Platform-managed permissions."""
    connector = "connector"
    """Mirrored from a source system (§14.3)."""
    manual = "manual"
    """Manually assigned."""


class PermissionFidelity(StrEnum):
    """Reliability of the permission data.

    See docs/contracts/README.md TenancyBlock.permission_fidelity.
    """

    authoritative = "authoritative"
    """Fully reliable ACL data."""
    best_effort = "best_effort"
    """Partially reliable — connector could not supply full ACLs."""
    unavailable = "unavailable"
    """Connector cannot supply reliable ACLs (requires explicit acknowledgement §14.3)."""


# ---------------------------------------------------------------------------
# TenancyBlock
# ---------------------------------------------------------------------------


class TenancyBlock(BaseModel):
    """Workspace, KB, and permission facts carried on every contract root and sub-record.

    See docs/contracts/README.md#tenancy-block for the authoritative field table.
    Present in all six contracts from Phase 0 (§19 Phase 0 MUST).

    Invariants:
    - permission_mode=source_mirrored requires permission_source=connector.
    - permission_fidelity=unavailable MUST NOT reach an index without acknowledgement (§14.3).
    """

    workspace_id: str = Field(
        description="Owning workspace ULID (§2.1). Stable for the life of the workspace."
    )
    kb_id: str = Field(
        description=(
            "Owning knowledge base ULID. A document, segment, chunk belongs to exactly one KB."
        )
    )
    permission_mode: PermissionMode = Field(description="How permission_principals is interpreted.")
    permission_principals: list[str] = Field(
        description=(
            "Resolved principal identifiers permitted to retrieve. "
            "Empty + public_to_kb = all KB members. Empty + restricted = nobody but admins."
        )
    )
    permission_source: PermissionSource = Field(description="Where the permission facts came from.")
    permission_fidelity: PermissionFidelity = Field(
        description=(
            "Reliability of the permission data. unavailable requires explicit "
            "ingestion acknowledgement (§14.3)."
        )
    )
    permission_resolved_at: datetime | None = Field(
        default=None,
        description="When permissions were last resolved from source. Null for manual/platform.",
    )

    @model_validator(mode="after")
    def _source_mirrored_requires_connector(self) -> TenancyBlock:
        """source_mirrored requires permission_source=connector (§14.3, contracts README)."""
        if (
            self.permission_mode == PermissionMode.source_mirrored
            and self.permission_source != PermissionSource.connector
        ):
            raise ValueError("permission_mode=source_mirrored requires permission_source=connector")
        return self


# ---------------------------------------------------------------------------
# Enums — SourceLocation
# ---------------------------------------------------------------------------


class LocatorKind(StrEnum):
    """Which coordinate system is authoritative for a SourceLocation.

    See docs/contracts/README.md SourceLocation.locator_kind.
    """

    page = "page"
    """1-based page range (PDF, scan)."""
    byte_range = "byte_range"
    """0-based byte offsets into the decoded source stream."""
    char_range = "char_range"
    """0-based Unicode codepoint offsets into extracted text."""
    cell_range = "cell_range"
    """Spreadsheet cell/region address in A1 notation."""
    dom_path = "dom_path"
    """Stable structural path into an HTML export."""
    time_range = "time_range"
    """Reserved for future A/V; not served in v1."""


# ---------------------------------------------------------------------------
# SourceLocation
# ---------------------------------------------------------------------------


class SourceLocation(BaseModel):
    """Position within the original source document.

    See docs/contracts/README.md#source-location-block for the authoritative field table.
    Must address native PDFs, scans, HTML, and spreadsheets.

    Invariants:
    - At least the coordinate set named by locator_kind is fully populated (both ends).
    - Every extraction, segment, and chunk traces to a SourceLocation (§8, §12).
    - Offsets address the ORIGINAL source stream, independent of any downstream transformation.
    """

    locator_kind: LocatorKind = Field(
        description="Which coordinate system is authoritative for this location."
    )
    page_start: int | None = Field(
        default=None,
        description=(
            "1-based first page. Required when locator_kind=page or for any paged source."
        ),
    )
    page_end: int | None = Field(
        default=None,
        description="1-based last page (inclusive). Equals page_start for single-page.",
    )
    byte_start: int | None = Field(
        default=None,
        description="0-based byte offset into the decoded source stream.",
    )
    byte_end: int | None = Field(
        default=None,
        description="Exclusive end byte offset.",
    )
    char_start: int | None = Field(
        default=None,
        description=(
            "0-based Unicode codepoint offset into extracted text. "
            "Used for HTML/native text where byte offsets are unstable across encodings."
        ),
    )
    char_end: int | None = Field(
        default=None,
        description="Exclusive end codepoint offset.",
    )
    cell_range: str | None = Field(
        default=None,
        description=(
            "Spreadsheet cell/region address in A1 notation (e.g. 'Sheet1!B2:D40'). "
            "Required for spreadsheet-report locations."
        ),
    )
    dom_path: str | None = Field(
        default=None,
        description="Stable structural path into an HTML export (e.g. Confluence).",
    )
    bbox: Annotated[list[float], Field(min_length=4, max_length=4)] | None = Field(
        default=None,
        description=(
            "Bounding box in PDF user-space units [x0,y0,x1,y1], for scanned regions "
            "and injection-position evidence (§14.1 off-page detection)."
        ),
    )
    coordinate_note: str | None = Field(
        default=None,
        description="Free text where a coordinate is approximate (e.g. OCR region estimated).",
    )


# ---------------------------------------------------------------------------
# Enums — TransformationRecord
# ---------------------------------------------------------------------------


class TransformationTier(int, Enum):
    """Transformation tier (§7.2).

    See docs/contracts/README.md TransformationRecord.tier.
    """

    tier_1 = 1
    """Structure normalization."""
    tier_2 = 2
    """Contextual augmentation."""
    tier_3 = 3
    """Full rewriting."""


class AppliedBy(StrEnum):
    """Whether a model was involved in this transformation.

    See docs/contracts/README.md TransformationRecord.applied_by.
    """

    deterministic = "deterministic"
    """No model involved (most Tier 1 ops)."""
    model = "model"
    """A model was used (all Tier 2 augmentation, Tier 3)."""


# ---------------------------------------------------------------------------
# TransformationRecord
# ---------------------------------------------------------------------------


class TransformationRecord(BaseModel):
    """One transformation applied to a chunk, in application order.

    See docs/contracts/README.md#transformation-record for the authoritative field table.
    The chunk carries the full ordered list (§8).

    Invariants:
    - List is ordered by application; order is significant and preserved to the chunk.
    - Tier 2 ops MUST have changed_text=False (Tier 2 never touches text).
    - Tier 3 rewrite sets changed_text=True.
    """

    tier: TransformationTier = Field(
        description="1=structure normalization; 2=contextual augmentation; 3=full rewriting (§7.2)."
    )
    operation: str = Field(
        description=(
            "Stable operation id, e.g. ocr_cleanup, table_to_markdown, "
            "whitespace_repair, breadcrumb_augment, table_description, "
            "class_context, rewrite."
        )
    )
    applied_by: AppliedBy = Field(description="Whether a model was involved.")
    model_ref: str | None = Field(
        default=None,
        description=(
            "Provider/model identity when applied_by=model, for audit and reproducibility. "
            "Never a secret (§14.2)."
        ),
    )
    changed_text: bool = Field(
        description=(
            "Whether this operation altered the chunk text relative to what it received. "
            "Tier 2 MUST be False. Tier 1 that changes bytes sets True; no-ops set False. "
            "Tier 3 rewrite sets True."
        )
    )
    note: str | None = Field(
        default=None,
        description="Human-readable detail (e.g. 'OCR corrected 3 substitutions; changed_text').",
    )

    @model_validator(mode="after")
    def _tier_2_never_changes_text(self) -> TransformationRecord:
        """Tier 2 never touches text — changed_text must be False for tier=2 (§7.2)."""
        if self.tier == TransformationTier.tier_2 and self.changed_text:
            raise ValueError(
                "TransformationRecord with tier=2 must have changed_text=False "
                "(Tier 2 contextual augmentation never alters chunk text, §7.2)"
            )
        return self


# ---------------------------------------------------------------------------
# Enums — Provenance
# ---------------------------------------------------------------------------


class SegmentType(StrEnum):
    """Segment type taxonomy.

    See docs/architecture/segment-taxonomy.md for the authoritative definitions.
    13 content types plus one sentinel (unknown), for a total of 14 types.
    """

    prose = "prose"
    heading = "heading"
    table = "table"
    list_ = "list"  # 'list' is a Python builtin; stored as "list" on the wire
    code = "code"
    figure_caption = "figure_caption"
    figure_region = "figure_region"
    form_field = "form_field"
    boilerplate = "boilerplate"
    front_matter = "front_matter"
    revision_history = "revision_history"
    cross_reference = "cross_reference"
    scanned_region = "scanned_region"
    unknown = "unknown"


class SalienceTier(StrEnum):
    """Default retrieval weight class (§6.3).

    See docs/contracts/README.md Provenance.salience_tier.
    """

    primary = "primary"
    supporting = "supporting"
    boilerplate = "boilerplate"
    excluded = "excluded"


class SalienceSignalKind(StrEnum):
    """All salience signal kinds from the taxonomy §4.1.

    See docs/contracts/segment-set.md SalienceSignalKind.
    Replaces the prior four-value enum (C-R3).
    """

    explicit_user_exclusion = "explicit_user_exclusion"
    """§4.1 hard excluded, highest precedence."""
    unservable_detection = "unservable_detection"
    """§7.5 unservable content class."""
    ocr_confidence_floor = "ocr_confidence_floor"
    """OCR below the excluded floor."""
    class_description = "class_description"
    """§6.5 LLM classification against the class description."""
    boilerplate_detection = "boilerplate_detection"
    """§6.2 corpus-wide boilerplate machinery."""
    ocr_confidence_warn = "ocr_confidence_warn"
    """OCR between floor and warning level → supporting + flag."""
    segment_type_prior = "segment_type_prior"
    """Taxonomy §4.2 default tier by type."""
    structural_position = "structural_position"
    """Heading-hierarchy modifier."""
    default = "default"
    """No signal fired → supporting."""


class InvisibleContentKind(StrEnum):
    """Invisible-content detection mechanisms from §14.1.

    See docs/contracts/README.md Provenance.invisible_content_flags.
    """

    white_on_white = "white_on_white"
    zero_size_font = "zero_size_font"
    off_page = "off_page"
    metadata_only = "metadata_only"
    render_hidden = "render_hidden"


class SensitivityFlag(StrEnum):
    """PII/sensitive-category detections (§14.4).

    See docs/contracts/README.md Provenance.sensitivity_flags.
    """

    pii = "pii"
    phi = "phi"
    financial = "financial"
    credential = "credential"
    legal_privileged = "legal_privileged"
    other = "other"


class TrustLevel(StrEnum):
    """Content trust label (§14.1).

    See docs/contracts/README.md Provenance.trust_level.
    Single value in v1: all ingested content is untrusted material.
    """

    untrusted_ingested = "untrusted_ingested"


# ---------------------------------------------------------------------------
# SalienceSignal
# ---------------------------------------------------------------------------


class SalienceSignal(BaseModel):
    """One firing salience signal (winning or contributing).

    See docs/contracts/segment-set.md SalienceSignal.
    Mirrors the taxonomy §4.1 signal matrix exactly so provenance can reproduce
    the full tier decision (taxonomy §4.3, C-R3).

    Invariants:
    - Exactly one entry per segment/chunk has won=True.
    - kind of won=True entry equals salience_basis on the parent.
    """

    kind: SalienceSignalKind = Field(description="Which signal fired.")
    implied_tier: SalienceTier = Field(description="The tier this signal argued for.")
    won: bool = Field(description="Whether this signal is the one that set salience_basis.")
    detail: str | None = Field(
        default=None,
        description=(
            "Human-readable evidence "
            "(e.g. 'matched across 412 corpus docs', "
            "'class description: incident reports are primary')."
        ),
    )


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------


class Provenance(BaseModel):
    """Complete §8 provenance block carried on every chunk.

    See docs/contracts/README.md#provenance-block-provenance for the authoritative field table.
    Assembled incrementally across stages; only complete at the chunk (§12).

    Invariants:
    - Non-null on every chunk; all subfields present (§8, §12).
    - confidence reflects ocr_confidence when the latter is present.
    - injection_suspicion, invisible_content_flags, sensitivity_flags always present.
    """

    source_document_id: Annotated[str, Field(min_length=1)] = Field(
        description=(
            "Stable document identity (§8 'source document identity'). "
            "Matches Inventory document_id. Empty string is never valid."
        )
    )
    source_document_version: Annotated[str, Field(min_length=1)] = Field(
        description=(
            "Which version of the document this came from (§8 'and version'). "
            "Same value as Inventory content_hash; makes replace-by-document correct (§10.5). "
            "Empty string is never valid."
        )
    )
    source_location: SourceLocation = Field(description="Position within source (§8).")
    structural_path: list[str] = Field(
        description=(
            "Ordered heading breadcrumb from document root (§8, §6.3). "
            "Empty list = document had no heading structure (recorded, not omitted)."
        )
    )
    transformations: list[TransformationRecord] = Field(
        description=(
            "Ordered list of transformations with tier of each (§8). "
            "Empty list = no transformations applied."
        )
    )
    confidence: Annotated[float, Field(ge=0.0, le=1.0)] = Field(
        description=(
            "Composite confidence for this chunk, "
            "incorporating OCR confidence where applicable (§8)."
        )
    )
    ocr_confidence: Annotated[float, Field(ge=0.0, le=1.0)] | None = Field(
        default=None,
        description=(
            "Per-region OCR confidence, retained not thresholded (§6.2, §6.4). "
            "Null for native-text sources. Filterable at retrieval (§6.2)."
        ),
    )
    segment_type: SegmentType = Field(
        description="The segment's type (prose, table, list, code, etc.) from the taxonomy §6.3."
    )
    salience_tier: SalienceTier = Field(description="Default retrieval weight class (§6.3).")
    salience_basis: SalienceSignalKind = Field(
        description=(
            "The winning salience signal that set salience_tier (taxonomy §4.3). "
            "Enables explain mode (§11.5) to state why this chunk got its tier."
        )
    )
    salience_signals: list[SalienceSignal] = Field(
        description=(
            "All firing signals (winning and contributing), carried through from the segment "
            "(taxonomy §4.3, C-R3). Enables the §11.5 'why this tier' trace at retrieval."
        )
    )
    language: Annotated[str, Field(min_length=1)] = Field(
        description=(
            "Detected language of the segment (§7.6) in BCP-47 format (e.g. 'en', 'es'). "
            "'und' for undetermined; recorded, never omitted. Empty string is never valid."
        )
    )
    injection_suspicion: Annotated[float, Field(ge=0.0, le=1.0)] = Field(
        description=(
            "Injection-pattern suspicion score (§14.1). Retrievable and filterable; "
            "never used to silently exclude. 0.0 when no pattern detected."
        )
    )
    invisible_content_flags: list[InvisibleContentKind] = Field(
        description=(
            "Invisible-content detections from parse (§14.1). "
            "Empty list = none detected (recorded, not omitted)."
        )
    )
    sensitivity_flags: list[SensitivityFlag] = Field(
        description=(
            "PII/sensitive-category detections (§14.4). Advisory; never triggers silent redaction. "
            "Empty list = none detected."
        )
    )
    trust_level: TrustLevel = Field(
        description=(
            "Content trust label (§14.1). Single value in v1: all ingested content is "
            "untrusted material. Surfaced at retrieval and in the MCP tool description."
        )
    )


# ---------------------------------------------------------------------------
# Audit / break-glass (Phase 0 shape, Phase 4 enforcement)
# ---------------------------------------------------------------------------


class BreakGlassGrant(BaseModel):
    """Break-glass access grant (control-plane record).

    See docs/contracts/README.md#audit--break-glass-seeds.
    Shapes sketched in Phase 0; enforcement in Phase 4.
    """

    grant_id: str = Field(description="Identity of the grant (ULID).")
    grantor: str = Field(description="The admin/principal who issued the grant.")
    reason: str = Field(description="Stated reason — required, never blank (§2.3).")
    scope_kb_id: str = Field(description="The KB the grant authorizes access to.")
    granted_at: datetime = Field(description="When the grant took effect (UTC).")
    expires_at: datetime = Field(
        description="Time bound — required and finite (§2.3). A grant cannot be open-ended."
    )
    revoked_at: datetime | None = Field(
        default=None,
        description="Set if revoked before expiry.",
    )


class AuditRecord(BaseModel):
    """Append-only audit log record.

    See docs/contracts/README.md#audit--break-glass-seeds.
    """

    actor: str = Field(description="Principal who performed the action.")
    action: str = Field(description="What was done (e.g. retrieve, explain, config_change).")
    resource: str = Field(description="What was acted on (KB, chunk set, config).")
    tenancy: TenancyBlock = Field(description="Workspace/KB scope of the action.")
    timestamp: datetime = Field(description="When it happened (UTC).")
    grant_id: str | None = Field(
        default=None,
        description=(
            "The BreakGlassGrant.grant_id when the action was performed under break-glass; "
            "null otherwise."
        ),
    )


# Suppress unused-import noise; Any is available for dict[str, Any] in sub-models
__all__ = [
    "Any",
    "PermissionMode",
    "PermissionSource",
    "PermissionFidelity",
    "TenancyBlock",
    "LocatorKind",
    "SourceLocation",
    "TransformationTier",
    "AppliedBy",
    "TransformationRecord",
    "SegmentType",
    "SalienceTier",
    "SalienceSignalKind",
    "InvisibleContentKind",
    "SensitivityFlag",
    "TrustLevel",
    "SalienceSignal",
    "Provenance",
    "BreakGlassGrant",
    "AuditRecord",
]
