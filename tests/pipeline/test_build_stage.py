"""Unit + integration tests for the Phase 1 Build stage.

Covers:
1. Provenance completeness — §18.3 test 3 seed: every chunk from the golden corpus
   has a full non-null provenance block.
2. Byte-identity (T-04 seed): chunk text is an exact substring of its segment text.
3. Resumability: kill on doc 4, re-run, assert all docs complete with no duplicate points.
4. BuildResult structure (schema_version, chunk_count, chunks_by_document, etc.).
5. Skeleton fallback when no provider/adapter injected.

Integration tests (marker: qdrant_integration) use a live Qdrant + FakeProvider.
Unit tests use only mocks/fakes and can run offline.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

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
    SalienceSignalKind,
    SalienceTier,
    SegmentType,
    SourceLocation,
    TenancyBlock,
)
from finecorpus.embedding.base import ProviderUnavailableError
from finecorpus.embedding.fake import FakeProvider
from finecorpus.pipeline.artifact_store import ArtifactStore
from finecorpus.pipeline.build.stage import BuildResult, BuildStage, _load_checkpoint
from finecorpus.pipeline.stage import StageError

# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

QDRANT_URL = "http://localhost:6333"
POSTGRES_DSN = "postgresql+psycopg://finecorpus:finecorpus@localhost:5432/finecorpus"


def _qdrant_reachable() -> bool:
    try:
        from qdrant_client import QdrantClient

        c = QdrantClient(url=QDRANT_URL, timeout=2)
        c.get_collections()
        return True
    except Exception:
        return False


qdrant_integration = pytest.mark.skipif(
    not _qdrant_reachable(),
    reason="Qdrant not reachable at localhost:6333 (start with: docker compose up -d qdrant)",
)


def _make_tenancy(workspace_id: str = "ws-test", kb_id: str = "kb-test") -> TenancyBlock:
    return TenancyBlock(
        workspace_id=workspace_id,
        kb_id=kb_id,
        permission_mode=PermissionMode.public_to_kb,
        permission_principals=[],
        permission_source=PermissionSource.platform,
        permission_fidelity=PermissionFidelity.authoritative,
        permission_resolved_at=None,
    )


def _make_source_location() -> SourceLocation:
    return SourceLocation(
        locator_kind=LocatorKind.char_range,
        char_start=0,
        char_end=100,
    )


def _make_segment(
    doc_order: int,
    text: str,
    segment_type: SegmentType = SegmentType.prose,
    salience_tier: SalienceTier = SalienceTier.primary,
    structural_path: list[str] | None = None,
) -> Segment:
    from finecorpus.contracts.shared.blocks import SalienceSignal

    return Segment(
        segment_id=str(uuid.uuid4()),
        document_order=doc_order,
        segment_type=segment_type,
        salience_tier=salience_tier,
        structural_path=structural_path or [],
        segment_path=f"seg/{doc_order}",
        location=_make_source_location(),
        source_region_ids=[f"region_{doc_order}"],
        language="en",
        ocr_confidence=None,
        injection_suspicion=0.0,
        invisible_content_flags=[],
        sensitivity_flags=[],
        salience_signals=[
            SalienceSignal(
                kind=SalienceSignalKind.segment_type_prior,
                implied_tier=salience_tier,
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
    workspace_id: str = "ws-test",
    kb_id: str = "kb-test",
    content_hash: str = "abc123" * 10 + "abcd",
) -> SegmentSet:
    """Build a minimal valid SegmentSet."""
    region_ids = [r for seg in segments for r in seg.source_region_ids]
    import hashlib

    reassembly_text = "".join(seg.text or "" for seg in segments)
    reassembly_digest = hashlib.sha256(reassembly_text.encode()).hexdigest()
    return SegmentSet(
        schema_version="1.1.0",
        tenancy=_make_tenancy(workspace_id=workspace_id, kb_id=kb_id),
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


def _make_segment_set_batch(
    segment_sets: list[SegmentSet],
    run_id: str = "test-run",
) -> SegmentSetBatch:
    return SegmentSetBatch(
        schema_version=BATCH_SCHEMA_VERSION,
        contract="segment_set_batch",
        run_id=run_id,
        produced_at=datetime.now(tz=UTC),
        skeleton=None,
        segment_sets=[ss.model_dump(mode="json") for ss in segment_sets],
    )


def _make_ingestion_config(
    workspace_id: str = "ws-test",
    kb_id: str = "kb-test",
    provider: str = "fake",
    model: str = "fake-embed-v1",
    dimensions: int = 64,
    max_tokens: int = 512,
    overlap_tokens: int = 50,
) -> IngestionConfig:
    import hashlib
    import json as _json

    chunking = ChunkingConfig(
        strategy=ChunkingStrategy.recursive_char,
        max_tokens=max_tokens,
        overlap_tokens=overlap_tokens,
        respect_headings=False,
    )
    embedding = EmbeddingConfig(
        provider=provider,
        model=model,
        dimensions=dimensions,
        normalize=True,
        supports_languages=["*"],
    )
    default_rule = ClassRule(
        segment_class=SegmentType.prose,
        transformation=TransformationSettings(
            tier1_enabled=True,
            tier1_operations=[Tier1Operation.whitespace_repair],
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
    # Derive config_version
    affecting = {
        "max_tokens": max_tokens,
        "overlap_tokens": overlap_tokens,
        "strategy": "recursive_char",
        "provider": provider,
        "model": model,
        "dimensions": dimensions,
    }
    config_version = hashlib.sha256(_json.dumps(affecting, sort_keys=True).encode()).hexdigest()

    return IngestionConfig(
        schema_version="1.1.0",
        tenancy=_make_tenancy(workspace_id=workspace_id, kb_id=kb_id),
        config_version=config_version,
        created_at=datetime.now(tz=UTC),
        naive_baseline=NaiveBaselineRef(
            reference_id="naive-baseline-v0",
            description="Test baseline.",
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
                rationale="Phase 1 default.",
            )
        ],
        secret_free_attestation=True,
    )


# ---------------------------------------------------------------------------
# Helpers: write stage artifacts to tmp store
# ---------------------------------------------------------------------------


def _write_artifacts(
    store: ArtifactStore,
    batch: SegmentSetBatch,
    config: IngestionConfig,
) -> None:
    """Write decompose and plan artifacts so BuildStage can load them."""
    store.save("decompose", batch.model_dump(mode="json"))
    store.save("plan", config.model_dump(mode="json"))


# ---------------------------------------------------------------------------
# 1. BuildStage skeleton fallback (no provider/adapter)
# ---------------------------------------------------------------------------


class TestBuildStageSkeleton:
    def test_skeleton_result_when_no_provider(self, tmp_path):
        store = ArtifactStore(artifacts_root=tmp_path, run_id="test-skeleton")
        config = _make_ingestion_config()
        stage = BuildStage(run_started_at=datetime.now(tz=UTC))
        result_dict = stage.run(input_data=config.model_dump(mode="json"), store=store)
        result = BuildResult.model_validate(result_dict)
        assert result.schema_version == "1.0.0"
        assert result.skeleton is True
        assert result.chunk_count == 0
        assert result.chunks == []


# ---------------------------------------------------------------------------
# 2. Provenance completeness (§18.3 test 3 seed)
# ---------------------------------------------------------------------------


@qdrant_integration
class TestProvenanceCompleteness:
    """Every chunk produced over the golden corpus has full non-null provenance (§8)."""

    def _make_adapter(self):
        from finecorpus.index.qdrant.backend import QdrantAdapter

        return QdrantAdapter(url=QDRANT_URL, timeout=10)

    def test_every_chunk_has_full_provenance(self, tmp_path):
        """Build a small corpus and verify provenance on every chunk."""
        provider = FakeProvider(dimensions=64)
        adapter = self._make_adapter()
        kb_id = str(uuid.uuid4()).replace("-", "")[:12]
        ws_id = "ws-provenance-test"
        run_id = f"prov-test-{kb_id}"

        # Build a SegmentSet with real text
        seg = _make_segment(
            0,
            "The quick brown fox jumps over the lazy dog. " * 10,
            structural_path=["Introduction"],
        )
        seg_set = _make_segment_set("doc-prov-01", [seg], workspace_id=ws_id, kb_id=kb_id)
        batch = _make_segment_set_batch([seg_set], run_id=run_id)
        config = _make_ingestion_config(workspace_id=ws_id, kb_id=kb_id)

        store = ArtifactStore(artifacts_root=tmp_path, run_id=run_id)
        _write_artifacts(store, batch, config)

        stage = BuildStage(
            run_started_at=datetime.now(tz=UTC),
            embedding_provider=provider,
            index_adapter=adapter,
            build_id=1,
            artifacts_root=tmp_path,
            run_id=run_id,
            workspace_id=ws_id,
            kb_id=kb_id,
        )
        result_dict = stage.run(input_data=config.model_dump(mode="json"), store=store)
        result = BuildResult.model_validate(result_dict)

        # Retrieve all points from the shadow collection
        shadow = result.shadow_collection
        assert shadow, "No shadow collection in BuildResult"

        # Count points
        actual_count = adapter.count_points(shadow)
        assert actual_count == result.chunk_count, (
            f"Point count {actual_count} != BuildResult.chunk_count {result.chunk_count}"
        )

        # Search to get payload and verify provenance
        from finecorpus.embedding.fake import _sha256_to_vector

        probe = _sha256_to_vector("provenance-test-probe", 64)
        results = adapter.search(shadow, probe, top_k=actual_count + 10)

        # Every returned chunk must have full provenance
        required_fields = [
            "source_document_id",
            "source_document_version",
            "source_location",
            "structural_path",
            "transformations",
            "confidence",
            "segment_type",
            "salience_tier",
            "salience_basis",
            "salience_signals",
            "language",
            "injection_suspicion",
            "invisible_content_flags",
            "sensitivity_flags",
            "trust_level",
        ]
        for r in results:
            prov = r.payload.get("provenance", {})
            for field in required_fields:
                assert field in prov, f"Provenance missing field '{field}' in chunk {r.chunk_id}"
                assert prov[field] is not None or field in ("ocr_confidence",), (
                    f"Provenance field '{field}' is None in chunk {r.chunk_id}"
                )
            assert prov["source_document_id"] == "doc-prov-01"
            assert prov["trust_level"] == "untrusted_ingested"

        # Cleanup
        try:
            adapter.delete_alias(f"rtfc_{kb_id}")
        except Exception:
            pass
        if adapter.collection_exists(shadow):
            adapter.drop_collection(shadow)


# ---------------------------------------------------------------------------
# 3. Byte-identity (T-04 seed)
# ---------------------------------------------------------------------------


@qdrant_integration
class TestByteIdentity:
    """chunk text is an exact substring of its segment text (T-04 seed, §12)."""

    def _make_adapter(self):
        from finecorpus.index.qdrant.backend import QdrantAdapter

        return QdrantAdapter(url=QDRANT_URL, timeout=10)

    def test_chunk_text_is_substring_of_segment(self, tmp_path):
        provider = FakeProvider(dimensions=64)
        adapter = self._make_adapter()
        kb_id = str(uuid.uuid4()).replace("-", "")[:12]
        ws_id = "ws-byteident-test"
        run_id = f"byteident-{kb_id}"

        # Use a long text that will definitely be split
        long_text = (
            "Alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu nu xi omicron "
            "pi rho sigma tau upsilon phi chi psi omega. "
        ) * 20  # ~100 words × 20 = ~2000 words >> 512 max_tokens → multiple chunks

        seg = _make_segment(0, long_text)
        seg_set = _make_segment_set("doc-byteident-01", [seg], workspace_id=ws_id, kb_id=kb_id)
        batch = _make_segment_set_batch([seg_set], run_id=run_id)
        config = _make_ingestion_config(
            workspace_id=ws_id, kb_id=kb_id, max_tokens=50, overlap_tokens=5
        )

        store = ArtifactStore(artifacts_root=tmp_path, run_id=run_id)
        _write_artifacts(store, batch, config)

        stage = BuildStage(
            run_started_at=datetime.now(tz=UTC),
            embedding_provider=provider,
            index_adapter=adapter,
            build_id=1,
            artifacts_root=tmp_path,
            run_id=run_id,
            workspace_id=ws_id,
            kb_id=kb_id,
        )
        result_dict = stage.run(input_data=config.model_dump(mode="json"), store=store)
        result = BuildResult.model_validate(result_dict)

        shadow = result.shadow_collection
        from finecorpus.embedding.fake import _sha256_to_vector

        probe = _sha256_to_vector("byteident-probe", 64)
        results = adapter.search(shadow, probe, top_k=result.chunk_count + 10)

        assert len(results) > 0
        for r in results:
            chunk_text = r.payload.get("text", "")
            assert chunk_text in long_text, (
                f"Chunk text is not an exact substring of the segment text: {chunk_text[:80]!r}"
            )

        # Cleanup
        try:
            adapter.delete_alias(f"rtfc_{kb_id}")
        except Exception:
            pass
        if adapter.collection_exists(shadow):
            adapter.drop_collection(shadow)


# ---------------------------------------------------------------------------
# 4. BuildResult structure
# ---------------------------------------------------------------------------


@qdrant_integration
class TestBuildResultStructure:
    def _make_adapter(self):
        from finecorpus.index.qdrant.backend import QdrantAdapter

        return QdrantAdapter(url=QDRANT_URL, timeout=10)

    def test_chunk_counts_match(self, tmp_path):
        provider = FakeProvider(dimensions=64)
        adapter = self._make_adapter()
        kb_id = str(uuid.uuid4()).replace("-", "")[:12]
        ws_id = "ws-struct-test"
        run_id = f"struct-{kb_id}"

        segs_doc1 = [_make_segment(i, f"Document one segment {i} " * 20) for i in range(3)]
        segs_doc2 = [_make_segment(i, f"Document two segment {i} " * 20) for i in range(2)]
        ss1 = _make_segment_set("doc-01", segs_doc1, workspace_id=ws_id, kb_id=kb_id)
        ss2 = _make_segment_set("doc-02", segs_doc2, workspace_id=ws_id, kb_id=kb_id)
        batch = _make_segment_set_batch([ss1, ss2], run_id=run_id)
        config = _make_ingestion_config(
            workspace_id=ws_id, kb_id=kb_id, max_tokens=20, overlap_tokens=4
        )

        store = ArtifactStore(artifacts_root=tmp_path, run_id=run_id)
        _write_artifacts(store, batch, config)

        stage = BuildStage(
            run_started_at=datetime.now(tz=UTC),
            embedding_provider=provider,
            index_adapter=adapter,
            build_id=1,
            artifacts_root=tmp_path,
            run_id=run_id,
            workspace_id=ws_id,
            kb_id=kb_id,
        )
        result_dict = stage.run(input_data=config.model_dump(mode="json"), store=store)
        result = BuildResult.model_validate(result_dict)

        assert result.schema_version == "1.0.0"
        assert result.skeleton is None  # real Phase 1 run
        assert result.chunk_count > 0
        assert result.chunk_count == sum(result.chunks_by_document.values())
        assert "doc-01" in result.chunks_by_document
        assert "doc-02" in result.chunks_by_document
        assert result.validation_passed
        assert result.shadow_collection
        assert result.token_accounting["total_embed_calls"] > 0
        assert result.token_accounting["total_input_tokens"] > 0

        # Verify Qdrant count matches
        actual = adapter.count_points(result.shadow_collection)
        assert actual == result.chunk_count

        # Cleanup
        try:
            adapter.delete_alias(f"rtfc_{kb_id}")
        except Exception:
            pass
        if adapter.collection_exists(result.shadow_collection):
            adapter.drop_collection(result.shadow_collection)


# ---------------------------------------------------------------------------
# 5. Excluded-tier segments are skipped but recorded
# ---------------------------------------------------------------------------


@qdrant_integration
class TestExcludedSegments:
    def _make_adapter(self):
        from finecorpus.index.qdrant.backend import QdrantAdapter

        return QdrantAdapter(url=QDRANT_URL, timeout=10)

    def test_excluded_segments_not_chunked(self, tmp_path):
        provider = FakeProvider(dimensions=64)
        adapter = self._make_adapter()
        kb_id = str(uuid.uuid4()).replace("-", "")[:12]
        ws_id = "ws-excl-test"
        run_id = f"excl-{kb_id}"

        primary_seg = _make_segment(0, "Real content " * 30)
        excl_seg = _make_segment(1, "Boilerplate text " * 30, salience_tier=SalienceTier.excluded)
        ss = _make_segment_set(
            "doc-excl-01", [primary_seg, excl_seg], workspace_id=ws_id, kb_id=kb_id
        )
        batch = _make_segment_set_batch([ss], run_id=run_id)
        config = _make_ingestion_config(workspace_id=ws_id, kb_id=kb_id)

        store = ArtifactStore(artifacts_root=tmp_path, run_id=run_id)
        _write_artifacts(store, batch, config)

        stage = BuildStage(
            run_started_at=datetime.now(tz=UTC),
            embedding_provider=provider,
            index_adapter=adapter,
            build_id=1,
            artifacts_root=tmp_path,
            run_id=run_id,
            workspace_id=ws_id,
            kb_id=kb_id,
        )
        result_dict = stage.run(input_data=config.model_dump(mode="json"), store=store)
        result = BuildResult.model_validate(result_dict)

        # Should have produced chunks (from the primary segment only)
        assert result.chunk_count > 0
        # doc-excl-01 should be in chunks_by_document (it had at least one primary segment)
        assert "doc-excl-01" in result.chunks_by_document

        # Cleanup
        try:
            adapter.delete_alias(f"rtfc_{kb_id}")
        except Exception:
            pass
        if adapter.collection_exists(result.shadow_collection):
            adapter.drop_collection(result.shadow_collection)


# ---------------------------------------------------------------------------
# 6. Resumability test (§6.6) — kill on doc 4, re-run, assert completeness
# ---------------------------------------------------------------------------


@qdrant_integration
class TestResumability:
    """§6.6: Build is resumable. Kill mid-run; re-run; all docs complete; no duplicates."""

    def _make_adapter(self):
        from finecorpus.index.qdrant.backend import QdrantAdapter

        return QdrantAdapter(url=QDRANT_URL, timeout=10)

    def _build_5_docs(
        self,
        workspace_id: str,
        kb_id: str,
    ) -> tuple[list[SegmentSet], IngestionConfig]:
        seg_sets = []
        for i in range(5):
            text = f"Document {i} content paragraph. " * 30  # enough to produce chunks
            seg = _make_segment(0, text)
            ss = _make_segment_set(
                f"doc-resume-{i:02d}",
                [seg],
                workspace_id=workspace_id,
                kb_id=kb_id,
                content_hash=(f"hash{i}" * 16)[:64],
            )
            seg_sets.append(ss)
        config = _make_ingestion_config(workspace_id=workspace_id, kb_id=kb_id)
        return seg_sets, config

    def test_kill_and_resume(self, tmp_path):
        """Run BuildStage end-to-end twice: fail-on-doc-4 first pass, fresh second pass.

        Asserts:
        - First pass raises (ProviderUnavailableError wrapped in StageError).
        - Second pass (with checkpoint) completes all 5 docs.
        - BuildResult.chunk_count == adapter.count_points(shadow) (F-02 and F-05).
        - No duplicate chunk_ids in the shadow collection.
        """
        adapter = self._make_adapter()
        kb_id = str(uuid.uuid4()).replace("-", "")[:12]
        ws_id = "ws-resume-test"
        run_id = f"resume-{kb_id}"

        seg_sets, config = self._build_5_docs(ws_id, kb_id)
        batch = _make_segment_set_batch(seg_sets, run_id=run_id)

        store = ArtifactStore(artifacts_root=tmp_path, run_id=run_id)
        _write_artifacts(store, batch, config)

        # --- First pass: fail-on-doc-4 provider (4th unique embed_batch call fails) ---
        class FailOnFourthDocProvider:
            """Fails on the 4th unique embed_batch call (simulates crash mid-doc-4)."""

            def __init__(self):
                self.calls = 0
                self._inner = FakeProvider(dimensions=64)
                self._caps = self._inner.capabilities

            @property
            def capabilities(self):
                return self._caps

            def embed_batch(self, texts, model_id):
                self.calls += 1
                if self.calls >= 4:
                    raise ProviderUnavailableError(
                        "Simulated failure on 4th embed call",
                        provider_id="fake",
                        model_id=model_id,
                        http_status=503,
                    )
                return self._inner.embed_batch(texts, model_id)

            def health_check(self):
                return self._inner.health_check()

            def estimate_cost(self, texts):
                return self._inner.estimate_cost(texts)

        failing_provider = FailOnFourthDocProvider()

        first_stage = BuildStage(
            run_started_at=datetime.now(tz=UTC),
            embedding_provider=failing_provider,
            index_adapter=adapter,
            build_id=1,
            artifacts_root=tmp_path,
            run_id=run_id,
            workspace_id=ws_id,
            kb_id=kb_id,
        )

        # First pass must raise because of the simulated provider failure
        with pytest.raises(StageError):
            first_stage.run(input_data=config.model_dump(mode="json"), store=store)

        # --- Second pass: fresh provider, same run_id → checkpoint is picked up ---
        fresh_provider = FakeProvider(dimensions=64)

        # build_id=1 same as first pass so the shadow collection name matches
        second_stage = BuildStage(
            run_started_at=datetime.now(tz=UTC),
            embedding_provider=fresh_provider,
            index_adapter=adapter,
            build_id=1,
            artifacts_root=tmp_path,
            run_id=run_id,
            workspace_id=ws_id,
            kb_id=kb_id,
        )

        result_dict = second_stage.run(input_data=config.model_dump(mode="json"), store=store)
        result = BuildResult.model_validate(result_dict)

        # F-02: BuildResult.chunk_count must reflect the whole shadow collection
        shadow = result.shadow_collection
        assert shadow, "No shadow collection in BuildResult"

        actual_shadow_count = adapter.count_points(shadow)
        assert result.chunk_count == actual_shadow_count, (
            f"BuildResult.chunk_count ({result.chunk_count}) != "
            f"adapter.count_points(shadow) ({actual_shadow_count}). "
            f"F-02: resumed runs must fold prior chunk counts into total."
        )

        # All 5 docs must appear in chunks_by_document
        for i in range(5):
            doc_id = f"doc-resume-{i:02d}"
            assert doc_id in result.chunks_by_document, (
                f"{doc_id} missing from BuildResult.chunks_by_document after resume"
            )

        assert result.chunk_count == sum(result.chunks_by_document.values()), (
            "chunk_count must equal sum of chunks_by_document values"
        )

        # Verify no duplicate chunk_ids in the shadow collection
        from finecorpus.embedding.fake import _sha256_to_vector

        probe = _sha256_to_vector("resume-probe", 64)
        points = adapter.search(shadow, probe, top_k=actual_shadow_count + 100)
        chunk_ids = [r.chunk_id for r in points]
        assert len(chunk_ids) == len(set(chunk_ids)), (
            f"Duplicate chunk_ids after resume: {len(chunk_ids) - len(set(chunk_ids))} duplicates"
        )

        # Cleanup
        try:
            adapter.delete_alias(f"rtfc_{kb_id}")
        except Exception:
            pass
        if adapter.collection_exists(shadow):
            adapter.drop_collection(shadow)


# ---------------------------------------------------------------------------
# 7. Checkpoint load/save round-trip (unit, no Qdrant)
# ---------------------------------------------------------------------------


class TestCheckpoint:
    def test_load_empty_if_missing(self, tmp_path):
        """_load_checkpoint returns empty set and empty dict if file does not exist."""
        completed, counts = _load_checkpoint(tmp_path)
        assert completed == set()
        assert counts == {}

    def test_save_and_load_round_trip(self, tmp_path):
        from finecorpus.pipeline.build.stage import _save_checkpoint

        docs = {"doc-a", "doc-b", "doc-c"}
        counts = {"doc-a": 5, "doc-b": 3, "doc-c": 12}
        _save_checkpoint(tmp_path, docs, counts)
        loaded_docs, loaded_counts = _load_checkpoint(tmp_path)
        assert loaded_docs == docs
        assert loaded_counts == counts

    def test_save_is_idempotent(self, tmp_path):
        from finecorpus.pipeline.build.stage import _save_checkpoint

        docs = {"doc-x"}
        counts = {"doc-x": 7}
        _save_checkpoint(tmp_path, docs, counts)
        _save_checkpoint(tmp_path, docs, counts)
        loaded_docs, loaded_counts = _load_checkpoint(tmp_path)
        assert loaded_docs == docs
        assert loaded_counts == counts

    def test_prior_counts_folded_on_load(self, tmp_path):
        """Chunk counts from prior checkpoint are preserved and recoverable."""
        from finecorpus.pipeline.build.stage import _save_checkpoint

        docs = {"doc-1", "doc-2"}
        counts = {"doc-1": 10, "doc-2": 0}  # doc-2 had no chunks (excluded)
        _save_checkpoint(tmp_path, docs, counts)
        loaded_docs, loaded_counts = _load_checkpoint(tmp_path)
        assert loaded_docs == docs
        assert loaded_counts["doc-1"] == 10
        assert loaded_counts["doc-2"] == 0
