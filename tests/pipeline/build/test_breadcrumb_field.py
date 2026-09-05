"""Unit tests for M-097 — parent_breadcrumb augmentation field.

Module: src/finecorpus/pipeline/build/augment.py

Tests:
- parent_breadcrumb is populated for heading-structured segments.
- parent_breadcrumb is None for flat documents (empty structural_path).
- Content matches heading ancestry order (root → leaf).
- Field is produced only when breadcrumb_augment is in tier2_operations.
- Works correctly through augment_chunk and compose_embedding_input.
"""

from __future__ import annotations

import uuid

import pytest

from finecorpus.contracts.ingestion_config import (
    ChunkingConfig,
    ChunkingStrategy,
    ClassRule,
    RetrievalStrategy,
    RetrievalTreatment,
    Tier1Operation,
    Tier2Operation,
    TransformationSettings,
)
from finecorpus.contracts.segment_set import Segment
from finecorpus.contracts.shared.blocks import (
    LocatorKind,
    SalienceSignal,
    SalienceSignalKind,
    SalienceTier,
    SegmentType,
    SourceLocation,
)
from finecorpus.pipeline.build.augment import (
    _derive_parent_breadcrumb,
    augment_chunk,
    compose_embedding_input,
)
from finecorpus.pipeline.build.chunker import ChunkSpan

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_span(text: str = "Some body text.") -> ChunkSpan:
    return ChunkSpan(
        text=text,
        chunk_index=0,
        char_start=0,
        char_end=len(text),
        token_count=len(text.split()),
    )


def _make_segment(
    structural_path: list[str],
    text: str = "Some body text.",
    segment_type: SegmentType = SegmentType.prose,
) -> Segment:
    return Segment(
        segment_id=str(uuid.uuid4()),
        document_order=0,
        segment_type=segment_type,
        salience_tier=SalienceTier.primary,
        structural_path=structural_path,
        segment_path="seg/0",
        location=SourceLocation(
            locator_kind=LocatorKind.char_range,
            char_start=0,
            char_end=len(text),
        ),
        source_region_ids=["region_0"],
        language="en",
        ocr_confidence=None,
        injection_suspicion=0.0,
        invisible_content_flags=[],
        sensitivity_flags=[],
        salience_signals=[
            SalienceSignal(
                kind=SalienceSignalKind.segment_type_prior,
                implied_tier=SalienceTier.primary,
                won=True,
                detail=None,
            )
        ],
        salience_basis=SalienceSignalKind.segment_type_prior,
        text=text,
    )


def _make_rule(ops: list[Tier2Operation]) -> ClassRule:
    return ClassRule(
        segment_class=SegmentType.prose,
        transformation=TransformationSettings(
            tier1_enabled=True,
            tier1_operations=[Tier1Operation.whitespace_repair],
            tier2_enabled=True,
            tier2_operations=ops,
            tier3_enabled=False,
        ),
        chunking=ChunkingConfig(
            strategy=ChunkingStrategy.recursive_char,
            max_tokens=512,
            overlap_tokens=50,
            respect_headings=False,
        ),
        embedding_override=None,
        metadata_schema=[],
        retrieval_treatment=RetrievalTreatment(
            default_salience_filter=[SalienceTier.primary, SalienceTier.supporting],
            salience_weights=None,
            rerank_eligible=False,
            strategy=RetrievalStrategy.dense,
        ),
    )


# ---------------------------------------------------------------------------
# _derive_parent_breadcrumb unit tests (pure helper)
# ---------------------------------------------------------------------------


class TestDeriveBreadcrumb:
    def test_empty_path_returns_none(self):
        assert _derive_parent_breadcrumb([]) is None

    def test_single_heading(self):
        result = _derive_parent_breadcrumb(["Introduction"])
        assert result == "Introduction"

    def test_two_level_path(self):
        result = _derive_parent_breadcrumb(["Chapter 1", "Section 1.1"])
        assert result == "Chapter 1 > Section 1.1"

    def test_three_level_path(self):
        result = _derive_parent_breadcrumb(["Part I", "Chapter 3", "Background"])
        assert result == "Part I > Chapter 3 > Background"

    def test_order_preserved(self):
        path = ["Root", "A", "B", "C"]
        result = _derive_parent_breadcrumb(path)
        # Must be in document order: root first
        assert result == "Root > A > B > C"
        # Not reversed
        assert "C > B" not in result

    def test_path_with_special_chars(self):
        result = _derive_parent_breadcrumb(["§1 Introduction", "§1.2 Scope & Purpose"])
        assert "§1 Introduction" in result
        assert "§1.2 Scope & Purpose" in result


# ---------------------------------------------------------------------------
# augment_chunk — breadcrumb populated for heading-structured documents
# ---------------------------------------------------------------------------


class TestBreadcrumbInAugmentChunk:
    def test_heading_structured_breadcrumb_populated(self):
        span = _make_span()
        segment = _make_segment(structural_path=["Chapter 1", "Section 1.1"])
        rule = _make_rule([Tier2Operation.breadcrumb_augment])

        aug, records = augment_chunk(span, segment, rule, None, None)

        assert aug.parent_breadcrumb is not None
        assert "Chapter 1" in aug.parent_breadcrumb
        assert "Section 1.1" in aug.parent_breadcrumb

    def test_flat_document_breadcrumb_is_none(self):
        span = _make_span()
        segment = _make_segment(structural_path=[])  # flat — no headings
        rule = _make_rule([Tier2Operation.breadcrumb_augment])

        aug, records = augment_chunk(span, segment, rule, None, None)

        assert aug.parent_breadcrumb is None

    def test_record_emitted_for_heading_segment(self):
        span = _make_span()
        segment = _make_segment(structural_path=["H1"])
        rule = _make_rule([Tier2Operation.breadcrumb_augment])

        _, records = augment_chunk(span, segment, rule, None, None)

        breadcrumb_records = [r for r in records if r.operation == "breadcrumb_augment"]
        assert len(breadcrumb_records) == 1

    def test_record_emitted_for_flat_document_too(self):
        """A record is still emitted for flat docs — it documents the attempted op."""
        span = _make_span()
        segment = _make_segment(structural_path=[])
        rule = _make_rule([Tier2Operation.breadcrumb_augment])

        _, records = augment_chunk(span, segment, rule, None, None)

        breadcrumb_records = [r for r in records if r.operation == "breadcrumb_augment"]
        assert len(breadcrumb_records) == 1
        assert breadcrumb_records[0].changed_text is False

    def test_breadcrumb_not_in_text_field(self):
        """Breadcrumb must NEVER appear in span.text (T-04)."""
        original_text = "Prose body without headings."
        span = _make_span(original_text)
        segment = _make_segment(structural_path=["Chapter 1 > Section 2"])
        rule = _make_rule([Tier2Operation.breadcrumb_augment])

        aug, _ = augment_chunk(span, segment, rule, None, None)

        # Span text must not contain the breadcrumb
        assert span.text == original_text
        # Breadcrumb is only in the augmentation object, not in text
        assert aug.parent_breadcrumb is not None
        assert aug.parent_breadcrumb not in span.text or span.text == aug.parent_breadcrumb

    def test_not_requested_not_populated(self):
        """If breadcrumb_augment is not in tier2_operations, the field is None."""
        span = _make_span()
        segment = _make_segment(structural_path=["Chapter 1"])
        rule = _make_rule([])  # no ops

        aug, _ = augment_chunk(span, segment, rule, None, None)

        assert aug.parent_breadcrumb is None


# ---------------------------------------------------------------------------
# augment_chunk — content matches heading ancestry order
# ---------------------------------------------------------------------------


class TestBreadcrumbContent:
    @pytest.mark.parametrize(
        "structural_path,expected_fragment",
        [
            (["Introduction"], "Introduction"),
            (["Part I", "Chapter 2"], "Part I > Chapter 2"),
            (["Part I", "Chapter 2", "Background"], "Part I > Chapter 2 > Background"),
        ],
    )
    def test_breadcrumb_matches_ancestry(self, structural_path, expected_fragment):
        span = _make_span()
        segment = _make_segment(structural_path=structural_path)
        rule = _make_rule([Tier2Operation.breadcrumb_augment])

        aug, _ = augment_chunk(span, segment, rule, None, None)

        assert aug.parent_breadcrumb == expected_fragment

    def test_deep_path(self):
        path = [f"Level {i}" for i in range(6)]
        span = _make_span()
        segment = _make_segment(structural_path=path)
        rule = _make_rule([Tier2Operation.breadcrumb_augment])

        aug, _ = augment_chunk(span, segment, rule, None, None)

        assert aug.parent_breadcrumb is not None
        # All levels present
        for level in path:
            assert level in aug.parent_breadcrumb
        # Root comes first
        assert aug.parent_breadcrumb.startswith("Level 0")


# ---------------------------------------------------------------------------
# compose_embedding_input — breadcrumb in embedding, not in text
# ---------------------------------------------------------------------------


class TestBreadcrumbInEmbeddingInput:
    def test_breadcrumb_in_embedding_input_not_in_text(self):
        text = "Body paragraph text."
        from finecorpus.pipeline.build.augment import Augmentation

        aug = Augmentation(parent_breadcrumb="Chapter 1 > Section 2")
        embedding_input, _ = compose_embedding_input(aug, text)

        assert "Chapter 1 > Section 2" in embedding_input
        # Original text still present verbatim
        assert text in embedding_input
        # Embedding input differs from chunk text
        assert embedding_input != text

    def test_no_breadcrumb_embedding_equals_text(self):
        text = "Flat document body text."
        from finecorpus.pipeline.build.augment import Augmentation

        aug = Augmentation(parent_breadcrumb=None)
        embedding_input, records = compose_embedding_input(aug, text)

        assert embedding_input == text
        assert records == []

    def test_breadcrumb_prefix_ordering(self):
        """The breadcrumb prefix must appear BEFORE the chunk text."""
        text = "The actual chunk content."
        from finecorpus.pipeline.build.augment import Augmentation

        aug = Augmentation(parent_breadcrumb="Root > Sub")
        embedding_input, _ = compose_embedding_input(aug, text)

        breadcrumb_pos = embedding_input.find("Root > Sub")
        text_pos = embedding_input.find(text)
        assert breadcrumb_pos < text_pos, "Breadcrumb must precede chunk text in embedding_input"
