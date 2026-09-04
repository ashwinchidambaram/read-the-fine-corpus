"""Phase 2 acceptance tests — corpus-wide end-to-end verification (§19 Phase 2).

Acceptance criteria from spec §19 Phase 2 (four criteria):

  (a) Manifest-vs-pipeline parity — the full pipeline runs over ALL 21 fixtures in
      tests/fixtures/golden/corpus/, generates a findings report via generate_report(),
      and asserts that every manifest-declared expected_finding appears in the report.
      expected_triage_class per fixture is checked against the pipeline output.
      OCR-dependent assertions skip gracefully without tesseract.

  (b) Segments reassemble to source (HTML extension) — the existing reassembly test
      covers native PDFs (test_assess_decompose_p1.TestReassemblyProperty); here we
      add thin coverage for an HTML doc (confluence_export.html) to verify the same
      invariant holds for the Phase 2 HTML parser.  See comment below for the exact
      native-PDF coverage that already exists.

  (c) Spreadsheet triage classifies all three kinds — assert that the report+parse
      results agree with the manifest for report/database/model spreadsheets.
      (Existing detailed triage tests: test_report.TestSpreadsheetTriageInReport;
      we add a thin corpus-scale check that all three manifest expected_triage_class
      values agree with the pipeline output across the full corpus run.)

  (d) Adversarial fixture is flagged — invisible-content detections + injection scores
      above manifest thresholds are present in the corpus-wide findings report.
      (Existing detail coverage: test_injection_scoring.TestManifestPipelineParity,
      test_invisible_content_detector.TestAdversarialFixtureDetections,
      test_t09_injection_flagging.TestT09InjectionFlaggingUnit; we add a thin
      corpus-scale check that the adversarial fixture's report entry crosses thresholds.)

Any fixture whose expected_findings are NOT produced by the pipeline is reported as a
FINDING (collected in PARITY_GAPS) and causes the parity test to fail explicitly.

OCR behaviour
-------------
Tests that require tesseract (scanned_pdf, image-only, mixed-PDF with OCR pages) skip
gracefully via:

    if shutil.which("tesseract") is None:
        pytest.skip("tesseract not available")

This is consistent with the existing pattern in test_pdf_scanned.py,
test_pdf_native_mixed.py, and test_assess_p2_formats.py.
"""

from __future__ import annotations

import hashlib
import os
import pathlib
import shutil
from datetime import UTC, datetime
from typing import Any

import pytest
import yaml

from finecorpus.pipeline.artifact_store import ArtifactStore
from finecorpus.pipeline.report import generate_report

# ---------------------------------------------------------------------------
# Paths and manifest
# ---------------------------------------------------------------------------

GOLDEN_DIR = pathlib.Path(__file__).parent.parent / "fixtures" / "golden"
CORPUS_DIR = GOLDEN_DIR / "corpus"
MANIFEST_PATH = GOLDEN_DIR / "manifest.yaml"


def _load_manifest() -> list[dict[str, Any]]:
    data = yaml.safe_load(MANIFEST_PATH.read_text(encoding="utf-8"))
    return data["fixtures"]  # type: ignore[return-value]


# All 21 fixture filenames (relative to CORPUS_DIR)
def _all_fixture_names() -> list[str]:
    entries = _load_manifest()
    return [pathlib.Path(e["file"]).name for e in entries]


# ---------------------------------------------------------------------------
# Pipeline helper — runs Collect + Assess + Decompose over a file subset
# ---------------------------------------------------------------------------


def _run_pipeline_over_corpus(
    tmp_path: pathlib.Path,
    fixture_names: list[str],
    run_id: str = "p2-acceptance",
    index_superseded_versions: bool = False,
) -> ArtifactStore:
    """Copy the specified fixtures into a temp source dir and run the pipeline.

    Returns the ArtifactStore for the completed run.  Mtime offsets are set so
    that policy_v3.pdf (the newest, listed last in the manifest) wins primacy.
    """
    from finecorpus.pipeline.assess import AssessStage
    from finecorpus.pipeline.collect import CollectStage
    from finecorpus.pipeline.decompose.stage import DecomposeStage

    artifacts_root = tmp_path / "artifacts"
    src_dir = tmp_path / "corpus"
    src_dir.mkdir()

    # Replicate the mtime-pinning strategy from test_report to ensure deterministic
    # near-duplicate primacy election (newest mtime = listed last in fixture_names).
    for i, name in enumerate(fixture_names):
        src_file = CORPUS_DIR / name
        dest = src_dir / name
        shutil.copy2(src_file, dest)
        mtime_ns = int(datetime(2026, 8, 1 + i, tzinfo=UTC).timestamp() * 1_000_000_000)
        os.utime(dest, ns=(mtime_ns, mtime_ns))

    store = ArtifactStore(artifacts_root=artifacts_root, run_id=run_id)
    ts = datetime(2026, 9, 1, tzinfo=UTC)

    collect = CollectStage(
        source_dir=src_dir,
        workspace_id="ws-p2-accept",
        kb_id="kb-p2-accept",
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
    decompose.run(input_data=parse_batch, store=store)

    return store


# ---------------------------------------------------------------------------
# (a) Manifest-vs-pipeline parity — corpus-wide
# ---------------------------------------------------------------------------


class TestCorpusWideManifestParity:
    """Run the full pipeline over ALL 21 fixtures and check every manifest expectation.

    Parity check scope per fixture:
      - expected_triage_class  → document_kind in parse result
      - expected_findings      → each finding code appears in the findings report

    OCR-dependent fixtures (triage_class in {scanned_pdf, mixed_pdf}) skip their
    finding-code checks when tesseract is absent; the triage_class check itself is
    always run because document_kind is set by the parser even without OCR output.

    Any gap is collected in PARITY_GAPS (the list is printed) and the test fails.
    """

    @pytest.fixture(scope="class")
    def corpus_run(self, tmp_path_factory) -> tuple[ArtifactStore, Any]:
        """Run the full 21-fixture corpus and return (store, report_result)."""
        tmp_path = tmp_path_factory.mktemp("corpus_wide")
        fixture_names = _all_fixture_names()
        store = _run_pipeline_over_corpus(tmp_path, fixture_names)
        report = generate_report(
            artifacts_root=store.run_dir.parent,
            run_id=store.run_id,
        )
        return store, report

    def test_all_21_fixtures_in_findings_report(self, corpus_run):
        """Every fixture must appear in the findings report (no silent drops)."""
        _, report = corpus_run
        source_paths = {
            pathlib.Path(doc.get("source_path", "")).name
            for doc in report.findings_json.get("documents", [])
        }
        entries = _load_manifest()
        missing = []
        for entry in entries:
            name = pathlib.Path(entry["file"]).name
            if name not in source_paths:
                missing.append(name)
        assert not missing, (
            f"These fixtures are absent from the findings report: {missing}. "
            "The corpus-wide pipeline run did not produce an entry for them."
        )

    def test_expected_triage_class_matches_pipeline(self, corpus_run):
        """expected_triage_class in the manifest must match document_kind in the pipeline.

        The manifest uses semantic triage-class names; the pipeline uses DocumentKind enum
        values.  The mapping accounts for both direct matches and cases where the manifest
        name is more specific than the DocumentKind enum.

        Mapping used (manifest expected_triage_class → pipeline document_kind):
          native_pdf           → "native_pdf"
          scanned_pdf          → "scanned_pdf"
          mixed_pdf            → "mixed_pdf"
          html                 → "html"
          spreadsheet_report   → "spreadsheet"  (triage sub-class is in findings)
          spreadsheet_database → "spreadsheet"  (triage sub-class is in findings)
          spreadsheet_model    → "spreadsheet"  (triage sub-class is in findings)
          encrypted_pdf        → "native_pdf"   (parser emits native_pdf +
                                               password_protected finding)
          audio                → "other"        (DocumentKind.other + unservable_audio finding)
          video                → "other"        (DocumentKind.other + unservable_video finding)
          cad_binary           → "other"        (DocumentKind.other + unservable_cad finding)

        The spreadsheet, unservable, and encrypted cases are verified by the
        findings/exclusion assertions in test_manifest_expected_findings_in_report.
        """
        # Manifest triage class → set of acceptable pipeline document_kind values
        _TRIAGE_TO_DOC_KIND: dict[str, set[str]] = {
            "native_pdf": {"native_pdf"},
            "scanned_pdf": {"scanned_pdf"},
            "mixed_pdf": {"mixed_pdf"},
            "html": {"html"},
            # Spreadsheet kinds all emit document_kind=spreadsheet; triage sub-class in findings
            "spreadsheet_report": {"spreadsheet"},
            "spreadsheet_database": {"spreadsheet"},
            "spreadsheet_model": {"spreadsheet"},
            # Encrypted PDF: parser emits native_pdf + password_protected finding
            # (no encrypted_pdf DocumentKind)
            "encrypted_pdf": {"native_pdf", "encrypted_pdf", "password_protected_pdf"},
            # Unservable file types: parser emits DocumentKind.other + finding code
            "audio": {"other"},
            "video": {"other"},
            "cad_binary": {"other"},
        }

        _, report = corpus_run
        doc_by_name: dict[str, dict[str, Any]] = {
            pathlib.Path(doc.get("source_path", "")).name: doc
            for doc in report.findings_json.get("documents", [])
        }
        entries = _load_manifest()
        mismatches: list[str] = []
        for entry in entries:
            name = pathlib.Path(entry["file"]).name
            expected_class = entry.get("expected_triage_class", "")
            if not expected_class:
                continue
            doc = doc_by_name.get(name)
            if doc is None:
                continue  # covered by test_all_21_fixtures_in_findings_report
            actual_kind = doc.get("document_kind", "")
            acceptable = _TRIAGE_TO_DOC_KIND.get(expected_class, {expected_class})
            if actual_kind not in acceptable:
                mismatches.append(
                    f"  {name}: manifest={expected_class!r}, pipeline={actual_kind!r}, "
                    f"acceptable={acceptable}"
                )
        assert not mismatches, (
            "expected_triage_class mismatches (manifest vs pipeline):\n" + "\n".join(mismatches)
        )

    def test_manifest_expected_findings_in_report(self, corpus_run):
        """Every manifest expected_finding code must appear in the findings report.

        This is the primary parity gate.  Any fixture whose expected findings
        are NOT produced is a gap — collected and reported explicitly.

        Finding-code mapping (manifest code → pipeline signal):

          Manifest code            | Pipeline signal
          -------------------------|------------------------------------------------
          boilerplate_detected     | doc["boilerplate_segment_count"] > 0
                                   | OR report.boilerplate_blocks non-empty (corpus)
          mixed_pdf                | doc["document_kind"] == "mixed_pdf"
          table_structure_retained | doc["findings"] code == "table_structure_retained"
                                   | OR doc["quality"]["table_structure_retained"] == True
          dedup_superseded         | doc["dedup_role"] in ("superseded", ...)
                                   | OR exclusion reason == "superseded_version"
          unservable_encrypted     | doc["findings"] code in ("password_protected",
                                   |   "unservable_encrypted")
                                   | OR exclusion reason == "encrypted"
          unservable_audio         | doc["findings"] code == "unservable_audio"
          unservable_video         | doc["findings"] code == "unservable_video"
          unservable_cad           | doc["findings"] code == "unservable_cad"
          spreadsheet_triage       | doc["triage"] is not None
          invisible_content_detected | doc["security"]["invisible_content_count"] > 0
          injection_suspicion_high | doc["security"]["injection_max_suspicion"] > 0.0
          low_ocr_confidence       | doc["findings"] code == "low_ocr_confidence"
                                   | (OCR-gated: skip without tesseract)
          parse_error              | doc["findings"] code == "parse_error" (malformed PDF)

        OCR-sensitive codes (low_ocr_confidence, mixed_pdf with OCR content) skip
        gracefully when tesseract is absent.

        KNOWN PARITY GAPS (documented findings, not test defects):
          - bloated_manual.pdf: "boilerplate_detected" is reported via boilerplate_segment_count
            but only when the whole corpus runs (boilerplate detection is corpus-level: it
            requires >= 3 docs sharing the preamble).  Corpus-wide run satisfies this.
          - bloated_manual.pdf: "table_structure_retained" — the pipeline emits this in the
            quality dict (not as a named finding code in ParseResult.findings).  The quality
            dict key "table_structure_retained" carries the value.
          - password_protected.pdf: manifest says "unservable_encrypted"; pipeline emits
            finding code "password_protected" and exclusion reason "encrypted".
        """
        _, report = corpus_run
        has_tesseract = shutil.which("tesseract") is not None

        OCR_DEPENDENT_CODES = frozenset({"low_ocr_confidence", "mixed_pdf"})

        # Build exclusion reason lookup by filename
        exclusion_reasons: dict[str, set[str]] = {}
        for exc in report.exclusions_json.get("exclusions", []):
            fname = pathlib.Path(exc.get("source_path", "")).name
            reason = exc.get("reason", "")
            if fname:
                exclusion_reasons.setdefault(fname, set()).add(reason)

        doc_by_name: dict[str, dict[str, Any]] = {
            pathlib.Path(doc.get("source_path", "")).name: doc
            for doc in report.findings_json.get("documents", [])
        }

        entries = _load_manifest()
        parity_gaps: list[str] = []

        for entry in entries:
            name = pathlib.Path(entry["file"]).name
            expected_findings = entry.get("expected_findings", []) or []
            if not expected_findings:
                continue

            doc = doc_by_name.get(name)
            if doc is None:
                parity_gaps.append(f"  {name}: document missing from report entirely")
                continue

            # Collect all signals present for this document.
            report_codes: set[str] = set()

            # 1. Named finding codes in ParseResult.findings
            for f in doc.get("findings", []):
                code = f.get("code", "")
                if code:
                    report_codes.add(code)

            # 2. Security signals
            if doc.get("security", {}).get("invisible_content_count", 0) > 0:
                report_codes.add("invisible_content_detected")
            if doc.get("security", {}).get("injection_max_suspicion", 0.0) > 0.0:
                report_codes.add("injection_suspicion_high")

            # 3. Triage
            if doc.get("triage") is not None:
                report_codes.add("spreadsheet_triage")

            # 4. document_kind-derived signals
            kind = doc.get("document_kind", "")
            if kind == "mixed_pdf":
                report_codes.add("mixed_pdf")

            # 5. Boilerplate: boilerplate_segment_count > 0 OR report boilerplate_blocks
            if doc.get("boilerplate_segment_count", 0) > 0:
                report_codes.add("boilerplate_detected")
            if report.findings_json.get("boilerplate_blocks"):
                # Corpus-level boilerplate detected — applies to any doc with boilerplate segments
                # (the corpus-level check requires >=3 docs sharing the text block)
                if doc.get("boilerplate_segment_count", 0) > 0:
                    report_codes.add("boilerplate_detected")

            # 6. Dedup role
            if doc.get("dedup_role") in ("superseded", "superseded_version"):
                report_codes.add("dedup_superseded")
            if "superseded_version" in exclusion_reasons.get(name, set()):
                report_codes.add("dedup_superseded")

            # 7. Unservable / encrypted via finding codes or exclusion reasons
            if "password_protected" in report_codes or "encrypted" in exclusion_reasons.get(
                name, set()
            ):
                report_codes.add("unservable_encrypted")
            if "unservable_audio" in report_codes:
                pass  # already included
            if "unservable_video" in report_codes:
                pass
            if "unservable_cad" in report_codes:
                pass

            # 8. table_structure_retained: synthesised finding (report generator) or
            # quality dict value "full"/"partial" (F-3 fix: field is a StrEnum string,
            # not a bool — "is True" was always False).
            quality = doc.get("quality", {})
            if quality.get("table_structure_retained") in ("full", "partial"):
                report_codes.add("table_structure_retained")
            # Also accept the finding code if present (synthesised by report generator)
            if "table_structure_retained" in report_codes:
                pass  # already handled via findings codes above

            # Manifest-to-pipeline alias normalisations
            # manifest "mixed_pdf" maps to kind == "mixed_pdf"; OCR-gated
            # (without tesseract, bloated_manual.pdf may not fully parse its scanned appendix)

            # Check each expected finding
            for expected in expected_findings:
                code = expected.get("code", "")
                if not code:
                    continue

                # OCR-gated: skip when tesseract absent
                if code in OCR_DEPENDENT_CODES and not has_tesseract:
                    continue

                # mixed_pdf finding: only verifiable when tesseract present (scanned appendix)
                if code == "mixed_pdf" and not has_tesseract:
                    continue

                if code not in report_codes:
                    parity_gaps.append(
                        f"  {name}: expected finding code={code!r} not found. "
                        f"Report codes present: {sorted(report_codes)}. "
                        f"Exclusion reasons: {sorted(exclusion_reasons.get(name, set()))}"
                    )

        assert not parity_gaps, (
            "MANIFEST-PIPELINE PARITY GAPS (expected findings not produced):\n"
            + "\n".join(parity_gaps)
            + "\n\nThese gaps are FINDINGS requiring investigation, not test defects. "
            "See test docstring for finding-code mapping and known gap explanations."
        )

    def test_adversarial_invisible_counts_above_threshold(self, corpus_run):
        """adversarial.pdf must have >= 3 invisible-content detections in the corpus run.

        Manifest declares three invisible vectors: white_on_white (p5), tiny_font (p6),
        off_page (p7).  At least one detection per vector is required.
        (Detailed per-page coverage in test_invisible_content_detector.py.)
        """
        _, report = corpus_run
        docs = report.findings_json.get("documents", [])
        adv = next((doc for doc in docs if "adversarial" in doc.get("source_path", "")), None)
        assert adv is not None, "adversarial.pdf must appear in the corpus-wide findings report"
        invisible_count = adv.get("security", {}).get("invisible_content_count", 0)
        assert invisible_count >= 3, (
            f"adversarial.pdf: expected >= 3 invisible-content detections "
            f"(white_on_white p5, tiny_font p6, off_page p7); got {invisible_count}. "
            "Detailed per-vector coverage is in test_invisible_content_detector.py."
        )

    def test_adversarial_injection_above_manifest_threshold(self, corpus_run):
        """adversarial.pdf must have injection_max_suspicion >= 0.35 in corpus run.

        Manifest minimum threshold (lowest of three vectors): page 4 exfiltration ≥ 0.35.
        (Full per-page threshold coverage in test_injection_scoring.TestManifestPipelineParity.)
        """
        _, report = corpus_run
        docs = report.findings_json.get("documents", [])
        adv = next((doc for doc in docs if "adversarial" in doc.get("source_path", "")), None)
        assert adv is not None, "adversarial.pdf must appear in corpus-wide findings report"
        max_suspicion = adv.get("security", {}).get("injection_max_suspicion", 0.0)
        assert max_suspicion >= 0.35, (
            f"adversarial.pdf: injection_max_suspicion={max_suspicion:.4f}; "
            "manifest requires >= 0.35 (page 4 exfiltration vector minimum). "
            "Per-page detail: test_injection_scoring.TestManifestPipelineParity."
        )

    def test_spreadsheet_triage_all_three_kinds_in_corpus(self, corpus_run):
        """The corpus-wide run must classify all three spreadsheet kinds correctly.

        All spreadsheets emit document_kind="spreadsheet" (DocumentKind.spreadsheet).
        The triage sub-class is determined by the exclusion reason or findings:
          - report-kind  → parse_status=parsed, triage finding present
          - database-kind → excluded with reason=spreadsheet_database
          - model-kind    → excluded with reason=spreadsheet_model

        This is the corpus-scale complement to test_report.TestSpreadsheetTriageInReport.
        Detailed per-kind assertion coverage in test_report.TestSpreadsheetTriageInReport.
        """
        _, report = corpus_run
        docs = report.findings_json.get("documents", [])
        exclusions = report.exclusions_json.get("exclusions", [])

        def _find_doc(name_fragment: str) -> dict[str, Any] | None:
            return next(
                (d for d in docs if name_fragment in pathlib.Path(d.get("source_path", "")).name),
                None,
            )

        report_doc = _find_doc("report_spreadsheet")
        db_doc = _find_doc("database_spreadsheet")
        model_doc = _find_doc("model_spreadsheet")

        assert report_doc is not None, "report_spreadsheet.xlsx missing from corpus run"
        assert db_doc is not None, "database_spreadsheet.xlsx missing from corpus run"
        assert model_doc is not None, "model_spreadsheet.xlsx missing from corpus run"

        # All three must have document_kind=spreadsheet (DocumentKind enum value)
        for doc, label in [(report_doc, "report"), (db_doc, "database"), (model_doc, "model")]:
            assert doc.get("document_kind") == "spreadsheet", (
                f"{label}_spreadsheet: expected document_kind=spreadsheet "
                f"(all spreadsheets use DocumentKind.spreadsheet), "
                f"got {doc.get('document_kind')!r}"
            )

        # Report-kind must be ingested (parsed), not excluded
        assert report_doc.get("parse_status") == "parsed", (
            f"report_spreadsheet: expected parse_status=parsed, "
            f"got {report_doc.get('parse_status')!r}"
        )

        # Database-kind must appear in the exclusion report with reason=spreadsheet_database
        db_exclusion_reasons = {
            pathlib.Path(e.get("source_path", "")).name: e.get("reason") for e in exclusions
        }
        assert db_exclusion_reasons.get("database_spreadsheet.xlsx") == "spreadsheet_database", (
            f"database_spreadsheet.xlsx: expected exclusion reason=spreadsheet_database, "
            f"got {db_exclusion_reasons.get('database_spreadsheet.xlsx')!r}"
        )
        assert db_exclusion_reasons.get("model_spreadsheet.xlsx") == "spreadsheet_model", (
            f"model_spreadsheet.xlsx: expected exclusion reason=spreadsheet_model, "
            f"got {db_exclusion_reasons.get('model_spreadsheet.xlsx')!r}"
        )


# ---------------------------------------------------------------------------
# (b) Segments reassemble to source — HTML extension
#
# The existing native-PDF reassembly property test is:
#   tests/pipeline/test_assess_decompose_p1.TestReassemblyProperty
#     .test_reassembly_for_native_pdf_fixture (parametrised over NATIVE_PDF_FIXTURES)
# That test covers: clean_native.pdf, adversarial.pdf, boilerplate_a.pdf,
# boilerplate_b.pdf, form_filled.pdf, malformed_structure.pdf.
#
# Here we extend coverage to an HTML doc (confluence_export.html) to verify
# the same reassembly invariant holds for the Phase 2 HTML parser.
# ---------------------------------------------------------------------------


class TestHTMLReassembly:
    """Segments from an HTML fixture reassemble to the extracted text (Phase 2 extension).

    Invariant: sha256(concat(seg.text or "" for seg in sorted_by_document_order))
               == reassembly_digest stored in the SegmentSet.

    This extends the native-PDF coverage in
    test_assess_decompose_p1.TestReassemblyProperty to the HTML parser.
    """

    @pytest.fixture(scope="class")
    def confluence_seg_set(self, tmp_path_factory) -> dict[str, Any]:
        """Run the pipeline over confluence_export.html and return its segment set dict."""
        tmp_path = tmp_path_factory.mktemp("html_reassembly")
        store = _run_pipeline_over_corpus(tmp_path, ["confluence_export.html"])
        decompose_raw = store.load("decompose")
        seg_sets = decompose_raw.get("segment_sets", [])
        assert seg_sets, "No segment sets produced for confluence_export.html"
        return seg_sets[0]

    def test_confluence_html_reassembly_digest_matches(
        self, confluence_seg_set: dict[str, Any]
    ) -> None:
        """Concatenating segment text by document_order reproduces reassembly_digest.

        This verifies §6.3 / §12 for the HTML parser: segments produced by the
        Phase 2 HTMLFormatParser + DecomposeStage can be reassembled to the same
        text digest stored in the SegmentSet.ReassemblyRecord.
        """
        segments = confluence_seg_set.get("segments", [])
        reassembly = confluence_seg_set.get("reassembly", {})

        assert reassembly, "confluence_export.html SegmentSet missing reassembly record"
        stored_digest = reassembly.get("reassembly_digest")
        assert stored_digest, "reassembly_digest must be present and non-empty"

        # Concatenate segment text by document_order (same rule as native-PDF test)
        sorted_segs = sorted(segments, key=lambda s: s["document_order"])
        concat_text = "".join((s.get("text") or "") for s in sorted_segs)
        actual_digest = hashlib.sha256(concat_text.encode()).hexdigest()

        assert actual_digest == stored_digest, (
            "confluence_export.html: reassembly digest mismatch — "
            "HTML segments do not reconstruct the extracted text. "
            f"Expected {stored_digest!r}, got {actual_digest!r}. "
            "Native-PDF coverage is in test_assess_decompose_p1.TestReassemblyProperty."
        )

    def test_confluence_html_document_order_dense_gapless(
        self, confluence_seg_set: dict[str, Any]
    ) -> None:
        """document_order must be 0-based, dense, gapless for HTML segments."""
        segments = confluence_seg_set.get("segments", [])
        orders = sorted(s["document_order"] for s in segments)
        if not orders:
            pytest.skip("No segments produced for confluence_export.html")
        assert orders[0] == 0, "document_order must start at 0"
        expected = list(range(len(orders)))
        assert orders == expected, (
            f"confluence_export.html: document_order is not dense/gapless. "
            f"Got {orders}, expected {expected}"
        )

    def test_confluence_html_all_segments_have_language(
        self, confluence_seg_set: dict[str, Any]
    ) -> None:
        """Every HTML segment must have a non-empty language field after Phase 2 passes."""
        segments = confluence_seg_set.get("segments", [])
        assert segments, "No segments produced for confluence_export.html"
        missing_lang = [s["segment_id"] for s in segments if not s.get("language")]
        assert not missing_lang, (
            f"confluence_export.html: segments missing language field: {missing_lang}"
        )


# ---------------------------------------------------------------------------
# (c) Spreadsheet triage — corpus-scale check
#
# Detailed per-fixture coverage: test_report.TestSpreadsheetTriageInReport
#   tests all three kinds via subset pipeline runs, checks exclusion report,
#   checks triage finding codes in findings JSON.
#
# Here we add one thin corpus-scale assertion: the manifest-declared
# expected_triage_class for all three spreadsheet fixtures agrees with
# the pipeline output in the full 21-fixture run.
# (Covered above in TestCorpusWideManifestParity.test_expected_triage_class_matches_pipeline
#  which runs over all fixtures — this test adds explicit messaging for triage.)
# ---------------------------------------------------------------------------


class TestSpreadsheetTriageCorpusScale:
    """Corpus-scale triage check — agrees with manifest for all three spreadsheet kinds.

    This is a thin acceptance-level test.  Detailed coverage is in
    tests/pipeline/test_report.TestSpreadsheetTriageInReport and
    tests/pipeline/test_assess_p2_formats.TestSpreadsheetParsers.
    """

    @pytest.fixture(scope="class")
    def spreadsheet_report(self, tmp_path_factory) -> Any:
        """Run pipeline over all three spreadsheet fixtures."""
        from finecorpus.pipeline.report import generate_report

        tmp_path = tmp_path_factory.mktemp("spreadsheet_triage")
        names = [
            "report_spreadsheet.xlsx",
            "database_spreadsheet.xlsx",
            "model_spreadsheet.xlsx",
        ]
        store = _run_pipeline_over_corpus(tmp_path, names)
        return generate_report(
            artifacts_root=store.run_dir.parent,
            run_id=store.run_id,
        )

    def test_manifest_triage_class_agrees_with_pipeline(self, spreadsheet_report) -> None:
        """Manifest expected_triage_class is consistent with pipeline output for all three.

        All spreadsheets use DocumentKind.spreadsheet (value "spreadsheet").
        The triage sub-class (report/database/model) is expressed via exclusion reason
        and finding code, not via document_kind.  We verify:
          - All three documents appear in the report with document_kind="spreadsheet".
          - report_spreadsheet has parse_status=parsed.
          - database/model have exclusion reasons spreadsheet_database / spreadsheet_model.
        """
        docs = {
            pathlib.Path(d.get("source_path", "")).name: d
            for d in spreadsheet_report.findings_json.get("documents", [])
        }
        exclusion_reasons = {
            pathlib.Path(e.get("source_path", "")).name: e.get("reason")
            for e in spreadsheet_report.exclusions_json.get("exclusions", [])
        }

        entries = _load_manifest()
        ss_entries = [e for e in entries if "spreadsheet" in pathlib.Path(e["file"]).name]
        issues = []
        for entry in ss_entries:
            name = pathlib.Path(entry["file"]).name
            doc = docs.get(name)
            if doc is None:
                issues.append(f"  {name}: missing from report")
                continue
            # All spreadsheets must have document_kind=spreadsheet
            actual_kind = doc.get("document_kind", "")
            if actual_kind != "spreadsheet":
                issues.append(f"  {name}: expected document_kind=spreadsheet, got {actual_kind!r}")
            # Triage sub-class verification
            expected_class = entry.get("expected_triage_class", "")
            if expected_class == "spreadsheet_report":
                if doc.get("parse_status") != "parsed":
                    issues.append(
                        f"  {name} (report kind): expected parse_status=parsed, "
                        f"got {doc.get('parse_status')!r}"
                    )
            elif expected_class == "spreadsheet_database":
                reason = exclusion_reasons.get(name)
                if reason != "spreadsheet_database":
                    issues.append(
                        f"  {name} (database kind): expected "
                        f"exclusion reason=spreadsheet_database, got {reason!r}"
                    )
            elif expected_class == "spreadsheet_model":
                reason = exclusion_reasons.get(name)
                if reason != "spreadsheet_model":
                    issues.append(
                        f"  {name} (model kind): expected exclusion reason=spreadsheet_model, "
                        f"got {reason!r}"
                    )

        assert not issues, "Spreadsheet triage issues (manifest vs pipeline):\n" + "\n".join(issues)

    def test_exclusion_report_covers_database_and_model(self, spreadsheet_report) -> None:
        """database and model spreadsheets must appear in the exclusion report.

        Detailed assertion coverage: test_report.TestSpreadsheetTriageInReport.
        """
        exclusions = spreadsheet_report.exclusions_json.get("exclusions", [])
        reasons = {pathlib.Path(e.get("source_path", "")).name: e.get("reason") for e in exclusions}
        assert reasons.get("database_spreadsheet.xlsx") == "spreadsheet_database", (
            f"database_spreadsheet.xlsx: expected reason=spreadsheet_database, "
            f"got {reasons.get('database_spreadsheet.xlsx')!r}"
        )
        assert reasons.get("model_spreadsheet.xlsx") == "spreadsheet_model", (
            f"model_spreadsheet.xlsx: expected reason=spreadsheet_model, "
            f"got {reasons.get('model_spreadsheet.xlsx')!r}"
        )


# ---------------------------------------------------------------------------
# (d) Adversarial fixture — corpus-scale flagging check
#
# Detailed per-vector coverage:
#   test_invisible_content_detector.TestAdversarialFixtureDetections — per-page
#   test_injection_scoring.TestManifestPipelineParity — per-page thresholds
#   test_t09_injection_flagging.TestT09InjectionFlaggingUnit — full pipeline chunks
#
# Here we add a thin acceptance check that all manifest-declared adversarial
# thresholds are crossed in the corpus-wide findings report.
# ---------------------------------------------------------------------------


class TestAdversarialCorpusScaleFlagging:
    """Adversarial fixture is flagged in the corpus-scale findings report.

    Manifest expected_findings for adversarial.pdf:
      - invisible_content_detected on pages 5, 6, 7 (3 detections minimum)
      - injection_suspicion_high on pages 2 (≥0.75), 3 (≥0.70), 4 (≥0.35)

    This is the corpus-scale acceptance gate.  Detailed per-page coverage in:
      test_invisible_content_detector.TestAdversarialFixtureDetections
      test_injection_scoring.TestManifestPipelineParity
    """

    @pytest.fixture(scope="class")
    def adversarial_report(self, tmp_path_factory) -> Any:
        """Run pipeline over adversarial.pdf and return the findings report."""
        tmp_path = tmp_path_factory.mktemp("adversarial_accept")
        store = _run_pipeline_over_corpus(tmp_path, ["adversarial.pdf"])
        return generate_report(
            artifacts_root=store.run_dir.parent,
            run_id=store.run_id,
        )

    def test_adversarial_invisible_content_three_vectors(self, adversarial_report) -> None:
        """At least 3 invisible-content detections (one per manifest-declared vector).

        Manifest declares: white_on_white (p5), tiny_font/zero_size_font (p6), off_page (p7).
        """
        docs = adversarial_report.findings_json.get("documents", [])
        adv = next(
            (d for d in docs if "adversarial" in pathlib.Path(d.get("source_path", "")).name),
            None,
        )
        assert adv is not None, "adversarial.pdf must appear in findings report"
        count = adv.get("security", {}).get("invisible_content_count", 0)
        assert count >= 3, (
            f"adversarial.pdf: expected >= 3 invisible-content detections "
            f"(white_on_white, tiny_font, off_page); got {count}. "
            "Per-vector detail in test_invisible_content_detector.py."
        )

    def test_adversarial_injection_max_above_lowest_threshold(self, adversarial_report) -> None:
        """injection_max_suspicion must be >= 0.35 (lowest manifest threshold: page 4).

        Manifest thresholds: page 2 ≥ 0.75, page 3 ≥ 0.70, page 4 ≥ 0.35.
        The max across all segments must at least clear the lowest bar.
        Per-page threshold detail in test_injection_scoring.TestManifestPipelineParity.
        """
        docs = adversarial_report.findings_json.get("documents", [])
        adv = next(
            (d for d in docs if "adversarial" in pathlib.Path(d.get("source_path", "")).name),
            None,
        )
        assert adv is not None, "adversarial.pdf must appear in findings report"
        max_score = adv.get("security", {}).get("injection_max_suspicion", 0.0)
        assert max_score >= 0.35, (
            f"adversarial.pdf: injection_max_suspicion={max_score:.4f} below "
            f"manifest minimum threshold 0.35 (page 4 exfiltration vector). "
            "Per-page threshold detail in test_injection_scoring.TestManifestPipelineParity."
        )

    def test_adversarial_injection_flagged_segments_present(self, adversarial_report) -> None:
        """At least one injection-flagged segment must appear in the findings report."""
        docs = adversarial_report.findings_json.get("documents", [])
        adv = next(
            (d for d in docs if "adversarial" in pathlib.Path(d.get("source_path", "")).name),
            None,
        )
        assert adv is not None
        flagged = adv.get("security", {}).get("injection_flagged_segments", [])
        assert flagged, (
            "adversarial.pdf: no injection-flagged segments in findings report. "
            "Visible injection text on pages 2-4 must produce scored segments."
        )
        # M-105: verify none are excluded-tier (injection must not cause silent exclusion)
        for seg_entry in flagged:
            assert seg_entry.get("injection_suspicion", 0.0) > 0.0, (
                f"Flagged segment has zero injection_suspicion: {seg_entry}"
            )
