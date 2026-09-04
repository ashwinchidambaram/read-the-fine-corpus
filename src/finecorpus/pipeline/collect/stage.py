"""Collect stage — walks a source directory and produces an Inventory artifact.

See docs for design notes on document_id derivation and duplicate detection.
"""

from __future__ import annotations

import base64
import hashlib
import mimetypes
import os
import pathlib
from datetime import UTC, datetime
from typing import Any

from finecorpus.contracts.inventory import (
    CollectStatus,
    DedupRole,
    DocumentStatus,
    DuplicateGroup,
    Inventory,
    InventoryItem,
    SourceKind,
    SourceRun,
)
from finecorpus.contracts.shared.blocks import (
    PermissionFidelity,
    PermissionMode,
    PermissionSource,
    TenancyBlock,
)
from finecorpus.pipeline.artifact_store import ArtifactStore
from finecorpus.pipeline.stage import Stage

# The schema version this stage stamps on every Inventory it produces.
_INVENTORY_SCHEMA_VERSION = "1.0.0"


def _derive_document_id(content_hash: str) -> str:
    """Derive a deterministic, stable document_id from a content_hash.

    Scheme: base32(sha256(content_hash_bytes)[:10])
    - 80 bits → 16 base32 characters (no padding).
    - Deterministic: same content_hash → same document_id.
    - Stable across file moves/renames (content-addressed).
    - No timestamp component (would break re-collection stability).

    The result is an uppercase base32 string prefixed with "DOC" for readability
    in logs and reports.

    Design choice documented in collect/__init__.py.
    """
    raw = hashlib.sha256(content_hash.encode("ascii")).digest()[:10]
    encoded = base64.b32encode(raw).decode("ascii")  # 16 chars, no padding needed for 80 bits
    return f"DOC{encoded}"


def _sha256_file(path: pathlib.Path) -> str:
    """Return the sha256 hex digest of a file's raw bytes."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _detect_media_type(path: pathlib.Path) -> str:
    """Detect MIME type; fall back to application/octet-stream."""
    mime, _ = mimetypes.guess_type(str(path))
    return mime or "application/octet-stream"


def _mtime_to_utc(stat: os.stat_result) -> datetime:
    """Convert stat mtime_ns to a UTC datetime."""
    return datetime.fromtimestamp(stat.st_mtime_ns / 1e9, tz=UTC)


def _ctime_to_utc(stat: os.stat_result) -> datetime:
    """Convert stat ctime_ns to a UTC datetime.

    On Linux, ctime is the inode-change time, not creation time.
    We record it as source_created_at with appropriate caveats;
    it is still useful as an upper bound on file age.
    """
    return datetime.fromtimestamp(stat.st_ctime_ns / 1e9, tz=UTC)


class CollectStage(Stage):
    """Stage 1 — Collect.

    Walks source_dir, hashes every file, builds an Inventory.

    This is REAL implementation — not a skeleton.

    Args:
        source_dir: Directory to walk.
        workspace_id: Tenancy workspace identity.
        kb_id: Tenancy knowledge base identity.
        collected_at: Fixed timestamp for this run (caller-supplied for determinism).
    """

    name = "collect"
    consumed_contract = None  # First stage; no input contract.
    consumed_version_range = None
    produced_contract = "inventory"
    output_model = Inventory

    def __init__(
        self,
        source_dir: str | pathlib.Path,
        workspace_id: str,
        kb_id: str,
        collected_at: datetime | None = None,
    ) -> None:
        self._source_dir = pathlib.Path(source_dir)
        self._workspace_id = workspace_id
        self._kb_id = kb_id
        # collected_at is fixed by caller for determinism; default to now only if
        # caller does not care (tests should pass a fixed value).
        self._collected_at = collected_at or datetime.now(tz=UTC)

    def _produce(self, input_data: dict[str, Any] | None) -> dict[str, Any]:
        """Walk source_dir and build an Inventory dict."""
        tenancy = TenancyBlock(
            workspace_id=self._workspace_id,
            kb_id=self._kb_id,
            permission_mode=PermissionMode.public_to_kb,
            permission_principals=[],
            permission_source=PermissionSource.platform,
            permission_fidelity=PermissionFidelity.authoritative,
            permission_resolved_at=None,
        )

        source_run = SourceRun(
            source_kind=SourceKind.directory,
            connector_id=None,
            incremental_cursor=None,
            acknowledged_permission_gap=None,
        )

        items = self._walk_directory()

        # Exact-duplicate detection: group by content_hash
        hash_to_items: dict[str, list[InventoryItem]] = {}
        for item in items:
            hash_to_items.setdefault(item.content_hash, []).append(item)

        duplicate_groups: list[DuplicateGroup] = []
        # Assign dedup roles
        for content_hash, group in hash_to_items.items():
            if len(group) == 1:
                # Unique — role is already 'unique'
                continue
            # Multiple items with same hash: pick primary deterministically
            # (lexicographic on source_path for stability).
            sorted_group = sorted(group, key=lambda it: it.source_path)
            primary = sorted_group[0]

            # Derive a group_id deterministically from the content_hash
            _digest = hashlib.sha256(content_hash.encode()).digest()[:10]
            group_id = f"GRP{base64.b32encode(_digest).decode('ascii')}"

            for item in group:
                item_mut = item
                object.__setattr__(item_mut, "dedup_group_id", group_id)
                if item.source_path == primary.source_path:
                    object.__setattr__(item_mut, "dedup_role", DedupRole.primary)
                else:
                    object.__setattr__(item_mut, "dedup_role", DedupRole.exact_duplicate)

            dup_group = DuplicateGroup(
                group_id=group_id,
                content_hash=content_hash,
                member_document_ids=[it.document_id for it in sorted_group],
                primary_document_id=primary.document_id,
            )
            duplicate_groups.append(dup_group)

        inventory = Inventory(
            schema_version=_INVENTORY_SCHEMA_VERSION,
            tenancy=tenancy,
            collected_at=self._collected_at,
            source_run=source_run,
            items=items,
            links=[],
            duplicate_groups=duplicate_groups,
            version_families=[],
        )
        return inventory.model_dump(mode="json")

    def _walk_directory(self) -> list[InventoryItem]:
        """Walk self._source_dir recursively; return one InventoryItem per file."""
        items: list[InventoryItem] = []

        if not self._source_dir.exists():
            return items

        # Sort for determinism: same directory contents → same ordering.
        all_files = sorted(self._source_dir.rglob("*"))

        for path in all_files:
            if not path.is_file():
                continue
            item = self._collect_file(path)
            items.append(item)

        return items

    def _collect_file(self, path: pathlib.Path) -> InventoryItem:
        """Collect metadata for a single file and return an InventoryItem."""
        try:
            stat = path.stat()
            content_hash = _sha256_file(path)
            size_bytes = stat.st_size
            source_modified_at = _mtime_to_utc(stat)
            source_created_at = _ctime_to_utc(stat)
            collect_status = CollectStatus.collected
            status_detail = None
        except OSError as exc:
            # Unreadable files are represented, not dropped (§6 rule 6).
            content_hash = hashlib.sha256(str(path).encode()).hexdigest()
            size_bytes = 0
            source_modified_at = None
            source_created_at = None
            collect_status = CollectStatus.unreadable
            status_detail = f"OS error: {exc}"

        document_id = _derive_document_id(content_hash)
        media_type = _detect_media_type(path)
        source_path = str(path)
        display_name = path.name
        extension = path.suffix.lstrip(".") or None

        return InventoryItem(
            document_id=document_id,
            content_hash=content_hash,
            source_path=source_path,
            display_name=display_name,
            media_type=media_type,
            declared_extension=extension,
            size_bytes=size_bytes,
            source_metadata={},
            source_created_at=source_created_at,
            source_modified_at=source_modified_at,
            discovered_at=self._collected_at,
            source_permissions=None,
            dedup_role=DedupRole.unique,
            dedup_group_id=None,
            collect_status=collect_status,
            status_detail=status_detail,
            document_status=DocumentStatus.active,
        )

    def run(
        self,
        input_data: dict[str, Any] | None,
        store: ArtifactStore,
    ) -> dict[str, Any]:
        """Override: Collect has no input contract, so skip version check."""
        output_dict = self._produce(input_data)
        # Validate output
        Inventory.model_validate(output_dict)
        store.save(self.name, output_dict)
        return store.load(self.name)
