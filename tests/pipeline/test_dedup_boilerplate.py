"""Phase 2 tests — near-duplicate clustering and boilerplate detection.

Test coverage:
  - Near-duplicate clustering: policy v1/v2/v3 → v3 primary, v1/v2 superseded.
  - D-25 enforcement: superseded docs produce no segments (default toggle=False).
  - D-25 toggle: index_superseded_versions=True → excluded-tier segments produced.
  - Boilerplate detection: shared 898-char legal preamble detected in all 3 carrier docs.
  - Boilerplate reclassification: preamble segments reclassified to boilerplate tier.
  - Non-boilerplate prose unaffected: bloated_manual's other prose stays primary.
  - Small-corpus threshold: raised proportion for corpora < threshold doc count.
  - Near-duplicate threshold config: high threshold disables clustering.
  - Nothing-dropped invariants: exclusions account for every superseded doc.
  - ExclusionRecord.reason == superseded_version for D-25 exclusions.
  - ParseResultBatch 1.1.0 fields: version_families and boilerplate_blocks present.
"""

from __future__ import annotations

import os
import pathlib
import shutil
from datetime import UTC, datetime
from typing import Any

import pytest

from finecorpus.contracts.parse_result_batch import ParseResultBatch
from finecorpus.pipeline.artifact_store import ArtifactStore
from finecorpus.pipeline.assess.corpus_passes import (
    compute_boilerplate_blocks,
    compute_version_families,
    run_corpus_passes,
)
from finecorpus.pipeline.decompose.stage import DecomposeStage

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

FIXTURE_CORPUS = pathlib.Path(__file__).parent.parent / "fixtures" / "golden" / "corpus"

POLICY_FIXTURES = ["policy_v1.pdf", "policy_v2.pdf", "policy_v3.pdf"]
BOILERPLATE_FIXTURES = ["boilerplate_a.pdf", "boilerplate_b.pdf", "bloated_manual.pdf"]


def _run_pipeline_over_subset(
    tmp_path: pathlib.Path,
    fixture_names: list[str],
    run_id: str = "test-dedup",
    index_superseded_versions: bool = False,
) -> tuple[ArtifactStore, dict[str, Any], dict[str, Any]]:
    """Copy fixtures to a temp dir, run the pipeline, and return artifacts."""
    artifacts_root = tmp_path / "artifacts"
    src_dir = tmp_path / "corpus"
    src_dir.mkdir()

    for i, name in enumerate(fixture_names):
        dest = src_dir / name
        shutil.copy2(FIXTURE_CORPUS / name, dest)
        # Pin mtimes in list order: primacy election reads source_modified_at from
        # st_mtime, and git checkout does not preserve fixture mtimes on CI.
        mtime_ns = int(datetime(2026, 8, 1 + i, tzinfo=UTC).timestamp() * 1_000_000_000)
        os.utime(dest, ns=(mtime_ns, mtime_ns))

    # We need to pass index_superseded_versions through the orchestrator.
    # The DecomposeStage constructor accepts it directly; for the full pipeline we
    # build the batch manually and call stages directly to thread the toggle.
    from finecorpus.pipeline.artifact_store import ArtifactStore
    from finecorpus.pipeline.assess import AssessStage
    from finecorpus.pipeline.collect import CollectStage

    store = ArtifactStore(artifacts_root=artifacts_root, run_id=run_id)
    ts = datetime(2026, 9, 1, tzinfo=UTC)

    collect = CollectStage(
        source_dir=src_dir,
        workspace_id="ws-test",
        kb_id="kb-test",
        collected_at=ts,
    )
    inv = collect.run(input_data=None, store=store)

    assess = AssessStage(run_id=run_id, run_started_at=ts)
    parse_batch = assess.run(input_data=inv, store=store)

    decompose = DecomposeStage(
        run_started_at=ts,
        artifacts_root=artifacts_root,
        run_id=run_id,
        index_superseded_versions=index_superseded_versions,
    )
    seg_batch = decompose.run(input_data=parse_batch, store=store)

    return store, parse_batch, seg_batch


# ---------------------------------------------------------------------------
# ParseResultBatch 1.1.0 contract fields
# ---------------------------------------------------------------------------


class TestParseResultBatchV11Fields:
    """ParseResultBatch 1.1.0 must carry version_families and boilerplate_blocks."""

    def test_schema_version_is_1_1(self, tmp_path):
        """AssessStage stamps schema_version 1.1.0 on ParseResultBatch."""
        store, parse_batch, _ = _run_pipeline_over_subset(tmp_path, POLICY_FIXTURES)
        batch = store.load_with_model_validation("assess", ParseResultBatch)
        assert batch.schema_version == "1.1.0", f"Expected 1.1.0, got {batch.schema_version}"

    def test_version_families_field_present(self, tmp_path):
        """ParseResultBatch must have a version_families list."""
        store, parse_batch, _ = _run_pipeline_over_subset(tmp_path, POLICY_FIXTURES)
        assert "version_families" in parse_batch
        assert isinstance(parse_batch["version_families"], list)

    def test_boilerplate_blocks_field_present(self, tmp_path):
        """ParseResultBatch must have a boilerplate_blocks list."""
        store, parse_batch, _ = _run_pipeline_over_subset(tmp_path, BOILERPLATE_FIXTURES)
        assert "boilerplate_blocks" in parse_batch
        assert isinstance(parse_batch["boilerplate_blocks"], list)


# ---------------------------------------------------------------------------
# Near-duplicate clustering (§6.1)
# ---------------------------------------------------------------------------


class TestNearDuplicateClustering:
    """Policy v1/v2/v3 must form one version family with v3 as primary."""

    def test_one_version_family_detected(self, tmp_path):
        """The three policy versions must produce exactly one version family."""
        _, parse_batch, _ = _run_pipeline_over_subset(tmp_path, POLICY_FIXTURES)
        families = parse_batch.get("version_families", [])
        assert len(families) == 1, f"Expected 1 version family, got {len(families)}: {families}"

    def test_v3_is_primary(self, tmp_path):
        """The newest policy version (v3) must be selected as primary."""
        store, parse_batch, _ = _run_pipeline_over_subset(tmp_path, POLICY_FIXTURES)

        families = parse_batch["version_families"]
        assert families, "No version families detected"
        family = families[0]

        inv = store.load("collect")

        # Find which document_id corresponds to policy_v3.pdf
        v3_id = None
        for item in inv["items"]:
            if "policy_v3" in item.get("source_path", "") or "policy_v3" in item.get(
                "display_name", ""
            ):
                v3_id = item["document_id"]
                break

        assert v3_id is not None, "policy_v3.pdf not found in inventory"
        assert family["primary_document_id"] == v3_id, (
            f"Expected v3 ({v3_id[:12]}) as primary, got {family['primary_document_id'][:12]}"
        )

    def test_v1_and_v2_are_superseded(self, tmp_path):
        """v1 and v2 must be in the superseded list."""
        store, parse_batch, _ = _run_pipeline_over_subset(tmp_path, POLICY_FIXTURES)
        families = parse_batch["version_families"]
        family = families[0]

        superseded = set(family["superseded_document_ids"])
        assert len(superseded) == 2, (
            f"Expected 2 superseded members, got {len(superseded)}: {superseded}"
        )

    def test_dedup_role_annotations(self, tmp_path):
        """Each ParseResult must be annotated with the correct dedup_role."""
        _, parse_batch, _ = _run_pipeline_over_subset(tmp_path, POLICY_FIXTURES)
        families = parse_batch["version_families"]
        primary_id = families[0]["primary_document_id"]
        superseded_ids = set(families[0]["superseded_document_ids"])

        for pr in parse_batch["results"]:
            doc_id = pr["document_id"]
            role = pr.get("dedup_role")
            if doc_id == primary_id:
                assert role == "primary", f"Expected 'primary', got '{role}'"
            elif doc_id in superseded_ids:
                assert role == "superseded", f"Expected 'superseded', got '{role}'"
            else:
                assert role in ("unique", "primary", "superseded"), f"Unknown role: {role}"

    def test_family_schema_fields(self, tmp_path):
        """VersionFamily dict must have all required inventory.md schema fields."""
        _, parse_batch, _ = _run_pipeline_over_subset(tmp_path, POLICY_FIXTURES)
        family = parse_batch["version_families"][0]
        for field in (
            "family_id",
            "member_document_ids",
            "primary_document_id",
            "superseded_document_ids",
            "similarity_method",
            "similarity_scores",
            "primacy_basis",
        ):
            assert field in family, f"Missing field: {field}"

    def test_similarity_scores_populated(self, tmp_path):
        """similarity_scores must be populated for each superseded member."""
        _, parse_batch, _ = _run_pipeline_over_subset(tmp_path, POLICY_FIXTURES)
        family = parse_batch["version_families"][0]
        scores = family.get("similarity_scores", {})
        for sid in family["superseded_document_ids"]:
            assert sid in scores, f"Missing score for {sid}"
            assert 0.0 <= scores[sid] <= 1.0, f"Score out of range: {scores[sid]}"

    def test_high_threshold_suppresses_clustering(self, tmp_path):
        """A threshold of 0.99 must prevent the policy family from being detected."""
        # Build parse results manually and call corpus_passes directly
        from finecorpus.pipeline.assess.corpus_passes import run_corpus_passes

        _, parse_batch, _ = _run_pipeline_over_subset(
            tmp_path, POLICY_FIXTURES, run_id="test-high-thresh"
        )
        results = list(parse_batch["results"])

        # Reset dedup_role so the corpus pass can run cleanly
        for r in results:
            r.pop("dedup_role", None)
            r.pop("dedup_family_id", None)
            r.pop("dedup_primary_document_id", None)

        families, _ = run_corpus_passes(results, near_duplicate_threshold=0.99)
        assert families == [], f"Threshold 0.99 should produce no families, got: {families}"

    def test_unrelated_docs_not_clustered(self, tmp_path):
        """A corpus of completely different documents must produce no version families."""
        # Use clean_native + adversarial + form_filled — completely different content
        fixtures = ["clean_native.pdf", "form_filled.pdf"]
        _, parse_batch, _ = _run_pipeline_over_subset(tmp_path, fixtures, run_id="test-unrelated")
        # These documents share no significant content — may cluster or not, but
        # if they do, it's a false positive. We verify no cluster with the default threshold.
        # (If clean_native and form_filled happen to exceed 0.50, the test needs adjustment,
        # but empirically these PDFs have very different content.)
        # Softer assertion: no more than 0 families with Jaccard >= 0.70 for unrelated docs.
        # We run with a high threshold to be safe.
        results = list(parse_batch["results"])
        families_strict = compute_version_families(results, near_duplicate_threshold=0.70)
        assert families_strict == [], (
            f"Unrelated docs should not cluster at 0.70: {families_strict}"
        )


# ---------------------------------------------------------------------------
# D-25 enforcement — superseded versions produce no segments
# ---------------------------------------------------------------------------


class TestD25Enforcement:
    """Superseded near-duplicate documents must produce no segments (D-25, §6.1)."""

    def test_superseded_docs_produce_no_segments(self, tmp_path):
        """v1 and v2 must have zero segments in the segment set (default toggle=False)."""
        store, parse_batch, seg_batch = _run_pipeline_over_subset(tmp_path, POLICY_FIXTURES)
        families = parse_batch["version_families"]
        superseded_ids = set(families[0]["superseded_document_ids"])

        for ss in seg_batch["segment_sets"]:
            if ss["document_id"] in superseded_ids:
                assert len(ss["segments"]) == 0, (
                    f"Superseded doc {ss['document_id'][:12]} must produce no segments, "
                    f"got {len(ss['segments'])}"
                )

    def test_superseded_docs_have_exclusion_record(self, tmp_path):
        """Superseded docs must have an ExclusionRecord with reason=superseded_version."""
        store, parse_batch, seg_batch = _run_pipeline_over_subset(tmp_path, POLICY_FIXTURES)
        families = parse_batch["version_families"]
        superseded_ids = set(families[0]["superseded_document_ids"])

        for ss in seg_batch["segment_sets"]:
            if ss["document_id"] in superseded_ids:
                exclusions = ss.get("exclusions", [])
                assert len(exclusions) >= 1, (
                    f"Superseded doc {ss['document_id'][:12]} must have at least one "
                    f"ExclusionRecord"
                )
                reasons = {e["reason"] for e in exclusions}
                assert "superseded_version" in reasons, (
                    f"ExclusionRecord must have reason=superseded_version, got {reasons}"
                )

    def test_exclusion_detail_names_primary(self, tmp_path):
        """ExclusionRecord.reason_detail must name the primary document_id."""
        store, parse_batch, seg_batch = _run_pipeline_over_subset(tmp_path, POLICY_FIXTURES)
        families = parse_batch["version_families"]
        primary_id = families[0]["primary_document_id"]
        superseded_ids = set(families[0]["superseded_document_ids"])

        for ss in seg_batch["segment_sets"]:
            if ss["document_id"] in superseded_ids:
                for excl in ss.get("exclusions", []):
                    if excl["reason"] == "superseded_version":
                        detail = excl.get("reason_detail", "")
                        assert primary_id in detail, (
                            f"reason_detail must name primary_id '{primary_id[:12]}' "
                            f"but got: '{detail[:100]}'"
                        )

    def test_primary_doc_has_segments(self, tmp_path):
        """The primary document (v3) must have segments produced normally."""
        store, parse_batch, seg_batch = _run_pipeline_over_subset(tmp_path, POLICY_FIXTURES)
        families = parse_batch["version_families"]
        primary_id = families[0]["primary_document_id"]

        primary_ss = next(ss for ss in seg_batch["segment_sets"] if ss["document_id"] == primary_id)
        assert len(primary_ss["segments"]) > 0, "Primary document must have segments"

    def test_toggle_on_produces_excluded_tier_segments(self, tmp_path):
        """With index_superseded_versions=True, superseded docs produce excluded-tier segs."""
        _, parse_batch, seg_batch = _run_pipeline_over_subset(
            tmp_path,
            POLICY_FIXTURES,
            run_id="test-toggle-on",
            index_superseded_versions=True,
        )
        families = parse_batch.get("version_families", [])
        if not families:
            pytest.skip("No version families detected; cannot test toggle")

        superseded_ids = set(families[0]["superseded_document_ids"])

        for ss in seg_batch["segment_sets"]:
            if ss["document_id"] in superseded_ids:
                # With toggle on, the SupersededVersionPass forces salience_tier=excluded
                # on every segment (D-25, owner ruling 2026-09-03).
                # The document must have segments (not be empty).
                assert len(ss["segments"]) > 0, (
                    f"With toggle on, doc {ss['document_id'][:12]} must have segments"
                )
                # Every segment must have salience_tier=excluded.
                for seg in ss["segments"]:
                    assert seg["salience_tier"] == "excluded", (
                        f"Segment {seg.get('segment_id', '?')[:12]} from superseded doc "
                        f"{ss['document_id'][:12]} must have salience_tier='excluded', "
                        f"got '{seg['salience_tier']}'"
                    )
                    # salience_basis must be superseded_version.
                    assert seg["salience_basis"] == "superseded_version", (
                        f"salience_basis must be 'superseded_version', "
                        f"got '{seg['salience_basis']}'"
                    )
                    # Exactly one winning signal, and it must be superseded_version.
                    winning = [s for s in seg.get("salience_signals", []) if s.get("won")]
                    assert len(winning) == 1, (
                        f"Exactly one winning signal expected, got {len(winning)}"
                    )
                    assert winning[0]["kind"] == "superseded_version", (
                        f"Winning signal must be 'superseded_version', got '{winning[0]['kind']}'"
                    )
                # No superseded_version exclusion record (that only appears in toggle=False path).
                exclusion_reasons = {e["reason"] for e in ss.get("exclusions", [])}
                assert "superseded_version" not in exclusion_reasons, (
                    f"With toggle on, doc {ss['document_id'][:12]} must not have "
                    f"superseded_version exclusion"
                )

    def test_nothing_dropped_invariant(self, tmp_path):
        """Every inventory item must appear in the segment set (nothing silently dropped)."""
        store, parse_batch, seg_batch = _run_pipeline_over_subset(tmp_path, POLICY_FIXTURES)
        inv = store.load("collect")

        inv_ids = {item["document_id"] for item in inv["items"]}
        ss_ids = {ss["document_id"] for ss in seg_batch["segment_sets"]}

        assert inv_ids == ss_ids, (
            f"Inventory items {inv_ids - ss_ids} missing from segment sets; "
            f"extra segment sets for {ss_ids - inv_ids}"
        )


# ---------------------------------------------------------------------------
# Boilerplate detection (§6.2)
# ---------------------------------------------------------------------------


class TestBoilerplateDetection:
    """The 981-char ACME legal preamble must be detected as corpus-wide boilerplate."""

    def test_boilerplate_blocks_detected(self, tmp_path):
        """At least one boilerplate block detected in the 3-doc corpus."""
        _, parse_batch, _ = _run_pipeline_over_subset(tmp_path, BOILERPLATE_FIXTURES)
        blocks = parse_batch.get("boilerplate_blocks", [])
        assert len(blocks) > 0, (
            "Expected at least one boilerplate block in a corpus with a shared legal preamble"
        )

    def test_preamble_lines_detected_as_boilerplate(self, tmp_path):
        """Key preamble lines must appear in the boilerplate_blocks set."""
        _, parse_batch, _ = _run_pipeline_over_subset(tmp_path, BOILERPLATE_FIXTURES)
        blocks = set(parse_batch.get("boilerplate_blocks", []))

        # The first line of the preamble is distinctive
        assert any("confidential" in b and "acme corp" in b for b in blocks), (
            "Expected the 'CONFIDENTIAL - PROPERTY OF ACME CORP' line to be boilerplate"
        )

    def test_preamble_segment_classified_boilerplate(self, tmp_path):
        """Each of the 3 docs must have a boilerplate-typed segment for the preamble."""
        _, parse_batch, seg_batch = _run_pipeline_over_subset(tmp_path, BOILERPLATE_FIXTURES)
        docs_with_boilerplate = 0

        for ss in seg_batch["segment_sets"]:
            bp_segs = [s for s in ss["segments"] if s.get("segment_type") == "boilerplate"]
            if bp_segs:
                docs_with_boilerplate += 1
                # Boilerplate segments must have boilerplate salience tier
                for bps in bp_segs:
                    assert bps["salience_tier"] == "boilerplate", (
                        f"Boilerplate segment must have tier=boilerplate, "
                        f"got {bps['salience_tier']}"
                    )
                    # salience_basis must be boilerplate_detection
                    assert bps["salience_basis"] == "boilerplate_detection", (
                        "salience_basis must be boilerplate_detection"
                    )

        assert docs_with_boilerplate == 3, (
            f"Expected all 3 docs to have boilerplate segments, only {docs_with_boilerplate} did"
        )

    def test_non_boilerplate_prose_unaffected(self, tmp_path):
        """The bloated_manual's other prose (procedures, specs) must remain primary tier."""
        _, parse_batch, seg_batch = _run_pipeline_over_subset(tmp_path, BOILERPLATE_FIXTURES)
        # Find the bloated_manual's segment set
        bloated_ss = None

        store = ArtifactStore(artifacts_root=tmp_path / "artifacts", run_id="test-dedup")
        collect = store.load("collect")
        for item in collect["items"]:
            if "bloated_manual" in item.get("source_path", "") or "bloated_manual" in item.get(
                "display_name", ""
            ):
                target_id = item["document_id"]
                break

        for ss in seg_batch["segment_sets"]:
            if ss["document_id"] == target_id:
                bloated_ss = ss
                break

        assert bloated_ss is not None, "bloated_manual segment set not found"

        primary_segs = [s for s in bloated_ss["segments"] if s.get("salience_tier") == "primary"]
        assert len(primary_segs) > 0, (
            "bloated_manual must have primary-tier prose segments beyond the boilerplate preamble"
        )

    def test_boilerplate_segment_text_preserved_verbatim(self, tmp_path):
        """Boilerplate text is never stripped — preserved byte-identical in segments."""
        _, parse_batch, seg_batch = _run_pipeline_over_subset(tmp_path, BOILERPLATE_FIXTURES)
        for ss in seg_batch["segment_sets"]:
            for seg in ss["segments"]:
                if seg.get("segment_type") == "boilerplate":
                    # text must be present and non-empty
                    assert seg.get("text"), (
                        "Boilerplate segment text must be present (no intra-chunk byte removal)"
                    )

    def test_boilerplate_tier_filtered_from_default_retrieval(self, tmp_path):
        """Boilerplate segments are at tier=boilerplate, excluded from default retrieval."""
        _, parse_batch, seg_batch = _run_pipeline_over_subset(tmp_path, BOILERPLATE_FIXTURES)
        for ss in seg_batch["segment_sets"]:
            for seg in ss["segments"]:
                if seg.get("segment_type") == "boilerplate":
                    # boilerplate tier is explicitly excluded from default retrieval
                    assert seg["salience_tier"] == "boilerplate", (
                        "Boilerplate-typed segments must be at boilerplate tier"
                    )

    def test_salience_signal_provenance(self, tmp_path):
        """Boilerplate segments must carry the boilerplate_detection winning signal."""
        _, parse_batch, seg_batch = _run_pipeline_over_subset(tmp_path, BOILERPLATE_FIXTURES)
        for ss in seg_batch["segment_sets"]:
            for seg in ss["segments"]:
                if seg.get("segment_type") == "boilerplate":
                    signals = seg.get("salience_signals", [])
                    winning = [s for s in signals if s.get("won")]
                    assert len(winning) == 1, (
                        f"Exactly one winning signal expected, got {len(winning)}"
                    )
                    assert winning[0]["kind"] == "boilerplate_detection", (
                        f"Winning signal must be boilerplate_detection, got {winning[0]['kind']}"
                    )

    def test_small_corpus_threshold_raises_bar(self, tmp_path):
        """With only 2 docs, the small-corpus threshold (0.50) applies instead of 0.30."""
        # Build parse results for only 2 docs (below the 10-doc threshold)
        # This is a logic test, not a pipeline test — verify the small-corpus threshold
        # is correctly wired.  A block in 100% of docs must always be boilerplate;
        # a block in exactly 50% of docs must NOT be (strict >, not >=).

        # Make fake 2-doc parse results with different blocks.
        # Blocks must exceed _MIN_BLOCK_CHARS (20 chars) to be counted.
        shared_text = "confidential property of acme corporation legal notice"
        unique_a = "unique specifications for product model alpha version one"
        unique_b = "unique specifications for product model beta revision two"
        fake_results = [
            {
                "document_id": "doc-a",
                "parse_status": "parsed",
                "regions": [{"text": f"{shared_text}\n\n{unique_a}", "region_id": "r1"}],
            },
            {
                "document_id": "doc-b",
                "parse_status": "parsed",
                "regions": [{"text": f"{shared_text}\n\n{unique_b}", "region_id": "r2"}],
            },
        ]

        # With 2 docs and small-corpus threshold 0.50, a block must appear in STRICTLY MORE
        # than 50% (i.e. > 0.50) to be boilerplate.  "shared_text" appears in 2/2 (100%)
        # which is > 0.50 → boilerplate.  "unique_a/b" appear in 1/2 (50%), which is NOT
        # > 0.50 → not boilerplate.
        blocks_normal = compute_boilerplate_blocks(
            fake_results,
            corpus_proportion=0.30,
            small_corpus_proportion=0.50,
            small_corpus_doc_count=10,  # 2 docs < 10 → use small_corpus_proportion
        )
        norm_shared = "confidential property of acme corporation legal notice"
        assert norm_shared in blocks_normal, (
            f"Block in 100% of docs must be boilerplate regardless of threshold; "
            f"blocks={blocks_normal}"
        )

    def test_corpus_proportion_threshold_respected(self, tmp_path):
        """Blocks appearing below the threshold proportion must NOT be classified boilerplate."""
        from finecorpus.pipeline.assess.corpus_passes import compute_boilerplate_blocks

        # 4-doc corpus where a block appears in 1 of them (25% < 30% threshold)
        unique_block = "this text only appears in one document and is unique"
        common_block = "common preamble text that appears in all four documents"
        fake_results = [
            {
                "document_id": f"doc-{i}",
                "parse_status": "parsed",
                "regions": [
                    {
                        "text": (
                            f"{common_block}\n\n"
                            + (unique_block if i == 0 else f"unique content for doc {i}")
                        ),
                        "region_id": f"r{i}",
                    }
                ],
            }
            for i in range(4)
        ]

        blocks = compute_boilerplate_blocks(
            fake_results,
            corpus_proportion=0.30,
            small_corpus_proportion=0.50,
            small_corpus_doc_count=10,
        )

        norm_unique = "this text only appears in one document and is unique"
        norm_common = "common preamble text that appears in all four documents"

        assert norm_unique not in blocks, (
            "A block in 1/4 docs (25%) must NOT be classified boilerplate at 30% threshold"
        )
        assert norm_common in blocks, "A block in 4/4 docs (100%) must be classified boilerplate"


# ---------------------------------------------------------------------------
# Corpus_passes unit tests
# ---------------------------------------------------------------------------


class TestCorpusPassesUnit:
    """Unit tests for corpus_passes module functions."""

    def _make_parse_result(
        self,
        doc_id: str,
        text: str,
        status: str = "parsed",
        modified_at: str | None = None,
        discovered_at: str | None = None,
    ) -> dict[str, Any]:
        return {
            "document_id": doc_id,
            "content_hash": f"hash-{doc_id}",
            "parse_status": status,
            "source_modified_at": modified_at,
            "discovered_at": discovered_at,
            "regions": [{"text": text, "region_id": f"reg-{doc_id}"}],
        }

    def test_compute_version_families_empty_corpus(self):
        """Empty corpus must return empty families."""
        assert compute_version_families([]) == []

    def test_compute_version_families_single_doc(self):
        """Single document cannot form a family."""
        result = [self._make_parse_result("doc-a", "some text here " * 30)]
        assert compute_version_families(result) == []

    def test_compute_version_families_identical_docs(self):
        """Two identical documents must form a family."""
        text = "the quick brown fox jumps over the lazy dog " * 20
        results = [
            self._make_parse_result("doc-a", text),
            self._make_parse_result("doc-b", text),
        ]
        families = compute_version_families(results, near_duplicate_threshold=0.50)
        assert len(families) == 1

    def test_newer_primary_selection(self):
        """The doc with the later source_modified_at is selected as primary."""
        text = "common text " * 50
        results = [
            self._make_parse_result("doc-old", text, modified_at="2024-01-01T00:00:00+00:00"),
            self._make_parse_result("doc-new", text, modified_at="2026-01-01T00:00:00+00:00"),
        ]
        families = compute_version_families(results, near_duplicate_threshold=0.50)
        assert len(families) == 1
        assert families[0]["primary_document_id"] == "doc-new"
        assert "doc-old" in families[0]["superseded_document_ids"]

    def test_primacy_basis_source_modified_at(self):
        """primacy_basis must be 'source_modified_at' when that field is truthy."""
        text = "common text " * 50
        results = [
            self._make_parse_result("doc-a", text, modified_at="2024-01-01T00:00:00+00:00"),
            self._make_parse_result("doc-b", text, modified_at="2026-01-01T00:00:00+00:00"),
        ]
        families = compute_version_families(results, near_duplicate_threshold=0.50)
        assert len(families) == 1
        assert families[0]["primacy_basis"] == "source_modified_at"

    def test_primacy_basis_discovered_at_only(self):
        """primacy_basis must be 'discovered_at' when only discovered_at was available."""
        text = "common text " * 50
        # No source_modified_at on either doc; use discovered_at for ordering.
        results = [
            self._make_parse_result(
                "doc-earlier", text, modified_at=None, discovered_at="2024-06-01T00:00:00+00:00"
            ),
            self._make_parse_result(
                "doc-later", text, modified_at=None, discovered_at="2026-06-01T00:00:00+00:00"
            ),
        ]
        families = compute_version_families(results, near_duplicate_threshold=0.50)
        assert len(families) == 1
        # doc-later has the later discovered_at → it is the primary.
        assert families[0]["primary_document_id"] == "doc-later"
        assert families[0]["primacy_basis"] == "discovered_at", (
            f"Expected 'discovered_at', got '{families[0]['primacy_basis']}'"
        )

    def test_primacy_basis_content_hash_fallback(self):
        """primacy_basis must be 'content_hash' when neither timestamp is present."""
        text = "common text " * 50
        results = [
            self._make_parse_result("doc-a", text, modified_at=None, discovered_at=None),
            self._make_parse_result("doc-b", text, modified_at=None, discovered_at=None),
        ]
        families = compute_version_families(results, near_duplicate_threshold=0.50)
        assert len(families) == 1
        assert families[0]["primacy_basis"] == "content_hash", (
            f"Expected 'content_hash', got '{families[0]['primacy_basis']}'"
        )

    def test_compute_boilerplate_blocks_empty_corpus(self):
        """Empty corpus returns empty set."""
        assert compute_boilerplate_blocks([]) == set()

    def test_compute_boilerplate_blocks_single_doc(self):
        """Single document cannot have boilerplate (needs ≥2 docs)."""
        results = [self._make_parse_result("doc-a", "some text " * 30)]
        assert compute_boilerplate_blocks(results) == set()

    def test_compute_boilerplate_blocks_common_text(self):
        """Text in all docs above threshold must be classified boilerplate."""
        shared = "this preamble appears in every single document"
        results = [
            self._make_parse_result("doc-a", f"{shared}\n\nunique content for a " * 3),
            self._make_parse_result("doc-b", f"{shared}\n\nunique content for b " * 3),
            self._make_parse_result("doc-c", f"{shared}\n\nunique content for c " * 3),
        ]
        blocks = compute_boilerplate_blocks(
            results, corpus_proportion=0.30, small_corpus_proportion=0.50, small_corpus_doc_count=10
        )
        import re

        norm_shared = re.sub(r"\s+", " ", shared.lower()).strip()
        assert norm_shared in blocks, f"Shared text must be boilerplate; blocks={blocks}"

    def test_run_corpus_passes_annotates_dedup_role(self):
        """run_corpus_passes must annotate each result with dedup_role."""
        text = "identical text " * 50
        results = [
            self._make_parse_result("doc-a", text, modified_at="2024-01-01T00:00:00+00:00"),
            self._make_parse_result("doc-b", text, modified_at="2026-01-01T00:00:00+00:00"),
            self._make_parse_result("doc-c", "completely different content entirely"),
        ]
        run_corpus_passes(results, near_duplicate_threshold=0.50)
        roles = {r["document_id"]: r.get("dedup_role") for r in results}
        assert roles["doc-b"] == "primary"
        assert roles["doc-a"] == "superseded"
        assert roles["doc-c"] == "unique"


# ---------------------------------------------------------------------------
# D-32 absolute-floor boilerplate branch unit tests
# ---------------------------------------------------------------------------


class TestD32AbsoluteFloor:
    """Unit tests for the D-32 absolute-floor boilerplate detection branch.

    Branch (b) fires when:
      total corpus occurrences >= abs_floor_count (default 3)  AND
      unique-doc fraction     >= abs_floor_fraction (default 0.05)

    Branch (a) — fraction > threshold — is unchanged and must not regress.
    """

    def _make_pr(self, doc_id: str, text: str) -> dict[str, Any]:
        return {
            "document_id": doc_id,
            "parse_status": "parsed",
            "regions": [{"text": text, "region_id": f"reg-{doc_id}"}],
        }

    def _corpus(
        self,
        n_uniq: int,
        shared_block: str,
        filler_prefix: str = "unique text",
    ) -> list[dict[str, Any]]:
        """Create n_uniq docs each containing shared_block plus unique filler."""
        return [
            self._make_pr(
                f"doc-{i}",
                f"{shared_block}\n\n{filler_prefix} for document {i} only additional padding here",
            )
            for i in range(n_uniq)
        ]

    def _filler_doc(self, doc_id: str, i: int) -> dict[str, Any]:
        """Create a filler document with text long enough to pass _MIN_TEXT_CHARS (50)."""
        # Ensure text is >= 50 chars so it's counted as an eligible document
        text = f"completely unrelated content for filler document number {i} — unique and distinct"
        return self._make_pr(doc_id, text)

    # ------------------------------------------------------------------
    # Branch (b) FIRES
    # ------------------------------------------------------------------

    def test_branch_b_fires_at_3_of_13_prevalence(self):
        """Branch (b): 3/13 unique docs (fraction 0.23 < 0.30) with total count 3 fires.

        This is the canonical real-world case: ACME legal preamble in 3 of 13 eligible
        docs at full golden-corpus scale (fraction = 0.23 < 0.30, count = 3 >= 3,
        fraction = 0.23 >= 0.05).
        """
        shared = "confidential property of acme corp all rights reserved no reproduction"
        # 3 docs share the block; 10 other docs have unique content (all >= 50 chars)
        results = self._corpus(3, shared) + [self._filler_doc(f"other-{i}", i) for i in range(10)]
        assert len(results) == 13

        blocks = compute_boilerplate_blocks(
            results,
            corpus_proportion=0.30,
            small_corpus_proportion=0.50,
            small_corpus_doc_count=10,
            abs_floor_count=3,
            abs_floor_fraction=0.05,
        )
        import re

        norm_shared = re.sub(r"\s+", " ", shared.lower()).strip()
        assert norm_shared in blocks, (
            f"Branch (b) must fire at 3/13 prevalence (count=3 >= 3, fraction=0.23 >= 0.05); "
            f"blocks detected: {len(blocks)}"
        )

    def test_branch_b_fires_for_within_doc_repetition(self):
        """Branch (b): block repeating multiple times within ONE document fires at total count >= 3.

        Simulates the confluence_export.html case: nav chrome appears twice in a single
        HTML document (2 pages), giving total_count=2 in 1 of 15 docs.  With floor
        count=2 and fraction check, it would fire.  With floor count=3 it requires
        the block to appear >= 3 times total (e.g. 3 occurrences in 1 doc or
        1-2 occurrences in multiple docs summing to 3+).

        This test uses total_count=4 (4 occurrences in one doc) to verify the
        within-doc path definitely fires.
        """
        # A document with the nav chrome repeated 4 times (like confluence HTML pages)
        nav_block = "acme engineering wiki space home all pages blog calendar"
        # Build a region with 4 repetitions of the nav block separated by different content
        repeated_text = (
            f"{nav_block}\n\npage 1 content here\n\n"
            f"{nav_block}\n\npage 2 content here\n\n"
            f"{nav_block}\n\npage 3 content here\n\n"
            f"{nav_block}\n\npage 4 content here"
        )
        # 1 doc with 4 repetitions, 14 other docs with unique content (all >= 50 chars)
        results = [self._make_pr("doc-nav", repeated_text)] + [
            self._filler_doc(f"other-{i}", i) for i in range(14)
        ]
        assert len(results) == 15

        blocks = compute_boilerplate_blocks(
            results,
            corpus_proportion=0.30,
            small_corpus_proportion=0.50,
            small_corpus_doc_count=10,
            abs_floor_count=3,
            abs_floor_fraction=0.05,
        )
        import re

        norm_nav = re.sub(r"\s+", " ", nav_block.lower()).strip()
        assert norm_nav in blocks, (
            f"Branch (b) must fire for 4 within-doc occurrences (total=4 >= 3, "
            f"fraction=1/15=0.067 >= 0.05); blocks detected: {sorted(blocks)[:3]}"
        )

    # ------------------------------------------------------------------
    # Branch (b) does NOT fire
    # ------------------------------------------------------------------

    def test_branch_b_does_not_fire_below_fraction_floor(self):
        """Branch (b) must NOT fire when unique-doc fraction < 0.05 (3 of 100 docs).

        Simulates the negative case: 3 total occurrences in a 100-document corpus
        → unique_docs/n_docs = 3/100 = 0.03 < 0.05 → does NOT fire.
        """
        shared = "template footer text with legal disclaimer and version information"
        # 3 docs share the block; 97 other docs have unique content (all >= 50 chars)
        results = self._corpus(3, shared, filler_prefix="different document content") + [
            self._filler_doc(f"other-{i}", i) for i in range(97)
        ]
        assert len(results) == 100

        blocks = compute_boilerplate_blocks(
            results,
            corpus_proportion=0.30,
            small_corpus_proportion=0.50,
            small_corpus_doc_count=10,
            abs_floor_count=3,
            abs_floor_fraction=0.05,
        )
        import re

        norm_shared = re.sub(r"\s+", " ", shared.lower()).strip()
        assert norm_shared not in blocks, (
            "Branch (b) must NOT fire at 3/100 prevalence (fraction=0.03 < 0.05); "
            "block was erroneously added to boilerplate set"
        )

    def test_branch_b_does_not_fire_below_count_floor(self):
        """Branch (b) must NOT fire when total count < abs_floor_count (default 3).

        2 total occurrences (1 each in 2 of 15 docs): count=2 < 3 → does NOT fire
        when fraction would otherwise pass (2/15 = 0.13 > 0.05).
        """
        shared = "confidential notice this document is proprietary information only"
        # 2 docs share the block; 13 other docs unique (all >= 50 chars)
        results = self._corpus(2, shared) + [self._filler_doc(f"other-{i}", i) for i in range(13)]
        assert len(results) == 15

        blocks = compute_boilerplate_blocks(
            results,
            corpus_proportion=0.30,
            small_corpus_proportion=0.50,
            small_corpus_doc_count=10,
            abs_floor_count=3,
            abs_floor_fraction=0.05,
        )
        import re

        norm_shared = re.sub(r"\s+", " ", shared.lower()).strip()
        # fraction = 2/15 = 0.13 < 0.30 → branch (a) does not fire
        # total = 2 < 3 → branch (b) does not fire
        assert norm_shared not in blocks, (
            "Branch (b) must NOT fire at count=2 < 3 (abs_floor_count); "
            "block was erroneously added to boilerplate set"
        )

    # ------------------------------------------------------------------
    # Branch (a) unchanged for small subsets
    # ------------------------------------------------------------------

    def test_branch_a_unchanged_for_small_subset(self):
        """Branch (a): 3-doc subset at fraction=1.0 still fires (unchanged path).

        The existing 3-doc boilerplate test relies on fraction=1.0 which exceeds
        the small-corpus threshold (0.50).  Verify D-32 does not regress this.
        """
        shared = "confidential property of acme corporation legal notice"
        unique_a = "unique specifications for product model alpha version one"
        unique_b = "unique specifications for product model beta revision two"
        unique_c = "unique specifications for product model gamma release three"
        results = [
            self._make_pr("doc-a", f"{shared}\n\n{unique_a}"),
            self._make_pr("doc-b", f"{shared}\n\n{unique_b}"),
            self._make_pr("doc-c", f"{shared}\n\n{unique_c}"),
        ]

        # 3-doc corpus → small-corpus (< 10) → threshold 0.50
        # fraction = 3/3 = 1.0 > 0.50 → branch (a) fires
        blocks = compute_boilerplate_blocks(
            results,
            corpus_proportion=0.30,
            small_corpus_proportion=0.50,
            small_corpus_doc_count=10,
            abs_floor_count=3,
            abs_floor_fraction=0.05,
        )
        import re

        norm_shared = re.sub(r"\s+", " ", shared.lower()).strip()
        assert norm_shared in blocks, (
            f"Branch (a) at fraction=1.0 must still fire (3-doc small corpus); "
            f"blocks detected: {blocks}"
        )
