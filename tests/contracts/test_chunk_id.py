"""Unit tests for chunk-ID derivation (§10.5).

See docs/contracts/chunk.md#deterministic-chunk-id-derivation for the spec.

Tests:
- Canonical string matches the worked example byte-for-byte.
- Same inputs → same ID (idempotent).
- Each of the five inputs changing → different ID.
- chunk_id has the expected prefix and length.
- point_id derives from the same digest as chunk_id.
"""

from __future__ import annotations

import hashlib
import uuid

from finecorpus.contracts.chunk_id import (
    canonical_string,
    derive_chunk_id,
    derive_point_id,
)

# ---------------------------------------------------------------------------
# Worked example inputs (from docs/contracts/chunk.md#worked-example)
# ---------------------------------------------------------------------------

EXAMPLE_DOCUMENT_ID = "01J9Z3K7QMANUAL0001"
EXAMPLE_CONTENT_HASH = "9f2c8b1e77a4d0c3e5b19a24d8f6c0b2e4a7913d5c8f0a2b4d6e8f1a3c5b7d9e0"
# config_version is represented as a full 64-char sha256 hex in the spec;
# chunk.md shows it truncated as "c41d09f7b2e3a5..." — we use a full 64-char value
# that starts with those characters.
EXAMPLE_CONFIG_VERSION = (
    "c41d09f7b2e3a5" + "0" * 50  # padded to 64 hex chars to be a valid sha256 hex
)
EXAMPLE_SEGMENT_PATH = "Maintenance/Lubrication#3"
EXAMPLE_CHUNK_INDEX = 1

SEP = "\x1f"


class TestCanonicalString:
    """The canonical string must exactly match the spec derivation."""

    def test_structure_five_fields_joined_by_sep(self) -> None:
        """Canonical string is exactly five components joined by \\x1f."""
        canon = canonical_string(
            EXAMPLE_DOCUMENT_ID,
            EXAMPLE_CONTENT_HASH,
            EXAMPLE_CONFIG_VERSION,
            EXAMPLE_SEGMENT_PATH,
            EXAMPLE_CHUNK_INDEX,
        )
        parts = canon.split(SEP)
        assert len(parts) == 5  # noqa: PLR2004

    def test_components_match_inputs(self) -> None:
        """Each part of the canonical string matches the input in exact order."""
        canon = canonical_string(
            EXAMPLE_DOCUMENT_ID,
            EXAMPLE_CONTENT_HASH,
            EXAMPLE_CONFIG_VERSION,
            EXAMPLE_SEGMENT_PATH,
            EXAMPLE_CHUNK_INDEX,
        )
        parts = canon.split(SEP)
        assert parts[0] == EXAMPLE_DOCUMENT_ID
        assert parts[1] == EXAMPLE_CONTENT_HASH
        assert parts[2] == EXAMPLE_CONFIG_VERSION
        assert parts[3] == EXAMPLE_SEGMENT_PATH
        assert parts[4] == str(EXAMPLE_CHUNK_INDEX)

    def test_canonical_string_byte_for_byte_match(self) -> None:
        """Canonical string for the worked example matches the spec format byte-for-byte.

        Spec (docs/contracts/chunk.md#the-canonical-string):
            canonical = "\\x1f".join([document_id, content_hash, config_version,
                                      segment_path, str(chunk_index)])

        The spec's worked example shows the separator as '␟' (U+241F SYMBOL FOR UNIT SEPARATOR)
        for display; the actual separator is \\x1f (ASCII unit separator, 0x1F).
        """
        expected = SEP.join(
            [
                EXAMPLE_DOCUMENT_ID,
                EXAMPLE_CONTENT_HASH,
                EXAMPLE_CONFIG_VERSION,
                EXAMPLE_SEGMENT_PATH,
                "1",
            ]
        )
        actual = canonical_string(
            EXAMPLE_DOCUMENT_ID,
            EXAMPLE_CONTENT_HASH,
            EXAMPLE_CONFIG_VERSION,
            EXAMPLE_SEGMENT_PATH,
            EXAMPLE_CHUNK_INDEX,
        )
        assert actual == expected
        # Verify the separator byte is exactly 0x1F
        assert actual.encode("utf-8")[len(EXAMPLE_DOCUMENT_ID)] == 0x1F


class TestDeriveChunkId:
    """chunk_id = 'chk_' + base32_nopad(sha256(canonical.utf8))[:26]."""

    def test_prefix_is_chk(self) -> None:
        chunk_id = derive_chunk_id(
            EXAMPLE_DOCUMENT_ID,
            EXAMPLE_CONTENT_HASH,
            EXAMPLE_CONFIG_VERSION,
            EXAMPLE_SEGMENT_PATH,
            EXAMPLE_CHUNK_INDEX,
        )
        assert chunk_id.startswith("chk_")

    def test_total_length_is_30(self) -> None:
        """'chk_' (4) + 26 base32 chars = 30 chars total."""
        chunk_id = derive_chunk_id(
            EXAMPLE_DOCUMENT_ID,
            EXAMPLE_CONTENT_HASH,
            EXAMPLE_CONFIG_VERSION,
            EXAMPLE_SEGMENT_PATH,
            EXAMPLE_CHUNK_INDEX,
        )
        assert len(chunk_id) == 30  # noqa: PLR2004

    def test_lowercase_base32_nopad(self) -> None:
        """The 26 chars after 'chk_' must be lowercase base32 chars (a-z, 2-7)."""
        chunk_id = derive_chunk_id(
            EXAMPLE_DOCUMENT_ID,
            EXAMPLE_CONTENT_HASH,
            EXAMPLE_CONFIG_VERSION,
            EXAMPLE_SEGMENT_PATH,
            EXAMPLE_CHUNK_INDEX,
        )
        body = chunk_id[4:]
        valid_chars = set("abcdefghijklmnopqrstuvwxyz234567")
        assert all(c in valid_chars for c in body), f"Non-base32 chars in {body!r}"
        assert "=" not in chunk_id, "chunk_id must not contain padding"

    def test_idempotent_same_inputs_same_id(self) -> None:
        """Same inputs produce the same chunk_id every time."""
        id1 = derive_chunk_id(
            EXAMPLE_DOCUMENT_ID,
            EXAMPLE_CONTENT_HASH,
            EXAMPLE_CONFIG_VERSION,
            EXAMPLE_SEGMENT_PATH,
            EXAMPLE_CHUNK_INDEX,
        )
        id2 = derive_chunk_id(
            EXAMPLE_DOCUMENT_ID,
            EXAMPLE_CONTENT_HASH,
            EXAMPLE_CONFIG_VERSION,
            EXAMPLE_SEGMENT_PATH,
            EXAMPLE_CHUNK_INDEX,
        )
        assert id1 == id2

    def test_matches_manual_derivation(self) -> None:
        """chunk_id matches a manual computation of the same formula."""
        import base64

        canon = canonical_string(
            EXAMPLE_DOCUMENT_ID,
            EXAMPLE_CONTENT_HASH,
            EXAMPLE_CONFIG_VERSION,
            EXAMPLE_SEGMENT_PATH,
            EXAMPLE_CHUNK_INDEX,
        )
        digest = hashlib.sha256(canon.encode("utf-8")).digest()
        b32 = base64.b32encode(digest).decode("ascii").lower().rstrip("=")
        expected = "chk_" + b32[:26]

        actual = derive_chunk_id(
            EXAMPLE_DOCUMENT_ID,
            EXAMPLE_CONTENT_HASH,
            EXAMPLE_CONFIG_VERSION,
            EXAMPLE_SEGMENT_PATH,
            EXAMPLE_CHUNK_INDEX,
        )
        assert actual == expected


class TestChunkIdSensitivity:
    """Each of the five inputs changing must produce a different chunk_id."""

    BASE_KWARGS: dict = {
        "document_id": EXAMPLE_DOCUMENT_ID,
        "content_hash": EXAMPLE_CONTENT_HASH,
        "config_version": EXAMPLE_CONFIG_VERSION,
        "segment_path": EXAMPLE_SEGMENT_PATH,
        "chunk_index": EXAMPLE_CHUNK_INDEX,
    }

    def _base_id(self) -> str:
        return derive_chunk_id(**self.BASE_KWARGS)

    def test_different_document_id(self) -> None:
        """Changing document_id changes the chunk_id."""
        kwargs = {**self.BASE_KWARGS, "document_id": "01JDIFFERENT0001"}
        assert derive_chunk_id(**kwargs) != self._base_id()

    def test_different_content_hash(self) -> None:
        """Changing content_hash changes the chunk_id."""
        different_hash = "a" * 64  # a different 64-char hex
        kwargs = {**self.BASE_KWARGS, "content_hash": different_hash}
        assert derive_chunk_id(**kwargs) != self._base_id()

    def test_different_config_version(self) -> None:
        """Changing config_version changes the chunk_id."""
        different_config = "b" * 64
        kwargs = {**self.BASE_KWARGS, "config_version": different_config}
        assert derive_chunk_id(**kwargs) != self._base_id()

    def test_different_segment_path(self) -> None:
        """Changing segment_path changes the chunk_id."""
        kwargs = {**self.BASE_KWARGS, "segment_path": "Maintenance/Lubrication#4"}
        assert derive_chunk_id(**kwargs) != self._base_id()

    def test_different_chunk_index(self) -> None:
        """Changing chunk_index changes the chunk_id."""
        kwargs = {**self.BASE_KWARGS, "chunk_index": 2}
        assert derive_chunk_id(**kwargs) != self._base_id()


class TestDerivePointId:
    """point_id = UUID(bytes=sha256(canonical.utf8)[:16])."""

    def test_returns_uuid(self) -> None:
        point_id = derive_point_id(
            EXAMPLE_DOCUMENT_ID,
            EXAMPLE_CONTENT_HASH,
            EXAMPLE_CONFIG_VERSION,
            EXAMPLE_SEGMENT_PATH,
            EXAMPLE_CHUNK_INDEX,
        )
        assert isinstance(point_id, uuid.UUID)

    def test_derives_from_same_digest_as_chunk_id(self) -> None:
        """point_id derives from the first 16 bytes of the same sha256 digest as chunk_id."""
        canon = canonical_string(
            EXAMPLE_DOCUMENT_ID,
            EXAMPLE_CONTENT_HASH,
            EXAMPLE_CONFIG_VERSION,
            EXAMPLE_SEGMENT_PATH,
            EXAMPLE_CHUNK_INDEX,
        )
        digest = hashlib.sha256(canon.encode("utf-8")).digest()
        expected_point_id = uuid.UUID(bytes=digest[:16])

        actual_point_id = derive_point_id(
            EXAMPLE_DOCUMENT_ID,
            EXAMPLE_CONTENT_HASH,
            EXAMPLE_CONFIG_VERSION,
            EXAMPLE_SEGMENT_PATH,
            EXAMPLE_CHUNK_INDEX,
        )
        assert actual_point_id == expected_point_id

    def test_idempotent(self) -> None:
        id1 = derive_point_id(
            EXAMPLE_DOCUMENT_ID,
            EXAMPLE_CONTENT_HASH,
            EXAMPLE_CONFIG_VERSION,
            EXAMPLE_SEGMENT_PATH,
            EXAMPLE_CHUNK_INDEX,
        )
        id2 = derive_point_id(
            EXAMPLE_DOCUMENT_ID,
            EXAMPLE_CONTENT_HASH,
            EXAMPLE_CONFIG_VERSION,
            EXAMPLE_SEGMENT_PATH,
            EXAMPLE_CHUNK_INDEX,
        )
        assert id1 == id2
