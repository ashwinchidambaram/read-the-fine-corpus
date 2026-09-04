"""Stage 1 — Collect.

Walks a source directory and produces a valid Inventory contract artifact.
This is the only REAL stage in Phase 0; Assess/Decompose/Plan/Build are skeletons.

Design notes:
  document_id derivation:
    ULIDs encode a millisecond timestamp + random component; using them randomly
    would break determinism.  Instead we derive a deterministic "ULID-like" ID from
    the content_hash using the following scheme:

      base32(sha256(content_hash)[:10]) — 16 base32 chars = 80 bits

    This is:
      - Deterministic: same bytes → same ID across runs.
      - Stable: file rename/move does not change the ID.
      - Unique: sha256 collision probability negligible.
      - Inspectable: a hex-looking string in logs traces back to the file.

    We do NOT encode a time component because that would make the ID change
    between runs (violating the "stable across re-collections" invariant in §12).
    The discovered_at timestamp is stored separately in the InventoryItem.

  Duplicate detection:
    Exact duplicates are detected by grouping items with identical content_hash.
    The first-discovered file (lexicographic sort on source_path for determinism)
    becomes the primary; the rest are marked exact_duplicate.

  mtime:
    source_modified_at is populated from os.stat().st_mtime_ns, converted to UTC.
    source_created_at is set from os.stat().st_ctime_ns on POSIX (creation time
    not reliably available on Linux; ctime = inode change time).  This is a known
    limitation documented in the inventory item.
"""

from finecorpus.pipeline.collect.stage import CollectStage

__all__ = ["CollectStage"]
