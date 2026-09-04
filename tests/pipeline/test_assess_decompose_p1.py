"""Phase 1 tests — Assess and Decompose stages (native-text PDF).

Test coverage:
  - Nothing-dropped: parse entries == inventory count.
  - Nothing-dropped: segment sets == parse count.
  - Property test: reassembly (segments concat → matches reassembly_digest).
  - Malformed fixture partial-parse assertions (page 1 ok, page 2 failure recorded).
  - Frozen-artifact reuse (second run loads from cache, does not recompute).
  - Determinism (two runs same key → identical segment set JSON).
  - ParseResultBatch version-check now enforced (D-26).
  - SegmentSetBatch version-check now enforced (D-26).
  - Unservable files produce honest exclusions (no fake successes).
  - Encrypted PDF produces failed parse result.
  - Image-only PDF produces failed parse result with finding.
"""

from __future__ import annotations

import hashlib
import pathlib
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from finecorpus.contracts.versions import ContractVersionError
from finecorpus.pipeline import run_pipeline
from finecorpus.pipeline.artifact_store import ArtifactStore
from finecorpus.pipeline.decompose.stage import _CONFIG_VERSION, DecomposeStage

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

FIXTURE_CORPUS = pathlib.Path(__file__).parent.parent / "fixtures" / "golden" / "corpus"

NATIVE_PDF_FIXTURES = [
    "clean_native.pdf",
    "bloated_manual.pdf",
    "boilerplate_a.pdf",
    "boilerplate_b.pdf",
    "form_filled.pdf",
    "policy_v1.pdf",
    "policy_v2.pdf",
    "policy_v3.pdf",
]
"""Fixtures that pypdf can extract text from (native-text PDFs)."""

UNSERVABLE_FIXTURES = [
    "audio_stub.wav",
    "video_stub.mp4",
    "cad_binary.dwg",
]

HTML_FIXTURES = [
    "confluence_export.html",
    "nested_tables.html",
]

SPREADSHEET_FIXTURES = [
    "database_spreadsheet.xlsx",
    "model_spreadsheet.xlsx",
    "report_spreadsheet.xlsx",
]


def _run_pipeline_over_corpus(
    tmp_path: Path,
    source_dir: Path | None = None,
    run_id: str = "test-p1",
) -> tuple[ArtifactStore, dict[str, Any], dict[str, Any]]:
    """Run the full pipeline over corpus (or a subset dir) and return store + artifacts."""
    artifacts_root = tmp_path / "artifacts"
    src = source_dir or FIXTURE_CORPUS

    run_pipeline(
        source_dir=src,
        artifacts_root=artifacts_root,
        run_id=run_id,
        workspace_id="ws-p1-test",
        kb_id="kb-p1-test",
    )
    store = ArtifactStore(artifacts_root=artifacts_root, run_id=run_id)
    parse_batch = store.load("assess")
    seg_batch = store.load("decompose")
    return store, parse_batch, seg_batch


# ---------------------------------------------------------------------------
# Nothing-dropped invariants
# ---------------------------------------------------------------------------


class TestNothingDropped:
    """Assess and Decompose must cover every inventory item (§6 rule 6)."""

    def test_parse_entries_equal_inventory_count(self, tmp_path):
        """One ParseResult per inventory item — nothing silently dropped."""
        store, parse_batch, _ = _run_pipeline_over_corpus(tmp_path)
        inv = store.load("collect")
        assert len(parse_batch["results"]) == len(inv["items"]), (
            f"Parse entries ({len(parse_batch['results'])}) != "
            f"inventory count ({len(inv['items'])})"
        )

    def test_segment_sets_equal_parse_count(self, tmp_path):
        """One SegmentSet per ParseResult — nothing silently dropped."""
        _, parse_batch, seg_batch = _run_pipeline_over_corpus(tmp_path)
        assert len(seg_batch["segment_sets"]) == len(parse_batch["results"]), (
            f"SegmentSets ({len(seg_batch['segment_sets'])}) != "
            f"ParseResults ({len(parse_batch['results'])})"
        )

    def test_every_parse_result_has_schema_version(self, tmp_path):
        """Every ParseResult must have schema_version (contract invariant)."""
        _, parse_batch, _ = _run_pipeline_over_corpus(tmp_path)
        for pr in parse_batch["results"]:
            assert pr.get("schema_version"), (
                f"ParseResult for {pr.get('document_id')} missing schema_version"
            )

    def test_every_segment_set_has_schema_version(self, tmp_path):
        """Every SegmentSet must have schema_version."""
        _, _, seg_batch = _run_pipeline_over_corpus(tmp_path)
        for ss in seg_batch["segment_sets"]:
            assert ss.get("schema_version"), (
                f"SegmentSet for {ss.get('document_id')} missing schema_version"
            )


# ---------------------------------------------------------------------------
# Reassembly property test
# ---------------------------------------------------------------------------


class TestReassemblyProperty:
    """Segments reassemble to extracted text (§6.3, §12, §18.2)."""

    @pytest.mark.parametrize("fixture_name", NATIVE_PDF_FIXTURES)
    def test_reassembly_for_native_pdf_fixture(self, tmp_path, fixture_name):
        """Concatenating segment text by document_order reproduces reassembly_digest."""
        # Use a single-file corpus for isolation
        src_dir = tmp_path / "src"
        src_dir.mkdir()
        shutil.copy2(FIXTURE_CORPUS / fixture_name, src_dir / fixture_name)

        _, _, seg_batch = _run_pipeline_over_corpus(tmp_path, source_dir=src_dir)

        # Find the segment set for this fixture
        assert len(seg_batch["segment_sets"]) >= 1
        ss = seg_batch["segment_sets"][0]

        segments = ss.get("segments", [])
        reassembly = ss.get("reassembly", {})

        # Sort by document_order and concatenate
        sorted_segs = sorted(segments, key=lambda s: s["document_order"])
        concat_text = "".join((s.get("text") or "") for s in sorted_segs)
        actual_digest = hashlib.sha256(concat_text.encode()).hexdigest()

        assert actual_digest == reassembly["reassembly_digest"], (
            f"{fixture_name}: reassembly digest mismatch. "
            f"Segments do not reconstruct the extracted text. "
            f"Expected {reassembly['reassembly_digest']}, got {actual_digest}"
        )

    @pytest.mark.parametrize("fixture_name", NATIVE_PDF_FIXTURES)
    def test_document_order_is_dense_and_gapless(self, tmp_path, fixture_name):
        """document_order must be 0-based, dense, and gapless (§6.3, segment-set.md)."""
        src_dir = tmp_path / "src"
        src_dir.mkdir()
        shutil.copy2(FIXTURE_CORPUS / fixture_name, src_dir / fixture_name)

        _, _, seg_batch = _run_pipeline_over_corpus(tmp_path, source_dir=src_dir)
        ss = seg_batch["segment_sets"][0]
        segments = ss.get("segments", [])
        orders = sorted(s["document_order"] for s in segments)
        if orders:
            assert orders[0] == 0, f"{fixture_name}: document_order does not start at 0"
            expected = list(range(len(orders)))
            assert orders == expected, (
                f"{fixture_name}: document_order is not dense/gapless. "
                f"Got {orders}, expected {expected}"
            )

    @pytest.mark.parametrize("fixture_name", NATIVE_PDF_FIXTURES)
    def test_every_segment_has_required_fields(self, tmp_path, fixture_name):
        """Every segment must have segment_type, salience_tier, structural_path, location."""
        src_dir = tmp_path / "src"
        src_dir.mkdir()
        shutil.copy2(FIXTURE_CORPUS / fixture_name, src_dir / fixture_name)

        _, _, seg_batch = _run_pipeline_over_corpus(tmp_path, source_dir=src_dir)
        ss = seg_batch["segment_sets"][0]
        for seg in ss.get("segments", []):
            assert seg.get("segment_type"), f"Segment missing segment_type in {fixture_name}"
            assert seg.get("salience_tier"), f"Segment missing salience_tier in {fixture_name}"
            assert "structural_path" in seg, f"Segment missing structural_path in {fixture_name}"
            assert seg.get("location"), f"Segment missing location in {fixture_name}"
            assert seg.get("segment_path"), f"Segment missing segment_path in {fixture_name}"

    @pytest.mark.parametrize("fixture_name", NATIVE_PDF_FIXTURES)
    def test_salience_signals_complete(self, tmp_path, fixture_name):
        """Every segment must have exactly one won=True salience_signal."""
        src_dir = tmp_path / "src"
        src_dir.mkdir()
        shutil.copy2(FIXTURE_CORPUS / fixture_name, src_dir / fixture_name)

        _, _, seg_batch = _run_pipeline_over_corpus(tmp_path, source_dir=src_dir)
        ss = seg_batch["segment_sets"][0]
        for seg in ss.get("segments", []):
            signals = seg.get("salience_signals", [])
            winners = [s for s in signals if s.get("won") is True]
            assert len(winners) == 1, (
                f"{fixture_name}: segment {seg.get('segment_id')} has "
                f"{len(winners)} won=True salience signals (must be exactly 1)"
            )
            # salience_basis must match the winner's kind
            assert seg.get("salience_basis") == winners[0]["kind"], (
                f"{fixture_name}: salience_basis != won signal kind"
            )


# ---------------------------------------------------------------------------
# Malformed fixture partial-parse assertions
# ---------------------------------------------------------------------------


class TestMalformedFixture:
    """malformed_structure.pdf: page 1 ok, page 2 recorded as failure."""

    @pytest.fixture(scope="class")
    def malformed_parse_result(self, tmp_class_path):
        """Run assess on malformed_structure.pdf and return its ParseResult dict."""
        src_dir = tmp_class_path / "src"
        src_dir.mkdir()
        malformed = FIXTURE_CORPUS / "malformed_structure.pdf"
        shutil.copy2(malformed, src_dir / "malformed_structure.pdf")

        store, parse_batch, _ = _run_pipeline_over_corpus(
            tmp_class_path, source_dir=src_dir, run_id="malformed-test"
        )
        assert len(parse_batch["results"]) == 1
        return parse_batch["results"][0]

    @pytest.fixture(scope="class")
    def malformed_segment_set(self, tmp_class_path):
        """Run full pipeline on malformed_structure.pdf and return the SegmentSet dict."""
        src_dir = tmp_class_path / "src"
        src_dir.mkdir(exist_ok=True)
        malformed = FIXTURE_CORPUS / "malformed_structure.pdf"
        shutil.copy2(malformed, src_dir / "malformed_structure.pdf")

        _, _, seg_batch = _run_pipeline_over_corpus(
            tmp_class_path, source_dir=src_dir, run_id="malformed-test"
        )
        assert len(seg_batch["segment_sets"]) == 1
        return seg_batch["segment_sets"][0]

    @pytest.fixture(scope="class")
    def tmp_class_path(self, tmp_path_factory):
        return tmp_path_factory.mktemp("malformed")

    def test_parse_status_is_partial(self, malformed_parse_result):
        """malformed_structure.pdf must parse as partial (not failed, not parsed)."""
        status = malformed_parse_result.get("parse_status")
        assert status == "partial", (
            f"Expected parse_status=partial for malformed PDF, got {status!r}"
        )

    def test_page_1_has_text(self, malformed_parse_result):
        """Page 1 (valid content stream) must yield text."""
        regions = malformed_parse_result.get("regions", [])
        page1_regions = [r for r in regions if r.get("location", {}).get("page_start") == 1]
        assert page1_regions, "No region for page 1"
        page1_text = page1_regions[0].get("text") or ""
        assert len(page1_text) > 50, (  # noqa: PLR2004
            f"Page 1 should have substantial text, got {len(page1_text)} chars"
        )

    def test_page_2_failure_recorded(self, malformed_parse_result):
        """Page 2 (corrupted content stream) must have a failure recorded."""
        pages = malformed_parse_result.get("pages", [])
        page2 = next((p for p in pages if p.get("page_number") == 2), None)
        assert page2 is not None, "No PageResult for page 2"

        regions = malformed_parse_result.get("regions", [])
        page2_regions = [r for r in regions if r.get("location", {}).get("page_start") == 2]
        if page2_regions:
            # If there is a region, it should be empty (0 chars)
            region_text = page2_regions[0].get("text")
            assert region_text is None or region_text == "", (
                "Page 2 region should have no text (extraction failed)"
            )
        else:
            # No region = fully skipped — also acceptable (empty → exclusion)
            pass

    def test_parse_finding_for_page_2(self, malformed_parse_result):
        """A parse_error finding must be present for the failed page."""
        findings = malformed_parse_result.get("findings", [])
        parse_error_findings = [f for f in findings if f.get("code") == "parse_error"]
        assert parse_error_findings, "Expected at least one parse_error finding for malformed PDF"

    def test_segment_set_has_segments_from_page_1(self, malformed_segment_set):
        """Segments from page 1 must be present in the segment set."""
        segments = malformed_segment_set.get("segments", [])
        assert len(segments) >= 1, "Expected at least one segment from the valid page 1 content"

    def test_exclusion_records_page_2(self, malformed_segment_set):
        """The failed page 2 must appear as an ExclusionRecord (not silently dropped)."""
        exclusions = malformed_segment_set.get("exclusions", [])
        assert exclusions, "Expected ExclusionRecord(s) for the page 2 parse failure"
        reasons = {e.get("reason") for e in exclusions}
        assert "parse_failed" in reasons or "empty_region" in reasons, (
            f"Expected parse_failed or empty_region exclusion, got: {reasons}"
        )


# ---------------------------------------------------------------------------
# Unservable / excluded files
# ---------------------------------------------------------------------------


class TestUnservableFiles:
    """Non-PDF files and unservable PDFs produce honest exclusions (never fake success)."""

    @pytest.mark.parametrize(
        "fixture_name",
        [
            "audio_stub.wav",
            "video_stub.mp4",
            "cad_binary.dwg",
        ],
    )
    def test_unservable_parse_status(self, tmp_path, fixture_name):
        """Unservable files must have parse_status=excluded_pre_parse."""
        src_dir = tmp_path / "src"
        src_dir.mkdir()
        shutil.copy2(FIXTURE_CORPUS / fixture_name, src_dir / fixture_name)

        _, parse_batch, _ = _run_pipeline_over_corpus(tmp_path, source_dir=src_dir)
        pr = parse_batch["results"][0]
        assert pr["parse_status"] == "excluded_pre_parse", (
            f"{fixture_name}: expected excluded_pre_parse, got {pr['parse_status']!r}"
        )

    @pytest.mark.parametrize("fixture_name", HTML_FIXTURES)
    def test_html_parse_status(self, tmp_path, fixture_name):
        """HTML files must be excluded in Phase 1."""
        src_dir = tmp_path / "src"
        src_dir.mkdir()
        shutil.copy2(FIXTURE_CORPUS / fixture_name, src_dir / fixture_name)

        _, parse_batch, _ = _run_pipeline_over_corpus(tmp_path, source_dir=src_dir)
        pr = parse_batch["results"][0]
        assert pr["parse_status"] == "excluded_pre_parse", (
            f"{fixture_name}: expected excluded_pre_parse, got {pr['parse_status']!r}"
        )

    @pytest.mark.parametrize("fixture_name", SPREADSHEET_FIXTURES)
    def test_spreadsheet_parse_status(self, tmp_path, fixture_name):
        """Spreadsheets must be excluded in Phase 1."""
        src_dir = tmp_path / "src"
        src_dir.mkdir()
        shutil.copy2(FIXTURE_CORPUS / fixture_name, src_dir / fixture_name)

        _, parse_batch, _ = _run_pipeline_over_corpus(tmp_path, source_dir=src_dir)
        pr = parse_batch["results"][0]
        assert pr["parse_status"] == "excluded_pre_parse", (
            f"{fixture_name}: expected excluded_pre_parse, got {pr['parse_status']!r}"
        )

    def test_encrypted_pdf_parse_status(self, tmp_path):
        """password_protected.pdf must produce parse_status=failed."""
        src_dir = tmp_path / "src"
        src_dir.mkdir()
        shutil.copy2(FIXTURE_CORPUS / "password_protected.pdf", src_dir / "password_protected.pdf")

        _, parse_batch, _ = _run_pipeline_over_corpus(tmp_path, source_dir=src_dir)
        pr = parse_batch["results"][0]
        assert pr["parse_status"] == "failed", (
            f"Expected failed for encrypted PDF, got {pr['parse_status']!r}"
        )
        codes = {f["code"] for f in pr.get("findings", [])}
        assert "password_protected" in codes, f"Expected password_protected finding, got: {codes}"

    def test_image_only_pdf_parse_status(self, tmp_path):
        """image_only.pdf must produce parse_status=failed with unservable_image_only finding."""
        src_dir = tmp_path / "src"
        src_dir.mkdir()
        shutil.copy2(FIXTURE_CORPUS / "image_only.pdf", src_dir / "image_only.pdf")

        _, parse_batch, _ = _run_pipeline_over_corpus(tmp_path, source_dir=src_dir)
        pr = parse_batch["results"][0]
        assert pr["parse_status"] == "failed", (
            f"Expected failed for image-only PDF, got {pr['parse_status']!r}"
        )
        codes = {f["code"] for f in pr.get("findings", [])}
        assert "unservable_image_only" in codes, (
            f"Expected unservable_image_only finding, got: {codes}"
        )

    @pytest.mark.parametrize(
        "fixture_name",
        [
            "audio_stub.wav",
            "video_stub.mp4",
            "cad_binary.dwg",
            "confluence_export.html",
            "nested_tables.html",
            "database_spreadsheet.xlsx",
        ],
    )
    def test_excluded_file_produces_empty_segment_set_with_exclusion(self, tmp_path, fixture_name):
        """Excluded files must produce empty SegmentSet with at least one ExclusionRecord."""
        src_dir = tmp_path / "src"
        src_dir.mkdir()
        shutil.copy2(FIXTURE_CORPUS / fixture_name, src_dir / fixture_name)

        _, _, seg_batch = _run_pipeline_over_corpus(tmp_path, source_dir=src_dir)
        ss = seg_batch["segment_sets"][0]

        assert ss.get("segments") == [], (
            f"{fixture_name}: expected empty segments, got {ss.get('segments')}"
        )
        assert ss.get("exclusions"), (
            f"{fixture_name}: expected at least one ExclusionRecord, got none"
        )


# ---------------------------------------------------------------------------
# Native PDF parse quality
# ---------------------------------------------------------------------------


class TestNativePDFQuality:
    """Native-text PDFs must have parse_status=parsed and meaningful quality scores."""

    @pytest.mark.parametrize("fixture_name", NATIVE_PDF_FIXTURES)
    def test_native_pdf_parse_status(self, tmp_path, fixture_name):
        """Native-text PDFs should parse successfully."""
        src_dir = tmp_path / "src"
        src_dir.mkdir()
        shutil.copy2(FIXTURE_CORPUS / fixture_name, src_dir / fixture_name)

        _, parse_batch, _ = _run_pipeline_over_corpus(tmp_path, source_dir=src_dir)
        pr = parse_batch["results"][0]
        assert pr["parse_status"] == "parsed", (
            f"{fixture_name}: expected parsed, got {pr['parse_status']!r}"
        )

    @pytest.mark.parametrize("fixture_name", NATIVE_PDF_FIXTURES)
    def test_native_pdf_overall_quality_above_zero(self, tmp_path, fixture_name):
        """Native-text PDFs should have quality.overall > 0."""
        src_dir = tmp_path / "src"
        src_dir.mkdir()
        shutil.copy2(FIXTURE_CORPUS / fixture_name, src_dir / fixture_name)

        _, parse_batch, _ = _run_pipeline_over_corpus(tmp_path, source_dir=src_dir)
        pr = parse_batch["results"][0]
        quality = pr.get("quality", {})
        assert quality.get("overall", 0.0) > 0.0, (
            f"{fixture_name}: expected quality.overall > 0, got {quality.get('overall')}"
        )

    @pytest.mark.parametrize("fixture_name", NATIVE_PDF_FIXTURES)
    def test_native_pdf_confidence_1_0(self, tmp_path, fixture_name):
        """Native-text pages must have ocr_confidence=None (no OCR)."""
        src_dir = tmp_path / "src"
        src_dir.mkdir()
        shutil.copy2(FIXTURE_CORPUS / fixture_name, src_dir / fixture_name)

        _, parse_batch, _ = _run_pipeline_over_corpus(tmp_path, source_dir=src_dir)
        pr = parse_batch["results"][0]
        for page in pr.get("pages", []):
            assert page.get("ocr_confidence") is None, (
                f"{fixture_name}: native-text page should have ocr_confidence=None, "
                f"got {page.get('ocr_confidence')}"
            )

    @pytest.mark.parametrize("fixture_name", NATIVE_PDF_FIXTURES)
    def test_native_pdf_is_not_near_empty(self, tmp_path, fixture_name):
        """Native-text PDFs with real content should not be flagged as near-empty."""
        src_dir = tmp_path / "src"
        src_dir.mkdir()
        shutil.copy2(FIXTURE_CORPUS / fixture_name, src_dir / fixture_name)

        _, parse_batch, _ = _run_pipeline_over_corpus(tmp_path, source_dir=src_dir)
        pr = parse_batch["results"][0]
        assert not pr["quality"]["is_near_empty"], f"{fixture_name}: should not be near_empty"

    @pytest.mark.parametrize("fixture_name", NATIVE_PDF_FIXTURES)
    def test_native_pdf_produces_segments(self, tmp_path, fixture_name):
        """Native-text PDFs should produce at least one segment."""
        src_dir = tmp_path / "src"
        src_dir.mkdir()
        shutil.copy2(FIXTURE_CORPUS / fixture_name, src_dir / fixture_name)

        _, _, seg_batch = _run_pipeline_over_corpus(tmp_path, source_dir=src_dir)
        ss = seg_batch["segment_sets"][0]
        assert len(ss.get("segments", [])) >= 1, f"{fixture_name}: expected at least one segment"

    @pytest.mark.parametrize("fixture_name", NATIVE_PDF_FIXTURES)
    def test_parser_name_is_pypdf(self, tmp_path, fixture_name):
        """Parser reference must identify pypdf."""
        src_dir = tmp_path / "src"
        src_dir.mkdir()
        shutil.copy2(FIXTURE_CORPUS / fixture_name, src_dir / fixture_name)

        _, parse_batch, _ = _run_pipeline_over_corpus(tmp_path, source_dir=src_dir)
        pr = parse_batch["results"][0]
        assert pr["parser"]["name"] == "pypdf", (
            f"Expected parser=pypdf, got {pr['parser']['name']!r}"
        )


# ---------------------------------------------------------------------------
# Frozen-artifact reuse
# ---------------------------------------------------------------------------


class TestFrozenArtifactReuse:
    """Second run with same (document_id, content_hash, config_version) reuses cache."""

    def test_reuse_loads_from_cache_not_recomputed(self, tmp_path):
        """Run decompose twice; second run must reuse the frozen artifact."""
        # Small corpus: just one PDF
        src_dir = tmp_path / "src"
        src_dir.mkdir()
        shutil.copy2(FIXTURE_CORPUS / "clean_native.pdf", src_dir / "clean_native.pdf")

        artifacts_root = tmp_path / "artifacts"

        # Run 1 — computes and caches
        run_pipeline(
            source_dir=src_dir,
            artifacts_root=artifacts_root,
            run_id="reuse-run-1",
            workspace_id="ws-reuse",
            kb_id="kb-reuse",
        )

        # Find the frozen artifact file
        from finecorpus.pipeline.decompose.stage import _frozen_artifact_path  # noqa: PLC0415

        store1 = ArtifactStore(artifacts_root=artifacts_root, run_id="reuse-run-1")
        seg_batch1 = store1.load("decompose")
        ss1 = seg_batch1["segment_sets"][0]
        doc_id = ss1["document_id"]
        content_hash = ss1["content_hash"]

        frozen_path = _frozen_artifact_path(artifacts_root, doc_id, content_hash, _CONFIG_VERSION)
        assert frozen_path.exists(), f"Frozen artifact not found at {frozen_path}"

        # Record mtime of frozen artifact
        mtime_after_run1 = frozen_path.stat().st_mtime

        # Run 2 — should reuse cache, not rewrite
        run_pipeline(
            source_dir=src_dir,
            artifacts_root=artifacts_root,
            run_id="reuse-run-2",
            workspace_id="ws-reuse",
            kb_id="kb-reuse",
        )

        mtime_after_run2 = frozen_path.stat().st_mtime
        assert mtime_after_run2 == mtime_after_run1, (
            "Frozen artifact was modified on second run (recomputed instead of reused). "
            f"mtime run1={mtime_after_run1}, mtime run2={mtime_after_run2}"
        )

    def test_reuse_segment_set_is_identical(self, tmp_path):
        """Two runs with same key must produce identical SegmentSet JSON."""
        src_dir = tmp_path / "src"
        src_dir.mkdir()
        shutil.copy2(FIXTURE_CORPUS / "clean_native.pdf", src_dir / "clean_native.pdf")

        artifacts_root = tmp_path / "artifacts"

        run_pipeline(
            source_dir=src_dir,
            artifacts_root=artifacts_root,
            run_id="determ-run-1",
            workspace_id="ws-det",
            kb_id="kb-det",
        )
        run_pipeline(
            source_dir=src_dir,
            artifacts_root=artifacts_root,
            run_id="determ-run-2",
            workspace_id="ws-det",
            kb_id="kb-det",
        )

        store1 = ArtifactStore(artifacts_root=artifacts_root, run_id="determ-run-1")
        store2 = ArtifactStore(artifacts_root=artifacts_root, run_id="determ-run-2")

        ss1 = store1.load("decompose")["segment_sets"][0]
        ss2 = store2.load("decompose")["segment_sets"][0]

        # Remove decomposed_at (timestamp differs between runs)
        for ss in (ss1, ss2):
            ss.pop("decomposed_at", None)

        assert ss1 == ss2, (
            "SegmentSet differs between two runs with the same key. "
            "Frozen-artifact reuse must guarantee identical output."
        )


# ---------------------------------------------------------------------------
# Determinism (independent runs, same content → same output)
# ---------------------------------------------------------------------------


class TestDeterminism:
    """Two independent runs with the same inputs produce identical segment sets."""

    def test_two_runs_same_key_identical_segments(self, tmp_path):
        """After deleting the frozen cache, a fresh run must produce the same segment set."""
        src_dir = tmp_path / "src"
        src_dir.mkdir()
        shutil.copy2(FIXTURE_CORPUS / "policy_v3.pdf", src_dir / "policy_v3.pdf")

        artifacts1 = tmp_path / "run1"
        artifacts2 = tmp_path / "run2"

        run_pipeline(
            source_dir=src_dir,
            artifacts_root=artifacts1,
            run_id="det-a",
            workspace_id="ws-det2",
            kb_id="kb-det2",
        )
        run_pipeline(
            source_dir=src_dir,
            artifacts_root=artifacts2,
            run_id="det-b",
            workspace_id="ws-det2",
            kb_id="kb-det2",
        )

        store1 = ArtifactStore(artifacts_root=artifacts1, run_id="det-a")
        store2 = ArtifactStore(artifacts_root=artifacts2, run_id="det-b")

        ss1 = store1.load("decompose")["segment_sets"][0]
        ss2 = store2.load("decompose")["segment_sets"][0]

        # Strip timestamps
        for ss in (ss1, ss2):
            ss.pop("decomposed_at", None)

        assert ss1 == ss2, (
            "Two independent runs with the same document produced different segment sets. "
            "Decompose must be deterministic for native-text PDFs (no LLM calls)."
        )


# ---------------------------------------------------------------------------
# D-26 version-check enforcement
# ---------------------------------------------------------------------------


class TestD26VersionCheckEnforcement:
    """D-26: both batch envelopes must be version-checked by consuming stages."""

    def test_decompose_rejects_wrong_parse_result_batch_version(self, tmp_path):
        """DecomposeStage.run() must reject a ParseResultBatch with an unsupported version."""
        store = ArtifactStore(artifacts_root=tmp_path, run_id="d26-test")
        decompose = DecomposeStage(
            run_started_at=datetime(2026, 9, 1, tzinfo=UTC),
            artifacts_root=tmp_path,
        )
        # Write a fake ParseResultBatch with wrong major version
        # Save it to the store so the stage can load it

        bad_data = {
            "schema_version": "2.0.0",  # unsupported major
            "contract": "parse_result_batch",
            "results": [],
        }
        # run() checks schema_version before calling _produce
        with pytest.raises(ContractVersionError):
            decompose.run(input_data=bad_data, store=store)

    def test_plan_rejects_wrong_segment_set_batch_version(self, tmp_path):
        """PlanStage must reject a SegmentSetBatch with an unsupported version."""
        from finecorpus.contracts.versions import ContractVersionError  # noqa: PLC0415
        from finecorpus.pipeline.plan.stage import PlanStage  # noqa: PLC0415

        store = ArtifactStore(artifacts_root=tmp_path, run_id="d26-plan-test")
        plan = PlanStage(run_started_at=datetime(2026, 9, 1, tzinfo=UTC))
        bad_batch = {
            "schema_version": "3.0.0",  # unsupported major
            "contract": "segment_set_batch",
            "segment_sets": [],
        }
        with pytest.raises(ContractVersionError):
            plan.run(input_data=bad_batch, store=store)

    def test_parse_result_batch_is_official_contract(self):
        """ParseResultBatch must be importable from finecorpus.contracts."""
        from finecorpus.contracts import (
            SUPPORTED_PARSE_RESULT_BATCH,  # noqa: PLC0415
            ParseResultBatch,  # noqa: PLC0415
        )

        assert ParseResultBatch is not None
        assert SUPPORTED_PARSE_RESULT_BATCH is not None

    def test_segment_set_batch_is_official_contract(self):
        """SegmentSetBatch must be importable from finecorpus.contracts."""
        from finecorpus.contracts import (
            SUPPORTED_SEGMENT_SET_BATCH,  # noqa: PLC0415
            SegmentSetBatch,  # noqa: PLC0415
        )

        assert SegmentSetBatch is not None
        assert SUPPORTED_SEGMENT_SET_BATCH is not None
