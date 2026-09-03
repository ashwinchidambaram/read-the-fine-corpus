"""
Golden corpus fixture tests — §18.1, build rule 7.

Validates:
  1. manifest.yaml is parseable and has correct schema.
  2. Every file listed in manifest exists and is non-empty.
  3. Every file's sha256 matches the manifest entry.
  4. The corpus covers all §18.1 mandatory categories.
  5. Every §18.1 mandatory category maps to at least one fixture.

These tests do NOT run the pipeline. They verify only that the fixture corpus
is present, intact, and categorically complete — the prerequisite for any
integration test to be meaningful.
"""

import hashlib
from pathlib import Path
from typing import Any

import pytest
import yaml

# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

GOLDEN_DIR = Path(__file__).parent / "fixtures" / "golden"
MANIFEST_PATH = GOLDEN_DIR / "manifest.yaml"

# §18.1 mandatory categories — every one must have at least one fixture entry
# whose `role` or `flags` cover the category.
MANDATORY_CATEGORIES = {
    "clean_native_pdf": "§18.1 — clean native-text PDF",
    "poorly_scanned_pdf": "§18.1 — poorly scanned PDF",
    "bloated_manual": "§18.1 — bloated manual",
    "complex_nested_tables": "§18.1 — document with complex",
    "spreadsheet_report": "§18.1 — spreadsheet: report kind",
    "spreadsheet_database": "§18.1 — spreadsheet: database kind",
    "spreadsheet_model": "§18.1 — spreadsheet: model kind",
    "confluence_html": "§18.1 — Confluence-style HTML export",
    "near_duplicate": "§18.1 — near-duplicate",
    "unservable": "§18.1 — unservable",
    "adversarial": "§18.1 + §14.1 — adversarial",
    "form_field": "§18.1 Finding F-1",
    "malformed_structure": "§18.1 Finding F-2",
}


@pytest.fixture(scope="session")
def manifest() -> dict[str, Any]:
    """Load and return the parsed manifest.yaml."""
    assert MANIFEST_PATH.exists(), f"manifest.yaml not found at {MANIFEST_PATH}"
    data = yaml.safe_load(MANIFEST_PATH.read_text(encoding="utf-8"))
    return data  # type: ignore[return-value]


@pytest.fixture(scope="session")
def fixture_entries(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the list of fixture entries from the manifest."""
    assert "fixtures" in manifest, "manifest.yaml must have a 'fixtures' key"
    return manifest["fixtures"]  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Test 1: manifest is valid YAML with required schema
# ---------------------------------------------------------------------------

class TestManifestSchema:
    def test_manifest_parses(self, manifest: dict[str, Any]) -> None:
        """manifest.yaml must be parseable YAML."""
        assert isinstance(manifest, dict), "manifest.yaml must be a mapping"

    def test_schema_version_present(self, manifest: dict[str, Any]) -> None:
        """manifest.yaml must have schema_version."""
        assert "schema_version" in manifest, "manifest must have schema_version"
        assert isinstance(manifest["schema_version"], str)

    def test_fixtures_list_present(self, manifest: dict[str, Any]) -> None:
        """manifest.yaml must have a non-empty fixtures list."""
        assert "fixtures" in manifest, "manifest must have fixtures list"
        assert isinstance(manifest["fixtures"], list), "fixtures must be a list"
        assert len(manifest["fixtures"]) > 0, "fixtures list must not be empty"

    def test_each_entry_has_required_fields(self, fixture_entries: list[dict[str, Any]]) -> None:
        """Every fixture entry must have: file, role, sha256."""
        required = {"file", "role", "sha256"}
        for entry in fixture_entries:
            missing = required - set(entry.keys())
            assert not missing, (
                f"Fixture entry for {entry.get('file', '?')} missing required fields: {missing}"
            )

    def test_no_duplicate_files(self, fixture_entries: list[dict[str, Any]]) -> None:
        """No two entries may reference the same file."""
        files = [e["file"] for e in fixture_entries]
        seen: set[str] = set()
        duplicates: list[str] = []
        for f in files:
            if f in seen:
                duplicates.append(f)
            seen.add(f)
        assert not duplicates, f"Duplicate file entries in manifest: {duplicates}"


# ---------------------------------------------------------------------------
# Test 2: every fixture file exists and is non-empty
# ---------------------------------------------------------------------------

class TestFixtureFilesExist:
    def test_all_files_exist(self, fixture_entries: list[dict[str, Any]]) -> None:
        """Every file listed in the manifest must exist on disk."""
        missing: list[str] = []
        for entry in fixture_entries:
            path = GOLDEN_DIR / entry["file"]
            if not path.exists():
                missing.append(str(entry["file"]))
        assert not missing, f"Fixture files missing from disk: {missing}"

    def test_all_files_non_empty(self, fixture_entries: list[dict[str, Any]]) -> None:
        """Every fixture file must have size > 0."""
        empty: list[str] = []
        for entry in fixture_entries:
            path = GOLDEN_DIR / entry["file"]
            if path.exists() and path.stat().st_size == 0:
                empty.append(str(entry["file"]))
        assert not empty, f"Fixture files are empty (0 bytes): {empty}"

    def test_corpus_total_size_under_limit(self, fixture_entries: list[dict[str, Any]]) -> None:
        """Total corpus size must be under 5 MB (keep tests fast and repo lean)."""
        max_bytes = 5 * 1024 * 1024  # 5 MB
        total = sum(
            (GOLDEN_DIR / e["file"]).stat().st_size
            for e in fixture_entries
            if (GOLDEN_DIR / e["file"]).exists()
        )
        assert total < max_bytes, (
            f"Corpus total size {total:,} bytes exceeds {max_bytes:,} bytes (5 MB)"
        )


# ---------------------------------------------------------------------------
# Test 3: sha256 integrity
# ---------------------------------------------------------------------------

class TestSha256Integrity:
    def test_all_sha256_match(self, fixture_entries: list[dict[str, Any]]) -> None:
        """Every file's sha256 must match the manifest entry."""
        mismatches: list[tuple[str, str, str]] = []
        for entry in fixture_entries:
            path = GOLDEN_DIR / entry["file"]
            if not path.exists():
                continue  # file-existence tests cover this
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
            expected = entry["sha256"]
            if actual != expected:
                mismatches.append((str(entry["file"]), expected, actual))

        if mismatches:
            lines = [
                f"  {name}: expected {exp[:16]}... got {act[:16]}..."
                for name, exp, act in mismatches
            ]
            pytest.fail(
                "sha256 mismatch for fixtures (regenerate and update manifest):\n"
                + "\n".join(lines)
            )


# ---------------------------------------------------------------------------
# Test 4: §18.1 mandatory category coverage
# ---------------------------------------------------------------------------

class TestMandatoryCategoryCompleteness:
    def test_all_mandatory_categories_covered(
        self, fixture_entries: list[dict[str, Any]]
    ) -> None:
        """Every §18.1 mandatory category must be covered by at least one fixture entry."""
        roles = [e.get("role", "") for e in fixture_entries]

        missing_categories: list[str] = []
        for cat_id, role_substring in MANDATORY_CATEGORIES.items():
            covered = any(role_substring.lower() in r.lower() for r in roles)
            if not covered:
                missing_categories.append(f"{cat_id!r} (expected substring: {role_substring!r})")

        assert not missing_categories, (
            "The following §18.1 mandatory categories have no fixture coverage:\n"
            + "\n".join(f"  - {c}" for c in missing_categories)
        )

    def test_adversarial_has_injection_flags(
        self, fixture_entries: list[dict[str, Any]]
    ) -> None:
        """Adversarial fixture must declare injection vector flags."""
        adv_entries = [e for e in fixture_entries if "adversarial" in e.get("role", "").lower()]
        assert adv_entries, "No adversarial fixture found in manifest"
        adv = adv_entries[0]
        flags = adv.get("flags", [])
        injection_flags = [f for f in flags if "injection" in f]
        assert injection_flags, (
            "Adversarial fixture must have at least one 'injection_*' flag; "
            f"found flags: {flags}"
        )

    def test_adversarial_has_invisible_content_flags(
        self, fixture_entries: list[dict[str, Any]]
    ) -> None:
        """Adversarial fixture must declare invisible-content flags (§14.1 vectors)."""
        adv_entries = [e for e in fixture_entries if "adversarial" in e.get("role", "").lower()]
        assert adv_entries, "No adversarial fixture found in manifest"
        adv = adv_entries[0]
        flags = adv.get("flags", [])
        invisible_flags = [f for f in flags if "invisible" in f]
        assert invisible_flags, (
            "Adversarial fixture must have at least one 'invisible_*' flag; "
            f"found flags: {flags}"
        )

    def test_unservable_set_covers_required_types(
        self, fixture_entries: list[dict[str, Any]]
    ) -> None:
        """Unservable set must include encrypted PDF, audio, video, image-only, and CAD."""
        unservable = [
            e for e in fixture_entries
            if "unservable" in e.get("role", "").lower()
        ]
        classes = {e.get("expected_triage_class", "") for e in unservable}
        required_classes = {
            "encrypted_pdf",
            "audio",
            "video",
            "image_only_pdf",
            "cad_binary",
        }
        missing = required_classes - classes
        assert not missing, (
            f"Unservable set is missing triage classes: {missing}. "
            f"Present: {classes}"
        )

    def test_spreadsheet_three_kinds_present(
        self, fixture_entries: list[dict[str, Any]]
    ) -> None:
        """All three spreadsheet kinds (report, database, model) must be present."""
        classes = {e.get("expected_triage_class", "") for e in fixture_entries}
        for kind in ("spreadsheet_report", "spreadsheet_database", "spreadsheet_model"):
            assert kind in classes, (
                f"Spreadsheet kind {kind!r} not found in any fixture's expected_triage_class"
            )

    def test_near_duplicate_family_has_three_members(
        self, fixture_entries: list[dict[str, Any]]
    ) -> None:
        """Near-duplicate family must have exactly 3 members (v1, v2, v3)."""
        nd_entries = [
            e for e in fixture_entries
            if "near-duplicate" in e.get("role", "").lower()
        ]
        assert len(nd_entries) >= 3, (
            f"Near-duplicate family must have at least 3 members; found {len(nd_entries)}"
        )

    def test_near_duplicate_family_has_primary(
        self, fixture_entries: list[dict[str, Any]]
    ) -> None:
        """Near-duplicate family must have exactly one primary version."""
        nd_entries = [
            e for e in fixture_entries
            if "near-duplicate" in e.get("role", "").lower()
        ]
        primaries = [e for e in nd_entries if "primary" in e.get("role", "").lower()]
        assert len(primaries) == 1, (
            f"Near-duplicate family must have exactly 1 primary entry; found {len(primaries)}"
        )


# ---------------------------------------------------------------------------
# Test 5: generator scripts exist for every fixture
# ---------------------------------------------------------------------------

class TestGeneratorsExist:
    def test_all_generators_exist(self, fixture_entries: list[dict[str, Any]]) -> None:
        """Every manifest entry must reference a generator script that exists."""
        missing: list[str] = []
        for entry in fixture_entries:
            gen = entry.get("generator")
            if gen is None:
                missing.append(f"{entry['file']}: no generator field")
                continue
            gen_path = GOLDEN_DIR / gen
            if not gen_path.exists():
                missing.append(f"{entry['file']}: generator {gen} not found")
        assert not missing, (
            "Missing generator scripts:\n" + "\n".join(f"  {m}" for m in missing)
        )
