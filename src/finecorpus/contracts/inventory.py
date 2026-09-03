"""Contract 1 — Inventory.

Stage boundary: Collect → Assess.
See docs/contracts/inventory.md for the authoritative spec (§6.1, §12, §14.3).

The Inventory is the durable record of what files exist, their stable identities,
their content hashes, their source-system metadata, and the relationships between them.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from finecorpus.contracts.shared.blocks import PermissionFidelity, SourceLocation, TenancyBlock

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class SourceKind(StrEnum):
    """Where files came from (§6.1).

    See docs/contracts/inventory.md SourceRun.source_kind.
    """

    upload = "upload"
    directory = "directory"
    s3 = "s3"
    sharepoint = "sharepoint"
    confluence = "confluence"
    jira = "jira"
    gdrive = "gdrive"


class DedupRole(StrEnum):
    """Role of an inventory item in dedup/version relationships.

    See docs/contracts/inventory.md InventoryItem.dedup_role.
    """

    unique = "unique"
    exact_duplicate = "exact_duplicate"
    primary = "primary"
    superseded = "superseded"


class CollectStatus(StrEnum):
    """Outcome of collection for an InventoryItem.

    See docs/contracts/inventory.md InventoryItem.collect_status.
    Failures are represented, not dropped (§6 rule 6).
    """

    collected = "collected"
    unreadable = "unreadable"
    """Includes password-protected files (detail in status_detail)."""
    access_denied = "access_denied"
    too_large = "too_large"
    skipped_policy = "skipped_policy"


class DocumentStatus(StrEnum):
    """Lifecycle state of the document in the KB.

    See docs/contracts/inventory.md InventoryItem.document_status.
    Distinct from collect_status (which is a collection-time outcome).
    """

    active = "active"
    """In the index (or eligible), full participation."""
    superseded = "superseded"
    """Version-family member replaced by the newest version."""
    deleted = "deleted"
    """Removed from the KB after collection (§17.1)."""


class FetchPolicy(StrEnum):
    """Per-KB link fetch policy (§6.1).

    See docs/contracts/inventory.md LinkRecord.fetch_policy.
    """

    ignore = "ignore"
    """Default — link is recorded but not fetched."""
    snapshot = "snapshot"
    """A dated snapshot is taken."""
    crawl = "crawl"
    """Recursive crawl within domain allowlist (§6.1)."""


class FetchStatus(StrEnum):
    """Outcome of link fetch.

    See docs/contracts/inventory.md LinkRecord.fetch_status.
    """

    not_fetched = "not_fetched"
    snapshotted = "snapshotted"
    crawled = "crawled"
    fetch_failed = "fetch_failed"
    blocked_by_allowlist = "blocked_by_allowlist"


class PrimacyBasis(StrEnum):
    """Why a VersionFamily primary was chosen.

    See docs/contracts/inventory.md VersionFamily.primacy_basis.
    """

    source_modified_at = "source_modified_at"
    discovered_at = "discovered_at"
    filename_version = "filename_version"
    manual = "manual"


# ---------------------------------------------------------------------------
# SourceRun
# ---------------------------------------------------------------------------


class SourceRun(BaseModel):
    """The Collect job that produced this inventory.

    See docs/contracts/inventory.md SourceRun.
    """

    source_kind: SourceKind = Field(description="Where files came from (§6.1).")
    connector_id: str | None = Field(
        default=None,
        description="Which configured connector instance, if any. Not the credential (§14.2).",
    )
    incremental_cursor: str | None = Field(
        default=None,
        description=("Source-side change cursor for incremental connectors (§6.1). Opaque."),
    )
    acknowledged_permission_gap: bool | None = Field(
        default=None,
        description=(
            "Set when the operator acknowledged an 'unavailable' permission-fidelity "
            "connector (§14.3)."
        ),
    )


# ---------------------------------------------------------------------------
# SourcePermissions
# ---------------------------------------------------------------------------


class SourcePermissions(BaseModel):
    """Raw source-side ACLs when a connector supplies them (§14.3).

    See docs/contracts/inventory.md SourcePermissions.
    Before resolution into TenancyBlock.permission_principals.
    """

    principals_read: list[str] = Field(description="Source-side principals with read access.")
    fidelity: PermissionFidelity = Field(
        description=(
            "Reliability of the source ACL data (§14.3). "
            "Maps into TenancyBlock.permission_fidelity."
        )
    )
    raw: dict[str, Any] | None = Field(
        default=None,
        description="Verbatim source ACL payload retained for audit.",
    )


# ---------------------------------------------------------------------------
# InventoryItem
# ---------------------------------------------------------------------------


class InventoryItem(BaseModel):
    """One record per discovered file.

    See docs/contracts/inventory.md InventoryItem.

    Invariants:
    - document_id is stable across re-collections of the same logical file.
    - content_hash is the sha256 hex of the raw bytes; becomes Provenance.source_document_version.
    - dedup_role is always set; non-unique roles point to a group/family.
    - document_status reflects lifecycle state (active/superseded/deleted).
    - Failed/unreadable files are present with collect_status, not omitted (§6 rule 6).
    """

    document_id: str = Field(
        description=(
            "Stable file identity (ULID). Assigned once, stable across re-collections. "
            "Becomes Provenance.source_document_id."
        )
    )
    content_hash: str = Field(
        description=(
            "Hash (sha256 hex) of the raw bytes. Becomes Provenance.source_document_version. "
            "Basis of exact-dedup and change detection (§6.1, §10.3)."
        )
    )
    source_path: str = Field(
        description=(
            "Canonical path/URI within the source system "
            "(e.g. 's3://bucket/key', '/mnt/corpus/manual.pdf', Confluence page URL)."
        )
    )
    display_name: str = Field(description="Human-facing name for reports and citations.")
    media_type: str = Field(description="Detected MIME type. Drives parser selection in Assess.")
    declared_extension: str | None = Field(
        default=None,
        description=(
            "File extension as given by source, retained even when it disagrees with media_type."
        ),
    )
    size_bytes: int = Field(description="Raw size in bytes.")
    source_metadata: dict[str, Any] = Field(
        description=(
            "Source-system metadata (§6.1): author, created/modified in source, "
            "SharePoint/Confluence properties, labels. Free-form but never secrets (§14.2)."
        )
    )
    source_created_at: datetime | None = Field(
        default=None,
        description="Timestamp from source system.",
    )
    source_modified_at: datetime | None = Field(
        default=None,
        description=(
            "Timestamp from source system. Distinguishes content vs metadata change (§10.3)."
        ),
    )
    discovered_at: datetime = Field(description="When Collect first saw this file (UTC).")
    source_permissions: SourcePermissions | None = Field(
        default=None,
        description=(
            "Raw source-side ACLs when a connector supplies them (§14.3), "
            "before resolution into TenancyBlock.permission_principals."
        ),
    )
    dedup_role: DedupRole = Field(
        description=(
            "Role in dedup/version relationships. superseded = an older version-family member. "
            "Default (D-25): superseded produces no segments and no chunks; inventoried, retained, "
            "reported in exclusion report."
        )
    )
    dedup_group_id: str | None = Field(
        default=None,
        description="The DuplicateGroup or VersionFamily this item belongs to, if any.",
    )
    collect_status: CollectStatus = Field(
        description=(
            "Outcome of collection. Failures are represented, not dropped (§6 rule 6). "
            "unreadable includes password-protected files (detail in status_detail)."
        )
    )
    status_detail: str | None = Field(
        default=None,
        description="Reason string for any non-collected status. Feeds the exclusion report.",
    )
    document_status: DocumentStatus = Field(
        description=(
            "Lifecycle state of the document in the KB. Distinct from collect_status. "
            "active/superseded/deleted (§17.1)."
        )
    )


# ---------------------------------------------------------------------------
# LinkRecord
# ---------------------------------------------------------------------------


class LinkRecord(BaseModel):
    """Web link found inside a document, with fetch policy applied.

    See docs/contracts/inventory.md LinkRecord.
    Links are not content (§6.1); they are recorded references.
    """

    link_id: str = Field(description="Identity of this link occurrence (ULID).")
    found_in_document_id: str = Field(description="Document the link appeared in.")
    location: SourceLocation = Field(description="Where in the document the link was found.")
    url: str = Field(description="The referenced URL.")
    fetch_policy: FetchPolicy = Field(
        description="Per-KB policy applied (§6.1). ignore is the default."
    )
    fetch_status: FetchStatus = Field(description="Outcome. not_fetched for ignore.")
    snapshot_artifact_id: str | None = Field(
        default=None,
        description="Object-store id of the dated snapshot when snapshot/crawl applied.",
    )
    snapshot_fetched_at: datetime | None = Field(
        default=None,
        description="When snapshot was taken; marks the snapshot as potentially stale (§6.1).",
    )
    crawl_depth: int | None = Field(
        default=None,
        description="Depth at which a crawled link was reached (bounded, §6.1).",
    )


# ---------------------------------------------------------------------------
# DuplicateGroup
# ---------------------------------------------------------------------------


class DuplicateGroup(BaseModel):
    """Exact-duplicate cluster by content hash.

    See docs/contracts/inventory.md DuplicateGroup.
    """

    group_id: str = Field(description="Group identity (ULID).")
    content_hash: str = Field(description="The shared hash defining exact duplication.")
    member_document_ids: list[str] = Field(description="Items with identical bytes.")
    primary_document_id: str = Field(
        description="The one retained for processing; others are exact_duplicate."
    )


# ---------------------------------------------------------------------------
# VersionFamily
# ---------------------------------------------------------------------------


class VersionFamily(BaseModel):
    """Near-duplicate cluster representing a document version family (§6.1).

    See docs/contracts/inventory.md VersionFamily.

    Invariants:
    - Exactly one member is primary; all others are superseded.
    - Superseded members are RETAINED in object storage, never deleted (§6.1).
    - Default (D-25, owner ruling 2026-09-03): superseded members produce no segments/chunks
      and appear in the exclusion report. Toggle index_superseded_versions=true enables
      excluded-tier indexing.
    """

    family_id: str = Field(description="Family identity (ULID).")
    member_document_ids: list[str] = Field(description="Near-duplicate members (§6.1).")
    primary_document_id: str = Field(description="Newest member, treated as primary (§6.1).")
    superseded_document_ids: list[str] = Field(
        description=(
            "Older members, retained in object storage. Default (D-25): not indexed; "
            "appear in exclusion report. When index_superseded_versions=true: "
            "indexed at tier excluded (§6.1)."
        )
    )
    similarity_method: str = Field(
        description="How near-duplication was determined (e.g. minhash, simhash), for auditability."
    )
    similarity_scores: dict[str, float] | None = Field(
        default=None,
        description="Per-member similarity to primary, for inspection and override.",
    )
    primacy_basis: PrimacyBasis = Field(
        description=(
            "Why primary was chosen newest — recorded so a wrong pick is "
            "explainable and overridable."
        )
    )


# ---------------------------------------------------------------------------
# Inventory (root)
# ---------------------------------------------------------------------------


class Inventory(BaseModel):
    """Contract 1 root model — produced by Collect, consumed by Assess.

    See docs/contracts/inventory.md Inventory (root).

    Invariants:
    - Every item has a stable document_id, content_hash, source_path, source_metadata (§12).
    - Duplicate and version-family relationships are explicit (§12).
    - tenancy is present (Phase 0 MUST, §19).
    """

    schema_version: str = Field(
        description="Contract version (semver). See docs/contracts/README.md#contract-versioning."
    )
    tenancy: TenancyBlock = Field(description="Owning workspace/KB and default permission facts.")
    collected_at: datetime = Field(description="When this Collect run completed (UTC).")
    source_run: SourceRun = Field(
        description=(
            "The Collect job that produced this inventory (source kind, connector id, cursor)."
        )
    )
    items: list[InventoryItem] = Field(
        description=(
            "One record per discovered file. May be empty (empty corpus is valid, reported)."
        )
    )
    links: list[LinkRecord] = Field(
        description="Web links found inside documents, with fetch policy applied."
    )
    duplicate_groups: list[DuplicateGroup] = Field(
        description="Exact-duplicate clusters by content hash."
    )
    version_families: list[VersionFamily] = Field(
        description="Near-duplicate clusters representing document version families (§6.1)."
    )


__all__ = [
    "SourceKind",
    "DedupRole",
    "CollectStatus",
    "DocumentStatus",
    "FetchPolicy",
    "FetchStatus",
    "PrimacyBasis",
    "SourceRun",
    "SourcePermissions",
    "InventoryItem",
    "LinkRecord",
    "DuplicateGroup",
    "VersionFamily",
    "Inventory",
]
