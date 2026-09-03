"""Deterministic chunk ID derivation (§10.5).

See docs/contracts/chunk.md#deterministic-chunk-id-derivation for the authoritative spec.
Also see ADR-0006 for the design rationale.

The canonical string is:
    document_id + SEP + content_hash + SEP + config_version + SEP
    + segment_path + SEP + str(chunk_index)

where SEP = "\\x1f" (ASCII unit separator, which cannot occur in the component values).

chunk_id  = "chk_" + base32_nopad(sha256(canonical.encode("utf-8")))[:26]
point_id  = UUID(bytes=sha256(canonical.encode("utf-8"))[:16])

Both derive from the SAME digest, so they are 1:1 and either can be computed from
the other's inputs.

Worked example (from docs/contracts/chunk.md):
    document_id   = "01J9Z3K7QMANUAL0001"
    content_hash  = "9f2c8b1e77a4d0c3e5b19a24d8f6c0b2e4a7913d5c8f0a2b4d6e8f1a3c5b7d9e0"
    config_version= "c41d09f7b2e3a5..."   (sha256 hex, 64 chars)
    segment_path  = "Maintenance/Lubrication#3"
    chunk_index   = 1
"""

from __future__ import annotations

import base64
import hashlib
import uuid

# ASCII unit separator — cannot appear in ULID, sha256-hex, or segment_path
_SEP = "\x1f"


def canonical_string(
    document_id: str,
    content_hash: str,
    config_version: str,
    segment_path: str,
    chunk_index: int,
) -> str:
    """Build the canonical string for chunk-ID derivation.

    Five fields in fixed order, joined by ASCII unit separator (\\x1f).
    See docs/contracts/chunk.md#the-canonical-string.

    Args:
        document_id: ULID, stable file identity (Inventory document_id).
        content_hash: sha256 hex of source bytes — the document VERSION (§8).
        config_version: sha256 hex of build-affecting config fields (ingestion-config.md).
        segment_path: Canonical segment path within the document (segment-set.md).
        chunk_index: 0-based chunk position within the segment.

    Returns:
        The canonical string as a Python str (not yet encoded).
    """
    return _SEP.join(
        [
            document_id,
            content_hash,
            config_version,
            segment_path,
            str(chunk_index),
        ]
    )


def _digest(canonical: str) -> bytes:
    """Return the sha256 digest of the UTF-8-encoded canonical string."""
    return hashlib.sha256(canonical.encode("utf-8")).digest()


def derive_chunk_id(
    document_id: str,
    content_hash: str,
    config_version: str,
    segment_path: str,
    chunk_index: int,
) -> str:
    """Derive the deterministic chunk_id from the five stable inputs.

    chunk_id = "chk_" + base32_nopad(sha256(canonical.utf8))[:26]

    - sha256 of the UTF-8 canonical string.
    - Encode the digest in lowercase base32 without padding.
    - Take the first 26 chars, prefix "chk_".
    - Result is a stable, URL-safe, human-recognizable ID (32 chars total).
    - 26 base32 chars = 130 bits of the digest, collision-negligible at 5M chunks (§4.5 scale).

    See docs/contracts/chunk.md#the-hash-and-the-id-shape.

    Args:
        document_id: ULID, stable file identity (Inventory document_id).
        content_hash: sha256 hex of source bytes — the document VERSION (§8).
        config_version: sha256 hex of build-affecting config fields (ingestion-config.md).
        segment_path: Canonical segment path within the document (segment-set.md).
        chunk_index: 0-based chunk position within the segment.

    Returns:
        The chunk_id string, e.g. "chk_ab3f..." (32 chars total).
    """
    canon = canonical_string(document_id, content_hash, config_version, segment_path, chunk_index)
    digest = _digest(canon)
    b32 = base64.b32encode(digest).decode("ascii").lower().rstrip("=")
    return "chk_" + b32[:26]


def derive_point_id(
    document_id: str,
    content_hash: str,
    config_version: str,
    segment_path: str,
    chunk_index: int,
) -> uuid.UUID:
    """Derive the vector-DB point UUID from the five stable inputs.

    point_id = UUID(bytes=sha256(canonical.utf8)[:16])

    Both chunk_id (string) and point_id (UUID) derive from the SAME digest,
    so they are 1:1. The API always speaks chunk_id, never the raw point UUID
    (keeps the vector DB private to index/, C-2/C-3).

    See docs/contracts/chunk.md#vector-db-point-id-mapping.

    Args:
        document_id: ULID, stable file identity (Inventory document_id).
        content_hash: sha256 hex of source bytes — the document VERSION (§8).
        config_version: sha256 hex of build-affecting config fields (ingestion-config.md).
        segment_path: Canonical segment path within the document (segment-set.md).
        chunk_index: 0-based chunk position within the segment.

    Returns:
        The UUID point ID.
    """
    canon = canonical_string(document_id, content_hash, config_version, segment_path, chunk_index)
    digest = _digest(canon)
    return uuid.UUID(bytes=digest[:16])


__all__ = [
    "canonical_string",
    "derive_chunk_id",
    "derive_point_id",
]
