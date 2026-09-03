"""Contract 3 — Segment set.

Stage boundary: Decompose → Plan.
See docs/contracts/segment-set.md for the authoritative spec (§6.3, §7.1, §12, §6.4).

The Segment set is the typed decomposition of one document. The segment is the unit of the
system (§1.4, §7.1). Segments reassemble to the document in order (§6.3, §12), and no content
is lost between parse result and segment set except explicitly recorded exclusions (§12).

The Segment set is a frozen, content-addressed artifact (keyed on
(document_id, content_hash, config_version)) that makes chunk IDs deterministic across
re-runs (see "Determinism and freezing" in the spec page).
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, Field

from finecorpus.contracts.shared.blocks import (
    InvisibleContentKind,
    SalienceSignal,
    SalienceSignalKind,
    SalienceTier,
    SegmentType,
    SensitivityFlag,
    SourceLocation,
    TenancyBlock,
)

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class ExclusionReason(StrEnum):
    """Why a content span was explicitly excluded.

    See docs/contracts/segment-set.md ExclusionRecord.reason.
    The only permitted form of content loss between Parse and Segment set (§12).
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


class ReassemblyMethod(StrEnum):
    """Mechanism for reassembling document text from segments.

    See docs/contracts/segment-set.md ReassemblyRecord.method.
    """

    document_order_concat = "document_order_concat"
    """v1 mechanism: concatenate every segment's source text ordered by document_order."""


class CrossReferenceResolution(StrEnum):
    """Whether a cross-reference was resolved to a target.

    See docs/contracts/segment-set.md CrossReference.resolution.
    """

    resolved = "resolved"
    unresolved = "unresolved"


# ---------------------------------------------------------------------------
# Segment
# ---------------------------------------------------------------------------


class Segment(BaseModel):
    """One typed segment within a document's decomposition.

    See docs/contracts/segment-set.md Segment.

    Invariants:
    - Every segment has segment_type, salience_tier, structural_path, and location (§12).
    - salience_signals contains all firing signals (taxonomy §4.3, C-R3).
    - salience_basis equals the kind of the won=True entry in salience_signals.
    - document_order is dense, gapless, unique within the set.
    """

    segment_id: str = Field(
        description="Segment identity (ULID), stable within this document version."
    )
    document_order: int = Field(
        description=(
            "0-based ordinal position in document reading order. "
            "Dense, gapless, unique within the set — the ordering key for reassembly."
        )
    )
    segment_type: SegmentType = Field(
        description="Typed segment class from the taxonomy (§6.3). See segment-taxonomy.md."
    )
    salience_tier: SalienceTier = Field(
        description=(
            "Default retrieval weight class (§6.3). excluded still indexed; "
            "tier controls default retrieval, not existence (§6.3 salience-not-pruning)."
        )
    )
    structural_path: list[str] = Field(
        description=(
            "Ordered heading breadcrumb from document root (§6.3 step 2). "
            "Empty = no heading structure. Component of chunk identity."
        )
    )
    segment_path: str = Field(
        description=(
            "Canonical stable path string for this segment within the document, "
            "used in chunk-ID derivation (see chunk.md). Derived from structural_path + "
            "a within-heading ordinal so it is stable across re-runs of the same document version."
        )
    )
    location: SourceLocation = Field(description="Position in the original source (§12).")
    source_region_ids: list[str] = Field(
        description=(
            "The ParseResult.RegionResult region_ids this segment was assembled from — "
            "the trace back that proves no content vanished."
        )
    )
    language: str = Field(description="Segment language in BCP-47 (§7.6). 'und' if undetermined.")
    ocr_confidence: Annotated[float, Field(ge=0.0, le=1.0)] | None = Field(
        default=None,
        description="Carried from source regions where applicable (§6.4 scans).",
    )
    injection_suspicion: Annotated[float, Field(ge=0.0, le=1.0)] = Field(
        description=(
            "Injection-pattern suspicion for this segment (§14.1). 0.0 if none. "
            "Filterable; never used to silently exclude."
        )
    )
    invisible_content_flags: list[InvisibleContentKind] = Field(
        description="Invisible-content flags carried from parse (§14.1)."
    )
    sensitivity_flags: list[SensitivityFlag] = Field(description="PII/sensitive flags (§14.4).")
    salience_signals: list[SalienceSignal] = Field(
        description=(
            "All salience signals that fired for this segment, winning and losing, "
            "recorded as contributing evidence (taxonomy §4.3: lower signals are recorded "
            "even when they do not win). Enables the §11.5 explain-mode 'why this tier' trace."
        )
    )
    salience_basis: SalienceSignalKind = Field(
        description=(
            "The winning signal — the one that actually set the tier (taxonomy §4.3 precedence). "
            "MUST equal the kind of the salience_signals entry with won=True."
        )
    )
    text: str | None = Field(
        default=None,
        description=(
            "The segment's source text (byte-identical to source; Tier transformations happen "
            "at Build, not here). "
            "Null only for non-text segments (e.g. figure whose caption is separate)."
        ),
    )


# ---------------------------------------------------------------------------
# ReassemblyRecord
# ---------------------------------------------------------------------------


class ReassemblyRecord(BaseModel):
    """The mechanism that satisfies 'segments reassemble to the document in order' (§6.3, §12).

    See docs/contracts/segment-set.md ReassemblyRecord.

    Reassembly invariant: ordering all segments by document_order and concatenating their text
    (with ExclusionRecords accounting for any non-segmented spans) reproduces the parse-level
    document text. Verified by the property test in §18.2.
    """

    method: ReassemblyMethod = Field(
        description=(
            "v1 mechanism: concatenate every segment's source text ordered by document_order."
        )
    )
    covered_region_ids: list[str] = Field(
        description=(
            "Union of all source_region_ids across segments plus exclusions. "
            "MUST equal the full set of RegionResult.region_id from the Parse result."
        )
    )
    reassembly_digest: str = Field(
        description=(
            "Hash (sha256 hex) of the reassembled text, "
            "checked by the reassembly property test (§18.2)."
        )
    )


# ---------------------------------------------------------------------------
# ExclusionRecord
# ---------------------------------------------------------------------------


class ExclusionRecord(BaseModel):
    """Explicitly-recorded content exclusion — the ONLY permitted form of content loss (§12).

    See docs/contracts/segment-set.md ExclusionRecord.
    Everything not carried into a segment is accounted for here.

    Invariants:
    - reversible=True in v1 — exclusion never destroys source (§1.4 principle 1).
    """

    exclusion_id: str = Field(description="Identity (ULID).")
    location: SourceLocation = Field(description="The excluded span in the source.")
    source_region_ids: list[str] = Field(description="Regions excluded, for coverage accounting.")
    reason: ExclusionReason = Field(description="Why excluded (§7.5, §6.4).")
    reason_detail: str | None = Field(
        default=None,
        description="Plain-language detail for the exclusion report (§7.5).",
    )
    reversible: bool = Field(
        description=(
            "Whether the original is retained and the exclusion can be undone (§1.4 principle 1). "
            "Always True in v1 — exclusion never destroys source."
        )
    )


# ---------------------------------------------------------------------------
# CrossReference
# ---------------------------------------------------------------------------


class CrossReference(BaseModel):
    """Resolved pointer or explicit unresolved record for intra-document cross-references (§6.4).

    See docs/contracts/segment-set.md CrossReference.
    Silent loss is not acceptable (§6.4).
    """

    xref_id: str = Field(description="Identity (ULID).")
    from_segment_id: str = Field(description="Segment containing the reference.")
    surface_text: str = Field(
        description="The literal reference text ('see section 4.2', 'Appendix B')."
    )
    location: SourceLocation = Field(description="Where the reference appears.")
    resolution: CrossReferenceResolution = Field(description="Whether a target was found (§6.4).")
    target_segment_id: str | None = Field(
        default=None,
        description="The referenced segment, when resolved (a followable pointer, §6.4).",
    )
    target_note: str | None = Field(
        default=None,
        description=(
            "When unresolved, an explicit note of what was referenced and why it could not "
            "be resolved (§6.4: recorded explicitly)."
        ),
    )


# ---------------------------------------------------------------------------
# SegmentSet (root)
# ---------------------------------------------------------------------------


class SegmentSet(BaseModel):
    """Contract 3 root model — produced by Decompose, consumed by Plan.

    See docs/contracts/segment-set.md SegmentSet (root).

    Invariants:
    - Every segment has segment_type, salience_tier, structural_path, and location (§12).
    - Reassembly: ordering segments by document_order and concatenating reproduces
      parse-level text (§12).
    - No silent content loss: every RegionResult.region_id appears in exactly one segment's
      source_region_ids OR in exactly one ExclusionRecord.source_region_ids (§12).
    - Exclusions are explicit, reasoned, and reversible=True (§1.4, §7.5).
    - Cross-references are resolved or unresolved with a note; never dropped (§6.4).
    - Salience is a tier, not a filter: excluded-tier segments are present and carried forward.
    - Every segment records all firing signals in salience_signals; salience_basis equals the
      kind of the won=True entry (C-R3).
    - Frozen artifact: content-addressed on (document_id, content_hash, config_version);
      persisted at Decompose; reused (never regenerated) by Build (§10.5, attack 3).
    - tenancy present and inherited by every segment (§7.1, Phase 0 MUST).
    """

    schema_version: str = Field(description="Contract version (semver).")
    tenancy: TenancyBlock = Field(
        description="Inherited from the document; every segment inherits it in turn (§7.1)."
    )
    document_id: str = Field(description="Source document (Inventory document_id).")
    content_hash: str = Field(
        description="Version of the document decomposed (Inventory content_hash)."
    )
    segments: list[Segment] = Field(
        description="The typed segments, in document order (see document_order)."
    )
    reassembly: ReassemblyRecord = Field(
        description="The mechanism that reconstitutes the document from its segments (§6.3 step 4)."
    )
    exclusions: list[ExclusionRecord] = Field(
        description=(
            "Explicitly-recorded content exclusions — "
            "the ONLY permitted form of content loss (§12)."
        )
    )
    cross_references: list[CrossReference] = Field(
        description="Resolved pointers or explicit unresolved records (§6.4)."
    )
    decomposed_at: datetime = Field(description="When decomposition completed (UTC).")


__all__ = [
    "ExclusionReason",
    "ReassemblyMethod",
    "CrossReferenceResolution",
    "Segment",
    "ReassemblyRecord",
    "ExclusionRecord",
    "CrossReference",
    "SegmentSet",
]
