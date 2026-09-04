# Collect Stage

Status: **real implementation** (Phase 0). Governing spec: §5, §6.1, §12 (Inventory contract).

Collect is the first and only fully-implemented stage in Phase 0. It walks a source directory,
hashes every file, builds per-file metadata, detects exact duplicates, and persists a
contract-valid `Inventory` artifact.

---

## document_id derivation

Every `InventoryItem` carries a `document_id` — a stable, deterministic, content-addressed
identifier. The derivation in `finecorpus/pipeline/collect/stage.py`:

```python
def _derive_document_id(content_hash: str) -> str:
    raw = hashlib.sha256(content_hash.encode("ascii")).digest()[:10]
    encoded = base64.b32encode(raw).decode("ascii")  # 16 chars, no padding needed for 80 bits
    return f"DOC{encoded}"
```

The scheme is:

1. SHA-256 the `content_hash` string (itself a SHA-256 hex digest of the file's raw bytes).
2. Take the first 10 bytes of the resulting digest (80 bits).
3. Base32-encode those 10 bytes, producing 16 uppercase ASCII characters (no padding).
4. Prefix with `"DOC"` for readability in logs and reports.

The resulting `document_id` is a 19-character string such as `DOCABBCCDDEEFFGGHHII`.

**Properties:**

- **Deterministic.** The same file content — regardless of filename, path, or collection
  time — always produces the same `document_id`. Re-collecting a corpus that has not changed
  produces an identical Inventory.
- **Stable across moves and renames.** The ID depends only on content, not on filesystem
  metadata.
- **No timestamp component.** A timestamp in the ID would cause the ID to differ between runs,
  breaking the "stable across re-collections" invariant (§12). The collection timestamp is
  stored separately in `InventoryItem.discovered_at`.
- **Content-addressed.** Two files with identical bytes receive the same `document_id` (they
  are the same document; the Inventory tracks this via `dedup_role`).

---

## Inventory fields populated

The Collect stage populates every field on `InventoryItem` from filesystem data:

| Field | Source | Notes |
|---|---|---|
| `document_id` | `_derive_document_id(content_hash)` | See derivation above. |
| `content_hash` | `sha256` of raw file bytes | Full hex digest; 64 hex chars. |
| `source_path` | `str(path)` | Absolute filesystem path at collection time. |
| `display_name` | `path.name` | Filename only (no directory component). |
| `media_type` | `mimetypes.guess_type()` | Falls back to `application/octet-stream` when unrecognised. |
| `declared_extension` | `path.suffix.lstrip(".")` | `None` when the file has no extension. |
| `size_bytes` | `stat.st_size` | 0 on unreadable files (see below). |
| `source_created_at` | `stat.st_ctime_ns` → UTC | On Linux this is inode change time, not creation time; known limitation. |
| `source_modified_at` | `stat.st_mtime_ns` → UTC | Last-modified time. `None` on unreadable files. |
| `discovered_at` | `collected_at` (run-start timestamp) | Fixed for the full run by the orchestrator for determinism. |
| `dedup_role` | Assigned after duplicate detection | `unique`, `primary`, or `exact_duplicate`. |
| `dedup_group_id` | Assigned after duplicate detection | `None` when `dedup_role=unique`. |
| `collect_status` | `collected` or `unreadable` | See error handling below. |
| `status_detail` | OS error message | `None` when `collect_status=collected`. |
| `document_status` | `active` | Set for all files in Phase 0. |
| `source_metadata` | `{}` | Empty in Phase 0; connector-supplied metadata in Phase 6. |
| `source_permissions` | `None` | Not yet resolved; connector-supplied in Phase 4+. |

The top-level `Inventory` record also includes:

- `tenancy` — `TenancyBlock` built from the caller-supplied `workspace_id` and `kb_id`.
- `collected_at` — the fixed run-start timestamp.
- `source_run` — `SourceRun(source_kind=directory, connector_id=None, ...)`.
- `items` — all `InventoryItem` records.
- `links` — empty list (link resolution not yet implemented).
- `duplicate_groups` — one `DuplicateGroup` per exact-duplicate cluster.
- `version_families` — empty list (near-duplicate clustering not yet implemented).

---

## Exact-duplicate detection

After walking the directory, Collect groups items by `content_hash`. Within each group of
size > 1:

- Items are sorted lexicographically by `source_path` for determinism.
- The first item (smallest `source_path`) is assigned `dedup_role=primary`.
- All other items are assigned `dedup_role=exact_duplicate`.
- All items in the group share a `dedup_group_id` derived from the content hash:
  `"GRP" + base32(sha256(content_hash)[:10])`.
- A `DuplicateGroup` record is added to `Inventory.duplicate_groups`.

Files with no duplicates have `dedup_role=unique` and `dedup_group_id=None` and produce no
`DuplicateGroup` entry.

The primary selection is deterministic (same directory contents → same primary) across
independent runs.

---

## Unreadable files

If a file cannot be stat'd or read (an `OSError` at any point in `_collect_file()`), the
Collect stage does not drop the file. Instead it creates an `InventoryItem` with:

- `collect_status=unreadable`
- `status_detail` — the OS error message
- `content_hash` — `sha256(str(path))` (path-based, not content-based; for unreadable files
  only, so the item can still be assigned a stable ID)
- `size_bytes=0`, `source_modified_at=None`, `source_created_at=None`

This satisfies §6 rule 6: every discovered file is represented, not silently dropped.

---

## Walk ordering

`_walk_directory()` sorts all paths returned by `rglob("*")` before processing them. Sorting
guarantees that the same directory contents produce the same `InventoryItem` ordering across
runs on any platform.

---

## What is NOT implemented in Phase 0

The following capabilities are explicitly deferred:

| Capability | Planned phase | Spec reference |
|---|---|---|
| Near-duplicate clustering (`version_families`) | Phase 1+ | §6.1 |
| Connector-based collection (API, SharePoint, Confluence, etc.) | Phase 6 | §6.1 |
| Source permission resolution (`source_permissions`, `permission_mode=source_mirrored`) | Phase 4 | §14.3 |
| Link resolution (`Inventory.links`) | Phase 2+ | §12 inventory contract |

`Inventory.version_families` is always an empty list in Phase 0. `Inventory.links` is always an
empty list. These fields are present in the contract and will be populated in their respective
phases without a schema version bump (they are optional/list fields).

---

## Related pages

- [Pipeline overview](README.md) — stage abstraction, orchestrator, ArtifactStore
- [Inventory contract](../contracts/inventory.md) — full field reference for the Inventory schema
- [Segment taxonomy §3.2](../architecture/segment-taxonomy.md) — near-duplicate handling and
  `index_superseded_versions` toggle (D-25)
