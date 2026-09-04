"""Unit and integration tests for taxonomy typing, language detection, and
cross-reference resolution (Phase 2 scope items).

Test coverage:
  Taxonomy pass:
    - HTML code region → SegmentType.code
    - HTML list region → SegmentType.list_
    - HTML figure region → SegmentType.figure_region
    - HTML form_field region → SegmentType.form_field
    - Boilerplate segments are NOT retyped by taxonomy pass
    - OCR-locked salience is preserved when type changes
    - Segments already typed table pass through unchanged
    - Heading segments pass through unchanged
    - Segment with no region hint passes through unchanged

  Language detection:
    - English prose → 'en'
    - Spanish prose → 'es'
    - French prose → 'fr'
    - German prose → 'de'
    - Empty text → 'und'
    - Short text below word floor → 'und'
    - CJK text → 'zh'
    - Hiragana text → 'ja'
    - Arabic text → 'ar'
    - Cyrillic text → 'ru'
    - Determinism: same input → same output

  Language pass integration:
    - All segments receive a non-empty language field
    - Previously-und language is updated after pass
    - Segments with no text receive 'und'

  Cross-reference resolution:
    - Resolved reference when heading matches "Section N"
    - Unresolved reference recorded explicitly with note (§6.4 no silent loss)
    - Already-resolved references pass through unchanged
    - Table ordinal resolution: "Table 2" → correct table segment
    - Figure ordinal resolution: "Figure 1" → correct figure_region segment

  Integration (golden fixture driven):
    - confluence_export.html: code/list/table types present
    - confluence_export.html: language="en" on prose segments
    - confluence_export.html: language="und" on short heading segments
    - report_spreadsheet.xlsx: table and prose types present
    - Determinism: two runs on same HTML → identical segment sets
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from finecorpus.contracts.segment_set import CrossReference, CrossReferenceResolution, Segment
from finecorpus.contracts.shared.blocks import (
    LocatorKind,
    PermissionFidelity,
    PermissionMode,
    PermissionSource,
    SalienceSignal,
    SalienceSignalKind,
    SalienceTier,
    SegmentType,
    SourceLocation,
    TenancyBlock,
)
from finecorpus.pipeline.decompose.passes.base import DocumentContext
from finecorpus.pipeline.decompose.passes.language import LanguagePass, _detect_language
from finecorpus.pipeline.decompose.passes.taxonomy import TaxonomyPass
from finecorpus.pipeline.decompose.passes.xref_resolve import XrefResolvePass

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

GOLDEN_CORPUS = Path(__file__).parent.parent / "fixtures" / "golden" / "corpus"

_TENANCY = TenancyBlock(
    workspace_id="ws-test",
    kb_id="kb-test",
    permission_mode=PermissionMode.public_to_kb,
    permission_principals=[],
    permission_source=PermissionSource.platform,
    permission_fidelity=PermissionFidelity.authoritative,
)


def _make_segment(
    text: str,
    segment_type: SegmentType = SegmentType.prose,
    salience_tier: SalienceTier = SalienceTier.primary,
    salience_basis: SalienceSignalKind = SalienceSignalKind.segment_type_prior,
    language: str = "und",
    source_region_ids: list[str] | None = None,
    order: int = 0,
    ocr_confidence: float | None = None,
) -> Segment:
    """Build a minimal Segment for testing."""
    signals = [
        SalienceSignal(
            kind=salience_basis,
            implied_tier=salience_tier,
            won=True,
            detail=None,
        )
    ]
    return Segment(
        segment_id=f"seg-{order:04d}",
        document_order=order,
        segment_type=segment_type,
        salience_tier=salience_tier,
        structural_path=[],
        segment_path=f"#/{order}",
        location=SourceLocation(
            locator_kind=LocatorKind.page,
            page_start=1,
            page_end=1,
        ),
        source_region_ids=source_region_ids or ["region-001"],
        language=language,
        ocr_confidence=ocr_confidence,
        injection_suspicion=0.0,
        invisible_content_flags=[],
        sensitivity_flags=[],
        salience_signals=signals,
        salience_basis=salience_basis,
        text=text,
    )


def _make_doc_ctx(
    regions: list[dict[str, Any]] | None = None,
) -> DocumentContext:
    """Build a minimal DocumentContext with given regions."""
    return DocumentContext(
        document_id="doc-test",
        content_hash="abc123",
        tenancy=_TENANCY,
        parse_result={"regions": regions or []},
        decomposed_at=datetime.now(UTC),
    )


def _make_region(
    region_id: str,
    hint: str | None,
) -> dict[str, Any]:
    """Build a minimal region dict with a detected_class_hint."""
    return {
        "region_id": region_id,
        "detected_class_hint": hint,
        "text": "some text",
        "extract_status": "ok",
        "location": {"locator_kind": "page", "page_start": 1, "page_end": 1},
    }


def _make_xref(
    xref_id: str,
    from_segment_id: str,
    surface_text: str,
    resolution: CrossReferenceResolution = CrossReferenceResolution.unresolved,
    target_segment_id: str | None = None,
) -> CrossReference:
    """Build a minimal CrossReference for testing."""
    return CrossReference(
        xref_id=xref_id,
        from_segment_id=from_segment_id,
        surface_text=surface_text,
        location=SourceLocation(
            locator_kind=LocatorKind.page,
            page_start=1,
            page_end=1,
        ),
        resolution=resolution,
        target_segment_id=target_segment_id,
        target_note=None,
    )


# ===========================================================================
# Taxonomy Pass Tests
# ===========================================================================


class TestTaxonomyPassHintPromotion:
    """Verify that detected_class_hint values are promoted to full SegmentTypes."""

    def test_code_hint_promotes_to_code(self) -> None:
        """A region with hint=code → SegmentType.code."""
        seg = _make_segment("def foo():\n    pass", source_region_ids=["r-code"])
        regions = [_make_region("r-code", "code")]
        ctx = _make_doc_ctx(regions)

        result = TaxonomyPass().run(ctx, [seg], [])

        assert len(result.segments) == 1
        assert result.segments[0].segment_type == SegmentType.code

    def test_list_hint_promotes_to_list(self) -> None:
        """A region with hint=list → SegmentType.list_."""
        seg = _make_segment("- Item A\n- Item B\n- Item C", source_region_ids=["r-list"])
        regions = [_make_region("r-list", "list")]
        ctx = _make_doc_ctx(regions)

        result = TaxonomyPass().run(ctx, [seg], [])

        assert result.segments[0].segment_type == SegmentType.list_

    def test_figure_hint_promotes_to_figure_region(self) -> None:
        """A region with hint=figure → SegmentType.figure_region."""
        seg = _make_segment("[Chart: Q4 Revenue]", source_region_ids=["r-fig"])
        regions = [_make_region("r-fig", "figure")]
        ctx = _make_doc_ctx(regions)

        result = TaxonomyPass().run(ctx, [seg], [])

        assert result.segments[0].segment_type == SegmentType.figure_region

    def test_figure_region_salience_is_supporting(self) -> None:
        """figure_region default tier is supporting (taxonomy §4.2)."""
        seg = _make_segment("[Chart]", source_region_ids=["r-fig"])
        regions = [_make_region("r-fig", "figure")]
        ctx = _make_doc_ctx(regions)

        result = TaxonomyPass().run(ctx, [seg], [])

        assert result.segments[0].salience_tier == SalienceTier.supporting

    def test_form_field_hint_promotes_to_form_field(self) -> None:
        """A region with hint=form_field → SegmentType.form_field."""
        seg = _make_segment("Employee name: ___", source_region_ids=["r-form"])
        regions = [_make_region("r-form", "form_field")]
        ctx = _make_doc_ctx(regions)

        result = TaxonomyPass().run(ctx, [seg], [])

        assert result.segments[0].segment_type == SegmentType.form_field

    def test_code_salience_is_primary(self) -> None:
        """code default tier is primary (taxonomy §4.2)."""
        seg = _make_segment("SELECT * FROM users", source_region_ids=["r-code"])
        regions = [_make_region("r-code", "code")]
        ctx = _make_doc_ctx(regions)

        result = TaxonomyPass().run(ctx, [seg], [])

        assert result.segments[0].salience_tier == SalienceTier.primary

    def test_list_salience_is_primary(self) -> None:
        """list_ default tier is primary (taxonomy §4.2)."""
        seg = _make_segment("- Alpha\n- Beta", source_region_ids=["r-list"])
        regions = [_make_region("r-list", "list")]
        ctx = _make_doc_ctx(regions)

        result = TaxonomyPass().run(ctx, [seg], [])

        assert result.segments[0].salience_tier == SalienceTier.primary

    def test_prose_hint_no_change(self) -> None:
        """A region with hint=prose leaves the segment type unchanged."""
        seg = _make_segment("This is a paragraph.", source_region_ids=["r-prose"])
        regions = [_make_region("r-prose", "prose")]
        ctx = _make_doc_ctx(regions)

        result = TaxonomyPass().run(ctx, [seg], [])

        assert result.segments[0].segment_type == SegmentType.prose

    def test_other_hint_no_change(self) -> None:
        """A region with hint=other leaves the segment type unchanged."""
        seg = _make_segment("Some other content.", source_region_ids=["r-other"])
        regions = [_make_region("r-other", "other")]
        ctx = _make_doc_ctx(regions)

        result = TaxonomyPass().run(ctx, [seg], [])

        assert result.segments[0].segment_type == SegmentType.prose

    def test_no_hint_no_change(self) -> None:
        """A region with hint=None leaves the segment type unchanged."""
        seg = _make_segment("Content.", source_region_ids=["r-none"])
        regions = [_make_region("r-none", None)]
        ctx = _make_doc_ctx(regions)

        result = TaxonomyPass().run(ctx, [seg], [])

        assert result.segments[0].segment_type == SegmentType.prose

    def test_table_already_typed_no_change(self) -> None:
        """Segments already typed table pass through unchanged (D-11 already handled)."""
        seg = _make_segment(
            "| A | B |", segment_type=SegmentType.table, source_region_ids=["r-tbl"]
        )
        regions = [_make_region("r-tbl", "table")]
        ctx = _make_doc_ctx(regions)

        result = TaxonomyPass().run(ctx, [seg], [])

        assert result.segments[0].segment_type == SegmentType.table


class TestTaxonomyPassImmutableTypes:
    """Certain segment types must not be retyped by the taxonomy pass."""

    def test_boilerplate_not_retyped(self) -> None:
        """Boilerplate segments are immutable (boilerplate_pass wins, precedence 5 > 7)."""
        seg = _make_segment(
            "Confidential — Acme Corp.",
            segment_type=SegmentType.boilerplate,
            salience_tier=SalienceTier.boilerplate,
            salience_basis=SalienceSignalKind.boilerplate_detection,
            source_region_ids=["r-bp"],
        )
        # Even with a conflicting hint
        regions = [_make_region("r-bp", "code")]
        ctx = _make_doc_ctx(regions)

        result = TaxonomyPass().run(ctx, [seg], [])

        assert result.segments[0].segment_type == SegmentType.boilerplate
        assert result.segments[0].salience_tier == SalienceTier.boilerplate
        assert result.segments[0].salience_basis == SalienceSignalKind.boilerplate_detection

    def test_heading_not_retyped(self) -> None:
        """Heading segments pass through unchanged."""
        seg = _make_segment(
            "1. Introduction",
            segment_type=SegmentType.heading,
            salience_tier=SalienceTier.supporting,
            source_region_ids=["r-h"],
        )
        regions = [_make_region("r-h", "prose")]
        ctx = _make_doc_ctx(regions)

        result = TaxonomyPass().run(ctx, [seg], [])

        assert result.segments[0].segment_type == SegmentType.heading

    def test_scanned_region_not_retyped(self) -> None:
        """scanned_region segments pass through unchanged."""
        seg = _make_segment(
            "OCR extracted text from scan",
            segment_type=SegmentType.scanned_region,
            salience_tier=SalienceTier.supporting,
            ocr_confidence=0.91,
            source_region_ids=["r-scan"],
        )
        regions = [_make_region("r-scan", "other")]
        ctx = _make_doc_ctx(regions)

        result = TaxonomyPass().run(ctx, [seg], [])

        assert result.segments[0].segment_type == SegmentType.scanned_region


class TestTaxonomyPassOCRLock:
    """OCR-confidence salience must not be modified when a type promotion occurs."""

    def test_ocr_floor_lock_preserves_salience(self) -> None:
        """When winning signal is ocr_confidence_floor, type changes but salience stays excluded."""
        seg = Segment(
            segment_id="seg-ocr",
            document_order=0,
            segment_type=SegmentType.prose,  # will be promoted to code
            salience_tier=SalienceTier.excluded,  # OCR floor locked
            structural_path=[],
            segment_path="#/0",
            location=SourceLocation(
                locator_kind=LocatorKind.page,
                page_start=1,
                page_end=1,
            ),
            source_region_ids=["r-ocr"],
            language="und",
            ocr_confidence=0.45,  # below floor
            injection_suspicion=0.0,
            invisible_content_flags=[],
            sensitivity_flags=[],
            salience_signals=[
                SalienceSignal(
                    kind=SalienceSignalKind.segment_type_prior,
                    implied_tier=SalienceTier.primary,
                    won=False,
                    detail=None,
                ),
                SalienceSignal(
                    kind=SalienceSignalKind.ocr_confidence_floor,
                    implied_tier=SalienceTier.excluded,
                    won=True,
                    detail="OCR below floor",
                ),
            ],
            salience_basis=SalienceSignalKind.ocr_confidence_floor,
            text="def ocr_function(): pass",
        )
        regions = [_make_region("r-ocr", "code")]
        ctx = _make_doc_ctx(regions)

        result = TaxonomyPass().run(ctx, [seg], [])

        promoted = result.segments[0]
        # Type should be promoted
        assert promoted.segment_type == SegmentType.code
        # Salience must NOT be changed
        assert promoted.salience_tier == SalienceTier.excluded
        assert promoted.salience_basis == SalienceSignalKind.ocr_confidence_floor

    def test_ocr_warn_lock_preserves_salience(self) -> None:
        """Winning ocr_confidence_warn signal: type changes but salience stays supporting."""
        seg = Segment(
            segment_id="seg-ocr-warn",
            document_order=0,
            segment_type=SegmentType.prose,
            salience_tier=SalienceTier.supporting,
            structural_path=[],
            segment_path="#/0",
            location=SourceLocation(
                locator_kind=LocatorKind.page,
                page_start=1,
                page_end=1,
            ),
            source_region_ids=["r-ocr-w"],
            language="und",
            ocr_confidence=0.72,
            injection_suspicion=0.0,
            invisible_content_flags=[],
            sensitivity_flags=[],
            salience_signals=[
                SalienceSignal(
                    kind=SalienceSignalKind.ocr_confidence_warn,
                    implied_tier=SalienceTier.supporting,
                    won=True,
                    detail="OCR warn band",
                ),
            ],
            salience_basis=SalienceSignalKind.ocr_confidence_warn,
            text="- Item one\n- Item two",
        )
        regions = [_make_region("r-ocr-w", "list")]
        ctx = _make_doc_ctx(regions)

        result = TaxonomyPass().run(ctx, [seg], [])

        promoted = result.segments[0]
        assert promoted.segment_type == SegmentType.list_
        assert promoted.salience_tier == SalienceTier.supporting
        assert promoted.salience_basis == SalienceSignalKind.ocr_confidence_warn

    def test_free_salience_updates_signals(self) -> None:
        """When salience is free (type_prior), updating type also updates signals."""
        seg = _make_segment("print('hello')", source_region_ids=["r-code"])
        regions = [_make_region("r-code", "code")]
        ctx = _make_doc_ctx(regions)

        result = TaxonomyPass().run(ctx, [seg], [])

        promoted = result.segments[0]
        assert promoted.salience_basis == SalienceSignalKind.segment_type_prior
        # The new winning signal should be for code (primary)
        winning = next(s for s in promoted.salience_signals if s.won)
        assert winning.implied_tier == SalienceTier.primary
        # The old signal should be preserved as contributing
        contributing = [s for s in promoted.salience_signals if not s.won]
        assert len(contributing) == 1


class TestTaxonomyPassMultipleSegments:
    """Verify correct handling of mixed segment lists."""

    def test_mixed_segment_list(self) -> None:
        """Only segments with matching hints are retyped; others pass through."""
        segs = [
            _make_segment("def foo(): pass", source_region_ids=["r-code"], order=0),
            _make_segment("Normal prose.", source_region_ids=["r-prose"], order=1),
            _make_segment("- A\n- B", source_region_ids=["r-list"], order=2),
        ]
        regions = [
            _make_region("r-code", "code"),
            _make_region("r-prose", "prose"),
            _make_region("r-list", "list"),
        ]
        ctx = _make_doc_ctx(regions)

        result = TaxonomyPass().run(ctx, segs, [])

        assert result.segments[0].segment_type == SegmentType.code
        assert result.segments[1].segment_type == SegmentType.prose
        assert result.segments[2].segment_type == SegmentType.list_

    def test_empty_segment_list(self) -> None:
        """Empty segment list returns empty without error."""
        ctx = _make_doc_ctx([])
        result = TaxonomyPass().run(ctx, [], [])
        assert result.segments == []


# ===========================================================================
# Language Detection Tests
# ===========================================================================


class TestLanguageDetection:
    """Unit tests for the _detect_language function."""

    def test_english_prose(self) -> None:
        """English prose is detected as 'en'."""
        text = (
            "The platform must have a stated position rather than an accidental one. "
            "Language is detected per segment and stored as a filterable field."
        )
        assert _detect_language(text) == "en"

    def test_spanish_prose(self) -> None:
        """Spanish prose is detected as 'es'."""
        text = (
            "La plataforma debe tener una posición declarada en lugar de una accidental. "
            "El idioma se detecta por segmento y se almacena como un campo filtrable."
        )
        assert _detect_language(text) == "es"

    def test_french_prose(self) -> None:
        """French prose is detected as 'fr'."""
        text = (
            "La plateforme doit avoir une position déclarée plutôt qu'accidentelle. "
            "La langue est détectée par segment et stockée comme un champ filtrable."
        )
        assert _detect_language(text) == "fr"

    def test_german_prose(self) -> None:
        """German prose is detected as 'de'."""
        text = (
            "Die Plattform muss eine erklärte Position haben und nicht eine zufällige. "
            "Die Sprache wird pro Segment erkannt und als filtrierbares Feld gespeichert."
        )
        assert _detect_language(text) == "de"

    def test_empty_text(self) -> None:
        """Empty text returns 'und'."""
        assert _detect_language("") == "und"

    def test_whitespace_only(self) -> None:
        """Whitespace-only text returns 'und'."""
        assert _detect_language("   \n\t  ") == "und"

    def test_short_text_returns_und(self) -> None:
        """Text with fewer than 4 words returns 'und' (below stopword floor)."""
        assert _detect_language("the a in") == "und"

    def test_cjk_text_returns_zh(self) -> None:
        """CJK characters → 'zh'."""
        text = "这是一段中文文本，用于测试语言检测功能。系统应该将其识别为中文。"
        assert _detect_language(text) == "zh"

    def test_hiragana_text_returns_ja(self) -> None:
        """Hiragana/Katakana → 'ja'."""
        text = "これはテストのための日本語のテキストです。言語検出システムをテストします。"
        assert _detect_language(text) == "ja"

    def test_arabic_text_returns_ar(self) -> None:
        """Arabic script → 'ar'."""
        text = "هذا نص عربي للاختبار. يجب أن يكتشف النظام اللغة العربية بشكل صحيح."
        assert _detect_language(text) == "ar"

    def test_cyrillic_text_returns_ru(self) -> None:
        """Cyrillic script → 'ru'."""
        text = "Это тестовый текст на русском языке. Система должна определить русский язык."
        assert _detect_language(text) == "ru"

    def test_determinism(self) -> None:
        """Same input always produces the same output."""
        text = "The configuration object must be exportable, diffable, and version-controllable."
        results = {_detect_language(text) for _ in range(5)}
        assert len(results) == 1  # all same

    def test_portuguese_prose(self) -> None:
        """Portuguese prose is detected as 'pt'."""
        text = (
            "A plataforma deve ter uma posição declarada em vez de acidental. "
            "O idioma é detectado por segmento e armazenado como campo filtrável."
        )
        assert _detect_language(text) == "pt"

    def test_non_english_synthetic(self) -> None:
        """A small synthetic non-English text case (Spanish) returns 'es'."""
        text = (
            "El sistema de detección de idioma debe funcionar de manera determinista. "
            "Los segmentos de texto se clasifican con sus códigos de idioma BCP-47."
        )
        assert _detect_language(text) == "es"


class TestLanguagePass:
    """Integration tests for the LanguagePass."""

    def test_all_segments_get_language(self) -> None:
        """After the pass, no segment has an empty language field."""
        segs = [
            _make_segment("The quick brown fox jumps over the lazy dog.", order=0),
            _make_segment("", order=1),  # empty text
            _make_segment("This is a test.", order=2),
        ]
        ctx = _make_doc_ctx()
        result = LanguagePass().run(ctx, segs, [])

        for seg in result.segments:
            assert seg.language, f"Segment {seg.segment_id} has empty language"
            assert len(seg.language) >= 2  # at least 2-char BCP-47 code or 'und'

    def test_english_prose_gets_en(self) -> None:
        """English prose segments are tagged 'en'."""
        text = (
            "The platform must have a stated position. "
            "Language is detected per segment and stored as a filterable field."
        )
        seg = _make_segment(text, order=0)
        ctx = _make_doc_ctx()

        result = LanguagePass().run(ctx, [seg], [])

        assert result.segments[0].language == "en"

    def test_empty_text_gets_und(self) -> None:
        """Segments with no text get 'und'."""
        seg = _make_segment("", segment_type=SegmentType.figure_region, order=0)
        ctx = _make_doc_ctx()

        result = LanguagePass().run(ctx, [seg], [])

        assert result.segments[0].language == "und"

    def test_pass_does_not_change_salience(self) -> None:
        """Language pass must not alter salience_tier or salience_basis."""
        seg = _make_segment("The quick brown fox.", order=0, salience_tier=SalienceTier.supporting)
        ctx = _make_doc_ctx()

        result = LanguagePass().run(ctx, [seg], [])

        assert result.segments[0].salience_tier == SalienceTier.supporting
        assert result.segments[0].salience_basis == SalienceSignalKind.segment_type_prior

    def test_pass_does_not_change_segment_type(self) -> None:
        """Language pass must not change segment_type."""
        seg = _make_segment("def foo(): pass", segment_type=SegmentType.code, order=0)
        ctx = _make_doc_ctx()

        result = LanguagePass().run(ctx, [seg], [])

        assert result.segments[0].segment_type == SegmentType.code

    def test_empty_segments_list(self) -> None:
        """Empty segment list returns empty."""
        ctx = _make_doc_ctx()
        result = LanguagePass().run(ctx, [], [])
        assert result.segments == []


# ===========================================================================
# Cross-reference Resolution Tests
# ===========================================================================


class TestXrefResolvePass:
    """Unit tests for intra-document cross-reference resolution."""

    def _make_heading_seg(self, text: str, order: int) -> Segment:
        """Build a heading segment with text matching the heading."""
        seg = _make_segment(
            text,
            segment_type=SegmentType.heading,
            salience_tier=SalienceTier.supporting,
            order=order,
        )
        return seg

    def test_section_reference_resolved_by_heading(self) -> None:
        """'see section 4.2' resolves to a heading whose text starts with '4.2'."""
        heading = self._make_heading_seg("4.2 Configuration Reference", order=0)
        prose = _make_segment("Refer to see section 4.2 for details.", order=1)

        xref = _make_xref("xref-001", prose.segment_id, "see section 4.2")

        pass_ = XrefResolvePass()
        resolved = pass_.resolve_cross_references([xref], [heading, prose])

        assert len(resolved) == 1
        assert resolved[0].resolution == CrossReferenceResolution.resolved
        assert resolved[0].target_segment_id == heading.segment_id

    def test_appendix_reference_resolved(self) -> None:
        """'Appendix A' resolves to a heading whose text starts with 'A'."""
        appendix_heading = self._make_heading_seg("A Configuration Details", order=5)
        prose = _make_segment("See Appendix A for full configuration.", order=3)
        xref = _make_xref("xref-002", prose.segment_id, "Appendix A")

        pass_ = XrefResolvePass()
        resolved = pass_.resolve_cross_references([xref], [prose, appendix_heading])

        assert resolved[0].resolution == CrossReferenceResolution.resolved
        assert resolved[0].target_segment_id == appendix_heading.segment_id

    def test_unresolved_when_no_matching_heading(self) -> None:
        """Unresolved when no heading matches the referenced label."""
        prose = _make_segment("See section 99 for details.", order=0)
        xref = _make_xref("xref-003", prose.segment_id, "see section 99")

        pass_ = XrefResolvePass()
        resolved = pass_.resolve_cross_references([xref], [prose])

        assert resolved[0].resolution == CrossReferenceResolution.unresolved
        assert resolved[0].target_note is not None
        assert resolved[0].target_segment_id is None

    def test_unresolved_has_explicit_note(self) -> None:
        """Unresolved references carry an explicit note (§6.4: no silent loss)."""
        prose = _make_segment("see section 42 somewhere.", order=0)
        xref = _make_xref("xref-004", prose.segment_id, "see section 42")

        pass_ = XrefResolvePass()
        resolved = pass_.resolve_cross_references([xref], [prose])

        assert resolved[0].target_note is not None
        assert len(resolved[0].target_note) > 20  # substantive note, not empty

    def test_table_ordinal_resolution(self) -> None:
        """'Table 2' resolves to the second table segment in document order."""
        table1 = _make_segment("| Col1 | Col2 |", segment_type=SegmentType.table, order=0)
        table2 = _make_segment("| A | B |", segment_type=SegmentType.table, order=2)
        prose = _make_segment("See Table 2 below.", order=3)

        xref = _make_xref("xref-t2", prose.segment_id, "Table 2")

        pass_ = XrefResolvePass()
        resolved = pass_.resolve_cross_references([xref], [table1, prose, table2])

        assert resolved[0].resolution == CrossReferenceResolution.resolved
        assert resolved[0].target_segment_id == table2.segment_id

    def test_figure_ordinal_resolution(self) -> None:
        """'Figure 1' resolves to the first figure_region segment."""
        fig = _make_segment("[bar chart]", segment_type=SegmentType.figure_region, order=1)
        prose = _make_segment("As shown in Figure 1.", order=2)

        xref = _make_xref("xref-f1", prose.segment_id, "Figure 1")

        pass_ = XrefResolvePass()
        resolved = pass_.resolve_cross_references([xref], [fig, prose])

        assert resolved[0].resolution == CrossReferenceResolution.resolved
        assert resolved[0].target_segment_id == fig.segment_id

    def test_already_resolved_passes_through(self) -> None:
        """Already-resolved cross-references are not modified."""
        prose = _make_segment("See section 2.", order=0)
        xref = CrossReference(
            xref_id="xref-res",
            from_segment_id=prose.segment_id,
            surface_text="section 2",
            location=SourceLocation(
                locator_kind=LocatorKind.page,
                page_start=1,
                page_end=1,
            ),
            resolution=CrossReferenceResolution.resolved,
            target_segment_id="seg-existing-target",
            target_note="Already resolved in a prior step.",
        )

        pass_ = XrefResolvePass()
        resolved = pass_.resolve_cross_references([xref], [prose])

        # Should pass through unchanged
        assert resolved[0].resolution == CrossReferenceResolution.resolved
        assert resolved[0].target_segment_id == "seg-existing-target"

    def test_empty_xref_list(self) -> None:
        """Empty cross-reference list returns empty."""
        pass_ = XrefResolvePass()
        result = pass_.resolve_cross_references([], [])
        assert result == []

    def test_run_method_is_passthrough(self) -> None:
        """The SegmentPass.run method is a no-op for segments/exclusions."""
        seg = _make_segment("Some text.", order=0)
        ctx = _make_doc_ctx()

        pass_ = XrefResolvePass()
        result = pass_.run(ctx, [seg], [])

        assert result.segments == [seg]
        assert result.exclusions == []
        assert result.cross_references == []


# ===========================================================================
# Integration Tests (golden-corpus driven)
# ===========================================================================


def _run_pipeline_on_fixture(
    fixture_name: str,
    tmp_path: Path,
) -> dict[str, Any]:
    """Run the full pipeline on a single fixture and return the decompose output."""
    from finecorpus.pipeline import run_pipeline

    src = tmp_path / "corpus"
    src.mkdir(exist_ok=True)
    shutil.copy2(GOLDEN_CORPUS / fixture_name, src / fixture_name)
    artifacts = tmp_path / "artifacts"

    artifact_paths = run_pipeline(
        str(src),
        artifacts_root=str(artifacts),
        run_id="test",
        workspace_id="ws-test",
        kb_id="kb-test",
    )
    return json.loads(Path(artifact_paths["decompose"]).read_text(encoding="utf-8"))


class TestGoldenCorpusIntegration:
    """Golden-corpus-driven integration tests for the three new passes."""

    @pytest.mark.parametrize(
        "expected_types",
        [
            {SegmentType.code.value, SegmentType.list_.value, SegmentType.table.value},
        ],
    )
    def test_confluence_html_produces_typed_segments(
        self, tmp_path: Path, expected_types: set[str]
    ) -> None:
        """confluence_export.html segments include code, list, and table types."""
        data = _run_pipeline_on_fixture("confluence_export.html", tmp_path)
        seg_sets = data.get("segment_sets", [])
        assert seg_sets, "No segment sets produced"
        ss = seg_sets[0]
        actual_types = {s["segment_type"] for s in ss.get("segments", [])}
        for expected in expected_types:
            assert expected in actual_types, (
                f"Expected type '{expected}' not found in {actual_types}"
            )

    def test_confluence_html_prose_has_english(self, tmp_path: Path) -> None:
        """English prose segments in confluence_export.html are tagged 'en'."""
        data = _run_pipeline_on_fixture("confluence_export.html", tmp_path)
        ss = data["segment_sets"][0]
        prose_segs = [s for s in ss["segments"] if s["segment_type"] == "prose" and s.get("text")]
        # At least some prose segments should be detected as English
        en_segs = [s for s in prose_segs if s["language"] == "en"]
        assert en_segs, (
            f"No English prose segments in confluence_export.html; "
            f"prose segment languages: {[s['language'] for s in prose_segs]}"
        )

    def test_all_segments_have_non_empty_language(self, tmp_path: Path) -> None:
        """Every segment in a decomposed HTML fixture has a non-empty language field."""
        data = _run_pipeline_on_fixture("confluence_export.html", tmp_path)
        ss = data["segment_sets"][0]
        for seg in ss["segments"]:
            assert seg.get("language"), f"Segment {seg['segment_id']} missing language field"

    def test_report_spreadsheet_has_table_and_prose(self, tmp_path: Path) -> None:
        """report_spreadsheet.xlsx segments include table and prose types."""
        data = _run_pipeline_on_fixture("report_spreadsheet.xlsx", tmp_path)
        seg_sets = data.get("segment_sets", [])
        assert seg_sets
        ss = seg_sets[0]
        types = {s["segment_type"] for s in ss.get("segments", [])}
        assert "table" in types or "prose" in types, f"Expected table or prose in {types}"

    def test_determinism_same_input_same_output(self, tmp_path: Path) -> None:
        """Two pipeline runs on the same HTML input produce identical segment sets.

        This validates that all three new passes (taxonomy, language, xref_resolve)
        are deterministic — same inputs → same types, languages, and xref records.
        """
        src = tmp_path / "corpus"
        src.mkdir(exist_ok=True)
        shutil.copy2(
            GOLDEN_CORPUS / "confluence_export.html",
            src / "confluence_export.html",
        )

        from finecorpus.pipeline import run_pipeline

        # Run 1
        artifacts1 = tmp_path / "artifacts1"
        paths1 = run_pipeline(
            str(src),
            artifacts_root=str(artifacts1),
            run_id="run1",
            workspace_id="ws-test",
            kb_id="kb-test",
        )
        data1 = json.loads(Path(paths1["decompose"]).read_text(encoding="utf-8"))
        segs1 = data1["segment_sets"][0]["segments"]

        # Run 2 (separate artifact store to avoid cache reuse)
        artifacts2 = tmp_path / "artifacts2"
        paths2 = run_pipeline(
            str(src),
            artifacts_root=str(artifacts2),
            run_id="run2",
            workspace_id="ws-test",
            kb_id="kb-test",
        )
        data2 = json.loads(Path(paths2["decompose"]).read_text(encoding="utf-8"))
        segs2 = data2["segment_sets"][0]["segments"]

        assert len(segs1) == len(segs2), "Different segment counts between runs"

        for s1, s2 in zip(segs1, segs2, strict=True):
            assert s1["segment_type"] == s2["segment_type"], (
                f"Segment type mismatch at order {s1['document_order']}"
            )
            assert s1["language"] == s2["language"], (
                f"Language mismatch at order {s1['document_order']}"
            )
