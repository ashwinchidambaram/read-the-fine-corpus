"""Tests for the scanned-PDF OCR parser (Phase 2).

Coverage:
  - scanned_poor.pdf: per-page confidences populated, page 3 lowest confidence
    (assert relative ordering, not absolute values), all regions retained
    including low-confidence pages, segments carry ocr_confidence.
  - image_only.pdf: now parsed via OCR (not unservable), produces scanned_pdf
    document_kind with per-page confidence.
  - Missing-tesseract path: monkeypatched availability check → honest failed
    result with missing_dependency finding.
  - Salience tier assignment: below floor → excluded, in warn band → supporting,
    above warn → type-prior tier applies.
  - Registry ordering: can_parse returns False for native-text PDFs.
"""

from __future__ import annotations

import pathlib
import shutil
from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch

import pytest

from finecorpus.contracts.parse_result import ParseStatus
from finecorpus.contracts.shared.blocks import (
    PermissionFidelity,
    PermissionMode,
    PermissionSource,
    TenancyBlock,
)
from finecorpus.pipeline import run_pipeline
from finecorpus.pipeline.artifact_store import ArtifactStore
from finecorpus.pipeline.assess.parsers.base import ParserContext
from finecorpus.pipeline.assess.parsers.pdf_scanned import ScannedPDFParser, pdf_scanned_parser

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

FIXTURE_CORPUS = pathlib.Path(__file__).parent.parent.parent / "fixtures" / "golden" / "corpus"

_TENANCY = TenancyBlock(
    workspace_id="ws-test",
    kb_id="kb-test",
    permission_mode=PermissionMode.public_to_kb,
    permission_principals=[],
    permission_source=PermissionSource.platform,
    permission_fidelity=PermissionFidelity.authoritative,
    permission_resolved_at=None,
)

_CTX = ParserContext()

_PARSED_AT = datetime(2026, 9, 3, tzinfo=UTC)


def _make_item(path: pathlib.Path) -> dict[str, Any]:
    import hashlib

    content = path.read_bytes()
    return {
        "document_id": f"doc-{path.stem}",
        "content_hash": hashlib.sha256(content).hexdigest(),
        "source_path": str(path),
    }


def _run_pipeline_over_single(tmp_path: pathlib.Path, fixture_name: str):
    """Run the full pipeline over a single fixture file; return (parse_batch, seg_batch)."""
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    shutil.copy2(FIXTURE_CORPUS / fixture_name, src_dir / fixture_name)

    artifacts_root = tmp_path / "artifacts"
    run_pipeline(
        source_dir=src_dir,
        artifacts_root=artifacts_root,
        run_id="test-ocr",
        workspace_id="ws-test",
        kb_id="kb-test",
    )
    store = ArtifactStore(artifacts_root=artifacts_root, run_id="test-ocr")
    parse_batch = store.load("assess")
    seg_batch = store.load("decompose")
    return parse_batch["results"][0], seg_batch["segment_sets"][0]


# ---------------------------------------------------------------------------
# Registry ordering: can_parse
# ---------------------------------------------------------------------------


class TestCanParse:
    """can_parse claims image-only PDFs and rejects native-text PDFs."""

    def test_claims_image_only_pdf(self):
        """pdf_scanned_parser must claim image_only.pdf (no extractable text)."""
        item = _make_item(FIXTURE_CORPUS / "image_only.pdf")
        assert pdf_scanned_parser.can_parse(item) is True

    def test_claims_scanned_poor_pdf(self):
        """pdf_scanned_parser must claim scanned_poor.pdf (all-image pages)."""
        item = _make_item(FIXTURE_CORPUS / "scanned_poor.pdf")
        assert pdf_scanned_parser.can_parse(item) is True

    def test_rejects_native_pdf(self):
        """pdf_scanned_parser must NOT claim native-text PDFs (has extractable text)."""
        item = _make_item(FIXTURE_CORPUS / "clean_native.pdf")
        assert pdf_scanned_parser.can_parse(item) is False

    def test_rejects_non_pdf(self):
        """pdf_scanned_parser must NOT claim non-PDF files."""
        item = _make_item(FIXTURE_CORPUS / "audio_stub.wav")
        assert pdf_scanned_parser.can_parse(item) is False


# ---------------------------------------------------------------------------
# scanned_poor.pdf — OCR confidence correctness
# ---------------------------------------------------------------------------


class TestScannedPoorPDF:
    """scanned_poor.pdf: 12-page scanned document with page 3 most degraded."""

    @pytest.fixture(scope="class")
    def parse_result(self, tmp_class_path):
        """Parse scanned_poor.pdf once; share across tests in class."""
        item = _make_item(FIXTURE_CORPUS / "scanned_poor.pdf")
        return pdf_scanned_parser.parse(item, _TENANCY, _PARSED_AT, _CTX)

    @pytest.fixture(scope="class")
    def tmp_class_path(self, tmp_path_factory):
        return tmp_path_factory.mktemp("scanned_poor")

    def test_parse_status_is_parsed(self, parse_result):
        """scanned_poor.pdf must parse successfully via OCR."""
        assert parse_result.parse_status in (ParseStatus.parsed, ParseStatus.partial), (
            f"Expected parsed or partial, got {parse_result.parse_status!r}"
        )

    def test_document_kind_is_scanned_pdf(self, parse_result):
        """scanned_poor.pdf must be identified as scanned_pdf."""
        assert parse_result.document_kind.value == "scanned_pdf"

    def test_all_pages_have_ocr_confidence(self, parse_result):
        """Every page must have ocr_confidence populated (§6.2 MUST retain per-page conf)."""
        assert parse_result.pages, "Expected at least one page result"
        for page in parse_result.pages:
            assert page.ocr_confidence is not None, (
                f"Page {page.page_number}: ocr_confidence must not be None for scanned pages"
            )
            assert 0.0 <= page.ocr_confidence <= 1.0, (
                f"Page {page.page_number}: ocr_confidence {page.ocr_confidence} out of [0,1] range"
            )

    def test_page_3_has_lowest_confidence(self, parse_result):
        """Page 3 must have the lowest OCR confidence of all pages (fixture property).

        Asserts relative ordering, not absolute values, per the spec instruction.
        The fixture is generated with a degraded page 3 (simulated ~0.42 confidence).
        """
        assert len(parse_result.pages) >= 3, "Expected at least 3 pages"
        page_confs = {p.page_number: p.ocr_confidence for p in parse_result.pages}
        page3_conf = page_confs[3]
        other_confs = [c for p, c in page_confs.items() if p != 3]
        # Page 3 must be the minimum (or tied for minimum)
        assert page3_conf is not None
        assert page3_conf <= min(c for c in other_confs if c is not None), (
            f"Page 3 confidence {page3_conf:.4f} is not <= all other pages "
            f"(other min: {min(c for c in other_confs if c is not None):.4f}). "
            "Fixture guarantees page 3 is the most degraded."
        )

    def test_all_regions_retained_including_low_confidence(self, parse_result):
        """Regions must be retained for ALL pages — no thresholding at parse time (§6.2 MUST)."""
        # Every page must have exactly one corresponding region
        page_nums_with_region = {r.location.page_start for r in parse_result.regions}
        page_nums = {p.page_number for p in parse_result.pages}
        assert page_nums_with_region == page_nums, (
            "Some pages are missing from regions — thresholding must NOT happen at parse time. "
            f"Pages: {page_nums}, regions: {page_nums_with_region}"
        )

    def test_regions_have_ocr_confidence(self, parse_result):
        """Every region from a scanned page must carry ocr_confidence."""
        for region in parse_result.regions:
            assert region.ocr_confidence is not None, (
                f"Region {region.region_id}: ocr_confidence must not be None for scanned regions"
            )

    def test_mean_ocr_confidence_in_quality(self, parse_result):
        """quality.mean_ocr_confidence must be populated for OCR documents."""
        assert parse_result.quality.mean_ocr_confidence is not None, (
            "quality.mean_ocr_confidence must be set for scanned PDFs"
        )
        assert 0.0 <= parse_result.quality.mean_ocr_confidence <= 1.0

    def test_low_ocr_confidence_finding_present(self, parse_result):
        """A low_ocr_confidence finding must be emitted for the degraded page."""
        codes = {f.code for f in parse_result.findings}
        assert "low_ocr_confidence" in codes, f"Expected low_ocr_confidence finding, got: {codes}"

    def test_parser_ref_identifies_tesseract(self, parse_result):
        """parser.ocr_engine must identify tesseract."""
        assert parse_result.parser.ocr_engine == "tesseract", (
            f"Expected ocr_engine=tesseract, got {parse_result.parser.ocr_engine!r}"
        )


# ---------------------------------------------------------------------------
# scanned_poor.pdf — Decompose interaction (segments carry ocr_confidence)
# ---------------------------------------------------------------------------


class TestScannedPoorDecompose:
    """Decompose stage: segments from scanned_poor carry ocr_confidence; tiers correct."""

    @pytest.fixture(scope="class")
    def pipeline_results(self, tmp_class_path):
        """Run full pipeline over scanned_poor.pdf once; share across tests."""
        return _run_pipeline_over_single(tmp_class_path, "scanned_poor.pdf")

    @pytest.fixture(scope="class")
    def tmp_class_path(self, tmp_path_factory):
        return tmp_path_factory.mktemp("decompose_scanned")

    def test_segments_carry_ocr_confidence(self, pipeline_results):
        """Segments from scanned pages must carry ocr_confidence (§6.4)."""
        _, seg_set = pipeline_results
        segments = seg_set.get("segments", [])
        # At least some segments must have ocr_confidence set
        ocr_segments = [s for s in segments if s.get("ocr_confidence") is not None]
        assert ocr_segments, (
            "Expected at least one segment with ocr_confidence from scanned_poor.pdf"
        )

    def test_low_confidence_segments_are_excluded_or_supporting(self, pipeline_results):
        """Segments with confidence < floor (0.60) must be excluded tier.
        Segments with confidence in warn band [0.60, 0.80) must be supporting.
        """
        _, seg_set = pipeline_results
        segments = seg_set.get("segments", [])
        floor = 0.60
        warn = 0.80

        for seg in segments:
            conf = seg.get("ocr_confidence")
            if conf is None:
                continue
            tier = seg.get("salience_tier")
            if conf < floor:
                assert tier == "excluded", (
                    f"Segment with ocr_confidence={conf:.4f} < floor {floor} "
                    f"must be excluded tier, got {tier!r}"
                )
            elif conf < warn:
                assert tier == "supporting", (
                    f"Segment with ocr_confidence={conf:.4f} in warn band "
                    f"must be supporting tier, got {tier!r}"
                )

    def test_ocr_confidence_signal_in_salience_signals(self, pipeline_results):
        """Segments overridden by OCR signal must have the OCR signal in salience_signals."""
        _, seg_set = pipeline_results
        segments = seg_set.get("segments", [])
        floor = 0.60
        warn = 0.80

        for seg in segments:
            conf = seg.get("ocr_confidence")
            if conf is None or conf >= warn:
                continue
            signals = seg.get("salience_signals", [])
            signal_kinds = {s.get("kind") for s in signals}
            if conf < floor:
                assert "ocr_confidence_floor" in signal_kinds, (
                    f"Segment ocr_confidence={conf:.4f} < floor must have "
                    f"ocr_confidence_floor signal; got {signal_kinds}"
                )
            else:
                assert "ocr_confidence_warn" in signal_kinds, (
                    f"Segment ocr_confidence={conf:.4f} in warn band must have "
                    f"ocr_confidence_warn signal; got {signal_kinds}"
                )

    def test_exactly_one_winning_signal_per_segment(self, pipeline_results):
        """Every segment must have exactly one won=True signal (invariant)."""
        _, seg_set = pipeline_results
        for seg in seg_set.get("segments", []):
            signals = seg.get("salience_signals", [])
            winners = [s for s in signals if s.get("won") is True]
            assert len(winners) == 1, (
                f"Segment {seg.get('segment_id')} has {len(winners)} won=True signals "
                "(must be exactly 1)"
            )


# ---------------------------------------------------------------------------
# image_only.pdf — Now OCR-parsed (Phase 2)
# ---------------------------------------------------------------------------


class TestImageOnlyPDF:
    """image_only.pdf is now parsed via OCR instead of returned as unservable."""

    @pytest.fixture(scope="class")
    def parse_result(self, tmp_class_path):
        item = _make_item(FIXTURE_CORPUS / "image_only.pdf")
        return pdf_scanned_parser.parse(item, _TENANCY, _PARSED_AT, _CTX)

    @pytest.fixture(scope="class")
    def tmp_class_path(self, tmp_path_factory):
        return tmp_path_factory.mktemp("image_only")

    def test_parse_status_is_parsed(self, parse_result):
        """image_only.pdf must be OCR-parsed (not failed/unservable)."""
        assert parse_result.parse_status == ParseStatus.parsed, (
            f"Expected parsed, got {parse_result.parse_status!r}"
        )

    def test_document_kind_is_scanned_pdf(self, parse_result):
        """document_kind must be scanned_pdf for OCR-parsed documents."""
        assert parse_result.document_kind.value == "scanned_pdf"

    def test_no_unservable_image_only_finding(self, parse_result):
        """Phase 2: unservable_image_only finding must NOT be emitted for OCR-parseable PDFs."""
        codes = {f.code for f in parse_result.findings}
        assert "unservable_image_only" not in codes, (
            "unservable_image_only finding must not appear for OCR-parsed documents"
        )

    def test_per_page_confidence_populated(self, parse_result):
        """All pages must have ocr_confidence populated."""
        assert parse_result.pages, "Expected at least one page"
        for page in parse_result.pages:
            assert page.ocr_confidence is not None, (
                f"Page {page.page_number}: ocr_confidence must be populated for scanned pages"
            )
            assert page.is_scanned is True, f"Page {page.page_number}: is_scanned must be True"


# ---------------------------------------------------------------------------
# Missing-tesseract path: honest failure, no crash
# ---------------------------------------------------------------------------


class TestMissingTesseract:
    """When tesseract is unavailable, the parser returns an honest failed result."""

    def test_missing_tesseract_produces_failed_result(self, tmp_path):
        """Monkeypatching shutil.which to return None simulates absent tesseract."""
        parser = ScannedPDFParser()
        item = _make_item(FIXTURE_CORPUS / "image_only.pdf")

        with patch(
            "finecorpus.pipeline.assess.parsers.pdf_scanned.shutil.which", return_value=None
        ):
            result = parser.parse(item, _TENANCY, _PARSED_AT, _CTX)

        assert result.parse_status == ParseStatus.failed, (
            f"Expected failed when tesseract absent, got {result.parse_status!r}"
        )

    def test_missing_tesseract_emits_missing_dependency_finding(self, tmp_path):
        """The failed result must include a missing_dependency finding naming tesseract."""
        parser = ScannedPDFParser()
        item = _make_item(FIXTURE_CORPUS / "image_only.pdf")

        with patch(
            "finecorpus.pipeline.assess.parsers.pdf_scanned.shutil.which", return_value=None
        ):
            result = parser.parse(item, _TENANCY, _PARSED_AT, _CTX)

        codes = {f.code for f in result.findings}
        assert "missing_dependency" in codes, (
            f"Expected missing_dependency finding when tesseract absent, got {codes}"
        )

    def test_missing_tesseract_does_not_raise(self, tmp_path):
        """parse() must never raise regardless of tesseract availability.

        Invariant from the FormatParser contract: parse() must not raise.
        """
        parser = ScannedPDFParser()
        item = _make_item(FIXTURE_CORPUS / "image_only.pdf")

        # Should not raise
        with patch(
            "finecorpus.pipeline.assess.parsers.pdf_scanned.shutil.which", return_value=None
        ):
            result = parser.parse(item, _TENANCY, _PARSED_AT, _CTX)

        # Result is a valid ParseResult (not an exception)
        assert result is not None

    def test_missing_tesseract_can_parse_still_works(self, tmp_path):
        """can_parse must not be affected by tesseract availability (it only opens the PDF)."""
        parser = ScannedPDFParser()
        item = _make_item(FIXTURE_CORPUS / "image_only.pdf")

        with patch(
            "finecorpus.pipeline.assess.parsers.pdf_scanned.shutil.which", return_value=None
        ):
            # can_parse does not call tesseract — it only checks for image-only PDF structure
            result = parser.can_parse(item)

        assert result is True, "can_parse must still return True even if tesseract is absent"


# ---------------------------------------------------------------------------
# Salience tier assignment per thresholds
# ---------------------------------------------------------------------------


class TestSalienceTierAssignment:
    """SaliencePass assigns correct tiers based on OCR confidence thresholds."""

    def _make_parse_result_dict(self, confidence: float) -> dict:
        """Build a minimal parsed ParseResultBatch dict with one scanned region."""
        return {
            "schema_version": "1.0.0",
            "contract": "parse_result_batch",
            "run_id": "test-salience",
            "produced_at": "2026-09-03T00:00:00+00:00",
            "skeleton": None,
            "results": [
                {
                    "schema_version": "1.0.0",
                    "tenancy": {
                        "workspace_id": "ws-test",
                        "kb_id": "kb-test",
                        "permission_mode": "public_to_kb",
                        "permission_principals": [],
                        "permission_source": "platform",
                        "permission_fidelity": "authoritative",
                        "permission_resolved_at": None,
                    },
                    "document_id": "doc-salience-test",
                    "content_hash": "a" * 64,
                    "parser": {
                        "name": "pytesseract",
                        "version": "5.5.2",
                        "ocr_engine": "tesseract",
                    },
                    "parsed_at": "2026-09-03T00:00:00+00:00",
                    "parse_status": "parsed",
                    "document_kind": "scanned_pdf",
                    "quality": {
                        "overall": confidence,
                        "text_extraction_ratio": 1.0,
                        "table_structure_retained": "n/a",
                        "is_near_empty": False,
                        "mean_ocr_confidence": confidence,
                    },
                    "pages": [
                        {
                            "page_number": 1,
                            "is_scanned": True,
                            "ocr_confidence": confidence,
                            "extraction_ratio": 0.8,
                            "invisible_content": [],
                        }
                    ],
                    "regions": [
                        {
                            "region_id": "page-1-r1",
                            "location": {
                                "locator_kind": "page",
                                "page_start": 1,
                                "page_end": 1,
                            },
                            "text": "OCR text from a scanned page. Has enough words to segment.",
                            "extract_status": "ok",
                            "ocr_confidence": confidence,
                            "language": None,
                            "detected_class_hint": "other",
                            "encoding_issue": False,
                        }
                    ],
                    "boilerplate_candidates": [],
                    "content_classes": ["scanned"],
                    "encoding_issues": [],
                    "language_distribution": [],
                    "findings": [],
                }
            ],
        }

    def _run_decompose(self, parse_batch_dict: dict, tmp_path: pathlib.Path) -> list:
        """Run DecomposeStage over a synthetic parse batch; return segments."""
        from finecorpus.pipeline.artifact_store import ArtifactStore  # noqa: PLC0415
        from finecorpus.pipeline.decompose.stage import DecomposeStage  # noqa: PLC0415

        artifacts_root = tmp_path / "artifacts"
        store = ArtifactStore(artifacts_root=artifacts_root, run_id="test-salience")
        decompose = DecomposeStage(
            run_started_at=datetime(2026, 9, 3, tzinfo=UTC),
            run_id="test-salience",
        )
        result = decompose.run(input_data=parse_batch_dict, store=store)
        return result["segment_sets"][0].get("segments", [])

    def test_below_floor_gets_excluded_tier(self, tmp_path):
        """confidence < 0.60 → excluded tier (OQ-7)."""
        batch = self._make_parse_result_dict(confidence=0.50)
        segments = self._run_decompose(batch, tmp_path)
        assert segments, "Expected at least one segment"
        for seg in segments:
            if seg.get("ocr_confidence") is not None:
                assert seg["salience_tier"] == "excluded", (
                    f"confidence=0.50 should produce excluded tier, got {seg['salience_tier']!r}"
                )

    def test_in_warn_band_gets_supporting_tier(self, tmp_path):
        """0.60 <= confidence < 0.80 → supporting tier (OQ-7)."""
        batch = self._make_parse_result_dict(confidence=0.70)
        segments = self._run_decompose(batch, tmp_path)
        assert segments, "Expected at least one segment"
        for seg in segments:
            if seg.get("ocr_confidence") is not None:
                assert seg["salience_tier"] == "supporting", (
                    f"confidence=0.70 should produce supporting tier, got {seg['salience_tier']!r}"
                )

    def test_above_warn_keeps_type_prior_tier(self, tmp_path):
        """confidence >= 0.80 → type-prior tier (no OCR override)."""
        batch = self._make_parse_result_dict(confidence=0.90)
        segments = self._run_decompose(batch, tmp_path)
        assert segments, "Expected at least one segment"
        for seg in segments:
            # Type-prior for prose/unknown/scanned_region = supporting or primary
            # The exact tier depends on what paragraph segmentation produces;
            # it must NOT be excluded (which would indicate incorrect OCR override at 0.90).
            assert seg["salience_tier"] != "excluded", (
                f"confidence=0.90 must NOT produce excluded tier, got {seg['salience_tier']!r}"
            )

    def test_below_floor_has_floor_signal(self, tmp_path):
        """Segments below floor must have ocr_confidence_floor signal (won=True)."""
        batch = self._make_parse_result_dict(confidence=0.50)
        segments = self._run_decompose(batch, tmp_path)
        for seg in segments:
            conf = seg.get("ocr_confidence")
            if conf is None or conf >= 0.60:
                continue
            signals = seg.get("salience_signals", [])
            floor_winners = [
                s
                for s in signals
                if s.get("kind") == "ocr_confidence_floor" and s.get("won") is True
            ]
            assert floor_winners, (
                f"Segment with confidence={conf:.4f} must have won=True ocr_confidence_floor signal"
            )

    def test_in_warn_band_has_warn_signal(self, tmp_path):
        """Segments in warn band must have ocr_confidence_warn signal (won=True)."""
        batch = self._make_parse_result_dict(confidence=0.70)
        segments = self._run_decompose(batch, tmp_path)
        for seg in segments:
            conf = seg.get("ocr_confidence")
            if conf is None or conf < 0.60 or conf >= 0.80:
                continue
            signals = seg.get("salience_signals", [])
            warn_winners = [
                s
                for s in signals
                if s.get("kind") == "ocr_confidence_warn" and s.get("won") is True
            ]
            assert warn_winners, (
                f"Segment with confidence={conf:.4f} must have won=True ocr_confidence_warn signal"
            )
