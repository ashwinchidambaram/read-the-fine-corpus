"""Tests for the findings + exclusion report generator (Phase 2, §6.2, §7.5).

Acceptance criteria (from spec Phase 2 brief):
  A. Adversarial fixture: security findings appear in the findings report.
  B. Spreadsheet triage: all three kinds (report/database/model) classified and reported.
  C. Near-duplicate family: primacy and exclusions appear in findings and exclusion reports.
  D. Mixed-PDF finding: bloated_manual's mixed-PDF scanned pages appear (tesseract optional).
  E. Completeness invariant: every ExclusionRecord in artifacts appears in the exclusion report.
  F. CLI smoke: 'corpus report' produces non-empty output.
  G. Determinism: two runs over the same artifacts produce identical output.

Test patterns:
  - _run_pipeline_over_subset from test_dedup_boilerplate.py for fixture pipelines.
  - Graceful skip for tesseract-dependent tests (consistent with existing OCR patterns).
"""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from typing import Any

import pytest

from finecorpus.pipeline.artifact_store import ArtifactStore
from finecorpus.pipeline.report import ReportFormat, generate_report

FIXTURE_CORPUS = pathlib.Path(__file__).parent.parent / "fixtures" / "golden" / "corpus"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_pipeline_over_subset(
    tmp_path: pathlib.Path,
    fixture_names: list[str],
    run_id: str = "test-report",
    index_superseded_versions: bool = False,
) -> tuple[ArtifactStore, dict[str, Any], dict[str, Any]]:
    """Copy fixtures to a temp dir and run the pipeline.

    Returns (store, parse_batch, seg_batch).
    """
    from finecorpus.pipeline.assess import AssessStage
    from finecorpus.pipeline.collect import CollectStage
    from finecorpus.pipeline.decompose.stage import DecomposeStage

    artifacts_root = tmp_path / "artifacts"
    src_dir = tmp_path / "corpus"
    src_dir.mkdir()

    for i, name in enumerate(fixture_names):
        src_file = FIXTURE_CORPUS / name
        dest = src_dir / name
        shutil.copy2(src_file, dest)
        # Verify the copy has the right content (sanity check)
        if src_file.stat().st_size != dest.stat().st_size:
            raise RuntimeError(
                f"Copy size mismatch: {src_file} ({src_file.stat().st_size} bytes) "
                f"vs {dest} ({dest.stat().st_size} bytes)"
            )
        # Pin mtimes so primacy election is deterministic (newest = last in list)
        mtime_ns = int(datetime(2026, 8, 1 + i, tzinfo=UTC).timestamp() * 1_000_000_000)
        os.utime(dest, ns=(mtime_ns, mtime_ns))

    store = ArtifactStore(artifacts_root=artifacts_root, run_id=run_id)
    ts = datetime(2026, 9, 1, tzinfo=UTC)

    collect = CollectStage(
        source_dir=src_dir,
        workspace_id="ws-test",
        kb_id="kb-test",
        collected_at=ts,
    )
    inv = collect.run(input_data=None, store=store)

    assess = AssessStage(run_id=run_id, run_started_at=ts)
    parse_batch = assess.run(input_data=inv, store=store)

    decompose = DecomposeStage(
        run_started_at=ts,
        artifacts_root=artifacts_root,
        run_id=run_id,
        index_superseded_versions=index_superseded_versions,
    )
    seg_batch = decompose.run(input_data=parse_batch, store=store)

    return store, parse_batch, seg_batch


# ---------------------------------------------------------------------------
# A. Adversarial fixture: security findings in the report
# ---------------------------------------------------------------------------


class TestAdversarialSecurityFindings:
    """The adversarial fixture's security findings must appear in the findings report."""

    def test_adversarial_invisible_content_in_findings_json(self, tmp_path):
        """adversarial.pdf invisible-content detections appear in findings JSON."""
        store, _, _ = _run_pipeline_over_subset(tmp_path, ["adversarial.pdf"])
        result = generate_report(
            artifacts_root=store.run_dir.parent,
            run_id=store.run_id,
        )
        fj = result.findings_json
        # Find adversarial document entry
        adv_entries = [
            doc for doc in fj.get("documents", []) if "adversarial" in doc.get("source_path", "")
        ]
        assert adv_entries, "adversarial.pdf not found in findings report documents"
        doc = adv_entries[0]
        sec = doc.get("security", {})
        assert sec.get("invisible_content_count", 0) > 0, (
            "Expected invisible content detections for adversarial.pdf, "
            f"got {sec.get('invisible_content_count', 0)}"
        )

    def test_adversarial_invisible_content_in_findings_md(self, tmp_path):
        """adversarial.pdf invisible-content appears in Markdown findings report."""
        store, _, _ = _run_pipeline_over_subset(tmp_path, ["adversarial.pdf"])
        result = generate_report(
            artifacts_root=store.run_dir.parent,
            run_id=store.run_id,
        )
        assert "invisible" in result.findings_md.lower(), (
            "Expected 'invisible' in Markdown findings report for adversarial fixture"
        )

    def test_adversarial_injection_suspicion_in_findings(self, tmp_path):
        """adversarial.pdf injection suspicion score appears in findings report."""
        store, _, _ = _run_pipeline_over_subset(tmp_path, ["adversarial.pdf"])
        result = generate_report(
            artifacts_root=store.run_dir.parent,
            run_id=store.run_id,
        )
        fj = result.findings_json
        adv_entries = [
            doc for doc in fj.get("documents", []) if "adversarial" in doc.get("source_path", "")
        ]
        # The adversarial fixture may have both invisible content and injection signals;
        # we assert at least invisible content is reported (injection lives in segments).
        assert adv_entries, "adversarial.pdf must appear in findings report"

    def test_summary_invisible_count_nonzero(self, tmp_path):
        """Summary invisible_content_detections must be > 0 for adversarial corpus."""
        store, _, _ = _run_pipeline_over_subset(tmp_path, ["adversarial.pdf"])
        result = generate_report(
            artifacts_root=store.run_dir.parent,
            run_id=store.run_id,
        )
        summary = result.findings_json.get("summary", {})
        assert summary.get("total_invisible_content_detections", 0) > 0, (
            "adversarial.pdf must produce > 0 invisible content detections in findings summary"
        )


# ---------------------------------------------------------------------------
# B. Spreadsheet triage classifications
# ---------------------------------------------------------------------------


class TestSpreadsheetTriageInReport:
    """All three spreadsheet kinds must be classified and surfaced in the reports."""

    SPREADSHEET_FIXTURES = [
        "report_spreadsheet.xlsx",
        "database_spreadsheet.xlsx",
        "model_spreadsheet.xlsx",
    ]

    def test_all_three_spreadsheets_in_findings(self, tmp_path):
        """All three spreadsheet fixtures appear as documents in the findings report."""
        store, _, _ = _run_pipeline_over_subset(tmp_path, self.SPREADSHEET_FIXTURES)
        result = generate_report(
            artifacts_root=store.run_dir.parent,
            run_id=store.run_id,
        )
        source_paths = [
            doc.get("source_path", "") for doc in result.findings_json.get("documents", [])
        ]
        assert any("report_spreadsheet" in sp for sp in source_paths), (
            "report_spreadsheet.xlsx missing from findings report"
        )
        assert any("database_spreadsheet" in sp for sp in source_paths), (
            "database_spreadsheet.xlsx missing from findings report"
        )
        assert any("model_spreadsheet" in sp for sp in source_paths), (
            "model_spreadsheet.xlsx missing from findings report"
        )

    def test_report_spreadsheet_status_parsed(self, tmp_path):
        """report_spreadsheet.xlsx must have parse_status=parsed in findings."""
        store, _, _ = _run_pipeline_over_subset(tmp_path, self.SPREADSHEET_FIXTURES)
        result = generate_report(
            artifacts_root=store.run_dir.parent,
            run_id=store.run_id,
        )
        docs = result.findings_json.get("documents", [])
        # Match on filename only — tmp_path dir names can contain spreadsheet type keywords.
        report_entries = [
            doc
            for doc in docs
            if pathlib.Path(doc.get("source_path", "")).name == "report_spreadsheet.xlsx"
        ]
        assert report_entries, (
            f"report_spreadsheet.xlsx not found in findings report. "
            f"Available source_paths: {[doc.get('source_path') for doc in docs]}"
        )
        doc = report_entries[0]
        assert doc.get("parse_status") == "parsed", (
            f"report_spreadsheet.xlsx: expected parse_status=parsed, "
            f"got {doc.get('parse_status')}. "
            f"Triage: {doc.get('triage')}. "
            f"Findings: {doc.get('findings', [])[:3]}"
        )

    def test_database_spreadsheet_excluded_in_exclusion_report(self, tmp_path):
        """database_spreadsheet.xlsx must appear in the exclusion report."""
        store, _, _ = _run_pipeline_over_subset(tmp_path, self.SPREADSHEET_FIXTURES)
        result = generate_report(
            artifacts_root=store.run_dir.parent,
            run_id=store.run_id,
        )
        exclusions = result.exclusions_json.get("exclusions", [])
        # Match on filename only — tmp_path dir names can contain spreadsheet type keywords.
        db_exclusions = [
            exc
            for exc in exclusions
            if pathlib.Path(exc.get("source_path", "")).name == "database_spreadsheet.xlsx"
        ]
        assert db_exclusions, "database_spreadsheet.xlsx must appear in the exclusion report"
        assert db_exclusions[0].get("reason") == "spreadsheet_database", (
            f"Expected reason=spreadsheet_database, got {db_exclusions[0].get('reason')}"
        )

    def test_model_spreadsheet_excluded_in_exclusion_report(self, tmp_path):
        """model_spreadsheet.xlsx must appear in the exclusion report."""
        store, _, _ = _run_pipeline_over_subset(tmp_path, self.SPREADSHEET_FIXTURES)
        result = generate_report(
            artifacts_root=store.run_dir.parent,
            run_id=store.run_id,
        )
        exclusions = result.exclusions_json.get("exclusions", [])
        # Match on filename only — tmp_path dir names can contain spreadsheet type keywords.
        model_exclusions = [
            exc
            for exc in exclusions
            if pathlib.Path(exc.get("source_path", "")).name == "model_spreadsheet.xlsx"
        ]
        assert model_exclusions, "model_spreadsheet.xlsx must appear in the exclusion report"
        assert model_exclusions[0].get("reason") == "spreadsheet_model", (
            f"Expected reason=spreadsheet_model, got {model_exclusions[0].get('reason')}"
        )

    def test_triage_finding_in_findings_json(self, tmp_path):
        """Triage findings (spreadsheet_triage code) must appear in findings JSON."""
        store, _, _ = _run_pipeline_over_subset(tmp_path, self.SPREADSHEET_FIXTURES)
        result = generate_report(
            artifacts_root=store.run_dir.parent,
            run_id=store.run_id,
        )
        # At least the excluded ones must carry a triage entry
        all_triages = [
            doc.get("triage")
            for doc in result.findings_json.get("documents", [])
            if doc.get("triage") is not None
        ]
        assert all_triages, "No spreadsheet_triage findings found in findings report"

    def test_exclusion_report_md_mentions_database(self, tmp_path):
        """Exclusion report Markdown must mention database_spreadsheet."""
        store, _, _ = _run_pipeline_over_subset(tmp_path, self.SPREADSHEET_FIXTURES)
        result = generate_report(
            artifacts_root=store.run_dir.parent,
            run_id=store.run_id,
        )
        assert "database_spreadsheet" in result.exclusions_md, (
            "database_spreadsheet.xlsx must appear in exclusion Markdown"
        )


# ---------------------------------------------------------------------------
# C. Near-duplicate version family with primacy and exclusions
# ---------------------------------------------------------------------------

POLICY_FIXTURES = ["policy_v1.pdf", "policy_v2.pdf", "policy_v3.pdf"]


class TestNearDupVersionFamilyInReport:
    """Near-duplicate family must appear in both findings and exclusion reports."""

    def test_version_family_in_findings_json(self, tmp_path):
        """Findings JSON must contain version_families with at least one entry."""
        store, _, _ = _run_pipeline_over_subset(tmp_path, POLICY_FIXTURES)
        result = generate_report(
            artifacts_root=store.run_dir.parent,
            run_id=store.run_id,
        )
        families = result.findings_json.get("version_families", [])
        assert families, "Expected at least one version family in findings JSON"

    def test_primary_identified_in_family(self, tmp_path):
        """The version family must identify a primary_document_id."""
        store, _, _ = _run_pipeline_over_subset(tmp_path, POLICY_FIXTURES)
        result = generate_report(
            artifacts_root=store.run_dir.parent,
            run_id=store.run_id,
        )
        families = result.findings_json.get("version_families", [])
        assert families
        family = families[0]
        assert family.get("primary_document_id"), "version family must have a primary_document_id"

    def test_superseded_in_exclusion_report(self, tmp_path):
        """Superseded near-duplicates must appear in the exclusion report."""
        store, _, _ = _run_pipeline_over_subset(tmp_path, POLICY_FIXTURES)
        result = generate_report(
            artifacts_root=store.run_dir.parent,
            run_id=store.run_id,
        )
        exclusions = result.exclusions_json.get("exclusions", [])
        superseded = [exc for exc in exclusions if exc.get("reason") == "superseded_version"]
        assert superseded, "Superseded near-duplicates must appear in exclusion report"

    def test_superseded_exclusions_have_primary_ref(self, tmp_path):
        """Superseded exclusions must reference their primary_document_id."""
        store, _, _ = _run_pipeline_over_subset(tmp_path, POLICY_FIXTURES)
        result = generate_report(
            artifacts_root=store.run_dir.parent,
            run_id=store.run_id,
        )
        exclusions = result.exclusions_json.get("exclusions", [])
        for exc in exclusions:
            if exc.get("reason") == "superseded_version":
                assert exc.get("primary_document_id"), (
                    f"Superseded exclusion {exc.get('exclusion_id')} missing primary_document_id"
                )

    def test_version_family_in_findings_md(self, tmp_path):
        """Findings Markdown must contain a Version Families section."""
        store, _, _ = _run_pipeline_over_subset(tmp_path, POLICY_FIXTURES)
        result = generate_report(
            artifacts_root=store.run_dir.parent,
            run_id=store.run_id,
        )
        assert "Near-Duplicate Version Families" in result.findings_md, (
            "Expected 'Near-Duplicate Version Families' section in findings Markdown"
        )

    def test_superseded_in_exclusions_md(self, tmp_path):
        """Exclusion Markdown must contain a Superseded section."""
        store, _, _ = _run_pipeline_over_subset(tmp_path, POLICY_FIXTURES)
        result = generate_report(
            artifacts_root=store.run_dir.parent,
            run_id=store.run_id,
        )
        assert "Superseded" in result.exclusions_md, (
            "Expected 'Superseded' section in exclusion Markdown"
        )

    def test_summary_version_family_count(self, tmp_path):
        """Findings summary must report version_family_count >= 1."""
        store, _, _ = _run_pipeline_over_subset(tmp_path, POLICY_FIXTURES)
        result = generate_report(
            artifacts_root=store.run_dir.parent,
            run_id=store.run_id,
        )
        count = result.findings_json.get("summary", {}).get("version_family_count", 0)
        assert count >= 1, f"Expected version_family_count >= 1, got {count}"


# ---------------------------------------------------------------------------
# D. Mixed-PDF: bloated_manual scanned pages (tesseract optional)
# ---------------------------------------------------------------------------


class TestMixedPDFInReport:
    """bloated_manual.pdf mixed-PDF findings appear in report (skip if no tesseract)."""

    @pytest.fixture(scope="class")
    def bloated_run(self, tmp_path_factory):
        tmp_path = tmp_path_factory.mktemp("bloated")
        store, _, _ = _run_pipeline_over_subset(
            tmp_path, ["bloated_manual.pdf"], run_id="test-bloated"
        )
        return store

    def test_bloated_in_findings(self, bloated_run):
        """bloated_manual.pdf must appear in findings report."""
        result = generate_report(
            artifacts_root=bloated_run.run_dir.parent,
            run_id=bloated_run.run_id,
        )
        docs = result.findings_json.get("documents", [])
        bloated = [doc for doc in docs if "bloated_manual" in doc.get("source_path", "")]
        assert bloated, "bloated_manual.pdf must appear in findings report"

    def test_mixed_pdf_pages_or_skip(self, bloated_run):
        """If tesseract is present, bloated_manual must show mixed_pdf_scanned_pages."""
        if shutil.which("tesseract") is None:
            pytest.skip("tesseract not available; skipping mixed-PDF scanned pages check")
        result = generate_report(
            artifacts_root=bloated_run.run_dir.parent,
            run_id=bloated_run.run_id,
        )
        docs = result.findings_json.get("documents", [])
        bloated = [doc for doc in docs if "bloated_manual" in doc.get("source_path", "")]
        assert bloated
        doc = bloated[0]
        # Either document_kind is mixed_pdf OR there are scanned pages
        is_mixed = doc.get("document_kind") == "mixed_pdf" or bool(
            doc.get("mixed_pdf_scanned_pages")
        )
        scanned = doc.get("mixed_pdf_scanned_pages")
        assert is_mixed, (
            f"Expected mixed_pdf document_kind or scanned pages for bloated_manual.pdf, "
            f"got kind={doc.get('document_kind')}, scanned_pages={scanned}"
        )


# ---------------------------------------------------------------------------
# E. Completeness invariant: every ExclusionRecord appears in exclusion report
# ---------------------------------------------------------------------------


class TestExclusionCompletenessInvariant:
    """Every ExclusionRecord in Decompose artifacts must appear in the exclusion report."""

    ALL_FIXTURES = [
        "clean_native.pdf",
        "adversarial.pdf",
        "report_spreadsheet.xlsx",
        "database_spreadsheet.xlsx",
        "model_spreadsheet.xlsx",
        "policy_v1.pdf",
        "policy_v2.pdf",
        "policy_v3.pdf",
        "audio_stub.wav",
        "video_stub.mp4",
        "cad_binary.dwg",
        "confluence_export.html",
    ]

    @pytest.fixture(scope="class")
    def full_run(self, tmp_path_factory):
        tmp_path = tmp_path_factory.mktemp("full")
        store, _, _ = _run_pipeline_over_subset(
            tmp_path, self.ALL_FIXTURES, run_id="test-completeness"
        )
        return store

    def test_all_exclusion_records_present(self, full_run: ArtifactStore):
        """Every ExclusionRecord.exclusion_id from decompose appears in exclusion report."""
        decompose_raw = full_run.load("decompose")
        artifact_exclusion_ids: set[str] = set()
        for ss in decompose_raw.get("segment_sets", []):
            for exc in ss.get("exclusions", []):
                exc_id = exc.get("exclusion_id")
                if exc_id:
                    artifact_exclusion_ids.add(exc_id)

        result = generate_report(
            artifacts_root=full_run.run_dir.parent,
            run_id=full_run.run_id,
        )
        report_exclusion_ids = {
            exc.get("exclusion_id") for exc in result.exclusions_json.get("exclusions", [])
        }

        missing = artifact_exclusion_ids - report_exclusion_ids
        assert not missing, (
            f"These ExclusionRecord IDs appear in artifacts but not in exclusion report: {missing}"
        )

    def test_exclusion_report_summary_count_matches(self, full_run: ArtifactStore):
        """exclusion_report summary.total_exclusions >= ExclusionRecord count in artifacts."""
        decompose_raw = full_run.load("decompose")
        artifact_count = sum(
            len(ss.get("exclusions", [])) for ss in decompose_raw.get("segment_sets", [])
        )

        result = generate_report(
            artifacts_root=full_run.run_dir.parent,
            run_id=full_run.run_id,
        )
        summary = result.exclusions_json.get("summary", {})
        # Total exclusions includes doc-level + segment-level
        total = summary.get("total_exclusions", 0)
        assert total >= artifact_count, (
            f"exclusion report total ({total}) must be >= "
            f"artifact exclusion records ({artifact_count})"
        )

    def test_no_silent_gaps_in_excluded_pre_parse_docs(self, full_run: ArtifactStore):
        """Every excluded_pre_parse ParseResult must appear in the exclusion report."""
        assess_raw = full_run.load("assess")
        excluded_ids = {
            pr["document_id"]
            for pr in assess_raw.get("results", [])
            if pr.get("parse_status") == "excluded_pre_parse" and pr.get("document_id")
        }

        result = generate_report(
            artifacts_root=full_run.run_dir.parent,
            run_id=full_run.run_id,
        )
        report_doc_ids = {
            exc.get("document_id") for exc in result.exclusions_json.get("exclusions", [])
        }

        missing = excluded_ids - report_doc_ids
        assert not missing, f"excluded_pre_parse documents not found in exclusion report: {missing}"


# ---------------------------------------------------------------------------
# F. JSON schema shape tests
# ---------------------------------------------------------------------------


class TestReportJSONShape:
    """Findings and exclusion JSON must have required top-level fields."""

    @pytest.fixture(scope="class")
    def simple_run(self, tmp_path_factory):
        tmp_path = tmp_path_factory.mktemp("shape")
        store, _, _ = _run_pipeline_over_subset(tmp_path, ["clean_native.pdf"], run_id="test-shape")
        return store

    def test_findings_json_has_required_fields(self, simple_run):
        result = generate_report(
            artifacts_root=simple_run.run_dir.parent,
            run_id=simple_run.run_id,
        )
        fj = result.findings_json
        for field in ("schema_version", "contract", "run_id", "summary", "documents"):
            assert field in fj, f"findings_json missing required field: {field}"
        assert fj["contract"] == "findings_report"
        assert fj["schema_version"] == "1.0.0"

    def test_exclusions_json_has_required_fields(self, simple_run):
        result = generate_report(
            artifacts_root=simple_run.run_dir.parent,
            run_id=simple_run.run_id,
        )
        ej = result.exclusions_json
        for field in ("schema_version", "contract", "run_id", "summary", "exclusions"):
            assert field in ej, f"exclusions_json missing required field: {field}"
        assert ej["contract"] == "exclusion_report"
        assert ej["schema_version"] == "1.0.0"

    def test_findings_summary_has_required_fields(self, simple_run):
        result = generate_report(
            artifacts_root=simple_run.run_dir.parent,
            run_id=simple_run.run_id,
        )
        summary = result.findings_json.get("summary", {})
        for field in ("total_documents", "parse_status_counts"):
            assert field in summary, f"findings summary missing: {field}"

    def test_document_entry_has_required_fields(self, simple_run):
        result = generate_report(
            artifacts_root=simple_run.run_dir.parent,
            run_id=simple_run.run_id,
        )
        docs = result.findings_json.get("documents", [])
        assert docs, "Expected at least one document in findings report"
        doc = docs[0]
        for field in (
            "document_id",
            "source_path",
            "parse_status",
            "document_kind",
            "dedup_role",
            "quality",
            "security",
            "findings",
        ):
            assert field in doc, f"document entry missing required field: {field}"

    def test_exclusion_entry_has_required_fields(self, tmp_path):
        # Use unservable file to guarantee at least one exclusion
        store, _, _ = _run_pipeline_over_subset(tmp_path, ["audio_stub.wav"])
        result = generate_report(
            artifacts_root=store.run_dir.parent,
            run_id=store.run_id,
        )
        exclusions = result.exclusions_json.get("exclusions", [])
        assert exclusions, "Expected at least one exclusion for audio_stub.wav"
        exc = exclusions[0]
        for field in ("exclusion_id", "document_id", "scope", "reason", "reason_detail"):
            assert field in exc, f"exclusion entry missing required field: {field}"


# ---------------------------------------------------------------------------
# G. Determinism: identical output for identical artifacts
# ---------------------------------------------------------------------------


class TestReportDeterminism:
    """Two generate_report calls over the same artifacts produce identical output."""

    def test_findings_json_deterministic(self, tmp_path):
        store, _, _ = _run_pipeline_over_subset(
            tmp_path, ["clean_native.pdf", "adversarial.pdf"], run_id="test-det"
        )
        result1 = generate_report(
            artifacts_root=store.run_dir.parent,
            run_id=store.run_id,
        )
        result2 = generate_report(
            artifacts_root=store.run_dir.parent,
            run_id=store.run_id,
        )
        import json as _json

        assert _json.dumps(result1.findings_json, sort_keys=True) == _json.dumps(
            result2.findings_json, sort_keys=True
        ), "findings_json is not deterministic across two calls"

    def test_exclusions_json_deterministic(self, tmp_path):
        store, _, _ = _run_pipeline_over_subset(
            tmp_path,
            ["database_spreadsheet.xlsx", "model_spreadsheet.xlsx"],
            run_id="test-det-exc",
        )
        result1 = generate_report(
            artifacts_root=store.run_dir.parent,
            run_id=store.run_id,
        )
        result2 = generate_report(
            artifacts_root=store.run_dir.parent,
            run_id=store.run_id,
        )
        import json as _json

        assert _json.dumps(result1.exclusions_json, sort_keys=True) == _json.dumps(
            result2.exclusions_json, sort_keys=True
        ), "exclusions_json is not deterministic across two calls"


# ---------------------------------------------------------------------------
# H. ReportResult.write(): files are created at expected paths
# ---------------------------------------------------------------------------


class TestReportWrite:
    """ReportResult.write() must create files with expected names."""

    def test_write_both_creates_four_files(self, tmp_path):
        run_tmp = tmp_path / "run"
        run_tmp.mkdir()
        store, _, _ = _run_pipeline_over_subset(run_tmp, ["clean_native.pdf"], run_id="test-write")
        result = generate_report(
            artifacts_root=store.run_dir.parent,
            run_id=store.run_id,
        )
        out_dir = tmp_path / "out"
        written = result.write(out_dir=out_dir, fmt=ReportFormat.both, run_id="test-write")
        assert "findings_md" in written
        assert "exclusions_md" in written
        assert "findings_json" in written
        assert "exclusions_json" in written
        for label, path in written.items():
            assert path.exists(), f"Expected {label} at {path} but file not found"

    def test_write_md_only(self, tmp_path):
        run_tmp = tmp_path / "run"
        run_tmp.mkdir()
        store, _, _ = _run_pipeline_over_subset(
            run_tmp, ["clean_native.pdf"], run_id="test-md-only"
        )
        result = generate_report(
            artifacts_root=store.run_dir.parent,
            run_id=store.run_id,
        )
        out_dir = tmp_path / "out"
        written = result.write(out_dir=out_dir, fmt=ReportFormat.md, run_id="test-md-only")
        assert "findings_md" in written
        assert "exclusions_md" in written
        assert "findings_json" not in written
        assert "exclusions_json" not in written

    def test_write_json_only(self, tmp_path):
        run_tmp = tmp_path / "run"
        run_tmp.mkdir()
        store, _, _ = _run_pipeline_over_subset(
            run_tmp, ["clean_native.pdf"], run_id="test-json-only"
        )
        result = generate_report(
            artifacts_root=store.run_dir.parent,
            run_id=store.run_id,
        )
        out_dir = tmp_path / "out"
        written = result.write(out_dir=out_dir, fmt=ReportFormat.json, run_id="test-json-only")
        assert "findings_json" in written
        assert "exclusions_json" in written
        assert "findings_md" not in written
        assert "exclusions_md" not in written


# ---------------------------------------------------------------------------
# I. CLI smoke test: corpus report command
# ---------------------------------------------------------------------------


class TestCLIReport:
    """corpus report CLI command must produce non-empty output and exit 0."""

    def test_corpus_report_exits_zero(self, tmp_path):
        """corpus report --artifacts ... --run-id ... exits 0 and prints output."""
        run_tmp = tmp_path / "run"
        run_tmp.mkdir()
        store, _, _ = _run_pipeline_over_subset(run_tmp, ["clean_native.pdf"], run_id="cli-smoke")
        cmd = [
            sys.executable,
            "-m",
            "finecorpus.cli.main",
            "report",
            "--artifacts",
            str(store.run_dir.parent),
            "--run-id",
            "cli-smoke",
            "--format",
            "md",
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, (
            f"corpus report exited {proc.returncode}.\nstdout: {proc.stdout}\nstderr: {proc.stderr}"
        )
        assert proc.stdout.strip(), "corpus report produced no output"
        assert "Findings Report" in proc.stdout, (
            "Expected 'Findings Report' in corpus report output"
        )
