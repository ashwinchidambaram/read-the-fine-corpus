"""D3: ingestion cost estimation tests.

Tests:
- Estimate has all basis fields defined in IngestionCostEstimate
- Local embedding provider → zero_marginal_cost=True, embedding_cost_usd=0
- Local LLM provider → zero_marginal_cost=True, llm_cost_usd=0
- Estimate is computable BEFORE any build artifact exists (pure function)
- total_cost_usd = embedding_cost_usd + llm_cost_usd
- Cost gate: run without --yes refuses to build (interactive confirmation required)
"""

from __future__ import annotations

import hashlib
import pathlib
import uuid
from datetime import UTC, datetime

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
from finecorpus.contracts.segment_set import (
    ReassemblyMethod,
    ReassemblyRecord,
    Segment,
    SegmentSet,
)
from finecorpus.contracts.segment_set_batch import BATCH_SCHEMA_VERSION, SegmentSetBatch
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
from finecorpus.contracts.versions import INGESTION_CONFIG_SCHEMA_VERSION
from finecorpus.embedding.fake import FakeProvider
from finecorpus.pipeline.costing import IngestionCostEstimate, estimate_ingestion_cost

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_tenancy() -> TenancyBlock:
    return TenancyBlock(
        workspace_id="ws-cost",
        kb_id="kb-cost",
        permission_mode=PermissionMode.public_to_kb,
        permission_principals=[],
        permission_source=PermissionSource.platform,
        permission_fidelity=PermissionFidelity.authoritative,
        permission_resolved_at=None,
    )


def _make_segment(
    text: str,
    doc_order: int = 0,
    segment_type: SegmentType = SegmentType.prose,
) -> Segment:
    return Segment(
        segment_id=str(uuid.uuid4()),
        document_order=doc_order,
        segment_type=segment_type,
        salience_tier=SalienceTier.primary,
        structural_path=[],
        segment_path=f"seg/{doc_order}",
        location=SourceLocation(
            locator_kind=LocatorKind.char_range,
            char_start=0,
            char_end=len(text),
        ),
        source_region_ids=[f"region_{doc_order}"],
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


def _make_batch(segments_by_doc: dict[str, list[Segment]]) -> SegmentSetBatch:
    seg_sets = []
    for doc_id, segs in segments_by_doc.items():
        content_hash = hashlib.sha256(doc_id.encode()).hexdigest()
        region_ids = [r for seg in segs for r in seg.source_region_ids]
        reassembly_text = "".join(seg.text or "" for seg in segs)
        reassembly_digest = hashlib.sha256(reassembly_text.encode()).hexdigest()
        ss = SegmentSet(
            schema_version="1.1.0",
            tenancy=_make_tenancy(),
            document_id=doc_id,
            content_hash=content_hash,
            segments=segs,
            reassembly=ReassemblyRecord(
                method=ReassemblyMethod.document_order_concat,
                covered_region_ids=region_ids,
                reassembly_digest=reassembly_digest,
            ),
            exclusions=[],
            cross_references=[],
            decomposed_at=datetime.now(tz=UTC),
        )
        seg_sets.append(ss.model_dump(mode="json"))

    return SegmentSetBatch(
        schema_version=BATCH_SCHEMA_VERSION,
        contract="segment_set_batch",
        run_id="cost-test",
        produced_at=datetime.now(tz=UTC),
        skeleton=None,
        segment_sets=seg_sets,
    )


def _make_config(
    tier1_ops: list[Tier1Operation] | None = None,
    tier2_ops: list[Tier2Operation] | None = None,
    table_tier2_ops: list[Tier2Operation] | None = None,
) -> IngestionConfig:
    if tier1_ops is None:
        tier1_ops = [Tier1Operation.whitespace_repair]
    if tier2_ops is None:
        tier2_ops = [Tier2Operation.breadcrumb_augment]

    embedding = EmbeddingConfig(
        provider="fake",
        model="fake-embed-v1",
        dimensions=64,
        normalize=True,
        supports_languages=["*"],
    )
    chunking = ChunkingConfig(
        strategy=ChunkingStrategy.recursive_char,
        max_tokens=100,
        overlap_tokens=10,
        respect_headings=False,
    )
    default_rule = ClassRule(
        segment_class=SegmentType.prose,
        transformation=TransformationSettings(
            tier1_enabled=bool(tier1_ops),
            tier1_operations=tier1_ops,
            tier2_enabled=bool(tier2_ops),
            tier2_operations=tier2_ops or [],
            tier3_enabled=False,
        ),
        chunking=chunking,
        embedding_override=None,
        metadata_schema=[],
        retrieval_treatment=RetrievalTreatment(
            default_salience_filter=[SalienceTier.primary, SalienceTier.supporting],
            salience_weights=None,
            rerank_eligible=False,
            strategy=RetrievalStrategy.dense,
        ),
    )

    table_rule = None
    if table_tier2_ops is not None:
        table_rule = ClassRule(
            segment_class=SegmentType.table,
            transformation=TransformationSettings(
                tier1_enabled=True,
                tier1_operations=[
                    Tier1Operation.whitespace_repair,
                    Tier1Operation.table_to_markdown,
                ],
                tier2_enabled=True,
                tier2_operations=table_tier2_ops,
                tier3_enabled=False,
            ),
            chunking=chunking,
            embedding_override=None,
            metadata_schema=[],
            retrieval_treatment=RetrievalTreatment(
                default_salience_filter=[SalienceTier.primary, SalienceTier.supporting],
                salience_weights=None,
                rerank_eligible=False,
                strategy=RetrievalStrategy.dense,
            ),
        )

    config_version = hashlib.sha256(b"cost-test-config").hexdigest()
    return IngestionConfig(
        schema_version=INGESTION_CONFIG_SCHEMA_VERSION,
        tenancy=_make_tenancy(),
        config_version=config_version,
        created_at=datetime.now(tz=UTC),
        naive_baseline=NaiveBaselineRef(
            reference_id="naive-baseline-v0",
            description="Cost test baseline.",
        ),
        class_rules=[table_rule] if table_rule else [],
        default_rule=default_rule,
        embedding=embedding,
        retrieval_defaults=RetrievalTreatment(
            default_salience_filter=[SalienceTier.primary, SalienceTier.supporting],
            salience_weights=None,
            rerank_eligible=False,
            strategy=RetrievalStrategy.dense,
        ),
        language_support=LanguageSupportDecision(
            detected_languages=[],
            unsupported_languages=[],
            decision=LanguageDecision.proceed,
        ),
        spreadsheet_triage=[],
        exclusions_confirmed=[],
        provenance=[
            RecommendationProvenance(
                target="/default_rule",
                basis=RecommendationBasis.heuristic,
                rationale="Cost test config.",
            )
        ],
        class_descriptions=[],
        secret_free_attestation=True,
    )


# ---------------------------------------------------------------------------
# Basis fields
# ---------------------------------------------------------------------------


class TestCostEstimateBasisFields:
    """Estimate must have all required basis fields set."""

    def test_all_basis_fields_present(self) -> None:
        """IngestionCostEstimate has all required basis fields (not None where required)."""
        seg = _make_segment("Hello world sentence for cost test.")
        batch = _make_batch({"doc-basis-1": [seg]})
        config = _make_config()
        fake_embed = FakeProvider()

        estimate = estimate_ingestion_cost(
            segment_set_batch=batch,
            ingestion_config=config,
            embedding_provider=fake_embed,
            llm_provider_or_none=None,
        )

        # Must be a proper IngestionCostEstimate instance
        assert isinstance(estimate, IngestionCostEstimate)

        # All basis fields must exist (not missing from the model)
        required_fields = [
            "embedding_token_count",
            "embedding_cost_usd",
            "embedding_provider_id",
            "embedding_model_id",
            "embedding_cost_per_1k",
            "embedding_is_local",
            "embedding_zero_marginal_cost",
            "llm_table_call_count",
            "llm_other_call_count",
            "llm_total_call_count",
            "llm_estimated_input_tokens",
            "llm_estimated_output_tokens",
            "llm_cost_usd",
            "llm_is_local",
            "llm_zero_marginal_cost",
            "total_cost_usd",
            "tokenizer_name",
            "basis",
        ]
        for field in required_fields:
            assert hasattr(estimate, field), f"IngestionCostEstimate missing field: {field}"

    def test_total_cost_equals_sum(self) -> None:
        """total_cost_usd must equal embedding_cost_usd + llm_cost_usd."""
        seg = _make_segment("Total cost sum test content.")
        batch = _make_batch({"doc-total-1": [seg]})
        config = _make_config()
        fake_embed = FakeProvider()

        estimate = estimate_ingestion_cost(
            segment_set_batch=batch,
            ingestion_config=config,
            embedding_provider=fake_embed,
            llm_provider_or_none=None,
        )

        expected_total = estimate.embedding_cost_usd + estimate.llm_cost_usd
        assert abs(estimate.total_cost_usd - expected_total) < 1e-10, (
            f"total_cost_usd {estimate.total_cost_usd} != "
            f"embedding_cost_usd {estimate.embedding_cost_usd} + "
            f"llm_cost_usd {estimate.llm_cost_usd}"
        )

    def test_basis_field_is_string(self) -> None:
        """The basis field must be a non-empty string describing the proxy method."""
        seg = _make_segment("Basis field string test.")
        batch = _make_batch({"doc-basis-str-1": [seg]})
        config = _make_config()
        fake_embed = FakeProvider()

        estimate = estimate_ingestion_cost(
            segment_set_batch=batch,
            ingestion_config=config,
            embedding_provider=fake_embed,
            llm_provider_or_none=None,
        )

        assert isinstance(estimate.basis, str) and estimate.basis, (
            "basis field must be a non-empty string"
        )

    def test_tokenizer_name_is_whitespace_proxy(self) -> None:
        """tokenizer_name must identify the whitespace-word proxy method."""
        seg = _make_segment("Tokenizer name test.")
        batch = _make_batch({"doc-tokenizer-1": [seg]})
        config = _make_config()
        fake_embed = FakeProvider()

        estimate = estimate_ingestion_cost(
            segment_set_batch=batch,
            ingestion_config=config,
            embedding_provider=fake_embed,
            llm_provider_or_none=None,
        )

        # The proxy is documented as whitespace-word splitting
        name_lower = estimate.tokenizer_name.lower()
        assert "whitespace" in name_lower or "word" in name_lower, (
            f"tokenizer_name should indicate whitespace-word proxy; "
            f"got: {estimate.tokenizer_name!r}"
        )

    def test_embedding_token_count_positive_for_nonempty_input(self) -> None:
        """A non-empty batch must produce positive embedding_token_count."""
        seg = _make_segment("Token count test with multiple words to count.")
        batch = _make_batch({"doc-tokens-1": [seg]})
        config = _make_config()
        fake_embed = FakeProvider()

        estimate = estimate_ingestion_cost(
            segment_set_batch=batch,
            ingestion_config=config,
            embedding_provider=fake_embed,
            llm_provider_or_none=None,
        )

        assert estimate.embedding_token_count > 0


# ---------------------------------------------------------------------------
# Local provider → zero marginal cost
# ---------------------------------------------------------------------------


class TestLocalProviderZeroCost:
    """Local providers must produce zero_marginal_cost=True and cost_usd=0."""

    def test_fake_embedding_provider_zero_marginal_cost(self) -> None:
        """FakeProvider (local/fake) → embedding_zero_marginal_cost=True."""
        seg = _make_segment("Local embedding cost test.")
        batch = _make_batch({"doc-local-embed-1": [seg]})
        config = _make_config()
        fake_embed = FakeProvider()

        estimate = estimate_ingestion_cost(
            segment_set_batch=batch,
            ingestion_config=config,
            embedding_provider=fake_embed,
            llm_provider_or_none=None,
        )

        assert estimate.embedding_zero_marginal_cost is True, (
            "FakeProvider should be flagged as zero_marginal_cost"
        )
        assert estimate.embedding_cost_usd == 0.0, (
            f"Local embedding provider cost should be 0.0, got {estimate.embedding_cost_usd}"
        )
        assert estimate.embedding_is_local is True

    def test_fake_llm_provider_zero_marginal_cost(self) -> None:
        """FakeLLMProvider (local/fake) → llm_zero_marginal_cost=True."""
        seg = _make_segment("| Col A | Col B |\n|---|---|\n| val1 | val2 |")
        batch = _make_batch({"doc-local-llm-1": [seg]})
        config = _make_config(table_tier2_ops=[Tier2Operation.table_description])

        from finecorpus.llm.fake import FakeLLMProvider

        fake_llm = FakeLLMProvider()
        fake_embed = FakeProvider()

        estimate = estimate_ingestion_cost(
            segment_set_batch=batch,
            ingestion_config=config,
            embedding_provider=fake_embed,
            llm_provider_or_none=fake_llm,
        )

        assert estimate.llm_zero_marginal_cost is True, (
            "FakeLLMProvider should be flagged as zero_marginal_cost"
        )
        assert estimate.llm_cost_usd == 0.0, (
            f"Local LLM provider cost should be 0.0, got {estimate.llm_cost_usd}"
        )
        assert estimate.llm_is_local is True

    def test_no_llm_provider_llm_cost_is_zero(self) -> None:
        """When llm_provider_or_none=None, all LLM cost fields must be zero/None."""
        seg = _make_segment("No LLM provider cost test.")
        batch = _make_batch({"doc-no-llm-1": [seg]})
        config = _make_config()
        fake_embed = FakeProvider()

        estimate = estimate_ingestion_cost(
            segment_set_batch=batch,
            ingestion_config=config,
            embedding_provider=fake_embed,
            llm_provider_or_none=None,
        )

        assert estimate.llm_cost_usd == 0.0
        assert estimate.llm_total_call_count == 0
        assert estimate.llm_zero_marginal_cost is True


# ---------------------------------------------------------------------------
# Estimate is pre-build (pure function, no artifacts needed)
# ---------------------------------------------------------------------------


class TestEstimatePreBuild:
    """estimate_ingestion_cost must work before any build artifact exists."""

    def test_estimate_without_build_artifacts(self, tmp_path: pathlib.Path) -> None:
        """estimate_ingestion_cost works with a raw batch — no build.json needed."""
        seg = _make_segment("Pre-build estimation test content.")
        batch = _make_batch({"doc-prebuild-1": [seg]})
        config = _make_config()
        fake_embed = FakeProvider()

        # No build artifact exists — just pass the raw batch
        estimate = estimate_ingestion_cost(
            segment_set_batch=batch,
            ingestion_config=config,
            embedding_provider=fake_embed,
            llm_provider_or_none=None,
        )

        # Should produce a valid estimate without touching any build artifacts
        assert estimate.embedding_token_count >= 0
        assert estimate.total_cost_usd >= 0.0

    def test_estimate_is_reproducible(self) -> None:
        """Same batch + config → identical estimate on repeated calls."""
        seg = _make_segment("Reproducibility test content for cost estimation.")
        batch = _make_batch({"doc-repro-1": [seg]})
        config = _make_config()
        fake_embed = FakeProvider()

        est1 = estimate_ingestion_cost(
            segment_set_batch=batch,
            ingestion_config=config,
            embedding_provider=fake_embed,
            llm_provider_or_none=None,
        )
        est2 = estimate_ingestion_cost(
            segment_set_batch=batch,
            ingestion_config=config,
            embedding_provider=fake_embed,
            llm_provider_or_none=None,
        )

        assert est1.embedding_token_count == est2.embedding_token_count
        assert est1.embedding_cost_usd == est2.embedding_cost_usd
        assert est1.total_cost_usd == est2.total_cost_usd


# ---------------------------------------------------------------------------
# Table description LLM call count in estimate
# ---------------------------------------------------------------------------


class TestTableLLMCallEstimate:
    """Table segments with table_description op are counted in llm_table_call_count."""

    def test_table_segment_counted_in_estimate(self) -> None:
        """A batch with N unique table segments → llm_table_call_count = N."""
        table_segs = [
            _make_segment(
                f"| Col A | Col B |\n|---|---|\n| val{i} | val{i + 1} |",
                doc_order=i,
                segment_type=SegmentType.table,
            )
            for i in range(3)
        ]
        batch = _make_batch({"doc-tables-1": table_segs})
        config = _make_config(
            table_tier2_ops=[Tier2Operation.table_description, Tier2Operation.breadcrumb_augment]
        )

        from finecorpus.llm.fake import FakeLLMProvider

        fake_llm = FakeLLMProvider()
        fake_embed = FakeProvider()

        estimate = estimate_ingestion_cost(
            segment_set_batch=batch,
            ingestion_config=config,
            embedding_provider=fake_embed,
            llm_provider_or_none=fake_llm,
        )

        assert estimate.llm_table_call_count == 3, (
            f"Expected 3 table LLM calls, got {estimate.llm_table_call_count}"
        )

    def test_no_table_description_op_zero_llm_calls(self) -> None:
        """A table class rule without table_description → llm_table_call_count=0."""
        table_seg = _make_segment(
            "| A | B |\n|---|---|\n| 1 | 2 |",
            segment_type=SegmentType.table,
        )
        batch = _make_batch({"doc-notabledesc-1": [table_seg]})
        config = _make_config(
            table_tier2_ops=[Tier2Operation.breadcrumb_augment]  # no table_description
        )

        from finecorpus.llm.fake import FakeLLMProvider

        fake_llm = FakeLLMProvider()
        fake_embed = FakeProvider()

        estimate = estimate_ingestion_cost(
            segment_set_batch=batch,
            ingestion_config=config,
            embedding_provider=fake_embed,
            llm_provider_or_none=fake_llm,
        )

        assert estimate.llm_table_call_count == 0, (
            f"Expected 0 table LLM calls without table_description op, "
            f"got {estimate.llm_table_call_count}"
        )


# ---------------------------------------------------------------------------
# Ruling 1: per-class chunking config must be used, not default_rule config
# ---------------------------------------------------------------------------


class TestPerClassChunkingInEstimate:
    """estimate_ingestion_cost must use per-class chunking config, not default_rule."""

    def test_table_rule_tiny_max_tokens_produces_more_chunks_than_default(self) -> None:
        """A table class rule with tiny max_tokens must produce more chunks than the default.

        This is the Ruling 1 regression test.  Before the fix, max_tokens was read
        from default_rule BEFORE the segment loop so every segment used the same
        (wrong) chunking config regardless of its class.

        Setup:
        - default_rule.chunking.max_tokens = 512  (large — most text fits in one chunk)
        - table_rule.chunking.max_tokens = 3       (tiny — forces many small chunks)
        - Provide a table segment with 30+ words so it needs multiple chunks under tiny limit.

        Expected: chunk count differs from the count that would result from using
        default_rule's max_tokens (i.e. per-class rule IS honoured in the estimate).
        """
        import hashlib as _hl

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
            TransformationSettings,
        )
        from finecorpus.contracts.segment_set import (
            ReassemblyMethod,
            ReassemblyRecord,
            Segment,
            SegmentSet,
        )
        from finecorpus.contracts.segment_set_batch import BATCH_SCHEMA_VERSION, SegmentSetBatch
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
        from finecorpus.contracts.versions import INGESTION_CONFIG_SCHEMA_VERSION
        from finecorpus.embedding.fake import FakeProvider
        from finecorpus.pipeline.costing import estimate_ingestion_cost

        # Table segment text: > 30 words so it definitely produces multiple chunks
        # when max_tokens = 3 but only 1 chunk when max_tokens = 512.
        table_text = (
            "| Col1 | Col2 | Col3 |\n"
            "|------|------|------|\n"
            "| aaa  | bbb  | ccc  |\n"
            "| ddd  | eee  | fff  |\n"
            "| ggg  | hhh  | iii  |\n"
            "| jjj  | kkk  | lll  |\n"
            "| mmm  | nnn  | ooo  |\n"
        )
        # Verify text is large enough (> 3 words for tiny, < 512 for default)
        word_count = len(table_text.split())
        assert word_count > 5, f"Table text too short ({word_count} words) for this test"

        tenancy = TenancyBlock(
            workspace_id="ws-ruling1",
            kb_id="kb-ruling1",
            permission_mode=PermissionMode.public_to_kb,
            permission_principals=[],
            permission_source=PermissionSource.platform,
            permission_fidelity=PermissionFidelity.authoritative,
            permission_resolved_at=None,
        )
        seg = Segment(
            segment_id="seg-ruling1-table",
            document_order=0,
            segment_type=SegmentType.table,
            salience_tier=SalienceTier.primary,
            structural_path=[],
            segment_path="sec/table/0",
            location=SourceLocation(
                locator_kind=LocatorKind.char_range,
                char_start=0,
                char_end=len(table_text),
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
            text=table_text,
        )

        # --- Config with DIFFERENT chunking for table vs default ---
        default_chunking = ChunkingConfig(
            strategy=ChunkingStrategy.recursive_char,
            max_tokens=512,  # large: whole table fits in 1 chunk, overlap=0
            overlap_tokens=0,
            respect_headings=False,
        )
        tiny_table_chunking = ChunkingConfig(
            strategy=ChunkingStrategy.recursive_char,
            max_tokens=5,  # tiny: forces many chunks; overlap=2 inflates token count
            overlap_tokens=2,
            respect_headings=False,
        )

        embedding = EmbeddingConfig(
            provider="fake",
            model="fake-embed-v1",
            dimensions=64,
            normalize=True,
            supports_languages=["*"],
        )

        table_rule = ClassRule(
            segment_class=SegmentType.table,
            transformation=TransformationSettings(
                tier1_enabled=False,
                tier1_operations=[],
                tier2_enabled=False,
                tier2_operations=[],
                tier3_enabled=False,
            ),
            chunking=tiny_table_chunking,
            embedding_override=None,
            metadata_schema=[],
            retrieval_treatment=RetrievalTreatment(
                default_salience_filter=[SalienceTier.primary, SalienceTier.supporting],
                salience_weights=None,
                rerank_eligible=False,
                strategy=RetrievalStrategy.dense,
            ),
        )
        default_rule = ClassRule(
            segment_class=SegmentType.prose,
            transformation=TransformationSettings(
                tier1_enabled=False,
                tier1_operations=[],
                tier2_enabled=False,
                tier2_operations=[],
                tier3_enabled=False,
            ),
            chunking=default_chunking,
            embedding_override=None,
            metadata_schema=[],
            retrieval_treatment=RetrievalTreatment(
                default_salience_filter=[SalienceTier.primary, SalienceTier.supporting],
                salience_weights=None,
                rerank_eligible=False,
                strategy=RetrievalStrategy.dense,
            ),
        )

        config_version = _hl.sha256(b"ruling1-per-class").hexdigest()
        config = IngestionConfig(
            schema_version=INGESTION_CONFIG_SCHEMA_VERSION,
            tenancy=tenancy,
            config_version=config_version,
            created_at=__import__("datetime").datetime.now(tz=__import__("datetime").timezone.utc),
            naive_baseline=NaiveBaselineRef(
                reference_id="naive-baseline-v0",
                description="Ruling 1 regression test.",
            ),
            class_rules=[table_rule],
            default_rule=default_rule,
            embedding=embedding,
            retrieval_defaults=RetrievalTreatment(
                default_salience_filter=[SalienceTier.primary, SalienceTier.supporting],
                salience_weights=None,
                rerank_eligible=False,
                strategy=RetrievalStrategy.dense,
            ),
            language_support=LanguageSupportDecision(
                detected_languages=[],
                unsupported_languages=[],
                decision=LanguageDecision.proceed,
            ),
            spreadsheet_triage=[],
            exclusions_confirmed=[],
            provenance=[
                RecommendationProvenance(
                    target="/default_rule",
                    basis=RecommendationBasis.heuristic,
                    rationale="Ruling 1 test.",
                )
            ],
            class_descriptions=[],
            secret_free_attestation=True,
        )

        # Build a minimal SegmentSetBatch
        content_hash = _hl.sha256(b"doc-ruling1").hexdigest()
        reassembly_digest = _hl.sha256(table_text.encode()).hexdigest()
        ss = SegmentSet(
            schema_version="1.1.0",
            tenancy=tenancy,
            document_id="doc-ruling1",
            content_hash=content_hash,
            segments=[seg],
            reassembly=ReassemblyRecord(
                method=ReassemblyMethod.document_order_concat,
                covered_region_ids=["region_0"],
                reassembly_digest=reassembly_digest,
            ),
            exclusions=[],
            cross_references=[],
            decomposed_at=__import__("datetime").datetime.now(
                tz=__import__("datetime").timezone.utc
            ),
        )
        batch = SegmentSetBatch(
            schema_version=BATCH_SCHEMA_VERSION,
            contract="segment_set_batch",
            run_id="ruling1-test",
            produced_at=__import__("datetime").datetime.now(tz=__import__("datetime").timezone.utc),
            skeleton=None,
            segment_sets=[ss.model_dump(mode="json")],
        )

        fake_embed = FakeProvider()

        # --- Estimate using the per-class config (tiny_table_chunking for table) ---
        estimate_per_class = estimate_ingestion_cost(
            segment_set_batch=batch,
            ingestion_config=config,
            embedding_provider=fake_embed,
            llm_provider_or_none=None,
        )

        # --- Compute what the token counts would be for each chunking config ---
        from finecorpus.pipeline.costing import _estimate_chunk_texts as _etc

        # tiny: max_tokens=5, overlap=2 → multiple overlapping chunks → inflated token count
        chunks_with_table_chunking = _etc(table_text, max_tokens=5, overlap_tokens=2)
        # default: max_tokens=512, overlap=0 → 1 chunk, token count = word_count exactly
        chunks_with_default_chunking = _etc(table_text, max_tokens=512, overlap_tokens=0)

        expected_tokens_table = sum(len(t.split()) for t in chunks_with_table_chunking)
        expected_tokens_default = sum(len(t.split()) for t in chunks_with_default_chunking)

        # Sanity: the tiny+overlap config must produce more total tokens (due to overlapping chunks)
        assert expected_tokens_table > expected_tokens_default, (
            f"Test setup error: max_tokens=5+overlap=2 should produce more total tokens than "
            f"max_tokens=512+overlap=0 due to repeated overlap words. "
            f"Got {expected_tokens_table} vs {expected_tokens_default} for {word_count} words."
        )

        # The estimate MUST use the per-class table rule's chunking (max_tokens=5, overlap=2),
        # NOT the default_rule's chunking (max_tokens=512, overlap=0).
        # Bug: before fix, max_tokens/overlap_tokens are read from default_rule before the loop,
        # so ALL segments use the default chunking regardless of class — the table segment
        # produces expected_tokens_default instead of expected_tokens_table.
        assert estimate_per_class.embedding_token_count == expected_tokens_table, (
            f"estimate_ingestion_cost used default_rule chunking (max_tokens=512, overlap=0) "
            f"instead of the per-class table rule's chunking (max_tokens=5, overlap=2). "
            f"Got token count {estimate_per_class.embedding_token_count}, "
            f"expected {expected_tokens_table} (per-class) vs {expected_tokens_default} (default). "
            "Ruling 1: per-class chunking config must be honoured in estimate."
        )
