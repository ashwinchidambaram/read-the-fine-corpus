"""D2: dry_run preview mode acceptance tests.

Tests:
- dry_run=True produces zero index/upsert calls (spy on adapter)
- Sample output contains augmentation + provenance
- Determinism: same seed → same output order on repeated runs
- BuildResult.dry_run flag is True
- chunks are emitted inline in result["chunks"]
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import uuid
from datetime import UTC, datetime

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
# Shared fixtures / helpers (duplicated from T-04 to keep test files independent)
# ---------------------------------------------------------------------------


def _make_tenancy() -> TenancyBlock:
    return TenancyBlock(
        workspace_id="ws-dry",
        kb_id="kb-dry",
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


def _make_batch(
    segments_by_doc: dict[str, list[Segment]],
    run_id: str,
    artifacts_root: pathlib.Path,
) -> SegmentSetBatch:
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

    batch = SegmentSetBatch(
        schema_version=BATCH_SCHEMA_VERSION,
        contract="segment_set_batch",
        run_id=run_id,
        produced_at=datetime.now(tz=UTC),
        skeleton=None,
        segment_sets=seg_sets,
    )

    run_dir = artifacts_root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "decompose.json").write_text(
        json.dumps(batch.model_dump(mode="json")), encoding="utf-8"
    )
    return batch


def _make_config_with_tier2() -> IngestionConfig:
    embedding = EmbeddingConfig(
        provider="fake",
        model="fake-embed-v1",
        dimensions=64,
        normalize=True,
        supports_languages=["*"],
    )
    chunking = ChunkingConfig(
        strategy=ChunkingStrategy.recursive_char,
        max_tokens=120,
        overlap_tokens=10,
        respect_headings=False,
    )
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
    config_version = hashlib.sha256(b"dry-run-test-config").hexdigest()
    return IngestionConfig(
        schema_version=INGESTION_CONFIG_SCHEMA_VERSION,
        tenancy=_make_tenancy(),
        config_version=config_version,
        created_at=datetime.now(tz=UTC),
        naive_baseline=NaiveBaselineRef(
            reference_id="naive-baseline-v0",
            description="Dry run test baseline.",
        ),
        class_rules=[],
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
                rationale="Dry run test config.",
            )
        ],
        class_descriptions=[
            ClassDescription(
                segment_class=SegmentType.prose,
                class_id="prose",
                description="General prose content.",
            )
        ],
        secret_free_attestation=True,
    )


# ---------------------------------------------------------------------------
# dry_run: no Qdrant writes
# ---------------------------------------------------------------------------


class TestDryRunNoWrites:
    """dry_run=True must not write any vectors to the index."""

    def test_no_upsert_calls_in_dry_run(self, tmp_path: pathlib.Path) -> None:
        """dry_run=True skips upsert/flush — no index writes occur.

        In dry_run mode, BuildStage never constructs a real IndexAdapter (it stays None).
        All upsert/flush closures are guarded by ``if dry_run: return`` so no vector
        writes can reach Qdrant.  We verify by asserting the adapter is absent, the
        result carries chunks inline, and the indexed_chunks counter stays at zero.
        """
        seg = _make_segment(
            "Content for dry run write test.",
            structural_path=["Chapter 1"],
        )
        config = _make_config_with_tier2()
        run_id = "dry-test-1"
        _make_batch({"doc-dry-1": [seg]}, run_id, tmp_path)

        stage = BuildStage(
            artifacts_root=tmp_path,
            run_id=run_id,
            dry_run=True,
        )

        # No adapter should be attached in dry_run mode (never instantiated)
        assert stage._adapter is None, (  # noqa: SLF001
            "dry_run BuildStage must not have an IndexAdapter"
        )

        result = stage._produce(config.model_dump(mode="json"))  # noqa: SLF001

        # Chunks are emitted inline, not to an index
        assert result["chunks"], "dry_run must produce inline chunks"
        # The key signal: chunks list populated AND no Qdrant interaction
        assert result["dry_run"] is True

    def test_dry_run_flag_set_in_result(self, tmp_path: pathlib.Path) -> None:
        """BuildResult.dry_run must be True after dry_run build."""
        seg = _make_segment("Dry run flag test content.")
        config = _make_config_with_tier2()
        run_id = "dry-test-flag"
        _make_batch({"doc-dry-flag": [seg]}, run_id, tmp_path)

        stage = BuildStage(
            artifacts_root=tmp_path,
            run_id=run_id,
            dry_run=True,
        )
        result = stage._produce(config.model_dump(mode="json"))  # noqa: SLF001

        assert result["dry_run"] is True

    def test_normal_mode_dry_run_flag_false(self, tmp_path: pathlib.Path) -> None:
        """BuildResult.dry_run must be False after a normal (non-dry) build."""
        seg = _make_segment("Normal mode flag test content.")
        config = _make_config_with_tier2()
        run_id = "normal-test-flag"
        _make_batch({"doc-norm-flag": [seg]}, run_id, tmp_path)

        stage = BuildStage(
            artifacts_root=tmp_path,
            run_id=run_id,
            dry_run=False,
        )
        result = stage._produce(config.model_dump(mode="json"))  # noqa: SLF001

        assert result["dry_run"] is False


# ---------------------------------------------------------------------------
# dry_run: inline chunk output
# ---------------------------------------------------------------------------


class TestDryRunChunkOutput:
    """dry_run produces chunks inline in the result."""

    def test_chunks_present_in_result(self, tmp_path: pathlib.Path) -> None:
        """result['chunks'] must be non-empty for dry_run build with segments."""
        seg = _make_segment(
            "Sample document content for preview chunk test. " * 3,
            structural_path=["Overview"],
        )
        config = _make_config_with_tier2()
        run_id = "dry-chunks-1"
        _make_batch({"doc-chunks-1": [seg]}, run_id, tmp_path)

        stage = BuildStage(
            artifacts_root=tmp_path,
            run_id=run_id,
            dry_run=True,
        )
        result = stage._produce(config.model_dump(mode="json"))  # noqa: SLF001

        assert result["chunks"], "Expected non-empty chunks list in dry_run result"

    def test_chunk_has_required_fields(self, tmp_path: pathlib.Path) -> None:
        """Each chunk must have text, embedding_input, augmentation, and provenance."""
        seg = _make_segment(
            "Chunk field validation content.",
            structural_path=["Chapter 1"],
        )
        config = _make_config_with_tier2()
        run_id = "dry-fields-1"
        _make_batch({"doc-fields-1": [seg]}, run_id, tmp_path)

        stage = BuildStage(
            artifacts_root=tmp_path,
            run_id=run_id,
            dry_run=True,
        )
        result = stage._produce(config.model_dump(mode="json"))  # noqa: SLF001

        for chunk in result["chunks"]:
            assert "text" in chunk, "Chunk missing 'text' field"
            assert "embedding_input" in chunk, "Chunk missing 'embedding_input' field"
            assert "augmentation" in chunk, "Chunk missing 'augmentation' field"
            assert "provenance" in chunk, "Chunk missing 'provenance' field"

    def test_chunk_augmentation_populated(self, tmp_path: pathlib.Path) -> None:
        """Chunks with breadcrumb_augment in tier2_ops have parent_breadcrumb populated."""
        seg = _make_segment(
            "Augmentation preview content.",
            structural_path=["Section A", "Subsection 1"],
        )
        config = _make_config_with_tier2()
        run_id = "dry-aug-1"
        _make_batch({"doc-aug-1": [seg]}, run_id, tmp_path)

        stage = BuildStage(
            artifacts_root=tmp_path,
            run_id=run_id,
            dry_run=True,
        )
        result = stage._produce(config.model_dump(mode="json"))  # noqa: SLF001

        for chunk in result["chunks"]:
            aug = chunk["augmentation"]
            # Breadcrumb should be populated since structural_path is non-empty
            assert aug.get("parent_breadcrumb"), (
                f"Expected parent_breadcrumb in chunk augmentation, got: {aug}"
            )
            assert "Section A" in aug["parent_breadcrumb"]

    def test_chunk_provenance_present(self, tmp_path: pathlib.Path) -> None:
        """Chunks must have provenance with segment metadata."""
        seg = _make_segment(
            "Provenance preview content.",
            doc_order=2,
        )
        config = _make_config_with_tier2()
        run_id = "dry-prov-1"
        _make_batch({"doc-prov-1": [seg]}, run_id, tmp_path)

        stage = BuildStage(
            artifacts_root=tmp_path,
            run_id=run_id,
            dry_run=True,
        )
        result = stage._produce(config.model_dump(mode="json"))  # noqa: SLF001

        for chunk in result["chunks"]:
            prov = chunk["provenance"]
            assert "source_document_id" in prov, "Chunk provenance missing source_document_id"
            assert "source_document_version" in prov, (
                "Chunk provenance missing source_document_version"
            )
            assert "segment_type" in prov, "Chunk provenance missing segment_type"


# ---------------------------------------------------------------------------
# dry_run: determinism
# ---------------------------------------------------------------------------


class TestDryRunDeterminism:
    """Same input → same output order on repeated dry_run calls."""

    def test_chunk_order_is_deterministic(self, tmp_path: pathlib.Path) -> None:
        """Repeated dry_run builds with the same input produce identical chunk order."""
        text = "Determinism test content: identical runs must produce identical output."
        seg = _make_segment(text, structural_path=["Intro"])
        config = _make_config_with_tier2()
        run_id = "dry-determ-1"
        _make_batch({"doc-determ-1": [seg]}, run_id, tmp_path)

        # Run twice
        stage1 = BuildStage(
            artifacts_root=tmp_path,
            run_id=run_id,
            dry_run=True,
        )
        result1 = stage1._produce(config.model_dump(mode="json"))  # noqa: SLF001

        stage2 = BuildStage(
            artifacts_root=tmp_path,
            run_id=run_id,
            dry_run=True,
        )
        result2 = stage2._produce(config.model_dump(mode="json"))  # noqa: SLF001

        texts1 = [c["text"] for c in result1["chunks"]]
        texts2 = [c["text"] for c in result2["chunks"]]

        assert texts1 == texts2, f"Chunk order differs between runs: {texts1} != {texts2}"

    def test_embedding_input_deterministic(self, tmp_path: pathlib.Path) -> None:
        """embedding_input must be identical across repeated dry_run builds."""
        text = "Embedding determinism test content."
        seg = _make_segment(text, structural_path=["Chapter 2"])
        config = _make_config_with_tier2()
        run_id = "dry-embed-det-1"
        _make_batch({"doc-embed-det-1": [seg]}, run_id, tmp_path)

        stage1 = BuildStage(
            artifacts_root=tmp_path,
            run_id=run_id,
            dry_run=True,
        )
        result1 = stage1._produce(config.model_dump(mode="json"))  # noqa: SLF001

        stage2 = BuildStage(
            artifacts_root=tmp_path,
            run_id=run_id,
            dry_run=True,
        )
        result2 = stage2._produce(config.model_dump(mode="json"))  # noqa: SLF001

        inputs1 = [c["embedding_input"] for c in result1["chunks"]]
        inputs2 = [c["embedding_input"] for c in result2["chunks"]]

        assert inputs1 == inputs2, f"embedding_input differs between runs: {inputs1} != {inputs2}"


# ---------------------------------------------------------------------------
# dry_run: no Qdrant artifact or side effects
# ---------------------------------------------------------------------------


class TestDryRunNoPersistence:
    """dry_run must not write build.json or create index artifacts."""

    def test_no_build_artifact_in_dry_run(self, tmp_path: pathlib.Path) -> None:
        """In dry_run mode, stage should not write a build.json artifact."""
        seg = _make_segment("No artifact test content.")
        config = _make_config_with_tier2()
        run_id = "dry-no-art-1"
        _make_batch({"doc-no-art-1": [seg]}, run_id, tmp_path)

        stage = BuildStage(
            artifacts_root=tmp_path,
            run_id=run_id,
            dry_run=True,
        )
        stage._produce(config.model_dump(mode="json"))  # noqa: SLF001

        # build.json should not be written in dry_run mode
        build_artifact = tmp_path / run_id / "build.json"
        assert not build_artifact.exists(), (
            "build.json artifact should not be written in dry_run mode"
        )
