"""Tests: Tier 3 rewriting wired into the Build path (§7.2 C-R7, D-14, D-19).

Covers:
- A Tier-3 chunk carries BOTH ``text`` (rewritten) AND ``original_text`` (canonical).
- The tier=3, changed_text=True TransformationRecord is recorded.
- The rewritten text carries the untrusted_ingested trust level (D-19).
- Diff preview is surfaced when ``diff_preview_required`` is set.
- Tier 3 OFF leaves chunks byte-identical to the canonical Tier-1 slice (T-04 unaffected).
- The retrieval response surfaces both ``text`` and ``original_text`` at citation time.
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
    Tier3Settings,
    TransformationSettings,
)
from finecorpus.contracts.retrieval_response import RetrievalResult
from finecorpus.contracts.segment_set import (
    ReassemblyMethod,
    ReassemblyRecord,
    Segment,
    SegmentSet,
)
from finecorpus.contracts.segment_set_batch import BATCH_SCHEMA_VERSION, SegmentSetBatch
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
    SourceLocation,
    TenancyBlock,
    TransformationRecord,
    TransformationTier,
    TrustLevel,
)
from finecorpus.contracts.versions import INGESTION_CONFIG_SCHEMA_VERSION
from finecorpus.llm.fake import FakeLLMProvider
from finecorpus.llm.operations import ResolvedOpConfig
from finecorpus.pipeline.build.stage import BuildStage

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tenancy() -> TenancyBlock:
    return TenancyBlock(
        workspace_id="ws-t3",
        kb_id="kb-t3",
        permission_mode=PermissionMode.public_to_kb,
        permission_principals=[],
        permission_source=PermissionSource.platform,
        permission_fidelity=PermissionFidelity.authoritative,
        permission_resolved_at=None,
    )


def _segment(
    text: str, doc_order: int = 0, segment_type: SegmentType = SegmentType.prose
) -> Segment:
    return Segment(
        segment_id=str(uuid.uuid4()),
        document_order=doc_order,
        segment_type=segment_type,
        salience_tier=SalienceTier.primary,
        structural_path=[],
        segment_path=f"seg/{doc_order}",
        location=SourceLocation(
            locator_kind=LocatorKind.char_range, char_start=0, char_end=len(text)
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


def _segment_set(document_id: str, segments: list[Segment]) -> SegmentSet:
    content_hash = hashlib.sha256(document_id.encode()).hexdigest()
    region_ids = [r for seg in segments for r in seg.source_region_ids]
    reassembly_text = "".join(seg.text or "" for seg in segments)
    reassembly_digest = hashlib.sha256(reassembly_text.encode()).hexdigest()
    return SegmentSet(
        schema_version="1.1.0",
        tenancy=_tenancy(),
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


def _retrieval_treatment() -> RetrievalTreatment:
    return RetrievalTreatment(
        default_salience_filter=[SalienceTier.primary, SalienceTier.supporting],
        salience_weights=None,
        rerank_eligible=False,
        strategy=RetrievalStrategy.dense,
    )


def _chunking() -> ChunkingConfig:
    return ChunkingConfig(
        strategy=ChunkingStrategy.recursive_char,
        max_tokens=100,
        overlap_tokens=10,
        respect_headings=False,
    )


def _embedding() -> EmbeddingConfig:
    return EmbeddingConfig(
        provider="fake",
        model="fake-embed-v1",
        dimensions=64,
        normalize=True,
        supports_languages=["*"],
    )


def _config(
    *,
    tier3_enabled: bool,
    diff_preview_required: bool = False,
    config_tag: str = "t3",
) -> IngestionConfig:
    """Build an IngestionConfig. When tier3_enabled, the PROSE default_rule stays
    tier3-off (M-035) and a separate TABLE class rule opts into Tier 3."""
    default_rule = ClassRule(
        segment_class=SegmentType.prose,
        transformation=TransformationSettings(
            tier1_enabled=True,
            tier1_operations=[Tier1Operation.whitespace_repair],
            tier2_enabled=False,
            tier2_operations=[],
            tier3_enabled=False,
        ),
        chunking=_chunking(),
        embedding_override=None,
        metadata_schema=[],
        retrieval_treatment=_retrieval_treatment(),
    )

    class_rules: list[ClassRule] = []
    if tier3_enabled:
        # Per-class opt-in (Tier 3 is applied to the PROSE default_rule class? No —
        # default_rule cannot opt in (M-035). We route the prose segment through a
        # dedicated prose ClassRule with tier3 enabled by making it the default match.)
        # We instead enable Tier 3 on the prose class rule itself (a class_rules entry).
        prose_tier3 = ClassRule(
            segment_class=SegmentType.prose,
            transformation=TransformationSettings(
                tier1_enabled=True,
                tier1_operations=[Tier1Operation.whitespace_repair],
                tier2_enabled=False,
                tier2_operations=[],
                tier3_enabled=True,
                tier3_settings=Tier3Settings(
                    model_ref="fake-rewriter-v1",
                    opt_in_ack=True,
                    diff_preview_required=diff_preview_required,
                ),
            ),
            chunking=_chunking(),
            embedding_override=None,
            metadata_schema=[],
            retrieval_treatment=_retrieval_treatment(),
        )
        class_rules = [prose_tier3]

    config_version = hashlib.sha256(config_tag.encode()).hexdigest()
    return IngestionConfig(
        schema_version=INGESTION_CONFIG_SCHEMA_VERSION,
        tenancy=_tenancy(),
        config_version=config_version,
        created_at=datetime.now(tz=UTC),
        naive_baseline=NaiveBaselineRef(reference_id="nb-v0", description="tier3 test"),
        class_rules=class_rules,
        default_rule=default_rule,
        embedding=_embedding(),
        retrieval_defaults=_retrieval_treatment(),
        language_support=LanguageSupportDecision(
            detected_languages=[], unsupported_languages=[], decision=LanguageDecision.proceed
        ),
        spreadsheet_triage=[],
        exclusions_confirmed=[],
        provenance=[
            RecommendationProvenance(
                target="/default_rule",
                basis=RecommendationBasis.heuristic,
                rationale="tier3 test",
            )
        ],
        class_descriptions=[],
        secret_free_attestation=True,
    )


def _run_dry_build(
    segments_by_doc: dict[str, list[Segment]],
    config: IngestionConfig,
    tmp_path: pathlib.Path,
    *,
    with_llm: bool,
) -> dict[str, Any]:
    run_id = "t3-test"
    artifacts_root = tmp_path
    seg_sets = [_segment_set(doc_id, segs) for doc_id, segs in segments_by_doc.items()]
    batch = SegmentSetBatch(
        schema_version=BATCH_SCHEMA_VERSION,
        contract="segment_set_batch",
        run_id=run_id,
        produced_at=datetime.now(tz=UTC),
        skeleton=None,
        segment_sets=[ss.model_dump(mode="json") for ss in seg_sets],
    )
    run_dir = artifacts_root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "decompose.json").write_text(
        json.dumps(batch.model_dump(mode="json")), encoding="utf-8"
    )

    llm_provider = FakeLLMProvider() if with_llm else None
    op_cfg = (
        ResolvedOpConfig(
            provider_id="fake",
            model_id="fake-llm-v1",
            temperature=0.0,
            max_output_tokens=128,
            max_retries=1,
        )
        if with_llm
        else None
    )

    stage = BuildStage(
        artifacts_root=artifacts_root,
        run_id=run_id,
        dry_run=True,
        llm_provider=llm_provider,
        llm_op_config=op_cfg,
    )
    return stage._produce(config.model_dump(mode="json"))  # noqa: SLF001


def _run_real_build(
    segments_by_doc: dict[str, list[Segment]],
    config: IngestionConfig,
    tmp_path: pathlib.Path,
    *,
    with_llm: bool,
) -> tuple[dict[str, Any], Any]:
    """Run a NON-dry-run (commit) build against a FakeAdapter + FakeProvider.

    Returns (build_result_dict, adapter) so the caller can read back the committed
    point payloads from ``adapter.collections[shadow]["points"]``.
    """
    from finecorpus.embedding.fake import FakeProvider
    from tests.retrieval.helpers import FakeAdapter

    run_id = "t3-commit-test"
    artifacts_root = tmp_path
    seg_sets = [_segment_set(doc_id, segs) for doc_id, segs in segments_by_doc.items()]
    batch = SegmentSetBatch(
        schema_version=BATCH_SCHEMA_VERSION,
        contract="segment_set_batch",
        run_id=run_id,
        produced_at=datetime.now(tz=UTC),
        skeleton=None,
        segment_sets=[ss.model_dump(mode="json") for ss in seg_sets],
    )
    run_dir = artifacts_root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "decompose.json").write_text(
        json.dumps(batch.model_dump(mode="json")), encoding="utf-8"
    )

    llm_provider = FakeLLMProvider() if with_llm else None
    op_cfg = (
        ResolvedOpConfig(
            provider_id="fake",
            model_id="fake-llm-v1",
            temperature=0.0,
            max_output_tokens=128,
            max_retries=1,
        )
        if with_llm
        else None
    )

    provider = FakeProvider(dimensions=64, model_id="fake-embed-v1")
    adapter = FakeAdapter()

    stage = BuildStage(
        embedding_provider=provider,
        index_adapter=adapter,
        build_id=1,
        artifacts_root=artifacts_root,
        run_id=run_id,
        workspace_id="ws-t3",
        kb_id="kb-t3",
        dry_run=False,
        llm_provider=llm_provider,
        llm_op_config=op_cfg,
    )
    result = stage._produce(config.model_dump(mode="json"))  # noqa: SLF001
    return result, adapter


def _committed_points(adapter: Any, shadow_collection: str) -> list[dict[str, Any]]:
    """Return the list of committed point dicts from the FakeAdapter shadow collection."""
    return adapter.collections.get(shadow_collection, {}).get("points", [])


# ---------------------------------------------------------------------------
# Tier 3 ON
# ---------------------------------------------------------------------------


class TestTier3On:
    def test_chunk_carries_both_texts(self, tmp_path: pathlib.Path) -> None:
        raw = "The revenue did not decline in Q3."
        config = _config(tier3_enabled=True, config_tag="t3-on")
        result = _run_dry_build({"doc-1": [_segment(raw)]}, config, tmp_path, with_llm=True)
        chunks = result["chunks"]
        assert chunks
        for chunk in chunks:
            # text is the REWRITTEN form (FakeLLM tags its output).
            assert "[fake:" in chunk["text"]
            # original_text is the canonical Tier-1 text (whitespace_repair no-op here).
            assert chunk["original_text"] == raw
            assert chunk["text"] != chunk["original_text"]

    def test_transformation_record_tier3_changed_text(self, tmp_path: pathlib.Path) -> None:
        config = _config(tier3_enabled=True, config_tag="t3-rec")
        result = _run_dry_build(
            {"doc-1": [_segment("Original prose content.")]}, config, tmp_path, with_llm=True
        )
        for chunk in result["chunks"]:
            tier3_records = [tr for tr in chunk["provenance"]["transformations"] if tr["tier"] == 3]
            assert tier3_records, "Expected a tier=3 transformation record"
            assert tier3_records[0]["changed_text"] is True
            assert tier3_records[0]["operation"] == "rewriting"
            assert tier3_records[0]["model_ref"] == "fake-rewriter-v1"

    def test_rewritten_text_is_untrusted(self, tmp_path: pathlib.Path) -> None:
        """D-19: the rewritten text is still untrusted_ingested."""
        config = _config(tier3_enabled=True, config_tag="t3-trust")
        result = _run_dry_build({"doc-1": [_segment("Prose.")]}, config, tmp_path, with_llm=True)
        for chunk in result["chunks"]:
            assert chunk["provenance"]["trust_level"] == TrustLevel.untrusted_ingested.value

    def test_diff_preview_surfaced_when_required(self, tmp_path: pathlib.Path) -> None:
        config = _config(tier3_enabled=True, diff_preview_required=True, config_tag="t3-diff")
        result = _run_dry_build(
            {"doc-1": [_segment("The cat sat on the mat.")]}, config, tmp_path, with_llm=True
        )
        for chunk in result["chunks"]:
            assert "diff_preview" in chunk
            assert "original (canonical Tier-1)" in chunk["diff_preview"]
            assert "rewritten (Tier-3)" in chunk["diff_preview"]

    def test_diff_preview_absent_when_not_required(self, tmp_path: pathlib.Path) -> None:
        config = _config(tier3_enabled=True, diff_preview_required=False, config_tag="t3-nodiff")
        result = _run_dry_build({"doc-1": [_segment("Content.")]}, config, tmp_path, with_llm=True)
        for chunk in result["chunks"]:
            assert "diff_preview" not in chunk

    def test_tier3_enabled_but_no_llm_stays_byte_identical(self, tmp_path: pathlib.Path) -> None:
        """If Tier 3 is enabled but no LLM provider is injected, chunks stay byte-identical."""
        raw = "The report is final."
        config = _config(tier3_enabled=True, config_tag="t3-nollm")
        result = _run_dry_build({"doc-1": [_segment(raw)]}, config, tmp_path, with_llm=False)
        for chunk in result["chunks"]:
            assert chunk["original_text"] is None
            position_slice = chunk["canonical_text"][chunk["char_start"] : chunk["char_end"]]
            assert chunk["text"].encode() == position_slice.encode()


# ---------------------------------------------------------------------------
# Tier 3 OFF — byte-identical (T-04 unaffected)
# ---------------------------------------------------------------------------


class TestTier3Off:
    def test_chunk_text_byte_identical_and_no_original(self, tmp_path: pathlib.Path) -> None:
        raw = "Byte-identical prose that must not change."
        config = _config(tier3_enabled=False, config_tag="t3-off")
        result = _run_dry_build({"doc-1": [_segment(raw)]}, config, tmp_path, with_llm=True)
        chunks = result["chunks"]
        assert chunks
        for chunk in chunks:
            # No Tier-3 rewrite → original_text is None.
            assert chunk["original_text"] is None
            # text is the canonical slice, position-exact.
            position_slice = chunk["canonical_text"][chunk["char_start"] : chunk["char_end"]]
            assert chunk["text"].encode() == position_slice.encode()
            # No tier=3 record.
            assert not any(tr["tier"] == 3 for tr in chunk["provenance"]["transformations"])
            assert "diff_preview" not in chunk


# ---------------------------------------------------------------------------
# Retrieval response surfaces both texts at citation time (D-14, §7.2 C-R7)
# ---------------------------------------------------------------------------


class TestRetrievalSurfacesBoth:
    def test_result_carries_text_and_original_text(self) -> None:
        """A RetrievalResult for a Tier-3 chunk carries both the rewritten text and
        the original_text; a non-Tier-3 result has original_text=None."""
        prov = Provenance(
            source_document_id="doc-1",
            source_document_version="v1",
            source_location=SourceLocation(
                locator_kind=LocatorKind.char_range, char_start=0, char_end=5
            ),
            structural_path=[],
            transformations=[
                TransformationRecord(
                    tier=TransformationTier.tier_3,
                    operation="rewriting",
                    applied_by=AppliedBy.model,
                    model_ref="fake-rewriter-v1",
                    changed_text=True,
                )
            ],
            confidence=1.0,
            segment_type=SegmentType.prose,
            salience_tier=SalienceTier.primary,
            salience_basis=SalienceSignalKind.segment_type_prior,
            salience_signals=[],
            language="en",
            injection_suspicion=0.0,
            invisible_content_flags=[],
            sensitivity_flags=[],
            trust_level=TrustLevel.untrusted_ingested,
        )
        tier3_result = RetrievalResult(
            chunk_id="chk-1",
            text="Rewritten form.",
            original_text="Original canonical text.",
            provenance=prov,
            score=0.9,
            trust_level=TrustLevel.untrusted_ingested,
        )
        assert tier3_result.text == "Rewritten form."
        assert tier3_result.original_text == "Original canonical text."

        # Non-Tier-3 result: original_text defaults to None.
        plain_result = RetrievalResult(
            chunk_id="chk-2",
            text="Canonical text.",
            provenance=prov,
            score=0.5,
            trust_level=TrustLevel.untrusted_ingested,
        )
        assert plain_result.original_text is None


# ---------------------------------------------------------------------------
# MAJOR-1: per-class model_ref — two tier3-enabled classes, different model_refs
# ---------------------------------------------------------------------------


def _two_class_tier3_config(
    *,
    prose_model_ref: str,
    table_model_ref: str,
    config_tag: str = "t3-2class",
) -> IngestionConfig:
    """Config with TWO tier3-enabled class rules (prose, table) with DIFFERENT model_refs.

    default_rule stays tier3-off (M-035). Each class rule opts into Tier 3 with its own
    tier3_settings.model_ref, so each class's chunks must be stamped with its own model_ref.
    """
    default_rule = ClassRule(
        segment_class=SegmentType.unknown,
        transformation=TransformationSettings(
            tier1_enabled=True,
            tier1_operations=[Tier1Operation.whitespace_repair],
            tier2_enabled=False,
            tier2_operations=[],
            tier3_enabled=False,
        ),
        chunking=_chunking(),
        embedding_override=None,
        metadata_schema=[],
        retrieval_treatment=_retrieval_treatment(),
    )

    def _tier3_rule(seg_class: SegmentType, model_ref: str) -> ClassRule:
        return ClassRule(
            segment_class=seg_class,
            transformation=TransformationSettings(
                tier1_enabled=True,
                tier1_operations=[Tier1Operation.whitespace_repair],
                tier2_enabled=False,
                tier2_operations=[],
                tier3_enabled=True,
                tier3_settings=Tier3Settings(
                    model_ref=model_ref,
                    opt_in_ack=True,
                    diff_preview_required=False,
                ),
            ),
            chunking=_chunking(),
            embedding_override=None,
            metadata_schema=[],
            retrieval_treatment=_retrieval_treatment(),
        )

    config_version = hashlib.sha256(config_tag.encode()).hexdigest()
    return IngestionConfig(
        schema_version=INGESTION_CONFIG_SCHEMA_VERSION,
        tenancy=_tenancy(),
        config_version=config_version,
        created_at=datetime.now(tz=UTC),
        naive_baseline=NaiveBaselineRef(reference_id="nb-v0", description="tier3 2class test"),
        class_rules=[
            _tier3_rule(SegmentType.prose, prose_model_ref),
            _tier3_rule(SegmentType.table, table_model_ref),
        ],
        default_rule=default_rule,
        embedding=_embedding(),
        retrieval_defaults=_retrieval_treatment(),
        language_support=LanguageSupportDecision(
            detected_languages=[], unsupported_languages=[], decision=LanguageDecision.proceed
        ),
        spreadsheet_triage=[],
        exclusions_confirmed=[],
        provenance=[
            RecommendationProvenance(
                target="/class_rules",
                basis=RecommendationBasis.heuristic,
                rationale="tier3 2class test",
            )
        ],
        class_descriptions=[],
        secret_free_attestation=True,
    )


class TestPerClassModelRef:
    """MAJOR-1: each tier3-active chunk is stamped with ITS OWN class rule's model_ref.

    Two class rules (prose, table) enable Tier 3 with DIFFERENT model_refs. Every
    tier=3 TransformationRecord on a prose chunk must carry the prose model_ref, and
    every tier=3 record on a table chunk must carry the table model_ref. The prior
    code built ONE client from the FIRST tier3-enabled rule and applied its model_ref
    to every class — this test fails against that behaviour and passes after the fix.
    """

    def test_each_class_stamped_with_own_model_ref(self, tmp_path: pathlib.Path) -> None:
        prose_ref = "prose-rewriter-vA"
        table_ref = "table-rewriter-vB"
        config = _two_class_tier3_config(prose_model_ref=prose_ref, table_model_ref=table_ref)
        result = _run_dry_build(
            {
                "doc-prose": [_segment("Prose content here.", segment_type=SegmentType.prose)],
                "doc-table": [_segment("Table content here.", segment_type=SegmentType.table)],
            },
            config,
            tmp_path,
            with_llm=True,
        )
        chunks = result["chunks"]
        assert chunks

        prose_chunks = [c for c in chunks if c["document_id"] == "doc-prose"]
        table_chunks = [c for c in chunks if c["document_id"] == "doc-table"]
        assert prose_chunks, "expected prose chunks"
        assert table_chunks, "expected table chunks"

        for chunk in prose_chunks:
            t3 = [tr for tr in chunk["provenance"]["transformations"] if tr["tier"] == 3]
            assert t3, "prose chunk missing tier-3 record"
            for rec in t3:
                assert rec["model_ref"] == prose_ref, (
                    f"prose chunk stamped with {rec['model_ref']!r}, expected {prose_ref!r}"
                )

        for chunk in table_chunks:
            t3 = [tr for tr in chunk["provenance"]["transformations"] if tr["tier"] == 3]
            assert t3, "table chunk missing tier-3 record"
            for rec in t3:
                assert rec["model_ref"] == table_ref, (
                    f"table chunk stamped with {rec['model_ref']!r}, expected {table_ref!r}"
                )


# ---------------------------------------------------------------------------
# MAJOR-2: diff_preview_required enforced on the COMMIT (non-dry-run) path
# ---------------------------------------------------------------------------


class TestCommitPathDiffEnforcement:
    """§7.2 MUST: with diff_preview_required=True, every tier-3 rewrite that COMMITS
    to the shadow collection must carry a persisted diff. A build cannot commit a
    tier-3 rewrite without one; absence is a hard error.
    """

    def test_committed_tier3_chunks_carry_persisted_diff(self, tmp_path: pathlib.Path) -> None:
        config = _config(
            tier3_enabled=True, diff_preview_required=True, config_tag="t3-commit-diff"
        )
        result, adapter = _run_real_build(
            {"doc-1": [_segment("The cat sat on the mat, twice.")]},
            config,
            tmp_path,
            with_llm=True,
        )
        shadow = result["shadow_collection"]
        assert shadow, "expected a shadow collection on a non-dry-run build"

        points = _committed_points(adapter, shadow)
        assert points, "expected committed points in the shadow collection"

        tier3_points = [p for p in points if p.get("payload", {}).get("original_text") is not None]
        assert tier3_points, "expected at least one committed tier-3 chunk"

        for p in tier3_points:
            payload = p["payload"]
            diff = payload.get("diff_preview")
            assert diff, (
                "committed tier-3 chunk is missing its persisted diff_preview — a rewrite "
                "may not commit without a diff when diff_preview_required=True (§7.2)."
            )
            assert "original (canonical Tier-1)" in diff
            assert "rewritten (Tier-3)" in diff

    def test_commit_without_required_diff_fails_closed(
        self, tmp_path: pathlib.Path, monkeypatch: Any
    ) -> None:
        """If the diff cannot be produced when required, the build fails loud rather
        than silently committing an unpreviewed rewrite."""
        import finecorpus.pipeline.build.diff_preview as diff_mod

        # Simulate an unproducible diff (empty string) to exercise the fail-closed guard.
        monkeypatch.setattr(diff_mod, "render_tier3_diff", lambda *a, **k: "")

        config = _config(
            tier3_enabled=True, diff_preview_required=True, config_tag="t3-commit-faildiff"
        )
        with pytest.raises(RuntimeError, match="diff_preview_required"):
            _run_real_build(
                {"doc-1": [_segment("Content requiring a diff.")]},
                config,
                tmp_path,
                with_llm=True,
            )
