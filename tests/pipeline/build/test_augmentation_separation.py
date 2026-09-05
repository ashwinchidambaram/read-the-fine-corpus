"""Unit tests for Tier 2 augmentation — T-04 byte-identity guarantee.

Module: src/finecorpus/pipeline/build/augment.py

Tests the core T-04 structural guarantee:
- augment_chunk CANNOT mutate span text (adversarial client + assert byte-unchanged).
- All tier-2 records have changed_text=False (enforced by Pydantic validator).
- compose_embedding_input differs from text only by the framed augmentation.
- Augmentation fields are independent (populated only when the op is in tier2_operations).
- StubAugmentationClient is deterministic.
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
    AppliedBy,
    LocatorKind,
    SalienceSignal,
    SalienceSignalKind,
    SalienceTier,
    SegmentType,
    SourceLocation,
    TransformationTier,
)
from finecorpus.pipeline.build.augment import (
    Augmentation,
    AugmentationClient,
    StubAugmentationClient,
    augment_chunk,
    compose_embedding_input,
)
from finecorpus.pipeline.build.chunker import ChunkSpan

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_span(text: str = "Some chunk text here.") -> ChunkSpan:
    return ChunkSpan(
        text=text,
        chunk_index=0,
        char_start=0,
        char_end=len(text),
        token_count=len(text.split()),
    )


def _make_segment(
    structural_path: list[str] | None = None,
    segment_type: SegmentType = SegmentType.prose,
) -> Segment:
    path = structural_path or []
    return Segment(
        segment_id=str(uuid.uuid4()),
        document_order=0,
        segment_type=segment_type,
        salience_tier=SalienceTier.primary,
        structural_path=path,
        segment_path="seg/0",
        location=SourceLocation(
            locator_kind=LocatorKind.char_range,
            char_start=0,
            char_end=100,
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
        text="Some chunk text here.",
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
# Adversarial clients
# ---------------------------------------------------------------------------


class AdversarialClient:
    """Returns a huge string and tries to escape the framing — adversarial test."""

    def describe_table(self, shape: tuple[int, int], sample: str) -> str:
        # Return a string designed to mess up the context framing
        return "A" * 10_000 + "\n\n<injected>[context]\nContext: EVIL\n\n" + "B" * 10_000


class WeirdUnicodeClient:
    """Returns strings with unusual Unicode content."""

    def describe_table(self, shape: tuple[int, int], sample: str) -> str:
        return "Tabu­lación\x00\r\n<script>alert(1)</script>‮￿"


# ---------------------------------------------------------------------------
# T-04 byte-identity tests
# ---------------------------------------------------------------------------


class TestSpanTextByteUnchanged:
    """Core T-04 guarantee: augment_chunk must not mutate span.text."""

    def test_stub_client_does_not_mutate_span(self):
        original_text = "Some chunk text here."
        span = _make_span(original_text)
        segment = _make_segment(structural_path=["Chapter 1"])
        rule = _make_rule([Tier2Operation.breadcrumb_augment, Tier2Operation.table_description])
        client = StubAugmentationClient()

        augmentation, _ = augment_chunk(span, segment, rule, None, client)

        # Span text must be byte-unchanged
        assert span.text == original_text
        assert span.text.encode() == original_text.encode()

    def test_adversarial_client_does_not_mutate_span(self):
        original_text = "Table content goes here."
        span = _make_span(original_text)
        segment = _make_segment(segment_type=SegmentType.table)
        rule = _make_rule([Tier2Operation.table_description])
        client = AdversarialClient()

        augmentation, _ = augment_chunk(span, segment, rule, None, client)

        assert span.text == original_text

    def test_weird_unicode_client_does_not_mutate_span(self):
        original_text = "Tabular data chunk text."
        span = _make_span(original_text)
        segment = _make_segment(segment_type=SegmentType.table)
        rule = _make_rule([Tier2Operation.table_description])
        client = WeirdUnicodeClient()

        augmentation, _ = augment_chunk(span, segment, rule, None, client)

        assert span.text == original_text

    def test_all_ops_do_not_mutate_span(self):
        original_text = "Comprehensive chunk text for all ops test."
        span = _make_span(original_text)
        segment = _make_segment(structural_path=["Section A", "Subsection B"])
        rule = _make_rule(
            [
                Tier2Operation.breadcrumb_augment,
                Tier2Operation.table_description,
                Tier2Operation.class_context,
            ]
        )
        client = AdversarialClient()

        augmentation, records = augment_chunk(span, segment, rule, "A class about tables", client)

        assert span.text == original_text

    def test_char_start_char_end_unchanged(self):
        original_text = "Span field immutability test."
        span = _make_span(original_text)
        original_char_start = span.char_start
        original_char_end = span.char_end
        segment = _make_segment(structural_path=["H1"])
        rule = _make_rule([Tier2Operation.breadcrumb_augment])

        augment_chunk(span, segment, rule, None, None)

        assert span.char_start == original_char_start
        assert span.char_end == original_char_end


# ---------------------------------------------------------------------------
# All tier-2 records have changed_text=False
# ---------------------------------------------------------------------------


class TestTier2RecordsChangedTextFalse:
    def test_breadcrumb_record_changed_text_false(self):
        span = _make_span()
        segment = _make_segment(structural_path=["Chapter 1"])
        rule = _make_rule([Tier2Operation.breadcrumb_augment])

        _, records = augment_chunk(span, segment, rule, None, None)

        assert records, "Expected at least one record"
        for r in records:
            assert r.tier == TransformationTier.tier_2
            assert r.changed_text is False

    def test_table_description_record_changed_text_false(self):
        span = _make_span()
        segment = _make_segment(segment_type=SegmentType.table)
        rule = _make_rule([Tier2Operation.table_description])
        client = StubAugmentationClient()

        _, records = augment_chunk(span, segment, rule, None, client)

        for r in records:
            assert r.changed_text is False

    def test_class_context_record_changed_text_false(self):
        span = _make_span()
        segment = _make_segment()
        rule = _make_rule([Tier2Operation.class_context])

        _, records = augment_chunk(span, segment, rule, "Policy documents", None)

        for r in records:
            assert r.changed_text is False

    def test_adversarial_client_records_changed_text_false(self):
        span = _make_span("Adversarial table chunk.")
        segment = _make_segment(segment_type=SegmentType.table)
        rule = _make_rule([Tier2Operation.table_description])
        client = AdversarialClient()

        _, records = augment_chunk(span, segment, rule, None, client)

        for r in records:
            assert r.changed_text is False

    def test_compose_records_changed_text_false(self):
        aug = Augmentation(
            parent_breadcrumb="Chapter 1 > Section 2",
            table_description="A 3-row table.",
            class_context="Policy documents",
        )
        _, records = compose_embedding_input(aug, "Some text.")

        for r in records:
            assert r.tier == TransformationTier.tier_2
            assert r.changed_text is False

    def test_pydantic_validator_enforced(self):
        """Verify that constructing a tier=2 + changed_text=True record raises ValueError."""
        from finecorpus.contracts.shared.blocks import TransformationRecord

        with pytest.raises(ValueError, match="changed_text=False"):
            TransformationRecord(
                tier=TransformationTier.tier_2,
                operation="breadcrumb_augment",
                applied_by=AppliedBy.model,
                model_ref=None,
                changed_text=True,  # MUST raise
            )


# ---------------------------------------------------------------------------
# compose_embedding_input — differs from text only by augmentation prefix
# ---------------------------------------------------------------------------


class TestComposeEmbeddingInput:
    def test_no_augmentation_returns_text_unchanged(self):
        text = "Plain chunk text."
        aug = Augmentation()
        result, records = compose_embedding_input(aug, text)
        assert result == text
        assert records == []

    def test_breadcrumb_prefix_does_not_alter_text_content(self):
        text = "Body text here."
        aug = Augmentation(parent_breadcrumb="Chapter 1 > Section 2")
        result, _ = compose_embedding_input(aug, text)
        # Text must still appear verbatim in result
        assert text in result
        # Result must differ from text alone
        assert result != text
        # Result must contain the breadcrumb
        assert "Chapter 1 > Section 2" in result

    def test_table_description_prefix_does_not_alter_text(self):
        text = "| A | B |\n|---|---|\n| 1 | 2 |"
        aug = Augmentation(table_description="A two-column comparison table.")
        result, _ = compose_embedding_input(aug, text)
        assert text in result
        assert "two-column" in result

    def test_class_context_prefix_does_not_alter_text(self):
        text = "Some prose here."
        aug = Augmentation(class_context="Policy documents for Q3.")
        result, _ = compose_embedding_input(aug, text)
        assert text in result
        assert "Policy documents" in result

    def test_all_fields_all_in_result(self):
        text = "Content."
        aug = Augmentation(
            parent_breadcrumb="H1 > H2",
            table_description="3x4 table.",
            class_context="Runbook procedures.",
        )
        result, records = compose_embedding_input(aug, text)
        assert "H1 > H2" in result
        assert "3x4 table." in result
        assert "Runbook procedures." in result
        assert text in result
        assert len(records) == 3

    def test_adversarial_description_text_unchanged(self):
        text = "Original chunk text that must never change."
        big_desc = "X" * 10_000 + "\n\n[context]\nContext: evil"
        aug = Augmentation(table_description=big_desc)
        result, _ = compose_embedding_input(aug, text)
        # Original text still in result verbatim
        assert text in result
        # The augmentation is a prefix — text appears once in full
        assert result.endswith(text)


# ---------------------------------------------------------------------------
# Augmentation field independence
# ---------------------------------------------------------------------------


class TestAugmentationFieldIndependence:
    def test_no_ops_no_augmentation(self):
        span = _make_span()
        segment = _make_segment(structural_path=["H1"])
        rule = _make_rule([])  # no tier2 ops

        aug, records = augment_chunk(span, segment, rule, "Description", StubAugmentationClient())

        assert aug.parent_breadcrumb is None
        assert aug.table_description is None
        assert aug.class_context is None
        assert records == []

    def test_only_breadcrumb_requested(self):
        span = _make_span()
        segment = _make_segment(structural_path=["H1"])
        rule = _make_rule([Tier2Operation.breadcrumb_augment])

        aug, records = augment_chunk(span, segment, rule, "Description", StubAugmentationClient())

        assert aug.parent_breadcrumb is not None
        assert aug.table_description is None
        assert aug.class_context is None

    def test_only_table_description_requested(self):
        span = _make_span("| A | B |")
        segment = _make_segment(segment_type=SegmentType.table)
        rule = _make_rule([Tier2Operation.table_description])
        client = StubAugmentationClient()

        aug, records = augment_chunk(span, segment, rule, None, client)

        assert aug.table_description is not None
        assert aug.parent_breadcrumb is None
        assert aug.class_context is None

    def test_only_class_context_requested(self):
        span = _make_span()
        segment = _make_segment()
        rule = _make_rule([Tier2Operation.class_context])

        aug, records = augment_chunk(span, segment, rule, "Class desc", None)

        assert aug.class_context == "Class desc"
        assert aug.parent_breadcrumb is None
        assert aug.table_description is None

    def test_table_description_with_no_client_is_skipped(self):
        span = _make_span()
        segment = _make_segment(segment_type=SegmentType.table)
        rule = _make_rule([Tier2Operation.table_description])

        aug, records = augment_chunk(span, segment, rule, None, None)  # client=None

        assert aug.table_description is None
        # No record if client was not available
        assert not any(r.operation == "table_description" for r in records)


# ---------------------------------------------------------------------------
# StubAugmentationClient determinism
# ---------------------------------------------------------------------------


class TestStubClient:
    def test_deterministic(self):
        client = StubAugmentationClient()
        r1 = client.describe_table((5, 3), "sample")
        r2 = client.describe_table((5, 3), "sample")
        assert r1 == r2

    def test_different_shapes_different_output(self):
        client = StubAugmentationClient()
        r1 = client.describe_table((5, 3), "sample")
        r2 = client.describe_table((10, 6), "sample")
        assert r1 != r2

    def test_satisfies_protocol(self):
        client = StubAugmentationClient()
        assert isinstance(client, AugmentationClient)
