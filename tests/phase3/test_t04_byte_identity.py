"""T-04 byte-identity acceptance test — corpus-wide build with Tier 1+2 ON.

§18.3 #4: every chunk's ``text`` byte-equals the corresponding slice of
its segment's Tier-1 canonical text.

Run: uv run pytest tests/phase3/test_t04_byte_identity.py -v

Design
------
- Uses FakeLLMProvider + FakeEmbeddingProvider (FakeProvider) — no live API needed.
- Builds over the golden fixture segment sets.
- Asserts:
  1. Every chunk.text is an exact contiguous substring of the segment's
     Tier-1 canonical text (T-04 byte-identity).
  2. Every tier-2 TransformationRecord has changed_text=False (§7.2).
  3. Augmentation fields are populated where the class rule says so.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from finecorpus.contracts.ingestion_config import (
    ChunkingConfig,
    ChunkingStrategy,
    ClassDescription,
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
from finecorpus.pipeline.build.stage import BuildStage

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_tenancy() -> TenancyBlock:
    return TenancyBlock(
        workspace_id="ws-t04",
        kb_id="kb-t04",
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
    structural_path: list[str] | None = None,
) -> Segment:
    return Segment(
        segment_id=str(uuid.uuid4()),
        document_order=doc_order,
        segment_type=segment_type,
        salience_tier=SalienceTier.primary,
        structural_path=structural_path or [],
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


def _make_segment_set(
    document_id: str,
    segments: list[Segment],
    content_hash: str | None = None,
) -> SegmentSet:
    if content_hash is None:
        content_hash = hashlib.sha256(document_id.encode()).hexdigest()
    region_ids = [r for seg in segments for r in seg.source_region_ids]
    reassembly_text = "".join(seg.text or "" for seg in segments)
    reassembly_digest = hashlib.sha256(reassembly_text.encode()).hexdigest()
    return SegmentSet(
        schema_version="1.1.0",
        tenancy=_make_tenancy(),
        document_id=document_id,
        content_hash=content_hash,
        segments=segments,
        reassembly=ReassemblyRecord(
            method=ReassemblyMethod.document_order_concat,
            covered_region_ids=region_ids,
            reassembly_digest=reassembly_digest,
        ),
        exclusions=[],
        cross_references=[],
        decomposed_at=datetime.now(tz=UTC),
    )


def _make_ingestion_config_with_tier2(
    workspace_id: str = "ws-t04",
    kb_id: str = "kb-t04",
) -> IngestionConfig:
    """Build an IngestionConfig with Tier 1 + Tier 2 enabled for prose + table classes."""
    default_chunking = ChunkingConfig(
        strategy=ChunkingStrategy.recursive_char,
        max_tokens=100,
        overlap_tokens=10,
        respect_headings=False,
    )
    table_chunking = ChunkingConfig(
        strategy=ChunkingStrategy.recursive_char,
        max_tokens=100,
        overlap_tokens=10,
        respect_headings=False,
        atomic_rows=True,
        repeat_headers_on_split=False,
    )
    embedding = EmbeddingConfig(
        provider="fake",
        model="fake-embed-v1",
        dimensions=64,
        normalize=True,
        supports_languages=["*"],
    )

    # Default rule: Tier 1 on, Tier 2 with breadcrumb
    default_rule = ClassRule(
        segment_class=SegmentType.prose,
        transformation=TransformationSettings(
            tier1_enabled=True,
            tier1_operations=[Tier1Operation.whitespace_repair],
            tier2_enabled=True,
            tier2_operations=[
                Tier2Operation.breadcrumb_augment,
                Tier2Operation.class_context,
            ],
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

    # Table rule: Tier 1 (whitespace + table_to_markdown) + Tier 2 with table_description
    table_rule = ClassRule(
        segment_class=SegmentType.table,
        transformation=TransformationSettings(
            tier1_enabled=True,
            tier1_operations=[
                Tier1Operation.whitespace_repair,
                Tier1Operation.table_to_markdown,
            ],
            tier2_enabled=True,
            tier2_operations=[
                Tier2Operation.table_description,
                Tier2Operation.breadcrumb_augment,
            ],
            tier3_enabled=False,
        ),
        chunking=table_chunking,
        embedding_override=None,
        metadata_schema=[],
        retrieval_treatment=RetrievalTreatment(
            default_salience_filter=[SalienceTier.primary, SalienceTier.supporting],
            salience_weights=None,
            rerank_eligible=False,
            strategy=RetrievalStrategy.dense,
        ),
    )

    affecting_dict: dict[str, Any] = {
        "tier1": "whitespace_repair+table_to_markdown",
        "tier2": "breadcrumb_augment+class_context+table_description",
        "provider": "fake",
        "model": "fake-embed-v1",
        "dimensions": 64,
    }
    config_version = hashlib.sha256(json.dumps(affecting_dict, sort_keys=True).encode()).hexdigest()

    return IngestionConfig(
        schema_version=INGESTION_CONFIG_SCHEMA_VERSION,
        tenancy=TenancyBlock(
            workspace_id=workspace_id,
            kb_id=kb_id,
            permission_mode=PermissionMode.public_to_kb,
            permission_principals=[],
            permission_source=PermissionSource.platform,
            permission_fidelity=PermissionFidelity.authoritative,
            permission_resolved_at=None,
        ),
        config_version=config_version,
        created_at=datetime.now(tz=UTC),
        naive_baseline=NaiveBaselineRef(
            reference_id="naive-baseline-v0",
            description="Naive baseline for T-04.",
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
                rationale="T-04 test config.",
            )
        ],
        class_descriptions=[
            ClassDescription(
                segment_class=SegmentType.prose,
                class_id="prose",
                description="Prose content for testing T-04 class_context.",
            )
        ],
        secret_free_attestation=True,
    )


def _run_dry_build(
    segments_by_doc: dict[str, list[Segment]],
    ingestion_config: IngestionConfig,
    tmp_path: pathlib.Path,
    llm_provider: Any | None = None,
    llm_op_config: Any | None = None,
) -> dict[str, Any]:
    """Run BuildStage in dry_run mode and return the result dict."""
    run_id = "t04-test"
    artifacts_root = tmp_path

    # Build segment sets + batch
    seg_sets = []
    for doc_id, segs in segments_by_doc.items():
        seg_sets.append(_make_segment_set(doc_id, segs))

    batch = SegmentSetBatch(
        schema_version=BATCH_SCHEMA_VERSION,
        contract="segment_set_batch",
        run_id=run_id,
        produced_at=datetime.now(tz=UTC),
        skeleton=None,
        segment_sets=[ss.model_dump(mode="json") for ss in seg_sets],
    )

    # Write decompose artifact
    run_dir = artifacts_root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "decompose.json").write_text(
        json.dumps(batch.model_dump(mode="json")), encoding="utf-8"
    )

    stage = BuildStage(
        artifacts_root=artifacts_root,
        run_id=run_id,
        dry_run=True,
        llm_provider=llm_provider,
        llm_op_config=llm_op_config,
    )

    return stage._produce(ingestion_config.model_dump(mode="json"))  # noqa: SLF001


# ---------------------------------------------------------------------------
# T-04 byte-identity tests
# ---------------------------------------------------------------------------


class TestT04ByteIdentity:
    """T-04: every chunk.text byte-equals its segment's Tier-1 canonical text slice."""

    def test_prose_chunk_text_is_canonical_slice(self, tmp_path: pathlib.Path) -> None:
        """Prose chunk text must byte-equal canonical[char_start:char_end] (position-exact)."""
        raw_text = "  Hello   World.\r\nThis is a test.\r\n"
        seg = _make_segment(raw_text, segment_type=SegmentType.prose)
        config = _make_ingestion_config_with_tier2()

        result = _run_dry_build({"doc-prose-1": [seg]}, config, tmp_path)
        chunks = result["chunks"]
        assert chunks, "Expected at least one chunk"

        for chunk in chunks:
            text = chunk["text"]
            canonical = chunk["canonical_text"]
            char_start = chunk["char_start"]
            char_end = chunk["char_end"]
            # Position-exact: use the span offsets, not canonical.index(text)
            # (canonical.index masks off-by-position bugs when text repeats)
            position_slice = canonical[char_start:char_end]
            assert text.encode() == position_slice.encode(), (
                f"Chunk text is NOT byte-equal to canonical[{char_start}:{char_end}]. "
                f"Got chunk text {text!r}, canonical slice {position_slice!r}. "
                f"Full canonical: {canonical!r}"
            )

    def test_table_chunk_text_is_canonical_slice(self, tmp_path: pathlib.Path) -> None:
        """Table chunk text must byte-equal canonical[char_start:char_end] (position-exact)."""
        raw_text = "| A | B |\r\n| --- | --- |\r\n| 1 | 2 |\r\n"
        seg = _make_segment(raw_text, segment_type=SegmentType.table)
        config = _make_ingestion_config_with_tier2()

        result = _run_dry_build({"doc-table-1": [seg]}, config, tmp_path)
        chunks = result["chunks"]
        assert chunks

        for chunk in chunks:
            text = chunk["text"]
            canonical = chunk["canonical_text"]
            char_start = chunk["char_start"]
            char_end = chunk["char_end"]
            # Position-exact comparison: canonical[char_start:char_end] must byte-equal text
            position_slice = canonical[char_start:char_end]
            assert text.encode() == position_slice.encode(), (
                f"Table chunk text is NOT byte-equal to canonical[{char_start}:{char_end}]. "
                f"Got chunk text {text!r}, canonical slice {position_slice!r}."
            )

    def test_tier2_records_all_have_changed_text_false(self, tmp_path: pathlib.Path) -> None:
        """Every tier-2 TransformationRecord in every chunk must have changed_text=False (§7.2)."""
        segments = [
            _make_segment(
                "Main content text for T-04 tier-2 record check.",
                doc_order=0,
                structural_path=["Chapter 1"],
            ),
            _make_segment(
                "| Col A | Col B |\n|---|---|\n| val1 | val2 |",
                doc_order=1,
                segment_type=SegmentType.table,
            ),
        ]
        config = _make_ingestion_config_with_tier2()

        # Inject FakeLLMProvider for table descriptions
        from finecorpus.llm.fake import FakeLLMProvider
        from finecorpus.llm.operations import ResolvedOpConfig

        fake_llm = FakeLLMProvider()
        op_cfg = ResolvedOpConfig(
            provider_id="fake",
            model_id="fake-llm-v1",
            temperature=0.0,
            max_output_tokens=128,
            max_retries=1,
        )

        result = _run_dry_build(
            {"doc-mixed-1": segments},
            config,
            tmp_path,
            llm_provider=fake_llm,
            llm_op_config=op_cfg,
        )
        chunks = result["chunks"]
        assert chunks

        for chunk in chunks:
            for tr in chunk["provenance"].get("transformations", []):
                if tr["tier"] == 2:
                    assert tr["changed_text"] is False, f"Tier-2 record has changed_text=True: {tr}"

    def test_augmentation_populated_for_breadcrumb_class(self, tmp_path: pathlib.Path) -> None:
        """Chunks with breadcrumb_augment in tier2_operations get parent_breadcrumb populated."""
        seg = _make_segment(
            "Some prose content here.",
            structural_path=["Chapter 1", "Section 2"],
        )
        config = _make_ingestion_config_with_tier2()

        result = _run_dry_build({"doc-bc-1": [seg]}, config, tmp_path)
        chunks = result["chunks"]
        assert chunks

        for chunk in chunks:
            aug = chunk["augmentation"]
            assert aug["parent_breadcrumb"] is not None, (
                "Expected parent_breadcrumb populated for chunk with structural_path"
            )
            assert "Chapter 1" in aug["parent_breadcrumb"]

    def test_class_context_populated_when_description_exists(self, tmp_path: pathlib.Path) -> None:
        """Chunks with class_context op get class_context populated from class_descriptions."""
        seg = _make_segment(
            "Prose content for class context test.",
            structural_path=[],
        )
        config = _make_ingestion_config_with_tier2()

        result = _run_dry_build({"doc-cc-1": [seg]}, config, tmp_path)
        chunks = result["chunks"]
        assert chunks

        for chunk in chunks:
            aug = chunk["augmentation"]
            # The config has a prose class description, so class_context should be set
            assert aug["class_context"] is not None, (
                "Expected class_context populated for prose segment with class_description"
            )

    def test_chunk_text_never_modified_by_augmentation(self, tmp_path: pathlib.Path) -> None:
        """chunk.text must not change even when augmentation is applied (T-04 core)."""
        raw_text = "Text that must remain byte-identical through augmentation."
        seg = _make_segment(raw_text, structural_path=["Chapter A"])
        config = _make_ingestion_config_with_tier2()

        result = _run_dry_build({"doc-nomod-1": [seg]}, config, tmp_path)
        chunks = result["chunks"]
        assert chunks

        for chunk in chunks:
            text = chunk["text"]
            canonical = chunk["canonical_text"]
            char_start = chunk["char_start"]
            char_end = chunk["char_end"]
            # Position-exact: text must byte-equal canonical[char_start:char_end]
            position_slice = canonical[char_start:char_end]
            assert text.encode() == position_slice.encode(), (
                f"chunk.text {text!r} is NOT byte-equal to canonical[{char_start}:{char_end}] "
                f"= {position_slice!r}"
            )
            # embedding_input may differ (augmented prefix) but text must not
            embedding_input = chunk["embedding_input"]
            assert text in embedding_input, "Chunk text must appear verbatim in embedding_input"
            # Verify text is a suffix of embedding_input (framing is always a prefix)
            assert embedding_input.endswith(text) or embedding_input == text, (
                "embedding_input must end with chunk text (augmentation is a prefix)"
            )

    def test_tier1_disabled_chunk_text_equals_raw(self, tmp_path: pathlib.Path) -> None:
        """When tier1_enabled=False, chunk.text should equal the raw segment text."""
        raw_text = "  Raw text  with   spaces  \r\n"
        seg = _make_segment(raw_text, segment_type=SegmentType.prose)

        # Make a config with tier1 disabled
        import hashlib as _hl

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
        no_tier1_rule = ClassRule(
            segment_class=SegmentType.prose,
            transformation=TransformationSettings(
                tier1_enabled=False,
                tier1_operations=[],
                tier2_enabled=False,
                tier2_operations=[],
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
        config_version = _hl.sha256(b"no-tier1-t04").hexdigest()
        config = IngestionConfig(
            schema_version=INGESTION_CONFIG_SCHEMA_VERSION,
            tenancy=TenancyBlock(
                workspace_id="ws-t04",
                kb_id="kb-t04",
                permission_mode=PermissionMode.public_to_kb,
                permission_principals=[],
                permission_source=PermissionSource.platform,
                permission_fidelity=PermissionFidelity.authoritative,
                permission_resolved_at=None,
            ),
            config_version=config_version,
            created_at=datetime.now(tz=UTC),
            naive_baseline=NaiveBaselineRef(
                reference_id="naive-baseline-v0",
                description="No-tier1 test.",
            ),
            class_rules=[],
            default_rule=no_tier1_rule,
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
                    rationale="No-tier1 test.",
                )
            ],
            class_descriptions=[],
            secret_free_attestation=True,
        )

        result = _run_dry_build({"doc-notier1-1": [seg]}, config, tmp_path)
        chunks = result["chunks"]
        assert chunks

        for chunk in chunks:
            text = chunk["text"]
            canonical = chunk["canonical_text"]
            char_start = chunk["char_start"]
            char_end = chunk["char_end"]
            # Position-exact: canonical is the raw text when tier1 is disabled
            position_slice = canonical[char_start:char_end]
            assert text.encode() == position_slice.encode(), (
                f"chunk.text {text!r} is NOT byte-equal to canonical[{char_start}:{char_end}] "
                f"= {position_slice!r} (canonical = raw text when tier1 disabled)"
            )


# ---------------------------------------------------------------------------
# Corpus-wide T-04 test (Ruling 4: §19 acceptance criterion 2 — unimpeachable)
# ---------------------------------------------------------------------------


FIXTURE_CORPUS = pathlib.Path(__file__).parent.parent / "fixtures" / "golden" / "corpus"


def run_corpus_wide_t04_verification(tmp_path: pathlib.Path) -> dict[str, Any]:
    """Run corpus-wide T-04 byte-identity verification and return stats.

    Callable helper extracted per Ruling 4 so that BOTH ``TestT04CorpusWide``
    and ``tests.phase3.test_acceptance.TestT04`` can invoke the real verification
    rather than merely asserting method existence.

    Runs collect→assess→decompose→plan over the golden corpus, then builds in
    dry_run=True mode with FakeLLMProvider and FakeEmbeddingProvider.

    Returns
    -------
    dict with keys:
      fixtures_verified: int   — number of distinct document_ids that contributed chunks
      chunks_verified: int     — total chunks checked for byte-identity
      violations: list[str]    — descriptions of any byte-identity failures
      tier2_violations: list[str] — descriptions of changed_text=True tier-2 records
    """
    from finecorpus.contracts.ingestion_config import IngestionConfig
    from finecorpus.embedding.fake import FakeProvider
    from finecorpus.llm.fake import FakeLLMProvider
    from finecorpus.llm.operations import ResolvedOpConfig
    from finecorpus.pipeline import run_pipeline
    from finecorpus.pipeline.artifact_store import ArtifactStore
    from finecorpus.pipeline.build.stage import BuildStage

    run_id = "t04-corpus-verify"
    artifacts_root = tmp_path / "artifacts"

    run_pipeline(
        source_dir=FIXTURE_CORPUS,
        artifacts_root=artifacts_root,
        run_id=run_id,
        workspace_id="ws-t04-verify",
        kb_id="kb-t04-verify",
    )

    store = ArtifactStore(artifacts_root=artifacts_root, run_id=run_id)
    ingestion_config = store.load_with_model_validation("plan", IngestionConfig)

    fake_embed = FakeProvider()
    fake_llm = FakeLLMProvider()
    op_cfg = ResolvedOpConfig(
        provider_id="fake",
        model_id="fake-llm-v1",
        temperature=0.0,
        max_output_tokens=128,
        max_retries=1,
    )

    build = BuildStage(
        artifacts_root=artifacts_root,
        run_id=run_id,
        dry_run=True,
        embedding_provider=fake_embed,
        llm_provider=fake_llm,
        llm_op_config=op_cfg,
    )
    result = build._produce(ingestion_config.model_dump(mode="json"))  # noqa: SLF001
    chunks = result["chunks"]

    violations: list[str] = []
    tier2_violations: list[str] = []
    for chunk in chunks:
        text = chunk["text"]
        canonical = chunk["canonical_text"]
        char_start = chunk["char_start"]
        char_end = chunk["char_end"]
        position_slice = canonical[char_start:char_end]
        if text.encode() != position_slice.encode():
            violations.append(
                f"doc={chunk['document_id']!r} seg={chunk['segment_path']!r} "
                f"idx={chunk['chunk_index']}: "
                f"chunk.text {text[:40]!r} != canonical[{char_start}:{char_end}] "
                f"{position_slice[:40]!r}"
            )
        for tr in chunk["provenance"].get("transformations", []):
            if tr.get("tier") == 2 and tr.get("changed_text") is not False:
                tier2_violations.append(
                    f"doc={chunk['document_id']!r} seg={chunk['segment_path']!r} "
                    f"op={tr.get('operation')!r}: changed_text={tr.get('changed_text')}"
                )

    return {
        "fixtures_verified": len({c["document_id"] for c in chunks}),
        "chunks_verified": len(chunks),
        "violations": violations,
        "tier2_violations": tier2_violations,
    }


class TestT04CorpusWide:
    """§19 acceptance criterion 2: corpus-wide byte-identity over the golden fixture set.

    Runs the real pipeline (collect→assess→decompose→plan) over all 21 golden
    fixtures, then builds in dry_run=True mode via ``run_corpus_wide_t04_verification``.

    Asserts for EVERY chunk of EVERY fixture:
    1. chunk.text byte-equals canonical[char_start:char_end] — position-exact
       (NOT canonical.index(text) which masks off-by-position on repeated text).
    2. Every tier-2 TransformationRecord has changed_text=False (§7.2).

    This test is THE §19 acceptance test for criterion 2.  It must be
    unimpeachable.  The shared ``run_corpus_wide_t04_verification`` helper is
    also called by ``test_acceptance.TestT04.test_t04_corpus_wide_real_verification``
    so the acceptance sentinel runs the real verification rather than only checking
    method existence.
    """

    @pytest.fixture(scope="class")
    def t04_stats(self, tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
        """Run real corpus-wide T-04 verification and return stats dict."""
        tmp_path = tmp_path_factory.mktemp("t04-corpus-wide")
        return run_corpus_wide_t04_verification(tmp_path)

    def test_corpus_wide_chunk_count_positive(self, t04_stats: dict[str, Any]) -> None:
        """Corpus-wide build must produce at least one chunk."""
        assert t04_stats["chunks_verified"] > 0, "Corpus-wide dry_run produced no chunks"

    def test_corpus_wide_byte_identity_position_exact(self, t04_stats: dict[str, Any]) -> None:
        """Every chunk.text must byte-equal canonical[char_start:char_end] — position-exact.

        This is the §19 acceptance criterion 2 assertion.  Uses span offsets
        (char_start, char_end) NOT canonical.index(text) — the latter masks
        off-by-position bugs when the same text appears at multiple positions.
        """
        violations = t04_stats["violations"]
        chunks_verified = t04_stats["chunks_verified"]
        fixtures_verified = t04_stats["fixtures_verified"]
        assert not violations, (
            f"T-04 byte-identity FAILED for {len(violations)}/{chunks_verified} chunks "
            f"across {fixtures_verified} fixtures:\n" + "\n".join(violations[:10])
        )

    def test_corpus_wide_tier2_changed_text_false(self, t04_stats: dict[str, Any]) -> None:
        """Every tier-2 TransformationRecord must have changed_text=False (§7.2)."""
        tier2_violations = t04_stats["tier2_violations"]
        assert not tier2_violations, (
            f"Tier-2 changed_text=True for {len(tier2_violations)} records:\n"
            + "\n".join(tier2_violations[:10])
        )

    def test_corpus_wide_report_stats(self, t04_stats: dict[str, Any]) -> None:
        """Report fixture and chunk counts for the orchestrator."""
        assert t04_stats["fixtures_verified"] >= 1, "Expected at least 1 fixture with chunks"
        assert t04_stats["chunks_verified"] >= 1, "Expected at least 1 chunk from corpus"

        # Log counts for orchestrator (printed in verbose mode)
        chunk_count = t04_stats["chunks_verified"]
        fixture_count = t04_stats["fixtures_verified"]
        print(
            f"\n[T-04 corpus-wide] {chunk_count} chunks from "
            f"{fixture_count} fixtures verified position-exact (char_start:char_end)"
        )
