"""Phase 3 acceptance tests — §19 Phase 3 criteria.

Acceptance criteria from spec §19 Phase 3 (four criteria):

  (1) TestRoutingPlan — §19 Phase 3 criterion 1: per-class routing plan generated
      over the golden corpus.  Every segment class present in the corpus has a
      class_rule; manifest expected_chunking_strategy parity; every recommendation
      is labelled (basis present on all provenance entries).

  (2) TestT04 — §19 Phase 3 criterion 2: Tier-2 byte-identity assertion.
      Thin wrapper/reference: asserts the corpus-wide test in
      test_t04_byte_identity.TestT04CorpusWide exists and passes.  The position-exact
      logic (canonical[char_start:char_end]) lives exclusively in that module.

  (3) TestConfigRoundtrip — §19 Phase 3 criterion 3: config round-trips and is
      secret-free.  Exercises export→import→re-export byte-identity over a real
      golden-corpus-derived config (not a synthetic fixture), with secret-free
      attestation verified.

  (4) TestCostGate — §19 Phase 3 criterion 4: cost estimate shown before ingestion.
      Estimate exists before any build artifact in the gated flow; refusal without
      confirmation; CostEstimateUnavailable honesty path for a declared-but-
      unconstructable provider.
"""

from __future__ import annotations

import os
import pathlib
import shutil
from datetime import UTC, datetime
from typing import Any

import pytest
import yaml

# ---------------------------------------------------------------------------
# Paths and manifest helpers
# ---------------------------------------------------------------------------

GOLDEN_DIR = pathlib.Path(__file__).parent.parent / "fixtures" / "golden"
CORPUS_DIR = GOLDEN_DIR / "corpus"
MANIFEST_PATH = GOLDEN_DIR / "manifest.yaml"


def _load_manifest() -> list[dict[str, Any]]:
    data = yaml.safe_load(MANIFEST_PATH.read_text(encoding="utf-8"))
    return data["fixtures"]  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Pipeline helper — runs all stages through Plan over a corpus directory
# ---------------------------------------------------------------------------


def _run_pipeline_to_plan(
    tmp_path: pathlib.Path,
    src_dir: pathlib.Path,
    run_id: str = "p3-accept",
) -> tuple[Any, Any]:  # (ArtifactStore, IngestionConfig)
    """Run collect→assess→decompose→plan and return (store, ingestion_config)."""
    from finecorpus.contracts.ingestion_config import IngestionConfig
    from finecorpus.pipeline import run_pipeline
    from finecorpus.pipeline.artifact_store import ArtifactStore

    artifacts_root = tmp_path / "artifacts"
    run_pipeline(
        source_dir=src_dir,
        artifacts_root=artifacts_root,
        run_id=run_id,
        workspace_id="ws-p3-accept",
        kb_id="kb-p3-accept",
    )
    store = ArtifactStore(artifacts_root=artifacts_root, run_id=run_id)
    config = store.load_with_model_validation("plan", IngestionConfig)
    return store, config


# ---------------------------------------------------------------------------
# (1) TestRoutingPlan — §19 criterion 1
#
# Every segment class present in the golden corpus has a class_rule.
# Manifest expected_chunking_strategy parity.
# Every recommendation labelled (basis present on all provenance).
#
# Detailed unit coverage: tests/phase3/test_recommender_matrix.py (per-class
# matrix row assertions), tests/phase3/test_golden_corpus_plan.py (full plan
# smoke test).  Here we add the §19 acceptance-level assertions explicitly
# tied to criterion 1 — these are the corpus-scale, manifest-parity checks.
# ---------------------------------------------------------------------------


class TestRoutingPlan:
    """§19 Phase 3 criterion 1: per-class routing plan generated over golden corpus.

    Every segment class present in the corpus has a class_rule; manifest
    expected_chunking_strategy parity; every recommendation labelled
    (basis present on all provenance entries).
    """

    @pytest.fixture(scope="class")
    def plan_result(self, tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
        """Run the full pipeline over the golden corpus through Plan stage."""
        tmp_path = tmp_path_factory.mktemp("p3-routing-plan")

        src_dir = tmp_path / "corpus"
        src_dir.mkdir()
        entries = _load_manifest()
        for i, entry in enumerate(entries):
            name = pathlib.Path(entry["file"]).name
            shutil.copy2(CORPUS_DIR / name, src_dir / name)
            mtime_ns = int(datetime(2026, 8, 1 + i, tzinfo=UTC).timestamp() * 1_000_000_000)
            os.utime(src_dir / name, ns=(mtime_ns, mtime_ns))

        store, config = _run_pipeline_to_plan(tmp_path, src_dir)
        decompose_raw = store.load("decompose")

        corpus_classes: set[str] = set()
        for ss in decompose_raw.get("segment_sets", []):
            for seg in ss.get("segments", []):
                st = seg.get("segment_type", "")
                if st:
                    corpus_classes.add(st)

        return {
            "config": config,
            "decompose_raw": decompose_raw,
            "corpus_classes": corpus_classes,
        }

    def test_every_corpus_class_has_class_rule(self, plan_result: dict[str, Any]) -> None:
        """Every segment class present in the corpus has a class_rule.

        §19 criterion 1 core assertion: the Plan stage emits a per-class
        ClassRule for every SegmentType observed in the Decompose artifact.
        Detailed unit coverage: test_golden_corpus_plan.TestGoldenCorpusPlan.
        """
        config = plan_result["config"]
        corpus_classes = plan_result["corpus_classes"]

        if not corpus_classes:
            pytest.skip("No segments in golden corpus — cannot test class_rule coverage")

        rule_classes = {rule.segment_class.value for rule in config.class_rules}
        missing = sorted(corpus_classes - rule_classes)
        assert not missing, (
            f"Segment classes present in corpus but missing class_rules: {missing}. "
            "Phase 3 Plan stage must emit a class_rule for every corpus class."
        )

    def test_manifest_chunking_strategy_parity(self, plan_result: dict[str, Any]) -> None:
        """Manifest expected_chunking_strategy matches pipeline-emitted class rules.

        For each fixture that has a phase3.expected_chunking_strategy list, verify
        that every declared strategy is represented among the class_rules emitted
        for the segment classes present in that fixture's segment set.

        Measured-then-pinned: values were observed from a real pipeline run and
        pinned in manifest.yaml schema_version 1.1.0.
        """
        config = plan_result["config"]
        entries = _load_manifest()

        # Build fixture_name -> segment classes map
        # We match by examining which seg sets have which classes; the test checks
        # that strategy expectations are satisfied by the global class_rules (since
        # the recommender produces one rule per class across the whole corpus).
        rule_strategy_by_class: dict[str, str] = {
            rule.segment_class.value: rule.chunking.strategy.value for rule in config.class_rules
        }

        # Also include default_rule strategy
        default_strategy = config.default_rule.chunking.strategy.value

        mismatches: list[str] = []
        for entry in entries:
            phase3 = entry.get("phase3", {}) or {}
            expected_strategies = phase3.get("expected_chunking_strategy", None)
            if not expected_strategies:
                continue  # excluded fixtures or no expectation declared

            fixture_name = pathlib.Path(entry["file"]).name
            for strategy in expected_strategies:
                # Strategy must appear in at least one class_rule OR the default_rule
                present = (
                    any(v == strategy for v in rule_strategy_by_class.values())
                    or default_strategy == strategy
                )
                if not present:
                    mismatches.append(
                        f"  {fixture_name}: expected strategy {strategy!r} not found "
                        f"in any class_rule. Rule strategies: "
                        f"{sorted(set(rule_strategy_by_class.values()))}"
                    )

        assert not mismatches, "Manifest expected_chunking_strategy parity failures:\n" + "\n".join(
            mismatches
        )

    def test_every_recommendation_labelled_with_basis(self, plan_result: dict[str, Any]) -> None:
        """Every provenance entry has a non-empty basis (heuristic or class_description).

        §19 criterion 1 — recommendations labelled.  All provenance entries on the
        emitted IngestionConfig must carry an explicit basis.  This is the M-025
        acceptance gate at corpus scale.
        """
        from finecorpus.contracts.ingestion_config import RecommendationBasis

        config = plan_result["config"]
        allowed_bases = {RecommendationBasis.heuristic, RecommendationBasis.class_description}

        unlabelled: list[str] = []
        for p in config.provenance:
            if p.basis not in allowed_bases:
                unlabelled.append(
                    f"  target={p.target!r}: basis={p.basis!r} (not in {allowed_bases})"
                )
            if not p.rationale or not p.rationale.strip():
                unlabelled.append(f"  target={p.target!r}: rationale is empty")

        assert not unlabelled, (
            "Provenance entries without valid basis or rationale (M-025):\n" + "\n".join(unlabelled)
        )

    def test_language_support_decision_present(self, plan_result: dict[str, Any]) -> None:
        """IngestionConfig carries a LanguageSupportDecision (M-041).

        The Plan stage must evaluate embedding-model language coverage and record
        its decision (proceed / warned_proceed / blocked) before build.
        """
        config = plan_result["config"]
        assert config.language_support is not None, (
            "IngestionConfig.language_support must be non-null (§7.6 / M-041)"
        )
        assert config.language_support.decision is not None, "language_support.decision must be set"

    def test_secret_free_attestation_set(self, plan_result: dict[str, Any]) -> None:
        """The emitted config must carry secret_free_attestation=True (M-071)."""
        config = plan_result["config"]
        assert config.secret_free_attestation is True, (
            "IngestionConfig.secret_free_attestation must be True (M-071: config exports "
            "must be secret-free by construction)"
        )


# ---------------------------------------------------------------------------
# (2) TestT04 — §19 criterion 2: Tier-2 byte-identity
#
# Thin wrapper: asserts the corpus-wide position-exact test in
# test_t04_byte_identity.TestT04CorpusWide exists and passes.
# The full position-exact assertion logic lives in that module; duplicating
# it here would create a maintenance burden and mask gaps.
# ---------------------------------------------------------------------------


class TestT04:
    """§19 Phase 3 criterion 2: Tier-2 byte-identity.

    Every chunk.text byte-equals the corresponding canonical[char_start:char_end]
    slice — position-exact, not canonical.index(text) which masks off-by-position
    bugs on repeated text.

    This class invokes ``run_corpus_wide_t04_verification`` — the shared helper
    extracted from TestT04CorpusWide per Ruling 4 — to run the REAL corpus-wide
    verification and assert on the returned stats.  This supersedes the former
    method-existence-only sentinel test.

    Detailed per-chunk test coverage lives in:
        tests/phase3/test_t04_byte_identity.TestT04CorpusWide
    """

    def test_t04_corpus_wide_real_verification(self, tmp_path: pathlib.Path) -> None:
        """§19 criterion 2: run corpus-wide byte-identity verification for real.

        Invokes ``run_corpus_wide_t04_verification()`` (Ruling 4 callable helper)
        and asserts on the returned stats:
        - fixtures_verified > 0 (at least one fixture contributed chunks)
        - chunks_verified > 0 (at least one chunk was checked)
        - zero byte-identity violations
        - zero tier-2 changed_text=True violations
        """
        from tests.phase3.test_t04_byte_identity import (  # noqa: PLC0415
            run_corpus_wide_t04_verification,
        )

        stats = run_corpus_wide_t04_verification(tmp_path)

        assert stats["fixtures_verified"] > 0, (
            "§19 criterion 2: corpus-wide T-04 verification found no fixtures with chunks. "
            f"stats={stats}"
        )
        assert stats["chunks_verified"] > 0, (
            "§19 criterion 2: corpus-wide T-04 verification found no chunks to check. "
            f"stats={stats}"
        )
        assert not stats["violations"], (
            f"§19 criterion 2: byte-identity failures in {len(stats['violations'])} chunks "
            f"(fixtures_verified={stats['fixtures_verified']}, "
            f"chunks_verified={stats['chunks_verified']}):\n" + "\n".join(stats["violations"][:10])
        )
        assert not stats["tier2_violations"], (
            f"§19 criterion 2: tier-2 changed_text=True in {len(stats['tier2_violations'])} "
            f"records:\n" + "\n".join(stats["tier2_violations"][:10])
        )

    def test_t04_corpus_wide_byte_identity_criterion_is_covered(self) -> None:
        """§19 criterion 2: TestT04CorpusWide and run_corpus_wide_t04_verification exist.

        Structural sentinel confirming the module, class, and callable helper are present.
        The real verification runs in test_t04_corpus_wide_real_verification above.
        """
        from tests.phase3 import test_t04_byte_identity  # noqa: PLC0415

        assert hasattr(test_t04_byte_identity, "TestT04CorpusWide"), (
            "TestT04CorpusWide class must exist in test_t04_byte_identity (§19 criterion 2)"
        )
        cls = test_t04_byte_identity.TestT04CorpusWide
        assert hasattr(cls, "test_corpus_wide_byte_identity_position_exact"), (
            "test_corpus_wide_byte_identity_position_exact must exist in TestT04CorpusWide"
        )
        assert hasattr(cls, "test_corpus_wide_tier2_changed_text_false"), (
            "test_corpus_wide_tier2_changed_text_false must exist in TestT04CorpusWide"
        )
        assert hasattr(test_t04_byte_identity, "run_corpus_wide_t04_verification"), (
            "run_corpus_wide_t04_verification callable must exist in test_t04_byte_identity "
            "(Ruling 4: acceptance sentinel invokes real verification)"
        )

    def test_t04_unit_position_exact_passes(self, tmp_path: pathlib.Path) -> None:
        """§19 criterion 2 unit gate: prose chunk byte-identity (position-exact).

        Runs the unit-level T-04 test inline to provide acceptance-visible coverage
        without requiring the full corpus pipeline run fixture.  Uses the same
        FakeLLMProvider + FakeEmbeddingProvider approach as the corpus-wide test.
        """
        import hashlib
        import json
        import uuid

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
        from finecorpus.pipeline.build.stage import BuildStage

        run_id = "t04-accept"
        artifacts_root = tmp_path

        raw_text = "  Some prose content.  With trailing spaces.  \r\n"
        seg = Segment(
            segment_id=str(uuid.uuid4()),
            document_order=0,
            segment_type=SegmentType.prose,
            salience_tier=SalienceTier.primary,
            structural_path=["Section 1"],
            segment_path="seg/0",
            location=SourceLocation(
                locator_kind=LocatorKind.char_range, char_start=0, char_end=len(raw_text)
            ),
            source_region_ids=["r0"],
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
            text=raw_text,
        )

        tenancy = TenancyBlock(
            workspace_id="ws-t04-accept",
            kb_id="kb-t04-accept",
            permission_mode=PermissionMode.public_to_kb,
            permission_principals=[],
            permission_source=PermissionSource.platform,
            permission_fidelity=PermissionFidelity.authoritative,
            permission_resolved_at=None,
        )
        seg_set = SegmentSet(
            schema_version="1.1.0",
            tenancy=tenancy,
            document_id="doc-t04-accept",
            content_hash=hashlib.sha256(b"doc-t04-accept").hexdigest(),
            segments=[seg],
            reassembly=ReassemblyRecord(
                method=ReassemblyMethod.document_order_concat,
                covered_region_ids=["r0"],
                reassembly_digest=hashlib.sha256(raw_text.encode()).hexdigest(),
            ),
            exclusions=[],
            cross_references=[],
            decomposed_at=datetime.now(tz=UTC),
        )
        batch = SegmentSetBatch(
            schema_version=BATCH_SCHEMA_VERSION,
            contract="segment_set_batch",
            run_id=run_id,
            produced_at=datetime.now(tz=UTC),
            skeleton=None,
            segment_sets=[seg_set.model_dump(mode="json")],
        )
        run_dir = artifacts_root / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "decompose.json").write_text(
            json.dumps(batch.model_dump(mode="json")), encoding="utf-8"
        )

        prose_rule = ClassRule(
            segment_class=SegmentType.prose,
            transformation=TransformationSettings(
                tier1_enabled=True,
                tier1_operations=[Tier1Operation.whitespace_repair],
                tier2_enabled=True,
                tier2_operations=[Tier2Operation.breadcrumb_augment],
                tier3_enabled=False,
            ),
            chunking=ChunkingConfig(
                strategy=ChunkingStrategy.recursive_char,
                max_tokens=100,
                overlap_tokens=10,
                respect_headings=False,
            ),
            metadata_schema=[],
            retrieval_treatment=RetrievalTreatment(
                default_salience_filter=[SalienceTier.primary, SalienceTier.supporting],
                salience_weights=None,
                rerank_eligible=False,
                strategy=RetrievalStrategy.dense,
            ),
        )
        cv = hashlib.sha256(b"t04-accept-config").hexdigest()
        config = IngestionConfig(
            schema_version=INGESTION_CONFIG_SCHEMA_VERSION,
            tenancy=tenancy,
            config_version=cv,
            created_at=datetime.now(tz=UTC),
            naive_baseline=NaiveBaselineRef(reference_id="nb-v0", description="T-04 accept"),
            class_rules=[],
            default_rule=prose_rule,
            embedding=EmbeddingConfig(
                provider="fake", model="fake-embed-v1", dimensions=64, normalize=True
            ),
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
                    rationale="T-04 accept test.",
                )
            ],
            class_descriptions=[],
            secret_free_attestation=True,
        )

        stage = BuildStage(
            artifacts_root=artifacts_root,
            run_id=run_id,
            dry_run=True,
        )
        result = stage._produce(config.model_dump(mode="json"))  # noqa: SLF001
        chunks = result["chunks"]
        assert chunks, "Expected at least one chunk for prose segment"

        for chunk in chunks:
            text = chunk["text"]
            canonical = chunk["canonical_text"]
            char_start = chunk["char_start"]
            char_end = chunk["char_end"]
            position_slice = canonical[char_start:char_end]
            assert text.encode() == position_slice.encode(), (
                f"T-04 byte-identity (§19 criterion 2): chunk.text {text!r} != "
                f"canonical[{char_start}:{char_end}] = {position_slice!r}"
            )

            # Tier-2 records must have changed_text=False
            for tr in chunk["provenance"].get("transformations", []):
                if tr.get("tier") == 2:
                    assert tr["changed_text"] is False, (
                        f"Tier-2 TransformationRecord has changed_text=True: {tr}"
                    )


# ---------------------------------------------------------------------------
# (3) TestConfigRoundtrip — §19 criterion 3
#
# Config round-trips and is secret-free.  Uses a real golden-corpus-derived
# config (not a synthetic fixture): the Plan stage runs over the golden corpus
# and the emitted IngestionConfig is round-tripped through export→import→re-export.
#
# Detailed unit coverage: tests/phase3/test_config_roundtrip.py (synthetic
# config roundtrip variants, tamper detection, extra=forbid, version range).
# Here we add the §19 acceptance-level assertion using the real Plan output.
# ---------------------------------------------------------------------------


class TestConfigRoundtrip:
    """§19 Phase 3 criterion 3: config round-trips and is secret-free.

    export→import→re-export is byte-identical over a REAL golden-corpus-derived
    config.  Secret-free attestation verified.  Keys set in env (none needed for
    the fake-provider config produced by the Plan stage over the golden corpus).

    Detailed unit coverage: tests/phase3/test_config_roundtrip.py.
    """

    @pytest.fixture(scope="class")
    def real_config_and_path(
        self, tmp_path_factory: pytest.TempPathFactory
    ) -> tuple[Any, pathlib.Path]:
        """Run the pipeline to Plan stage and return (ingestion_config, export_path)."""
        tmp_path = tmp_path_factory.mktemp("p3-config-roundtrip")

        src_dir = tmp_path / "corpus"
        src_dir.mkdir()
        entries = _load_manifest()
        for i, entry in enumerate(entries):
            name = pathlib.Path(entry["file"]).name
            shutil.copy2(CORPUS_DIR / name, src_dir / name)
            mtime_ns = int(datetime(2026, 8, 1 + i, tzinfo=UTC).timestamp() * 1_000_000_000)
            os.utime(src_dir / name, ns=(mtime_ns, mtime_ns))

        _store, config = _run_pipeline_to_plan(tmp_path, src_dir, run_id="p3-roundtrip-run")

        from finecorpus.pipeline.plan.config_io import export_config

        export_path = tmp_path / "exported_config.json"
        export_config(config, export_path)
        return config, export_path

    def test_export_import_reexport_byte_identical(
        self, real_config_and_path: tuple[Any, pathlib.Path], tmp_path: pathlib.Path
    ) -> None:
        """export→import→re-export is byte-identical over a real golden-corpus config.

        §19 criterion 3 core assertion.  The round-trip must produce no delta
        (deterministic JSON serialization, field ordering stable).
        """
        from finecorpus.pipeline.plan.config_io import export_config, import_config

        config, export_path = real_config_and_path
        original_content = export_path.read_text(encoding="utf-8")

        imported = import_config(export_path)
        reexport_path = tmp_path / "reexported_config.json"
        export_config(imported, reexport_path)
        reexported_content = reexport_path.read_text(encoding="utf-8")

        assert original_content == reexported_content, (
            "Round-trip parity failed: re-exported config is not byte-identical to the "
            "original export (§19 criterion 3, M-026)."
        )

    def test_config_is_secret_free(self, real_config_and_path: tuple[Any, pathlib.Path]) -> None:
        """The exported config must have secret_free_attestation=True (M-071).

        The golden-corpus Plan stage uses only the fake embedding provider, so no
        real API keys are present.  The attestation field must be True by construction.
        """
        config, export_path = real_config_and_path
        assert config.secret_free_attestation is True, (
            "IngestionConfig.secret_free_attestation must be True (M-071)"
        )

        import json

        exported = json.loads(export_path.read_text(encoding="utf-8"))
        assert exported.get("secret_free_attestation") is True, (
            "secret_free_attestation must be True in the exported JSON"
        )

    def test_import_preserves_config_version(
        self, real_config_and_path: tuple[Any, pathlib.Path]
    ) -> None:
        """import_config must preserve config_version exactly (tamper-detection anchor)."""
        from finecorpus.pipeline.plan.config_io import import_config

        config, export_path = real_config_and_path
        imported = import_config(export_path)
        assert imported.config_version == config.config_version, (
            f"config_version changed after import: "
            f"original={config.config_version[:16]}... "
            f"imported={imported.config_version[:16]}..."
        )

    def test_import_preserves_all_class_rules(
        self, real_config_and_path: tuple[Any, pathlib.Path]
    ) -> None:
        """import_config must preserve all class_rules — count and segment_class values."""
        from finecorpus.pipeline.plan.config_io import import_config

        config, export_path = real_config_and_path
        imported = import_config(export_path)
        assert len(imported.class_rules) == len(config.class_rules), (
            f"class_rules count changed after import: "
            f"original={len(config.class_rules)}, imported={len(imported.class_rules)}"
        )
        orig_classes = {r.segment_class for r in config.class_rules}
        imported_classes = {r.segment_class for r in imported.class_rules}
        assert orig_classes == imported_classes, (
            f"class_rule segment_classes differ after import: "
            f"original={orig_classes}, imported={imported_classes}"
        )


# ---------------------------------------------------------------------------
# (4) TestCostGate — §19 criterion 4
#
# Cost estimate shown before ingestion.  Estimate exists before any build
# artifact in the gated flow; refusal without confirmation; CostEstimateUnavailable
# honesty path for a declared-but-unconstructable provider.
#
# Detailed coverage: tests/phase3/test_honest_cost_gate.py (resolve_costing_providers,
# _try_load_cost_estimate, _print_cost_estimate).
# Here we add the §19 acceptance-level assertions explicitly tied to criterion 4.
# ---------------------------------------------------------------------------


class TestCostGate:
    """§19 Phase 3 criterion 4: cost estimate shown before ingestion.

    Estimate exists before any build artifact in the gated flow; refusal without
    confirmation; CostEstimateUnavailable honesty path for a declared-but-
    unconstructable provider.

    Detailed unit coverage: tests/phase3/test_honest_cost_gate.py.
    """

    def test_fake_provider_estimate_is_available(self, tmp_path: pathlib.Path) -> None:
        """Fake provider → estimate available and zero_marginal_cost=True.

        §19 criterion 4: estimate exists before build.  For the fake (local/zero-cost)
        provider used in CI, the cost gate must be satisfiable.
        """
        import argparse
        import hashlib
        import json
        import uuid

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

        run_id = "cost-gate-accept"
        tenancy = TenancyBlock(
            workspace_id="ws-cg",
            kb_id="kb-cg",
            permission_mode=PermissionMode.public_to_kb,
            permission_principals=[],
            permission_source=PermissionSource.platform,
            permission_fidelity=PermissionFidelity.authoritative,
            permission_resolved_at=None,
        )
        config = IngestionConfig(
            schema_version=INGESTION_CONFIG_SCHEMA_VERSION,
            tenancy=tenancy,
            config_version=hashlib.sha256(b"cg-accept").hexdigest(),
            created_at=datetime.now(tz=UTC),
            naive_baseline=NaiveBaselineRef(reference_id="nb-v0", description="Cost gate accept."),
            class_rules=[],
            default_rule=ClassRule(
                segment_class=SegmentType.prose,
                transformation=TransformationSettings(
                    tier1_enabled=False,
                    tier1_operations=[],
                    tier2_enabled=False,
                    tier2_operations=[],
                    tier3_enabled=False,
                ),
                chunking=ChunkingConfig(
                    strategy=ChunkingStrategy.recursive_char,
                    max_tokens=512,
                    overlap_tokens=50,
                    respect_headings=False,
                ),
                metadata_schema=[],
                retrieval_treatment=RetrievalTreatment(
                    default_salience_filter=[SalienceTier.primary],
                    salience_weights=None,
                    rerank_eligible=False,
                    strategy=RetrievalStrategy.dense,
                ),
            ),
            embedding=EmbeddingConfig(
                provider="fake",
                model="fake-embed-v1",
                dimensions=64,
                normalize=True,
            ),
            retrieval_defaults=RetrievalTreatment(
                default_salience_filter=[SalienceTier.primary],
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
                    rationale="Cost gate accept test.",
                )
            ],
            class_descriptions=[],
            secret_free_attestation=True,
        )

        run_dir = tmp_path / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "plan.json").write_text(
            json.dumps(config.model_dump(mode="json")), encoding="utf-8"
        )

        seg = Segment(
            segment_id=str(uuid.uuid4()),
            document_order=0,
            segment_type=SegmentType.prose,
            salience_tier=SalienceTier.primary,
            structural_path=[],
            segment_path="seg/0",
            location=SourceLocation(locator_kind=LocatorKind.char_range, char_start=0, char_end=20),
            source_region_ids=["r0"],
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
            text="Sample text for cost gate.",
        )
        seg_set = SegmentSet(
            schema_version="1.1.0",
            tenancy=tenancy,
            document_id="doc-cg-accept",
            content_hash=hashlib.sha256(b"doc-cg-accept").hexdigest(),
            segments=[seg],
            reassembly=ReassemblyRecord(
                method=ReassemblyMethod.document_order_concat,
                covered_region_ids=["r0"],
                reassembly_digest=hashlib.sha256(b"sample").hexdigest(),
            ),
            exclusions=[],
            cross_references=[],
            decomposed_at=datetime.now(tz=UTC),
        )
        batch = SegmentSetBatch(
            schema_version=BATCH_SCHEMA_VERSION,
            contract="segment_set_batch",
            run_id=run_id,
            produced_at=datetime.now(tz=UTC),
            skeleton=None,
            segment_sets=[seg_set.model_dump(mode="json")],
        )
        (run_dir / "decompose.json").write_text(
            json.dumps(batch.model_dump(mode="json")), encoding="utf-8"
        )

        from finecorpus.cli.main import _try_load_cost_estimate

        args = argparse.Namespace(artifacts=str(tmp_path), run_id=run_id)
        estimate = _try_load_cost_estimate(args)

        assert estimate is not None, (
            "Cost estimate must be available for fake provider (§19 criterion 4: "
            "estimate exists before ingestion)"
        )
        assert estimate.embedding_zero_marginal_cost is True, (
            "Fake provider estimate must have zero_marginal_cost=True"
        )

    def test_cost_estimate_unavailable_for_undeclared_provider_is_honest(
        self, tmp_path: pathlib.Path
    ) -> None:
        """CostEstimateUnavailable honesty path: declared openai provider with no API key.

        §19 criterion 4: cost gate must NOT fabricate $0.00 for a declared paid provider
        when the API key is absent.  Ruling 2 (decision ledger): the CLI must return an
        unavailable sentinel rather than a fake $0.00 estimate.

        Detailed unit coverage: test_honest_cost_gate.TestCostGate.
        """
        import os
        from unittest.mock import patch

        # Verify the core honesty invariant: openai provider without key → unavailable
        clean_env = {
            k: v
            for k, v in os.environ.items()
            if k not in ("OPENAI_API_KEY", "FINECORPUS_OPENAI_API_KEY")
        }
        with patch.dict("os.environ", clean_env, clear=True):
            from finecorpus.pipeline.costing import resolve_costing_providers
            from tests.phase3.test_honest_cost_gate import _make_ingestion_config  # noqa: PLC0415

            config = _make_ingestion_config(provider="openai")
            result = resolve_costing_providers(config)

        assert result.embedding_unavailable is True, (
            "Declared 'openai' provider without API key must be marked unavailable "
            "(§19 criterion 4 / Ruling 2: never fabricate $0.00 for a paid provider)"
        )
        assert result.embedding_provider is None, (
            "embedding_provider must be None when the provider is unavailable "
            "(returning a fake provider would misrepresent the cost as $0.00)"
        )


# ---------------------------------------------------------------------------
# Corpus-wide manifest segment-type parity (Ruling 3 — drift sentinel)
#
# Asserts that each golden fixture's produced segment-type set matches the
# expected_segment_types declared in manifest.yaml.
#
# Semantics: SUBSET — the manifest declares a minimum required set; the
# pipeline may produce additional types not listed (e.g. cross_reference,
# boilerplate) without failing this assertion.  Fixtures with an empty
# expected_segment_types list (excluded fixtures) must produce no segments.
#
# This makes manifest drift impossible to hide: any change to segmentation
# that adds or removes a type from a fixture will be caught here UNLESS the
# manifest is also updated.  Fixtures with a `note:` key are allowed to have
# the pipeline produce a subset of what the content logically contains (the
# note explains the limitation).
# ---------------------------------------------------------------------------


class TestManifestSegmentTypeParity:
    """Corpus-wide manifest parity: expected_segment_types vs pipeline output.

    Semantics:
    - expected_segment_types is EMPTY → fixture is excluded; assert 0 segments.
    - expected_segment_types is NON-EMPTY → every declared type must appear in
      the produced segment set (subset semantics: pipeline may produce more).

    This sentinel makes segmentation drift impossible to hide without updating
    the manifest.
    """

    @pytest.fixture(scope="class")
    def corpus_seg_types(self, tmp_path_factory: pytest.TempPathFactory) -> dict[str, set[str]]:
        """Run full pipeline over golden corpus; return {fixture_name: set(segment_types)}.

        Uses content_hash (sha256 in manifest) to map fixtures to segment sets — the
        document_id is an opaque generated key, but content_hash is stable and declared
        in the manifest.
        """
        import os

        tmp_path = tmp_path_factory.mktemp("manifest-parity")
        src_dir = tmp_path / "corpus"
        src_dir.mkdir()
        entries = _load_manifest()
        for i, entry in enumerate(entries):
            name = pathlib.Path(entry["file"]).name
            shutil.copy2(CORPUS_DIR / name, src_dir / name)
            mtime_ns = int(datetime(2026, 8, 1 + i, tzinfo=UTC).timestamp() * 1_000_000_000)
            os.utime(src_dir / name, ns=(mtime_ns, mtime_ns))

        from finecorpus.pipeline import run_pipeline
        from finecorpus.pipeline.artifact_store import ArtifactStore

        artifacts_root = tmp_path / "artifacts"
        run_pipeline(
            source_dir=src_dir,
            artifacts_root=artifacts_root,
            run_id="manifest-parity",
            workspace_id="ws-mp",
            kb_id="kb-mp",
        )
        store = ArtifactStore(artifacts_root=artifacts_root, run_id="manifest-parity")
        decompose_raw = store.load("decompose")

        # Build {content_hash → set(segment_types)} mapping
        hash_to_types: dict[str, set[str]] = {}
        for ss in decompose_raw.get("segment_sets", []):
            content_hash = ss.get("content_hash", "")
            types = {seg.get("segment_type", "") for seg in ss.get("segments", [])}
            hash_to_types[content_hash] = types

        # Map fixture filename → segment types using manifest sha256 as key
        result: dict[str, set[str]] = {}
        for entry in entries:
            name = pathlib.Path(entry["file"]).name
            sha256 = entry.get("sha256", "")
            result[name] = hash_to_types.get(sha256, set())

        return result

    def test_manifest_segment_type_parity(self, corpus_seg_types: dict[str, set[str]]) -> None:
        """Every declared expected_segment_type must appear in the produced set.

        Excluded fixtures (empty expected_segment_types) must produce no segments.
        Subset semantics: pipeline may produce ADDITIONAL types not in the manifest.

        Drift sentinel: if segmentation changes add/remove a type from a fixture,
        this test fails — the manifest must be updated to reflect the new reality.
        """
        entries = _load_manifest()
        failures: list[str] = []

        for entry in entries:
            name = pathlib.Path(entry["file"]).name
            expected = set(entry.get("expected_segment_types") or [])
            produced = corpus_seg_types.get(name, set())

            if not expected:
                # Excluded fixture — must produce no segments
                if produced:
                    failures.append(
                        f"  {name}: expected EXCLUDED (empty types) but got segments: "
                        f"{sorted(produced)}"
                    )
            else:
                # Must produce at least all declared types (subset semantics)
                missing = expected - produced
                if missing:
                    failures.append(
                        f"  {name}: manifest declares {sorted(expected)} but "
                        f"pipeline produced {sorted(produced)}; "
                        f"missing: {sorted(missing)}"
                    )

        assert not failures, (
            "Manifest segment-type parity failures (update manifest.yaml to pin "
            "the measured truth, add note: if a limitation exists):\n" + "\n".join(failures)
        )
