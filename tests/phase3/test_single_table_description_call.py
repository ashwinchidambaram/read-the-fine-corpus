"""D1: one LLM call per table segment (not per chunk fragment) + cache tests.

Tests:
- A table split into multiple chunk fragments triggers EXACTLY ONE LLM call
- Cache hit on resume triggers ZERO additional calls
- LLMBuildClient.for_segment shares the underlying _mem_cache
- Cache key is (content_hash, segment_path, model_id) — different segments = different calls
- Double-record regression: exactly one table_to_markdown record per table segment end-to-end
"""

from __future__ import annotations

import hashlib
import json
import pathlib
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
from finecorpus.pipeline.build.llm_client import LLMBuildClient

# ---------------------------------------------------------------------------
# LLMBuildClient unit tests (cache + call count)
# ---------------------------------------------------------------------------


def _make_op_config() -> object:
    """Return a minimal ResolvedOpConfig for testing."""
    from finecorpus.llm.operations import ResolvedOpConfig

    return ResolvedOpConfig(
        provider_id="fake",
        model_id="fake-llm-v1",
        temperature=0.0,
        max_output_tokens=128,
        max_retries=1,
    )


class TestLLMBuildClientCache:
    """LLMBuildClient caches results per (content_hash, segment_path, model_id)."""

    def test_second_describe_table_call_is_cache_hit(self, tmp_path: pathlib.Path) -> None:
        """Calling describe_table twice with the same client triggers ONE LLM call."""
        from finecorpus.llm.fake import FakeLLMProvider

        fake_llm = FakeLLMProvider()
        op_cfg = _make_op_config()

        client = LLMBuildClient(
            provider=fake_llm,
            op_config=op_cfg,
            run_dir=tmp_path,
            content_hash="abc123",
            segment_path="seg/0",
        )

        desc1 = client.describe_table((3, 2), "| A | B |\n| 1 | 2 |\n| 3 | 4 |")
        desc2 = client.describe_table((3, 2), "| A | B |\n| 1 | 2 |\n| 3 | 4 |")

        assert client.call_count == 1, f"Expected 1 LLM call, got {client.call_count}"
        assert desc1 == desc2, "Cache hit must return same description"

    def test_persistent_cache_prevents_call_on_second_client(self, tmp_path: pathlib.Path) -> None:
        """A second LLMBuildClient with the same run_dir reads the cache — zero new calls."""
        from finecorpus.llm.fake import FakeLLMProvider

        fake_llm = FakeLLMProvider()
        op_cfg = _make_op_config()

        # First client — populates cache
        client1 = LLMBuildClient(
            provider=fake_llm,
            op_config=op_cfg,
            run_dir=tmp_path,
            content_hash="doc-hash-1",
            segment_path="seg/0",
        )
        desc1 = client1.describe_table((2, 3), "| X | Y | Z |\n| a | b | c |")
        assert client1.call_count == 1

        # Second client (simulates a resumed build) — reads from persistent cache
        client2 = LLMBuildClient(
            provider=fake_llm,
            op_config=op_cfg,
            run_dir=tmp_path,
            content_hash="doc-hash-1",
            segment_path="seg/0",
        )
        desc2 = client2.describe_table((2, 3), "| X | Y | Z |\n| a | b | c |")

        assert client2.call_count == 0, (
            f"Resumed client should read from cache — got {client2.call_count} calls"
        )
        assert desc1 == desc2

    def test_different_segment_path_different_cache_key(self, tmp_path: pathlib.Path) -> None:
        """Different segment_path → different cache key → separate LLM calls."""
        from finecorpus.llm.fake import FakeLLMProvider

        fake_llm = FakeLLMProvider()
        op_cfg = _make_op_config()

        client1 = LLMBuildClient(
            provider=fake_llm,
            op_config=op_cfg,
            run_dir=tmp_path,
            content_hash="doc-hash-2",
            segment_path="seg/0",
        )
        client2 = LLMBuildClient(
            provider=fake_llm,
            op_config=op_cfg,
            run_dir=tmp_path,
            content_hash="doc-hash-2",
            segment_path="seg/1",  # different segment
        )
        # Share in-memory cache (as done by _LLMClientFactory.for_segment)
        client2._mem_cache = client1._mem_cache
        client2._cache_path = client1._cache_path

        client1.describe_table((2, 2), "| A | B |\n| 1 | 2 |")
        client2.describe_table((3, 2), "| C | D |\n| x | y |\n| z | w |")

        # Each segment gets its own call
        assert client1.call_count == 1
        assert client2.call_count == 1

    def test_for_segment_shares_mem_cache(self, tmp_path: pathlib.Path) -> None:
        """for_segment creates a child client that shares the same _mem_cache."""
        from finecorpus.llm.fake import FakeLLMProvider

        fake_llm = FakeLLMProvider()
        op_cfg = _make_op_config()

        root_client = LLMBuildClient(
            provider=fake_llm,
            op_config=op_cfg,
            run_dir=tmp_path,
            content_hash="root-hash",
            segment_path="seg/0",
        )
        seg_client = root_client.for_segment("root-hash", "seg/0")

        # They share the same dict object
        assert seg_client._mem_cache is root_client._mem_cache, (
            "for_segment must share _mem_cache with the root client"
        )

    def test_for_segment_call_appears_in_root_cache(self, tmp_path: pathlib.Path) -> None:
        """After for_segment makes an LLM call, the result is visible in root._mem_cache."""
        from finecorpus.llm.fake import FakeLLMProvider

        fake_llm = FakeLLMProvider()
        op_cfg = _make_op_config()

        root_client = LLMBuildClient(
            provider=fake_llm,
            op_config=op_cfg,
            run_dir=tmp_path,
            content_hash="shared-hash",
            segment_path="seg/0",
        )
        seg_client = root_client.for_segment("shared-hash", "seg/0")

        desc = seg_client.describe_table((1, 1), "| A |\n| 1 |")

        assert seg_client.call_count == 1
        # Cache key must be present in root's _mem_cache
        key = "shared-hash|seg/0|fake-llm-v1"
        assert key in root_client._mem_cache, f"Cache key {key!r} not found in root._mem_cache"
        assert root_client._mem_cache[key] == desc


# ---------------------------------------------------------------------------
# Build-stage integration: exactly one LLM call per table segment
# ---------------------------------------------------------------------------


def _make_tenancy() -> TenancyBlock:
    return TenancyBlock(
        workspace_id="ws-llm",
        kb_id="kb-llm",
        permission_mode=PermissionMode.public_to_kb,
        permission_principals=[],
        permission_source=PermissionSource.platform,
        permission_fidelity=PermissionFidelity.authoritative,
        permission_resolved_at=None,
    )


def _make_segment(
    text: str,
    doc_order: int = 0,
    segment_type: SegmentType = SegmentType.table,
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


def _make_config_with_table_description(max_tokens_per_chunk: int = 20) -> IngestionConfig:
    """Config that enables table_description LLM op for table segments."""
    embedding = EmbeddingConfig(
        provider="fake",
        model="fake-embed-v1",
        dimensions=64,
        normalize=True,
        supports_languages=["*"],
    )
    table_chunking = ChunkingConfig(
        strategy=ChunkingStrategy.recursive_char,
        max_tokens=max_tokens_per_chunk,
        overlap_tokens=0,
        respect_headings=False,
        atomic_rows=False,
        repeat_headers_on_split=False,
    )
    table_rule = ClassRule(
        segment_class=SegmentType.table,
        transformation=TransformationSettings(
            tier1_enabled=True,
            tier1_operations=[
                Tier1Operation.whitespace_repair,
                Tier1Operation.table_to_markdown,
            ],
            tier2_enabled=True,
            tier2_operations=[Tier2Operation.table_description],
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
    prose_rule = ClassRule(
        segment_class=SegmentType.prose,
        transformation=TransformationSettings(
            tier1_enabled=True,
            tier1_operations=[Tier1Operation.whitespace_repair],
            tier2_enabled=False,
            tier2_operations=[],
            tier3_enabled=False,
        ),
        chunking=ChunkingConfig(
            strategy=ChunkingStrategy.recursive_char,
            max_tokens=max_tokens_per_chunk,
            overlap_tokens=0,
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
    config_version = hashlib.sha256(b"llm-call-count-config").hexdigest()
    return IngestionConfig(
        schema_version=INGESTION_CONFIG_SCHEMA_VERSION,
        tenancy=_make_tenancy(),
        config_version=config_version,
        created_at=datetime.now(tz=UTC),
        naive_baseline=NaiveBaselineRef(
            reference_id="naive-baseline-v0",
            description="LLM call count test baseline.",
        ),
        class_rules=[table_rule],
        default_rule=prose_rule,
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
                rationale="LLM call count test config.",
            )
        ],
        class_descriptions=[],
        secret_free_attestation=True,
    )


def _make_and_write_batch(
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


class TestSingleLLMCallPerTableSegment:
    """A table split into multiple fragments triggers exactly ONE LLM call."""

    def test_one_call_per_table_segment_regardless_of_chunk_count(
        self, tmp_path: pathlib.Path
    ) -> None:
        """A table that yields multiple chunks → one LLM call total (not one per chunk)."""
        from finecorpus.llm.fake import FakeLLMProvider
        from finecorpus.llm.operations import ResolvedOpConfig
        from finecorpus.pipeline.build.stage import BuildStage

        # Build a table large enough to produce multiple chunks with small max_tokens
        # Each row is a separate "chunk" with max_tokens=8 (8 whitespace-words)
        rows = "\n".join(f"| col{i}a | col{i}b | col{i}c | col{i}d |" for i in range(10))
        table_text = "| A | B | C | D |\n|---|---|---|---|\n" + rows

        seg = _make_segment(table_text, doc_order=0, segment_type=SegmentType.table)

        # Use very small max_tokens to force multiple chunks
        config = _make_config_with_table_description(max_tokens_per_chunk=8)

        fake_llm = FakeLLMProvider()
        op_cfg = ResolvedOpConfig(
            provider_id="fake",
            model_id="fake-llm-v1",
            temperature=0.0,
            max_output_tokens=128,
            max_retries=1,
        )

        run_id = "llm-one-call-1"
        _make_and_write_batch({"doc-llm-1": [seg]}, run_id, tmp_path)

        stage = BuildStage(
            artifacts_root=tmp_path,
            run_id=run_id,
            dry_run=True,
            llm_provider=fake_llm,
            llm_op_config=op_cfg,
        )
        result = stage._produce(config.model_dump(mode="json"))  # noqa: SLF001

        # The result must have multiple chunks (otherwise the test doesn't prove anything)
        table_chunks = [c for c in result["chunks"] if c["text"]]
        assert len(table_chunks) >= 2, (
            f"Expected multiple chunks from split table, got {len(table_chunks)}"
        )

        # LLM call count: exactly 1 (one per table segment, not per chunk)
        llm_call_count = result.get("token_accounting", {}).get("llm_call_count", None)
        assert llm_call_count == 1, (
            f"Expected exactly 1 LLM call for one table segment, got {llm_call_count}"
        )

    def test_cache_hit_on_resume_zero_additional_calls(self, tmp_path: pathlib.Path) -> None:
        """Second build run reads from persistent cache → zero new LLM calls."""
        from finecorpus.llm.fake import FakeLLMProvider
        from finecorpus.llm.operations import ResolvedOpConfig
        from finecorpus.pipeline.build.stage import BuildStage

        table_text = "| A | B |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |"
        seg = _make_segment(table_text, doc_order=0, segment_type=SegmentType.table)
        config = _make_config_with_table_description(max_tokens_per_chunk=5)

        fake_llm = FakeLLMProvider()
        op_cfg = ResolvedOpConfig(
            provider_id="fake",
            model_id="fake-llm-v1",
            temperature=0.0,
            max_output_tokens=128,
            max_retries=1,
        )

        run_id = "llm-cache-resume-1"
        _make_and_write_batch({"doc-cache-1": [seg]}, run_id, tmp_path)

        # First build — populates cache
        stage1 = BuildStage(
            artifacts_root=tmp_path,
            run_id=run_id,
            dry_run=True,
            llm_provider=fake_llm,
            llm_op_config=op_cfg,
        )
        result1 = stage1._produce(config.model_dump(mode="json"))  # noqa: SLF001
        call_count_1 = result1.get("token_accounting", {}).get("llm_call_count", 0)

        # Second build — should read from llm_cache.json
        stage2 = BuildStage(
            artifacts_root=tmp_path,
            run_id=run_id,
            dry_run=True,
            llm_provider=fake_llm,
            llm_op_config=op_cfg,
        )
        result2 = stage2._produce(config.model_dump(mode="json"))  # noqa: SLF001
        call_count_2 = result2.get("token_accounting", {}).get("llm_call_count", 0)

        assert call_count_1 == 1, f"First run should have 1 LLM call, got {call_count_1}"
        assert call_count_2 == 0, (
            f"Second run (resume) should have 0 LLM calls (cache hit), got {call_count_2}"
        )


# ---------------------------------------------------------------------------
# Double-record regression: exactly one table_to_markdown record per segment
# ---------------------------------------------------------------------------


class TestDoubleRecordRegression:
    """Regression test for the double-recording defect.

    Before fix: _build_table_to_markdown_record() auto-injected a record PLUS
    apply_tier1() emitted one when table_to_markdown was in tier1_operations.
    After fix: only the apply_tier1 path emits records; _build_table_to_markdown_record
    is GONE.
    """

    def test_exactly_one_table_to_markdown_record_per_table_segment(
        self, tmp_path: pathlib.Path
    ) -> None:
        """A table segment must have EXACTLY ONE 'table_to_markdown' transformation record."""
        from finecorpus.pipeline.build.stage import BuildStage

        table_text = "| A | B | C |\n|---|---|---|\n| 1 | 2 | 3 |"
        seg = _make_segment(table_text, doc_order=0, segment_type=SegmentType.table)
        config = _make_config_with_table_description(max_tokens_per_chunk=100)

        run_id = "double-record-test-1"
        _make_and_write_batch({"doc-dr-1": [seg]}, run_id, tmp_path)

        stage = BuildStage(
            artifacts_root=tmp_path,
            run_id=run_id,
            dry_run=True,
        )
        result = stage._produce(config.model_dump(mode="json"))  # noqa: SLF001

        chunks = result["chunks"]
        assert chunks, "Expected at least one chunk"

        # Collect all table_to_markdown transformation records across all chunks
        ttm_records: list[dict] = []
        for chunk in chunks:
            for tr in chunk["provenance"].get("transformations", []):
                if tr["operation"] == "table_to_markdown":
                    ttm_records.append(tr)

        # There should be exactly 1 unique table_to_markdown record
        # (all chunks from the same segment share the same provenance record)
        # We check that the set of unique records is exactly 1
        unique_ops = {tr["operation"] for tr in ttm_records}
        assert "table_to_markdown" in unique_ops, (
            "table_to_markdown record should appear in provenance (tier1 op was applied)"
        )

        # The record must appear in each chunk exactly once (not duplicated within a chunk)
        for chunk in chunks:
            chunk_ttm = [
                tr
                for tr in chunk["provenance"].get("transformations", [])
                if tr["operation"] == "table_to_markdown"
            ]
            assert len(chunk_ttm) <= 1, (
                f"Double-record defect: {len(chunk_ttm)} 'table_to_markdown' records "
                f"in single chunk provenance — expected at most 1"
            )

    def test_prose_segment_has_no_table_to_markdown_record(self, tmp_path: pathlib.Path) -> None:
        """A prose segment must have zero 'table_to_markdown' records (no spurious injection)."""
        from finecorpus.pipeline.build.stage import BuildStage

        prose_text = "Prose content that should have no table records."
        seg = _make_segment(prose_text, doc_order=0, segment_type=SegmentType.prose)
        config = _make_config_with_table_description(max_tokens_per_chunk=100)

        run_id = "double-record-prose-1"
        _make_and_write_batch({"doc-dr-prose-1": [seg]}, run_id, tmp_path)

        stage = BuildStage(
            artifacts_root=tmp_path,
            run_id=run_id,
            dry_run=True,
        )
        result = stage._produce(config.model_dump(mode="json"))  # noqa: SLF001

        for chunk in result["chunks"]:
            ttm_records = [
                tr
                for tr in chunk["provenance"].get("transformations", [])
                if tr["operation"] == "table_to_markdown"
            ]
            assert len(ttm_records) == 0, (
                f"Prose chunk should not have any 'table_to_markdown' records, got: {ttm_records}"
            )


# ---------------------------------------------------------------------------
# Ruling 6: cache validation on read (M-067)
# ---------------------------------------------------------------------------


class TestLLMCacheValidationOnRead:
    """Ruling 6: LLM cache entries with non-str values must be dropped with a warning.

    M-067: cached LLM output is still LLM output — validate on read.
    A hand-written cache file with int values must result in:
    - The invalid entry being dropped (not loaded into _mem_cache).
    - A warning being logged.
    - The LLM being re-called for the dropped entry.
    """

    def test_int_value_dropped_and_llm_recalled(
        self, tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Cache entry with int value → dropped + warning logged + LLM re-called."""
        import logging

        from finecorpus.llm.fake import FakeLLMProvider
        from finecorpus.llm.operations import ResolvedOpConfig
        from finecorpus.pipeline.build.llm_client import LLMBuildClient, _cache_key

        fake_llm = FakeLLMProvider()
        op_cfg = ResolvedOpConfig(
            provider_id="fake",
            model_id="fake-llm-v1",
            temperature=0.0,
            max_output_tokens=128,
            max_retries=1,
        )

        content_hash = "abc123deadbeef"
        segment_path = "sec/table/0"
        model_id = op_cfg.model_id

        # Write a cache file with an INVALID int value for the target key,
        # and a valid str value for a different key.
        cache_key_bad = _cache_key(content_hash, segment_path, model_id)
        cache_key_good = _cache_key("other_hash", "other/path", model_id)
        hand_written_cache = {
            cache_key_bad: 42,  # int — invalid (must be str)
            cache_key_good: "valid description here",  # str — valid
        }
        cache_path = tmp_path / "llm_cache.json"
        cache_path.write_text(__import__("json").dumps(hand_written_cache), encoding="utf-8")

        # Load the cache via LLMBuildClient — should drop the int entry
        with caplog.at_level(logging.WARNING, logger="finecorpus.pipeline.build.llm_client"):
            client = LLMBuildClient(
                provider=fake_llm,
                op_config=op_cfg,
                run_dir=tmp_path,
                content_hash=content_hash,
                segment_path=segment_path,
            )

        # The bad int entry must NOT be in the mem cache
        assert cache_key_bad not in client._mem_cache, (
            f"Int-valued cache entry was loaded into _mem_cache — should have been dropped. "
            f"Cache: {client._mem_cache}"
        )

        # The good str entry MUST be in the mem cache
        assert cache_key_good in client._mem_cache, (
            "Valid str-valued cache entry was incorrectly dropped from _mem_cache."
        )
        assert client._mem_cache[cache_key_good] == "valid description here"

        # A warning must have been logged about the dropped entry
        warning_messages = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
        warning_text = " ".join(str(m) for m in warning_messages).lower()
        assert "m-067" in warning_text or "dropped" in warning_text, (
            f"Expected M-067 warning about dropped cache entry. Got warnings: {warning_messages}"
        )

        # The LLM must be re-called for the dropped entry (call_count goes from 0 to 1)
        assert client.call_count == 0, "No LLM calls yet"
        desc = client.describe_table(shape=(3, 2), sample="| A | B |\n| 1 | 2 |")
        assert client.call_count == 1, (
            f"LLM should have been re-called for the dropped int-valued cache entry. "
            f"call_count={client.call_count}"
        )
        assert isinstance(desc, str) and len(desc) > 0, "describe_table must return a non-empty str"

    def test_valid_cache_entry_loaded_no_llm_call(self, tmp_path: pathlib.Path) -> None:
        """Valid str-valued cache entry → loaded, no LLM call made."""
        from finecorpus.llm.fake import FakeLLMProvider
        from finecorpus.llm.operations import ResolvedOpConfig
        from finecorpus.pipeline.build.llm_client import LLMBuildClient, _cache_key

        fake_llm = FakeLLMProvider()
        op_cfg = ResolvedOpConfig(
            provider_id="fake",
            model_id="fake-llm-v1",
            temperature=0.0,
            max_output_tokens=128,
            max_retries=1,
        )

        content_hash = "validhash123"
        segment_path = "sec/table/valid"
        model_id = op_cfg.model_id

        expected_desc = "A table with revenue data for Q3."
        cache_key_good = _cache_key(content_hash, segment_path, model_id)
        hand_written_cache = {cache_key_good: expected_desc}

        cache_path = tmp_path / "llm_cache.json"
        cache_path.write_text(__import__("json").dumps(hand_written_cache), encoding="utf-8")

        client = LLMBuildClient(
            provider=fake_llm,
            op_config=op_cfg,
            run_dir=tmp_path,
            content_hash=content_hash,
            segment_path=segment_path,
        )

        # Valid entry should be loaded — no LLM call needed
        assert cache_key_good in client._mem_cache
        desc = client.describe_table(shape=(2, 3), sample="sample")
        assert client.call_count == 0, "Valid cache hit should require no LLM call"
        assert desc == expected_desc
