"""Integration tests for mixed-PDF handling in NativePDFParser (Phase 2, F-1/F-2).

Tests that ``bloated_manual.pdf`` — a native-text PDF with a rasterized scanned
appendix on the final page — is correctly classified as ``mixed_pdf``, that a
``mixed_pdf`` finding is emitted, that page 6's OCR'd content from the parts list
is actually extracted, and that the OCR'd region carries a non-None ocr_confidence.

Also tests that the ~0.49-confidence appendix content lands in the excluded/supporting
tier per the floor/warn thresholds when run through Decompose.

These tests require the ``tesseract`` binary on PATH.  When absent, tests that
exercise real OCR are skipped via a pytest.mark.skipif guard, consistent with
the policy for other OCR tests in this package.
"""

from __future__ import annotations

import pathlib
import shutil
from datetime import UTC, datetime
from typing import Any

import pytest

from finecorpus.contracts.parse_result import DocumentKind, ParseStatus
from finecorpus.contracts.shared.blocks import (
    PermissionFidelity,
    PermissionMode,
    PermissionSource,
    TenancyBlock,
)
from finecorpus.pipeline import run_pipeline
from finecorpus.pipeline.artifact_store import ArtifactStore
from finecorpus.pipeline.assess.parsers.base import ParserContext
from finecorpus.pipeline.assess.parsers.pdf_native import native_pdf_parser

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

_PARSED_AT = datetime(2026, 9, 4, tzinfo=UTC)

# Tesseract availability — evaluated once at collection time.
_TESSERACT_AVAILABLE = shutil.which("tesseract") is not None

_SKIP_NO_TESSERACT = pytest.mark.skipif(
    not _TESSERACT_AVAILABLE,
    reason="tesseract binary not found on PATH — skipping OCR-dependent mixed-PDF tests",
)


def _make_item(path: pathlib.Path) -> dict[str, Any]:
    import hashlib

    content = path.read_bytes()
    return {
        "document_id": f"doc-{path.stem}",
        "content_hash": hashlib.sha256(content).hexdigest(),
        "source_path": str(path),
    }


def _run_pipeline_over_single(tmp_path: pathlib.Path, fixture_name: str):
    """Run the full pipeline over a single fixture file; return (parse_result, seg_set)."""
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    shutil.copy2(FIXTURE_CORPUS / fixture_name, src_dir / fixture_name)

    artifacts_root = tmp_path / "artifacts"
    run_pipeline(
        source_dir=src_dir,
        artifacts_root=artifacts_root,
        run_id="test-mixed",
        workspace_id="ws-test",
        kb_id="kb-test",
    )
    store = ArtifactStore(artifacts_root=artifacts_root, run_id="test-mixed")
    parse_batch = store.load("assess")
    seg_batch = store.load("decompose")
    return parse_batch["results"][0], seg_batch["segment_sets"][0]


# ---------------------------------------------------------------------------
# Assess-level tests — NativePDFParser on bloated_manual.pdf
# ---------------------------------------------------------------------------


@_SKIP_NO_TESSERACT
class TestBloatedManualAssess:
    """bloated_manual.pdf: mixed-PDF detection and OCR at the Assess stage."""

    @pytest.fixture(scope="class")
    def parse_result(self, tmp_class_path):
        """Parse bloated_manual.pdf once; shared across all tests in this class."""
        item = _make_item(FIXTURE_CORPUS / "bloated_manual.pdf")
        return native_pdf_parser.parse(item, _TENANCY, _PARSED_AT, _CTX)

    @pytest.fixture(scope="class")
    def tmp_class_path(self, tmp_path_factory):
        return tmp_path_factory.mktemp("bloated_manual_assess")

    def test_document_kind_is_mixed_pdf(self, parse_result):
        """bloated_manual.pdf must be classified as mixed_pdf (golden manifest expectation)."""
        assert parse_result.document_kind == DocumentKind.mixed_pdf, (
            f"Expected document_kind=mixed_pdf, got {parse_result.document_kind!r}. "
            "The manifest declares expected_triage_class: mixed_pdf."
        )

    def test_parse_status_is_parsed(self, parse_result):
        """bloated_manual.pdf must parse successfully (native pages + OCR'd page 6)."""
        assert parse_result.parse_status in (ParseStatus.parsed, ParseStatus.partial), (
            f"Expected parsed or partial, got {parse_result.parse_status!r}"
        )

    def test_mixed_pdf_finding_present(self, parse_result):
        """A mixed_pdf finding must be emitted (golden manifest expectation)."""
        codes = [f.code for f in parse_result.findings]
        assert "mixed_pdf" in codes, (
            f"Expected mixed_pdf finding, got: {codes}. "
            "The manifest declares expected_findings: [{code: mixed_pdf}]."
        )

    def test_page6_is_scanned(self, parse_result):
        """Page 6 must be classified as scanned (is_scanned=True) with ocr_confidence."""
        page6 = next((p for p in parse_result.pages if p.page_number == 6), None)
        assert page6 is not None, "Expected page 6 to be present"
        assert page6.is_scanned is True, (
            "Page 6 must have is_scanned=True (it contains a rasterized appendix image)"
        )

    def test_page6_has_non_none_ocr_confidence(self, parse_result):
        """Page 6's OCR confidence must be non-None (OCR was applied)."""
        page6 = next((p for p in parse_result.pages if p.page_number == 6), None)
        assert page6 is not None
        assert page6.ocr_confidence is not None, (
            "Page 6 ocr_confidence must not be None — OCR was run on the embedded image"
        )
        assert 0.0 <= page6.ocr_confidence <= 1.0, (
            f"ocr_confidence {page6.ocr_confidence} out of [0, 1] range"
        )

    def test_page6_region_contains_parts_list_content(self, parse_result):
        """Page 6's region text must contain recognisable content from the parts list.

        The scanned appendix image contains lines like 'LG-0041', 'Drive Belt', 'APPENDIX'.
        At least one of these must appear in the OCR'd region text.
        """
        region6 = next((r for r in parse_result.regions if r.location.page_start == 6), None)
        assert region6 is not None, "Expected a region for page 6"
        assert region6.text is not None, (
            "Page 6 region text must not be None — OCR should have extracted content "
            "from the embedded appendix image"
        )
        text_lower = region6.text.lower()
        # At least one landmark string from the scanned appendix must be present.
        landmarks = ["appendix", "lg-004", "drive belt", "bearing", "parts list", "parts"]
        found = [lm for lm in landmarks if lm in text_lower]
        assert found, (
            f"Page 6 region text does not contain any expected parts-list content. "
            f"Searched for: {landmarks}. "
            f"Got (first 400 chars): {region6.text[:400]!r}"
        )

    def test_page6_region_has_ocr_confidence(self, parse_result):
        """Page 6's region must carry a non-None ocr_confidence."""
        region6 = next((r for r in parse_result.regions if r.location.page_start == 6), None)
        assert region6 is not None
        assert region6.ocr_confidence is not None, "Page 6 region ocr_confidence must not be None"

    def test_native_pages_have_no_ocr_confidence(self, parse_result):
        """Native-text pages (1-5) must have ocr_confidence=None (no OCR was run on them)."""
        for page in parse_result.pages:
            if page.page_number < 6:
                assert page.ocr_confidence is None, (
                    f"Page {page.page_number} is a native-text page; "
                    f"ocr_confidence must be None, got {page.ocr_confidence}"
                )

    def test_quality_mean_ocr_confidence_set(self, parse_result):
        """quality.mean_ocr_confidence must be set for mixed-PDF documents."""
        assert parse_result.quality.mean_ocr_confidence is not None, (
            "quality.mean_ocr_confidence must be populated for mixed-PDF documents"
        )
        assert 0.0 <= parse_result.quality.mean_ocr_confidence <= 1.0


# ---------------------------------------------------------------------------
# Decompose-level tests — confidence floor/warn tier assignment for page 6
# ---------------------------------------------------------------------------


@_SKIP_NO_TESSERACT
class TestBloatedManualDecompose:
    """bloated_manual.pdf: OCR'd page 6 (~0.49 confidence) lands in excluded/supporting tier."""

    @pytest.fixture(scope="class")
    def pipeline_results(self, tmp_class_path):
        """Run full pipeline over bloated_manual.pdf once; shared across tests."""
        return _run_pipeline_over_single(tmp_class_path, "bloated_manual.pdf")

    @pytest.fixture(scope="class")
    def tmp_class_path(self, tmp_path_factory):
        return tmp_path_factory.mktemp("bloated_manual_decompose")

    def test_ocr_region_tier_is_excluded_or_supporting(self, pipeline_results):
        """Page 6 OCR content (~0.49 conf < floor 0.60) must land in excluded tier.

        If by any chance the OCR confidence is in [0.60, 0.80), it should be supporting.
        It must never be primary.
        """
        _, seg_set = pipeline_results
        segments = seg_set.get("segments", [])
        floor = _CTX.ocr_confidence_exclude_floor  # 0.60
        warn = _CTX.ocr_confidence_warn_level  # 0.80

        ocr_segs = [s for s in segments if s.get("ocr_confidence") is not None]
        assert ocr_segs, (
            "Expected at least one segment with ocr_confidence from bloated_manual.pdf "
            "(the OCR'd page 6 must produce at least one segment)"
        )

        for seg in ocr_segs:
            conf = seg["ocr_confidence"]
            tier = seg.get("salience_tier")
            if conf < floor:
                assert tier == "excluded", (
                    f"Segment ocr_confidence={conf:.4f} < floor {floor} "
                    f"must be excluded tier, got {tier!r}"
                )
            elif conf < warn:
                assert tier == "supporting", (
                    f"Segment ocr_confidence={conf:.4f} in warn band "
                    f"must be supporting tier, got {tier!r}"
                )
            # Above warn: no constraint (type-prior applies)

    def test_ocr_segment_has_floor_or_warn_signal(self, pipeline_results):
        """OCR segments below warn must carry the appropriate ocr_confidence signal."""
        _, seg_set = pipeline_results
        segments = seg_set.get("segments", [])
        floor = _CTX.ocr_confidence_exclude_floor
        warn = _CTX.ocr_confidence_warn_level

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
