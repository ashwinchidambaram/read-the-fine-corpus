"""Representability smoke tests — Case 1 (bloated manual) from design-validation/walkthroughs.md.

See docs/design-validation/walkthroughs.md Case 1 for concrete field values.
One smoke test per contract, using the walkthrough's Stage 1–7 values.

These tests verify that each contract can represent the Case 1 data without information loss
or field abuse ("REPRESENTABLE" verdict in the design-validation document).

Note on W-1 (boilerplate_strip): The walkthrough identifies W-1 (boilerplate_strip as a Tier 1
op conflicts with byte-identity). The schema resolves this by NOT including boilerplate_strip
in Tier1Operation — per the segment-taxonomy.md (boilerplate is handled structurally, never by
removing bytes). The ingestion config in the walkthrough (which lists boilerplate_strip) reflects
the W-1 finding, not the final schema. In the smoke test below we use a valid Tier1Operation.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from finecorpus.contracts.chunk import Augmentation, Chunk, EmbeddingRef
from finecorpus.contracts.eval_set import (
    ConfidenceLevel,
    EvalQuestion,
    EvalSet,
    EvalSetOrigin,
    GenerationMethod,
    QuestionType,
    ReviewStatus,
)
from finecorpus.contracts.ingestion_config import (
    ChunkingConfig,
    ChunkingStrategy,
    ClassRule,
    EmbeddingConfig,
    IngestionConfig,
    LanguageDecision,
    LanguageSupportDecision,
    NaiveBaselineRef,
    RecommendationBasis,
    RecommendationProvenance,
    RetrievalStrategy,
    RetrievalTreatment,
    Tier1Operation,
    Tier2Operation,
    TransformationSettings,
)
from finecorpus.contracts.inventory import (
    CollectStatus,
    DedupRole,
    DocumentStatus,
    Inventory,
    InventoryItem,
    SourceKind,
    SourceRun,
)
from finecorpus.contracts.parse_result import (
    BoilerplateCandidate,
    DocumentKind,
    ExtractStatus,
    Finding,
    FindingSeverity,
    LanguageShare,
    PageResult,
    ParseResult,
    ParserRef,
    ParseStatus,
    QualityScore,
    RegionResult,
    TableStructureRetained,
)
from finecorpus.contracts.retrieval_response import (
    AppliedFilter,
    ErrorCode,
    ErrorEnvelope,
    FilterOrigin,
    RequestEcho,
    ResultStatus,
    RetrievalResponse,
    RetrievalResult,
)
from finecorpus.contracts.segment_set import (
    CrossReference,
    CrossReferenceResolution,
    ReassemblyMethod,
    ReassemblyRecord,
    Segment,
    SegmentSet,
)
from finecorpus.contracts.shared.blocks import (
    AppliedBy,
    LocatorKind,
    PermissionFidelity,
    PermissionMode,
    PermissionSource,
    Provenance,
    SalienceSignal,
    SalienceSignalKind,
    SalienceTier,
    SegmentType,
    SensitivityFlag,
    SourceLocation,
    TenancyBlock,
    TransformationRecord,
    TransformationTier,
    TrustLevel,
)

# ---------------------------------------------------------------------------
# Shared fixtures for Case 1
# ---------------------------------------------------------------------------

DOCUMENT_ID = "01JMANUAL0001"
CONTENT_HASH = "9f2c8b1e77a4d0c3e5b19a24d8f6c0b2e4a7913d5c8f0a2b4d6e8f1a3c5b7d9e0"
CONFIG_VERSION = "c41d09f7b2e3a5" + "0" * 50  # 64-char sha256 hex

TENANCY = TenancyBlock(
    workspace_id="01JWSPACE001",
    kb_id="01JKB000001",
    permission_mode=PermissionMode.public_to_kb,
    permission_principals=[],
    permission_source=PermissionSource.platform,
    permission_fidelity=PermissionFidelity.authoritative,
    permission_resolved_at=None,
)

NOW = datetime(2026, 9, 1, 8, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Stage 1 — Inventory
# ---------------------------------------------------------------------------


class TestInventoryRepresentability:
    """Case 1 Inventory record is fully representable."""

    def test_inventory_item_bloated_manual(self) -> None:
        """InventoryItem for the bloated manual (acme-ops-manual-v4.pdf) constructs."""
        item = InventoryItem(
            document_id=DOCUMENT_ID,
            content_hash=CONTENT_HASH,
            source_path="/corpus/acme-ops-manual-v4.pdf",
            display_name="ACME Operating Manual v4",
            media_type="application/pdf",
            declared_extension="pdf",
            size_bytes=8_432_190,
            source_metadata={"author": "Engineering Dept", "created": "2025-03-01"},
            source_created_at=datetime(2025, 3, 1, tzinfo=UTC),
            source_modified_at=datetime(2025, 8, 15, 14, 22, tzinfo=UTC),
            discovered_at=datetime(2026, 9, 1, 8, tzinfo=UTC),
            dedup_role=DedupRole.unique,
            collect_status=CollectStatus.collected,
            document_status=DocumentStatus.active,
        )
        assert item.document_id == DOCUMENT_ID
        assert item.dedup_role == DedupRole.unique
        assert item.document_status == DocumentStatus.active

    def test_full_inventory_constructs(self) -> None:
        """A complete Inventory root model with one item constructs."""
        item = InventoryItem(
            document_id=DOCUMENT_ID,
            content_hash=CONTENT_HASH,
            source_path="/corpus/acme-ops-manual-v4.pdf",
            display_name="ACME Operating Manual v4",
            media_type="application/pdf",
            size_bytes=8_432_190,
            source_metadata={},
            discovered_at=NOW,
            dedup_role=DedupRole.unique,
            collect_status=CollectStatus.collected,
            document_status=DocumentStatus.active,
        )
        inv = Inventory(
            schema_version="1.0.0",
            tenancy=TENANCY,
            collected_at=NOW,
            source_run=SourceRun(source_kind=SourceKind.upload),
            items=[item],
            links=[],
            duplicate_groups=[],
            version_families=[],
        )
        assert len(inv.items) == 1
        assert inv.tenancy.workspace_id == "01JWSPACE001"


# ---------------------------------------------------------------------------
# Stage 2 — Parse result
# ---------------------------------------------------------------------------


class TestParseResultRepresentability:
    """Case 1 Parse result (mixed_pdf with boilerplate) is fully representable."""

    def test_parse_result_bloated_manual(self) -> None:
        """ParseResult for the bloated manual constructs with all Case 1 fields."""
        loc_page1 = SourceLocation(
            locator_kind=LocatorKind.page,
            page_start=1,
            page_end=2,
        )
        pr = ParseResult(
            schema_version="1.0.0",
            tenancy=TENANCY,
            document_id=DOCUMENT_ID,
            content_hash=CONTENT_HASH,
            parser=ParserRef(name="pdfium", version="6.4.0", ocr_engine="tesseract-5.3.4"),
            parsed_at=NOW,
            parse_status=ParseStatus.partial,
            document_kind=DocumentKind.mixed_pdf,
            quality=QualityScore(
                overall=0.81,
                text_extraction_ratio=0.79,
                table_structure_retained=TableStructureRetained.partial,
                is_near_empty=False,
                mean_ocr_confidence=0.87,
            ),
            pages=[
                PageResult(
                    page_number=1,
                    is_scanned=False,
                    ocr_confidence=None,
                    invisible_content=[],
                ),
                PageResult(
                    page_number=95,
                    is_scanned=True,
                    ocr_confidence=0.91,
                    invisible_content=[],
                ),
            ],
            regions=[
                RegionResult(
                    region_id="01JREG00001",
                    location=loc_page1,
                    text="CONFIDENTIAL. This document...",
                    extract_status=ExtractStatus.ok,
                    encoding_issue=False,
                )
            ],
            boilerplate_candidates=[
                BoilerplateCandidate(
                    candidate_id="01JBPC0001",
                    text_fingerprint="sha1:a3f9b2c1",
                    occurrence_count=18,
                    example_locations=[loc_page1],
                )
            ],
            content_classes=[],
            encoding_issues=[],
            language_distribution=[LanguageShare(language="en", fraction=1.0)],
            findings=[
                Finding(
                    code="mixed_pdf",
                    severity=FindingSeverity.info,
                    message=(
                        "Document contains both native-text pages (1-94, 113-120) "
                        "and scanned pages (95-112)."
                    ),
                )
            ],
        )
        assert pr.document_kind == DocumentKind.mixed_pdf
        assert pr.quality.mean_ocr_confidence == pytest.approx(0.87)
        assert len(pr.boilerplate_candidates) == 1
        assert pr.boilerplate_candidates[0].occurrence_count == 18


# ---------------------------------------------------------------------------
# Stage 3 — Segment set
# ---------------------------------------------------------------------------


class TestSegmentSetRepresentability:
    """Case 1 Segment set (all segment types, cross-references) is fully representable."""

    def test_segment_set_with_all_types(self) -> None:
        """SegmentSet with boilerplate, revision_history, prose, table, scanned_region."""
        boilerplate_segment = Segment(
            segment_id="01JSEG00001",
            document_order=0,
            segment_type=SegmentType.boilerplate,
            salience_tier=SalienceTier.boilerplate,
            structural_path=[],
            segment_path="#0",
            location=SourceLocation(locator_kind=LocatorKind.page, page_start=1, page_end=2),
            source_region_ids=["01JREG00001", "01JREG00002"],
            language="en",
            injection_suspicion=0.0,
            invisible_content_flags=[],
            sensitivity_flags=[SensitivityFlag.legal_privileged],
            salience_signals=[
                SalienceSignal(
                    kind=SalienceSignalKind.boilerplate_detection,
                    implied_tier=SalienceTier.boilerplate,
                    won=True,
                )
            ],
            salience_basis=SalienceSignalKind.boilerplate_detection,
            text="CONFIDENTIAL. This document is the property of ACME Corp...",
        )
        prose_segment = Segment(
            segment_id="01JSEG00010",
            document_order=10,
            segment_type=SegmentType.prose,
            salience_tier=SalienceTier.primary,
            structural_path=["3. Installation", "3.2 Lubrication Procedure"],
            segment_path="3. Installation/3.2 Lubrication Procedure#0",
            location=SourceLocation(
                locator_kind=LocatorKind.page,
                page_start=22,
                page_end=23,
                char_start=48200,
                char_end=51900,
            ),
            source_region_ids=["01JREG00020"],
            language="en",
            injection_suspicion=0.0,
            invisible_content_flags=[],
            sensitivity_flags=[],
            salience_signals=[
                SalienceSignal(
                    kind=SalienceSignalKind.segment_type_prior,
                    implied_tier=SalienceTier.primary,
                    won=True,
                )
            ],
            salience_basis=SalienceSignalKind.segment_type_prior,
            text="Apply grease to all bearing surfaces before assembly...",
        )
        scanned_segment = Segment(
            segment_id="01JSEG00050",
            document_order=50,
            segment_type=SegmentType.scanned_region,
            salience_tier=SalienceTier.supporting,
            structural_path=["Appendix A — Legacy Parts"],
            segment_path="Appendix A — Legacy Parts#0",
            location=SourceLocation(locator_kind=LocatorKind.page, page_start=95, page_end=112),
            source_region_ids=["01JREG00095"],
            language="en",
            ocr_confidence=0.89,
            injection_suspicion=0.0,
            invisible_content_flags=[],
            sensitivity_flags=[],
            salience_signals=[
                SalienceSignal(
                    kind=SalienceSignalKind.segment_type_prior,
                    implied_tier=SalienceTier.supporting,
                    won=True,
                )
            ],
            salience_basis=SalienceSignalKind.segment_type_prior,
            text="APPENDIX A — LEGACY PARTS LIST\n\nPart No. LG-0041...",
        )
        cross_ref = CrossReference(
            xref_id="01JXREF0001",
            from_segment_id="01JSEG00010",
            surface_text="See section 4.2 for torque specifications",
            location=SourceLocation(
                locator_kind=LocatorKind.page,
                page_start=22,
                char_start=49100,
                char_end=49138,
            ),
            resolution=CrossReferenceResolution.resolved,
            target_segment_id="01JSEG00016",
        )
        ss = SegmentSet(
            schema_version="1.0.0",
            tenancy=TENANCY,
            document_id=DOCUMENT_ID,
            content_hash=CONTENT_HASH,
            segments=[boilerplate_segment, prose_segment, scanned_segment],
            reassembly=ReassemblyRecord(
                method=ReassemblyMethod.document_order_concat,
                covered_region_ids=["01JREG00001", "01JREG00002", "01JREG00020", "01JREG00095"],
                reassembly_digest="a7f3" + "0" * 60,
            ),
            exclusions=[],
            cross_references=[cross_ref],
            decomposed_at=NOW,
        )
        assert len(ss.segments) == 3  # noqa: PLR2004
        assert ss.segments[0].segment_type == SegmentType.boilerplate
        assert ss.segments[0].salience_tier == SalienceTier.boilerplate
        assert ss.segments[0].salience_basis == SalienceSignalKind.boilerplate_detection
        assert ss.cross_references[0].resolution == CrossReferenceResolution.resolved


# ---------------------------------------------------------------------------
# Stage 4 — Ingestion config
# ---------------------------------------------------------------------------


class TestIngestionConfigRepresentability:
    """Case 1 Ingestion config is fully representable."""

    def _make_prose_rule(self) -> ClassRule:
        return ClassRule(
            segment_class=SegmentType.prose,
            transformation=TransformationSettings(
                tier1_enabled=True,
                tier1_operations=[Tier1Operation.whitespace_repair],
                tier2_enabled=True,
                tier2_operations=[Tier2Operation.breadcrumb_augment, Tier2Operation.class_context],
                tier3_enabled=False,
            ),
            chunking=ChunkingConfig(
                strategy=ChunkingStrategy.structure_aware,
                max_tokens=512,
                overlap_tokens=64,
                respect_headings=True,
            ),
            metadata_schema=[],
            retrieval_treatment=RetrievalTreatment(
                default_salience_filter=[SalienceTier.primary, SalienceTier.supporting],
                rerank_eligible=True,
                strategy=RetrievalStrategy.dense,
            ),
        )

    def _make_table_rule(self) -> ClassRule:
        return ClassRule(
            segment_class=SegmentType.table,
            transformation=TransformationSettings(
                tier1_enabled=True,
                tier1_operations=[Tier1Operation.table_to_markdown],
                tier2_enabled=True,
                tier2_operations=[Tier2Operation.table_description],
                tier3_enabled=False,
            ),
            chunking=ChunkingConfig(
                strategy=ChunkingStrategy.table_atomic,
                max_tokens=1024,
                overlap_tokens=0,
                respect_headings=False,
                atomic_rows=True,
                repeat_headers_on_split=True,
            ),
            metadata_schema=[],
            retrieval_treatment=RetrievalTreatment(
                default_salience_filter=[SalienceTier.primary, SalienceTier.supporting],
                rerank_eligible=True,
                strategy=RetrievalStrategy.dense,
            ),
        )

    def test_ingestion_config_constructs(self) -> None:
        """IngestionConfig with prose and table class rules constructs successfully."""
        default_rule = ClassRule(
            segment_class=SegmentType.unknown,
            transformation=TransformationSettings(
                tier1_enabled=True,
                tier1_operations=[Tier1Operation.whitespace_repair],
                tier2_enabled=False,
                tier2_operations=[],
                tier3_enabled=False,
            ),
            chunking=ChunkingConfig(
                strategy=ChunkingStrategy.recursive_char,
                max_tokens=512,
                overlap_tokens=0,
                respect_headings=False,
            ),
            metadata_schema=[],
            retrieval_treatment=RetrievalTreatment(
                default_salience_filter=[SalienceTier.primary, SalienceTier.supporting],
                rerank_eligible=False,
                strategy=RetrievalStrategy.dense,
            ),
        )
        ic = IngestionConfig(
            schema_version="1.0.0",
            tenancy=TENANCY,
            config_version=CONFIG_VERSION,
            created_at=NOW,
            naive_baseline=NaiveBaselineRef(
                reference_id="baseline-v1", description="Default naive baseline"
            ),
            class_rules=[self._make_prose_rule(), self._make_table_rule()],
            default_rule=default_rule,
            embedding=EmbeddingConfig(
                provider="openai",
                model="text-embedding-3-large",
                dimensions=3072,
                normalize=True,
            ),
            retrieval_defaults=RetrievalTreatment(
                default_salience_filter=[SalienceTier.primary, SalienceTier.supporting],
                rerank_eligible=True,
                strategy=RetrievalStrategy.dense,
            ),
            language_support=LanguageSupportDecision(
                detected_languages=[LanguageShare(language="en", fraction=1.0)],
                unsupported_languages=[],
                decision=LanguageDecision.proceed,
            ),
            spreadsheet_triage=[],
            exclusions_confirmed=[],
            provenance=[
                RecommendationProvenance(
                    target="/embedding/model",
                    basis=RecommendationBasis.heuristic,
                    rationale="Default high-capacity model for English corpus.",
                )
            ],
            secret_free_attestation=True,
        )
        assert len(ic.class_rules) == 2  # noqa: PLR2004
        assert ic.embedding.provider == "openai"
        assert ic.secret_free_attestation is True


# ---------------------------------------------------------------------------
# Stage 5 — Chunk
# ---------------------------------------------------------------------------


class TestChunkRepresentability:
    """Chunk A (prose with breadcrumb augmentation) from Case 1 is fully representable."""

    def test_chunk_a_prose_constructs(self) -> None:
        """Chunk A (prose segment with breadcrumb augmentation) from walkthroughs.md."""
        provenance = Provenance(
            source_document_id=DOCUMENT_ID,
            source_document_version=CONTENT_HASH,
            source_location=SourceLocation(
                locator_kind=LocatorKind.page,
                page_start=22,
                page_end=23,
                char_start=48200,
                char_end=51900,
            ),
            structural_path=["3. Installation", "3.2 Lubrication Procedure"],
            transformations=[
                TransformationRecord(
                    tier=TransformationTier.tier_1,
                    operation="whitespace_repair",
                    applied_by=AppliedBy.deterministic,
                    changed_text=False,
                    note="Normalised 3 instances of non-breaking spaces.",
                ),
                TransformationRecord(
                    tier=TransformationTier.tier_2,
                    operation="breadcrumb_augment",
                    applied_by=AppliedBy.model,
                    model_ref="openai/gpt-4o-mini",
                    changed_text=False,
                    note="Prepended section path.",
                ),
            ],
            confidence=1.0,
            ocr_confidence=None,
            segment_type=SegmentType.prose,
            salience_tier=SalienceTier.primary,
            salience_basis=SalienceSignalKind.segment_type_prior,
            salience_signals=[
                SalienceSignal(
                    kind=SalienceSignalKind.segment_type_prior,
                    implied_tier=SalienceTier.primary,
                    won=True,
                )
            ],
            language="en",
            injection_suspicion=0.0,
            invisible_content_flags=[],
            sensitivity_flags=[],
            trust_level=TrustLevel.untrusted_ingested,
        )
        chunk = Chunk(
            schema_version="1.0.0",
            chunk_id="chk_k7m2p9x4rq8h3n6v0w1t5s2",
            tenancy=TENANCY,
            provenance=provenance,
            text=(
                "Apply grease to all bearing surfaces before assembly. "
                "See section 4.2 for torque specifications."
            ),
            augmentation=Augmentation(
                parent_breadcrumb="3. Installation > 3.2 Lubrication Procedure",
                class_context=("This document describes operating procedures for ACME equipment."),
                generated_by=["openai/gpt-4o-mini"],
            ),
            embedding_input=(
                "3. Installation > 3.2 Lubrication Procedure\n\n"
                "Apply grease to all bearing surfaces before assembly."
            ),
            embedding_ref=EmbeddingRef(
                provider="openai",
                model="text-embedding-3-large",
                dimensions=3072,
                config_version=CONFIG_VERSION,
            ),
            chunk_index=0,
            token_count=387,
        )
        # Verify all Case 1 walkthrough assertions
        assert chunk.provenance.injection_suspicion == pytest.approx(0.0)
        assert chunk.provenance.trust_level == TrustLevel.untrusted_ingested
        assert chunk.provenance.language == "en"
        assert chunk.provenance.salience_tier == SalienceTier.primary
        assert chunk.augmentation.parent_breadcrumb == "3. Installation > 3.2 Lubrication Procedure"
        assert chunk.augmentation.table_description is None


# ---------------------------------------------------------------------------
# Stage 6 — Eval set
# ---------------------------------------------------------------------------


class TestEvalSetRepresentability:
    """EvalSet for Case 1 (provisional generated set) is fully representable."""

    def test_provisional_eval_set_constructs(self) -> None:
        """Provisional generated EvalSet with one unreviewed question constructs."""
        q = EvalQuestion(
            question_id="01JQUESTION001",
            text="What is the lubrication procedure for bearing surfaces?",
            generation_method=GenerationMethod.generated_factual,
            review_status=ReviewStatus.unreviewed,
            source_segment_ids=["01JSEG00010"],
            source_unknown=False,
            question_type=QuestionType.factual_lookup,
            expected_segment_ids=["01JSEG00010"],
        )
        es = EvalSet(
            schema_version="1.0.0",
            tenancy=TENANCY,
            eval_set_id="01JEVALSET001",
            origin=EvalSetOrigin.generated,
            created_at=NOW,
            confidence_level=ConfidenceLevel.provisional,
            questions=[q],
        )
        assert es.confidence_level == ConfidenceLevel.provisional
        assert es.questions[0].review_status == ReviewStatus.unreviewed
        assert es.origin == EvalSetOrigin.generated


# ---------------------------------------------------------------------------
# Stage 7 — Retrieval response
# ---------------------------------------------------------------------------


class TestRetrievalResponseRepresentability:
    """RetrievalResponse with result_status=matches is fully representable."""

    def test_matches_response_constructs(self) -> None:
        """RetrievalResponse with matches and full provenance constructs."""
        provenance = Provenance(
            source_document_id=DOCUMENT_ID,
            source_document_version=CONTENT_HASH,
            source_location=SourceLocation(
                locator_kind=LocatorKind.page, page_start=22, page_end=23
            ),
            structural_path=["3. Installation", "3.2 Lubrication Procedure"],
            transformations=[],
            confidence=1.0,
            segment_type=SegmentType.prose,
            salience_tier=SalienceTier.primary,
            salience_basis=SalienceSignalKind.segment_type_prior,
            salience_signals=[
                SalienceSignal(
                    kind=SalienceSignalKind.segment_type_prior,
                    implied_tier=SalienceTier.primary,
                    won=True,
                )
            ],
            language="en",
            injection_suspicion=0.0,
            invisible_content_flags=[],
            sensitivity_flags=[],
            trust_level=TrustLevel.untrusted_ingested,
        )
        result = RetrievalResult(
            chunk_id="chk_k7m2p9x4rq8h3n6v0w1t5s2",
            text="Apply grease to all bearing surfaces before assembly.",
            provenance=provenance,
            score=0.92,
            trust_level=TrustLevel.untrusted_ingested,
        )
        response = RetrievalResponse(
            schema_version="1.0.0",
            request_echo=RequestEcho(
                query="How do I lubricate bearings?",
                filters_applied=[
                    AppliedFilter(
                        expression="tenancy.kb_id = '01JKB000001'",
                        origin=FilterOrigin.tenancy,
                    )
                ],
            ),
            result_status=ResultStatus.matches,
            results=[result],
        )
        assert response.result_status == ResultStatus.matches
        assert len(response.results) == 1
        assert response.results[0].trust_level == TrustLevel.untrusted_ingested
        assert response.results[0].provenance.injection_suspicion == pytest.approx(0.0)

    def test_error_response_constructs(self) -> None:
        """RetrievalResponse with result_status=error constructs."""
        response = RetrievalResponse(
            schema_version="1.0.0",
            request_echo=RequestEcho(
                query="How do I lubricate bearings?",
                filters_applied=[],
            ),
            result_status=ResultStatus.error,
            results=[],
            error=ErrorEnvelope(
                code=ErrorCode.EMBEDDING_MODEL_MISMATCH,
                message=(
                    "Query embedded with text-embedding-ada-002 "
                    "but index uses text-embedding-3-large."
                ),
                retriable=False,
            ),
        )
        assert response.result_status == ResultStatus.error
        assert response.error is not None
        assert response.error.code == ErrorCode.EMBEDDING_MODEL_MISMATCH

    def test_no_matches_response_constructs(self) -> None:
        """RetrievalResponse with result_status=no_matches constructs."""
        response = RetrievalResponse(
            schema_version="1.0.0",
            request_echo=RequestEcho(
                query="Find documents about quantum cryptography",
                filters_applied=[],
            ),
            result_status=ResultStatus.no_matches,
            results=[],
        )
        assert response.result_status == ResultStatus.no_matches
        assert response.results == []

    def test_filtered_to_zero_response_constructs(self) -> None:
        """RetrievalResponse with result_status=filtered_to_zero constructs."""
        response = RetrievalResponse(
            schema_version="1.0.0",
            request_echo=RequestEcho(
                query="Restricted content",
                filters_applied=[
                    AppliedFilter(
                        expression="permission_principals contains 'user-999'",
                        origin=FilterOrigin.tenancy,
                    )
                ],
            ),
            result_status=ResultStatus.filtered_to_zero,
            results=[],
        )
        assert response.result_status == ResultStatus.filtered_to_zero
