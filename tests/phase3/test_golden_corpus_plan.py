"""Golden-corpus smoke test for the Phase 3 Plan stage.

Runs the full pipeline over the golden fixtures and asserts:
- The emitted IngestionConfig has a class_rule for every segment class
  present in the corpus with provenance (Phase 3 contract).
- Every class_rule has at least one provenance entry with basis=heuristic (M-025).
- config_version is a valid sha256 hex string.
- Language support decision is present.

This extends the existing e2e test by asserting the real Plan stage output
rather than the Phase 0 skeleton.
"""

from __future__ import annotations

import pathlib

import pytest

from finecorpus.contracts.ingestion_config import (
    IngestionConfig,
    RecommendationBasis,
)
from finecorpus.pipeline import run_pipeline
from finecorpus.pipeline.artifact_store import ArtifactStore

FIXTURE_CORPUS = pathlib.Path(__file__).parent.parent / "fixtures" / "golden" / "corpus"


@pytest.fixture(scope="module")
def golden_pipeline_run(tmp_path_factory):
    """Run the pipeline once over the golden corpus; share across tests in this module."""
    artifacts_root = tmp_path_factory.mktemp("p3-plan-golden-artifacts")
    run_id = "test-p3-plan-golden"

    artifact_paths = run_pipeline(
        source_dir=FIXTURE_CORPUS,
        artifacts_root=artifacts_root,
        run_id=run_id,
        workspace_id="ws-test-p3-plan",
        kb_id="kb-test-p3-plan",
    )
    store = ArtifactStore(artifacts_root=artifacts_root, run_id=run_id)
    return {"paths": artifact_paths, "store": store, "run_id": run_id}


class TestGoldenCorpusPlan:
    """Phase 3 Plan stage assertions over the golden fixture corpus."""

    def test_plan_artifact_exists(self, golden_pipeline_run):
        store: ArtifactStore = golden_pipeline_run["store"]
        assert store.exists("plan"), "Plan artifact must exist after full pipeline run"

    def test_plan_loads_as_ingestion_config(self, golden_pipeline_run):
        store: ArtifactStore = golden_pipeline_run["store"]
        config = store.load_with_model_validation("plan", IngestionConfig)
        assert config.schema_version.startswith("1."), (
            f"IngestionConfig schema_version must be 1.x, got {config.schema_version}"
        )

    def test_config_version_is_sha256_hex(self, golden_pipeline_run):
        store: ArtifactStore = golden_pipeline_run["store"]
        config = store.load_with_model_validation("plan", IngestionConfig)
        cv = config.config_version
        assert len(cv) == 64, f"config_version must be 64-char sha256 hex, got len={len(cv)}"
        assert all(c in "0123456789abcdef" for c in cv), (
            f"config_version must be hex, got {cv[:16]}..."
        )

    def test_class_rule_for_every_corpus_class(self, golden_pipeline_run):
        """Every segment class present in the corpus has a class_rule with provenance (Phase 3)."""
        store: ArtifactStore = golden_pipeline_run["store"]
        config = store.load_with_model_validation("plan", IngestionConfig)

        # Load the decompose artifact to find all segment classes in the corpus
        decompose_raw = store.load("decompose")
        corpus_classes: set[str] = set()
        for ss in decompose_raw.get("segment_sets", []):
            for seg in ss.get("segments", []):
                seg_type = seg.get("segment_type", "")
                if seg_type:
                    corpus_classes.add(seg_type)

        if not corpus_classes:
            pytest.skip("Golden corpus has no segments — cannot test class_rule coverage")

        # Build set of classes covered by class_rules
        rule_classes = {rule.segment_class.value for rule in config.class_rules}

        # Every corpus class must have a rule
        for cls in sorted(corpus_classes):
            assert cls in rule_classes, (
                f"Segment class '{cls}' is present in the corpus but has no class_rule. "
                "Phase 3: every corpus class must have a per-class rule (completeness invariant)."
            )

    def test_every_class_rule_has_heuristic_provenance(self, golden_pipeline_run):
        """Every class_rule has at least one provenance entry with basis=heuristic (M-025)."""
        store: ArtifactStore = golden_pipeline_run["store"]
        config = store.load_with_model_validation("plan", IngestionConfig)

        # Build index: class → provenance entries
        prov_by_class: dict[str, list] = {}
        for p in config.provenance:
            target = p.target
            if target.startswith("/class_rules/"):
                parts = target.split("/")
                if len(parts) >= 3:
                    cls = parts[2]
                    prov_by_class.setdefault(cls, []).append(p)

        for rule in config.class_rules:
            cls = rule.segment_class.value
            class_prov = prov_by_class.get(cls, [])
            assert len(class_prov) >= 1, (
                f"class_rule for '{cls}' has no provenance entries (M-025 violated)"
            )
            # At least one entry must be heuristic
            heuristic_prov = [p for p in class_prov if p.basis == RecommendationBasis.heuristic]
            assert len(heuristic_prov) >= 1, (
                f"class_rule for '{cls}' has no heuristic provenance entries (M-025 violated)"
            )

    def test_language_support_decision_is_present(self, golden_pipeline_run):
        """IngestionConfig carries a LanguageSupportDecision (§7.6, M-041)."""
        store: ArtifactStore = golden_pipeline_run["store"]
        config = store.load_with_model_validation("plan", IngestionConfig)
        assert config.language_support is not None
        assert config.language_support.decision is not None

    def test_provenance_all_heuristic_or_class_description(self, golden_pipeline_run):
        """All provenance entries have basis=heuristic or basis=class_description (M-025)."""
        store: ArtifactStore = golden_pipeline_run["store"]
        config = store.load_with_model_validation("plan", IngestionConfig)

        allowed_bases = {RecommendationBasis.heuristic, RecommendationBasis.class_description}
        for p in config.provenance:
            assert p.basis in allowed_bases, (
                f"Provenance for target={p.target} has unexpected basis={p.basis}. "
                "All recommender provenance must be heuristic or class_description (M-025)."
            )

    def test_secret_free_attestation_is_true(self, golden_pipeline_run):
        store: ArtifactStore = golden_pipeline_run["store"]
        config = store.load_with_model_validation("plan", IngestionConfig)
        assert config.secret_free_attestation is True

    def test_default_rule_tier3_is_disabled(self, golden_pipeline_run):
        """default_rule must never have tier3_enabled=True (M-035)."""
        store: ArtifactStore = golden_pipeline_run["store"]
        config = store.load_with_model_validation("plan", IngestionConfig)
        assert config.default_rule.transformation.tier3_enabled is False

    def test_no_class_rule_has_tier3_enabled(self, golden_pipeline_run):
        """No class rule generated by the recommender should have tier3_enabled.

        The heuristic recommender never enables Tier 3 (per-class opt-in only, M-031).
        """
        store: ArtifactStore = golden_pipeline_run["store"]
        config = store.load_with_model_validation("plan", IngestionConfig)
        for rule in config.class_rules:
            assert rule.transformation.tier3_enabled is False, (
                f"class_rule for '{rule.segment_class.value}' has tier3_enabled=True. "
                "The heuristic recommender must not enable Tier 3 (per-class opt-in only, M-031)."
            )
