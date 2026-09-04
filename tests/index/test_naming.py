"""Unit tests for collection naming and alias naming (§2.1, §2.2)."""

from __future__ import annotations

from finecorpus.index.adapter import alias_name, collection_name


class TestCollectionName:
    def test_basic_format(self) -> None:
        name = collection_name("a3f9b2c1-d4e5-0000-0000-000000000000", 7)
        assert name == "rtfc_a3f9b2c1d4e500000000000000000000_00000007"

    def test_hyphen_stripped(self) -> None:
        """KB ID hyphens are stripped."""
        name = collection_name("aaaa-bbbb-cccc", 1)
        assert "-" not in name

    def test_build_id_zero_padded_8(self) -> None:
        """build_id is zero-padded to 8 digits."""
        assert collection_name("abc", 0).endswith("_00000000")
        assert collection_name("abc", 1).endswith("_00000001")
        assert collection_name("abc", 99999999).endswith("_99999999")
        assert collection_name("abc", 100000000).endswith("_100000000")

    def test_lowercase(self) -> None:
        """Collection name is all lowercase."""
        name = collection_name("AABBCCDD", 1)
        assert name == name.lower()

    def test_rtfc_prefix(self) -> None:
        name = collection_name("abc", 1)
        assert name.startswith("rtfc_")

    def test_deterministic(self) -> None:
        """Same inputs always produce the same output."""
        assert collection_name("abc", 5) == collection_name("abc", 5)

    def test_different_build_ids_differ(self) -> None:
        assert collection_name("abc", 1) != collection_name("abc", 2)

    def test_different_kb_ids_differ(self) -> None:
        assert collection_name("abc", 1) != collection_name("def", 1)


class TestAliasName:
    def test_basic_format(self) -> None:
        name = alias_name("a3f9b2c1-d4e5-0000-0000-000000000000")
        assert name == "rtfc_a3f9b2c1d4e500000000000000000000"

    def test_hyphen_stripped(self) -> None:
        name = alias_name("aaaa-bbbb-cccc")
        assert "-" not in name

    def test_lowercase(self) -> None:
        name = alias_name("AABBCC")
        assert name == name.lower()

    def test_rtfc_prefix(self) -> None:
        name = alias_name("abc")
        assert name.startswith("rtfc_")

    def test_stable_for_same_kb(self) -> None:
        assert alias_name("mykb") == alias_name("mykb")

    def test_no_build_id_in_alias(self) -> None:
        """Alias does not encode build_id."""
        n1 = alias_name("mykb")
        n2 = alias_name("mykb")
        assert n1 == n2
        # Unlike collection_name, alias has no build_id component
        assert "_0000" not in alias_name("mykb")
