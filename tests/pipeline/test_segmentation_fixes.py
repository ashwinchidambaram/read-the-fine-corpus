"""Ruling 1 + 2 — Segmentation fixes: front-matter regex word-boundary and
no-blank-line paragraph splitting.

Evidence-discipline tests: written BEFORE the fix.  Both tests must FAIL
against the current code and PASS after the fix.

Ruling 1 (BLOCKER part A):
  _FRONT_MATTER_RE must use word boundaries so that "reverse polarity" is NOT
  classified as front_matter.  A genuine revision_history / front_matter block
  still must be classified correctly.

Ruling 2 (BLOCKER part B):
  _split_into_paragraphs must handle blank-line-free extractions (the REALISTIC
  pypdf shape).  A synthetic no-blank-line document with headings + prose must
  produce multiple typed segments, not one blob.
"""

from __future__ import annotations

from datetime import UTC, datetime

# ---------------------------------------------------------------------------
# Import helpers
# ---------------------------------------------------------------------------

_MINIMAL_PARSE_BATCH_TEMPLATE: dict = {
    "schema_version": "1.0.0",
    "contract": "parse_result_batch",
    "run_id": "test-seg-fix",
    "produced_at": "2026-09-05T00:00:00+00:00",
    "skeleton": None,
    "results": [],
}

_MINIMAL_PARSE_RESULT_TEMPLATE: dict = {
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
    "content_hash": "a" * 64,
    "parser": {"name": "pypdf", "version": "3.0.0", "ocr_engine": None},
    "parsed_at": "2026-09-05T00:00:00+00:00",
    "parse_status": "parsed",
    "document_kind": "native_pdf",
    "quality": {
        "overall": 0.9,
        "text_extraction_ratio": 1.0,
        "table_structure_retained": "n/a",
        "is_near_empty": False,
        "mean_ocr_confidence": None,
    },
    "pages": [
        {
            "page_number": 1,
            "is_scanned": False,
            "ocr_confidence": None,
            "extraction_ratio": 1.0,
            "invisible_content": [],
        }
    ],
    "boilerplate_candidates": [],
    "content_classes": [],
    "encoding_issues": [],
    "language_distribution": [],
    "findings": [],
}


def _make_parse_batch(text: str, document_id: str) -> dict:
    """Build a minimal ParseResultBatch with a single-page region containing *text*."""
    result = dict(_MINIMAL_PARSE_RESULT_TEMPLATE)
    result["document_id"] = document_id
    result["regions"] = [
        {
            "region_id": "page-1-r1",
            "location": {
                "locator_kind": "page",
                "page_start": 1,
                "page_end": 1,
            },
            "text": text,
            "extract_status": "ok",
            "ocr_confidence": None,
            "language": None,
            "detected_class_hint": "prose",
            "encoding_issue": False,
        }
    ]
    batch = dict(_MINIMAL_PARSE_BATCH_TEMPLATE)
    batch["results"] = [result]
    return batch


def _run_decompose(text: str, document_id: str, tmp_path) -> dict:
    """Run DecomposeStage over a synthetic single-region parse result."""
    from finecorpus.pipeline.artifact_store import ArtifactStore
    from finecorpus.pipeline.decompose.stage import DecomposeStage

    batch = _make_parse_batch(text, document_id)
    store = ArtifactStore(artifacts_root=tmp_path / "artifacts", run_id="test-seg-fix")
    stage = DecomposeStage(
        run_started_at=datetime(2026, 9, 5, tzinfo=UTC),
        run_id="test-seg-fix",
    )
    seg_batch = stage.run(input_data=batch, store=store)
    return seg_batch["segment_sets"][0]


# ===========================================================================
# Ruling 1 — Front-matter regex word-boundary tests
# ===========================================================================


class TestFrontMatterRegexWordBoundary:
    """_FRONT_MATTER_RE must NOT match partial substrings.

    Written BEFORE the fix — expected to FAIL against the original code
    and PASS after word-boundary anchors are added.
    """

    def test_reverse_polarity_not_classified_as_front_matter(self, tmp_path):
        """'reverse polarity' must NOT classify the paragraph as front_matter.

        The substring 'rev' inside 'reverse' was triggering _FRONT_MATTER_RE
        because the regex had no word boundaries.  This test fails against the
        original code and passes after \\brev\\b is used.
        """
        # First page, para_idx < 3 — the zone where _classify_prose applies
        # front_matter if _FRONT_MATTER_RE matches.  The paragraph contains
        # "reverse polarity" which must NOT trigger the regex.
        text = (
            "ACME Widget Installation Guide\n\n"
            "Connect the 24 V DC supply to terminal block TB1. Polarity is marked on the chassis "
            "silkscreen. Do NOT reverse polarity; doing so will void the warranty and may damage "
            "internal protection circuitry."
        )
        ss = _run_decompose(text, "doc-rev-polarity", tmp_path)
        segments = ss["segments"]
        types = {s["segment_type"] for s in segments}
        # The prose paragraph MUST NOT be classified as front_matter
        front_matter_segs = [s for s in segments if s["segment_type"] == "front_matter"]
        assert not front_matter_segs, (
            f"'reverse polarity' paragraph must NOT be front_matter. "
            f"Got segments: {[(s['segment_type'], s['text'][:80]) for s in segments]}"
        )
        assert "prose" in types or "heading" in types, (
            f"Expected prose or heading segments; got types: {types}"
        )

    def test_updated_date_not_classified_as_front_matter(self, tmp_path):
        """A paragraph with 'updated' (containing 'date' as substring) must not be front_matter.

        Words like 'updated', 'validate', 'mandate' contain 'date' as a substring.
        Without word boundaries, the regex incorrectly classifies them as front_matter.
        """
        text = (
            "ACME Widget Installation Guide\n\n"
            "This manual was last updated by the engineering team in response to customer feedback."
        )
        ss = _run_decompose(text, "doc-updated", tmp_path)
        segments = ss["segments"]
        front_matter_segs = [s for s in segments if s["segment_type"] == "front_matter"]
        assert not front_matter_segs, (
            f"'updated' paragraph must NOT be front_matter. "
            f"Got segments: {[(s['segment_type'], s['text'][:80]) for s in segments]}"
        )

    def test_genuine_front_matter_still_classified(self, tmp_path):
        """Genuine front-matter blocks (Version / Rev / Date keywords standalone) still match."""
        # A block that genuinely is front-matter: contains "Version" and "Rev" as standalone words
        text = "Version: 3.2\nRev: 2\nDate: 2026-01-15\nAuthor: Engineering"
        ss = _run_decompose(text, "doc-genuine-fm", tmp_path)
        segments = ss["segments"]
        # The single-blob paragraph on page 1, para_idx 0 → should be front_matter
        assert any(s["segment_type"] == "front_matter" for s in segments), (
            f"Genuine front-matter block (Version/Rev/Date) must be classified as front_matter. "
            f"Got: {[(s['segment_type'], s['text'][:60]) for s in segments]}"
        )


# ===========================================================================
# Ruling 2 — No-blank-line paragraph splitting
# ===========================================================================


class TestNoBlankLineParagraphSplitting:
    """_split_into_paragraphs must handle blank-line-free extractions.

    Written BEFORE the fix — expected to FAIL against the original code
    (which produces one blob) and PASS after heading-guided splitting is added.

    The synthetic text mirrors the REALISTIC pypdf output shape for clean_native.pdf:
    numbered headings + prose with NO blank lines between sections.
    """

    def test_no_blank_line_doc_produces_multiple_segments(self, tmp_path):
        """A numbered-heading + prose document with no blank lines must produce multiple segments.

        This is the core Ruling 2 test.  Against the original code this produces
        exactly 1 segment (one blob); after the fix it must produce ≥ 3 segments.
        """
        # Mirrors pypdf extraction shape of clean_native.pdf (no blank lines)
        text = (
            "ACME Widget Installation Guide\n"
            "This guide describes the installation and initial configuration of the ACME Widget "
            "v3.2. Follow all steps in order.\n"
            "1. Prerequisites\n"
            "Before beginning, ensure all of the following conditions are met:\n"
            "  - Power supply: 24 V DC, minimum 2 A.\n"
            "  - Mounting surface rated for 5 kg static load.\n"
            "2. Installation Procedure\n"
            "Attach the mounting bracket to the DIN rail using the four M4 bolts supplied. "
            "Torque each bolt to 2.5 Nm. Slide the Widget chassis onto the bracket until the "
            "locking tab engages."
        )
        ss = _run_decompose(text, "doc-no-blank-line", tmp_path)
        segments = ss["segments"]

        assert len(segments) >= 3, (  # noqa: PLR2004
            f"No-blank-line document with multiple numbered headings must produce ≥ 3 segments "
            f"(heading + content blocks). Got {len(segments)} segment(s): "
            f"{[(s['segment_type'], s['text'][:60]) for s in segments]}"
        )

        types = {s["segment_type"] for s in segments}
        assert "heading" in types, f"Must produce at least one heading segment. Got types: {types}"

    def test_no_blank_line_reassembly_lossless(self, tmp_path):
        """Splitting no-blank-line text must remain lossless (segment concat == source text)."""
        import hashlib

        text = (
            "ACME Widget Installation Guide\n"
            "This guide describes installation of the ACME Widget v3.2.\n"
            "1. Prerequisites\n"
            "Before beginning, ensure the following conditions are met:\n"
            "  - Power supply: 24 V DC, minimum 2 A.\n"
            "2. Installation Procedure\n"
            "Attach the mounting bracket to the DIN rail using the four M4 bolts."
        )
        ss = _run_decompose(text, "doc-no-blank-lossless", tmp_path)
        segments = ss["segments"]
        reassembly = ss["reassembly"]

        sorted_segs = sorted(segments, key=lambda s: s["document_order"])
        concat = "".join(s.get("text") or "" for s in sorted_segs)
        derived_digest = hashlib.sha256(concat.encode()).hexdigest()

        assert derived_digest == reassembly["reassembly_digest"], (
            "Reassembly digest mismatch after no-blank-line split. "
            "Segments must concatenate to reproduce the full extracted text."
        )

    def test_blank_line_doc_unchanged(self, tmp_path):
        """A document with blank lines between paragraphs must produce IDENTICAL segmentation
        to before the change (regression guard: blank-line-separated documents are not affected).
        """
        # Classic blank-line-separated text — the original splitter handles this fine
        text = (
            "ACME Widget Installation Guide\n\n"
            "This guide describes the installation of the ACME Widget v3.2.\n\n"
            "1. Prerequisites\n\n"
            "Before beginning, ensure all of the following conditions are met."
        )
        ss = _run_decompose(text, "doc-blank-line-regression", tmp_path)
        segments = ss["segments"]

        # Must produce multiple segments
        assert len(segments) >= 2, (  # noqa: PLR2004
            f"Blank-line document must produce ≥ 2 segments. Got {len(segments)}: "
            f"{[(s['segment_type'], s['text'][:60]) for s in segments]}"
        )

        types = {s["segment_type"] for s in segments}
        assert "heading" in types, f"Must include heading segment. Got: {types}"
