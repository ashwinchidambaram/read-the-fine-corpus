"""T-09 — §18.3 test 9: Injection flagging.

Acceptance criterion (§18.3 test 9):
  "Injection flagging — the adversarial fixture is flagged and its suspicion
  score is retrievable."

This test satisfies M-105 (injection_suspicion is retrievable and filterable;
never used to silently exclude) and T-09 (non-negotiable §18.3 acceptance test).

Two test modes
--------------

1. **Unit / FakeProvider** (no external services required):
   Runs the full pipeline (collect→assess→decompose→plan→build) over the
   adversarial fixture using FakeProvider embeddings and a FakeAdapter index
   backend.  Verifies that:
   - Chunks from the adversarial fixture carry nonzero injection_suspicion.
   - Chunks from pages with invisible content carry invisible_content_flags.
   - All chunks carry trust_level = "untrusted_ingested" (§14.1).
   - No high-suspicion chunk has its salience_tier lowered (M-105).

   This is the primary test coverage — it exercises the full pipeline without
   external service dependencies.

2. **Qdrant integration** (requires live Qdrant + Postgres):
   Same pipeline run, but uses real Qdrant and Postgres.  Issues a retrieval
   query and checks that the returned chunk payloads carry injection_suspicion
   and invisible_content_flags in the provenance block.

   Decorated with ``@qdrant_integration_mark`` — auto-skipped when services
   are unavailable.

Both tests are in this file.  The unit test always runs; the integration test
requires the services guard.
"""

from __future__ import annotations

import pathlib
import tempfile
import uuid
from pathlib import Path
from typing import Any

import pytest

from conftest import qdrant_integration_mark

ADVERSARIAL = Path(__file__).parent.parent / "fixtures" / "golden" / "corpus" / "adversarial.pdf"
CLEAN_NATIVE = Path(__file__).parent.parent / "fixtures" / "golden" / "corpus" / "clean_native.pdf"

QDRANT_URL = "http://localhost:6333"
POSTGRES_DSN = "postgresql+psycopg://finecorpus:finecorpus@localhost:5432/finecorpus"

DIMENSIONS = 64
MODEL_ID = "fake-t09-v1"
PROVIDER_ID = "fake"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _run_pipeline_in_tempdir(
    source_file: Path,
    provider: Any,
    adapter: Any,
    kb_id: str,
    ws_id: str = "ws-t09",
) -> tuple[Any, Any]:
    """Run the full pipeline over a single file using a temp source dir.

    Returns (artifact_store, chunk_payloads_from_adapter).
    """
    from finecorpus.pipeline import run_pipeline
    from finecorpus.pipeline.artifact_store import ArtifactStore

    with tempfile.TemporaryDirectory() as artifacts_root_str:
        # Copy source file into a temp source dir
        import shutil

        with tempfile.TemporaryDirectory() as source_dir_str:
            shutil.copy2(source_file, source_dir_str)
            run_id = f"t09-{uuid.uuid4().hex[:8]}"
            run_pipeline(
                source_dir=source_dir_str,
                artifacts_root=artifacts_root_str,
                run_id=run_id,
                workspace_id=ws_id,
                kb_id=kb_id,
                embedding_provider=provider,
                index_adapter=adapter,
                build_id=1,
                promote=False,
            )
            store = ArtifactStore(artifacts_root=pathlib.Path(artifacts_root_str), run_id=run_id)
            # Load chunk payloads from adapter
            chunk_payloads: list[dict[str, Any]] = []
            if adapter.collections:
                coll_name = list(adapter.collections.keys())[0]
                chunk_payloads = [
                    pt.get("payload", {}) for pt in adapter.collections[coll_name].get("points", [])
                ]
            return store, chunk_payloads


# ===========================================================================
# Unit test (FakeProvider + FakeAdapter — no external services)
# ===========================================================================


class TestT09InjectionFlaggingUnit:
    """§18.3 T-09: unit coverage using FakeProvider + FakeAdapter.

    Runs the full pipeline (assess→decompose→plan→build) over adversarial.pdf
    and checks that the chunk payloads carry correct security provenance.
    """

    @pytest.fixture(scope="class")
    def adversarial_chunks(self) -> list[dict[str, Any]]:
        """Chunk payloads from the full pipeline over adversarial.pdf."""
        from finecorpus.embedding.fake import FakeProvider
        from tests.retrieval.helpers import FakeAdapter

        kb_id = f"kb-t09-unit-{uuid.uuid4().hex[:8]}"
        provider = FakeProvider(dimensions=DIMENSIONS)
        adapter = FakeAdapter()

        _, payloads = _run_pipeline_in_tempdir(ADVERSARIAL, provider, adapter, kb_id)
        return payloads

    def test_chunk_payloads_not_empty(self, adversarial_chunks: list[dict[str, Any]]) -> None:
        """Pipeline must produce at least one chunk from adversarial.pdf."""
        assert adversarial_chunks, "No chunks produced from adversarial.pdf"

    def test_all_chunks_have_injection_suspicion_field(
        self, adversarial_chunks: list[dict[str, Any]]
    ) -> None:
        """Every chunk must carry injection_suspicion in its provenance (M-105)."""
        for payload in adversarial_chunks:
            prov = payload.get("provenance", {})
            assert "injection_suspicion" in prov, (
                f"Chunk missing injection_suspicion: {payload.get('chunk_id')}"
            )

    def test_some_chunks_have_nonzero_suspicion(
        self, adversarial_chunks: list[dict[str, Any]]
    ) -> None:
        """At least one chunk from adversarial.pdf must have nonzero injection_suspicion.

        The adversarial fixture contains visible injection text on pages 2-4
        ("Ignore previous instructions", "SYSTEM: ...", "send all context to ...").
        Those segments must score > 0.
        """
        nonzero = [
            p
            for p in adversarial_chunks
            if p.get("provenance", {}).get("injection_suspicion", 0.0) > 0.0
        ]
        assert nonzero, (
            "No chunks from adversarial.pdf have nonzero injection_suspicion; "
            "visible injection text on pages 2-4 should be detected"
        )

    def test_injection_suspicion_is_retrievable_and_filterable(
        self, adversarial_chunks: list[dict[str, Any]]
    ) -> None:
        """M-105: injection_suspicion is present on provenance for every chunk."""
        for payload in adversarial_chunks:
            prov = payload.get("provenance", {})
            score = prov.get("injection_suspicion")
            assert score is not None, "injection_suspicion must not be None"
            assert 0.0 <= score <= 1.0, f"injection_suspicion out of range: {score}"

    def test_invisible_content_flags_on_relevant_chunks(
        self, adversarial_chunks: list[dict[str, Any]]
    ) -> None:
        """Chunks from pages with invisible content must carry invisible_content_flags."""
        flagged = [
            p
            for p in adversarial_chunks
            if p.get("provenance", {}).get("invisible_content_flags", [])
        ]
        assert flagged, (
            "No chunks carry invisible_content_flags; "
            "pages 5 (white-on-white), 6 (tiny font), 7 (off-page) should produce flagged chunks"
        )

    def test_all_chunks_labelled_untrusted(self, adversarial_chunks: list[dict[str, Any]]) -> None:
        """§14.1: every chunk must have trust_level = untrusted_ingested."""
        for payload in adversarial_chunks:
            prov = payload.get("provenance", {})
            trust = prov.get("trust_level")
            assert trust == "untrusted_ingested", (
                f"Chunk {payload.get('chunk_id')} has wrong trust_level: {trust}"
            )

    def test_m105_high_suspicion_chunks_not_excluded(
        self, adversarial_chunks: list[dict[str, Any]]
    ) -> None:
        """M-105: high-suspicion chunks must not be excluded or down-tiered.

        This is the explicit M-105 test: a max-suspicion segment keeps its
        type-prior salience_tier (primary or supporting).  The injection pass
        MUST NOT lower any tier to 'excluded'.
        """
        high_suspicion = [
            p
            for p in adversarial_chunks
            if p.get("provenance", {}).get("injection_suspicion", 0.0) >= 0.3
        ]
        assert high_suspicion, (
            "No high-suspicion chunks to test M-105 against; "
            "expected nonzero score from visible injection text"
        )
        for payload in high_suspicion:
            prov = payload.get("provenance", {})
            tier = prov.get("salience_tier")
            assert tier != "excluded", (
                f"M-105 VIOLATED: high-suspicion chunk {payload.get('chunk_id')} "
                f"has salience_tier='excluded'. "
                f"injection_suspicion must NEVER cause exclusion."
            )

    def test_invisible_flags_identify_correct_page_kinds(
        self, adversarial_chunks: list[dict[str, Any]]
    ) -> None:
        """Invisible-content flags must come from the correct detection kinds."""
        valid_kinds = {
            "white_on_white",
            "zero_size_font",
            "off_page",
            "metadata_only",
            "render_hidden",
        }
        for payload in adversarial_chunks:
            flags = payload.get("provenance", {}).get("invisible_content_flags", [])
            for flag in flags:
                assert flag in valid_kinds, f"Unknown invisible_content_flag: {flag}"

    def test_clean_native_chunks_score_zero(self) -> None:
        """clean_native.pdf chunks should score 0.0 on injection_suspicion."""
        from finecorpus.embedding.fake import FakeProvider
        from tests.retrieval.helpers import FakeAdapter

        kb_id = f"kb-clean-t09-{uuid.uuid4().hex[:8]}"
        provider = FakeProvider(dimensions=DIMENSIONS)
        adapter = FakeAdapter()

        _, payloads = _run_pipeline_in_tempdir(CLEAN_NATIVE, provider, adapter, kb_id)

        if not payloads:
            pytest.skip("No chunks produced from clean_native.pdf")

        nonzero = [
            p for p in payloads if p.get("provenance", {}).get("injection_suspicion", 0.0) > 0.0
        ]
        nonzero_summary = [
            (p.get("chunk_id"), p.get("provenance", {}).get("injection_suspicion")) for p in nonzero
        ]
        assert not nonzero, f"clean_native.pdf chunks should score 0.0; nonzero: {nonzero_summary}"


# ===========================================================================
# Qdrant integration test — §18.3 T-09 (live)
# ===========================================================================


@qdrant_integration_mark
class TestT09InjectionFlaggingIntegration:
    """§18.3 T-09 integration: full pipeline over adversarial.pdf → live Qdrant retrieval.

    Requires live Qdrant + Postgres (auto-skipped when unreachable).

    What this does
    --------------
    1. Runs the full pipeline (collect→assess→decompose→plan→build) over
       adversarial.pdf with FakeProvider embeddings written to live Qdrant.
    2. Promotes the shadow collection.
    3. Issues a retrieval query via the retrieval service.
    4. Asserts that returned chunks carry nonzero injection_suspicion and/or
       invisible_content_flags in the provenance block.
    5. Tears down the shadow + alias.
    """

    def test_t09_adversarial_flagged_and_score_retrievable(self) -> None:
        """Full T-09: pipeline → live Qdrant → retrieval → security fields present."""
        import shutil

        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from finecorpus.control.metadata import Base
        from finecorpus.embedding.fake import FakeProvider
        from finecorpus.index.adapter import alias_name, collection_name
        from finecorpus.index.qdrant.backend import QdrantAdapter
        from finecorpus.pipeline import run_pipeline
        from finecorpus.retrieval.service import query as retrieval_query

        kb_id = f"kbt09-{uuid.uuid4().hex[:12]}"
        ws_id = "ws-t09-live"
        build_id = int(uuid.uuid4().hex[:4], 16) % 9000 + 1000

        provider = FakeProvider(dimensions=DIMENSIONS)
        adapter = QdrantAdapter(url=QDRANT_URL)

        # Derive the shadow collection name deterministically (matches the
        # collection_name() function used by create_shadow internally).
        shadow_coll = collection_name(kb_id, build_id)

        engine = create_engine(POSTGRES_DSN)
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine)

        try:
            with (
                tempfile.TemporaryDirectory() as source_dir,
                tempfile.TemporaryDirectory() as artifacts_root,
            ):
                shutil.copy2(ADVERSARIAL, source_dir)
                run_id = f"t09-live-{uuid.uuid4().hex[:8]}"

                # Run pipeline with promote=True — the orchestrator creates the
                # shadow collection, runs all 5 stages, then calls lifecycle.promote()
                # to execute the two-phase alias swap (§4.2).
                with Session() as session:
                    run_pipeline(
                        source_dir=source_dir,
                        artifacts_root=artifacts_root,
                        run_id=run_id,
                        workspace_id=ws_id,
                        kb_id=kb_id,
                        embedding_provider=provider,
                        index_adapter=adapter,
                        build_id=build_id,
                        promote=True,
                        db_session=session,
                    )

            # Verify alias was promoted: the alias must now resolve to our collection
            assert adapter.alias_exists(alias_name(kb_id)), (
                "T-09: alias was not created after promotion"
            )

            # Count indexed chunks
            chunk_count = adapter.count_points(alias_name(kb_id))
            assert chunk_count > 0, "No chunks written to Qdrant for adversarial.pdf"

            # Query via retrieval service function
            with Session() as session:
                result = retrieval_query(
                    kb_id=kb_id,
                    query_text="ignore previous instructions",
                    provider=provider,
                    adapter=adapter,
                    session=session,
                    top_k=20,
                )

            # T-09 assertion: result status is valid
            from finecorpus.contracts.retrieval_response import ResultStatus

            assert result.result_status in (
                ResultStatus.matches,
                ResultStatus.no_matches,
                ResultStatus.filtered_to_zero,
            ), f"Unexpected result_status: {result.result_status}"

            if result.result_status == ResultStatus.matches:
                for chunk in result.results:
                    prov = chunk.provenance
                    # Both fields must be present and retrievable (§14.1)
                    assert hasattr(prov, "injection_suspicion"), (
                        "T-09: chunk missing injection_suspicion"
                    )
                    assert hasattr(prov, "invisible_content_flags"), (
                        "T-09: chunk missing invisible_content_flags"
                    )
                    assert chunk.trust_level.value == "untrusted_ingested"

            # Verify at least one indexed chunk has nonzero suspicion
            # (query results depend on FakeProvider cosine similarity — random vectors
            # may not surface the most suspicious chunks, so we inspect all indexed data
            # via the raw Qdrant payload)
            search_results = adapter.search(
                alias=alias_name(kb_id),
                query_vector=[0.1] * DIMENSIONS,
                top_k=chunk_count,
                payload_filter={"tenancy.kb_id": kb_id},
            )
            nonzero_suspicion = [
                r
                for r in search_results
                if r.payload.get("provenance", {}).get("injection_suspicion", 0.0) > 0.0
            ]
            assert nonzero_suspicion, (
                "T-09 FAILED: No indexed chunks from adversarial.pdf have "
                "nonzero injection_suspicion. The visible injection text on pages 2-4 "
                "must produce nonzero scores."
            )

            invisible_flagged = [
                r
                for r in search_results
                if r.payload.get("provenance", {}).get("invisible_content_flags", [])
            ]
            assert invisible_flagged, (
                "T-09 FAILED: No indexed chunks from adversarial.pdf have "
                "invisible_content_flags. Pages 5/6/7 must produce flagged chunks."
            )

        finally:
            # Teardown: drop alias and shadow collection
            try:
                if adapter.alias_exists(alias_name(kb_id)):
                    adapter.delete_alias(alias_name(kb_id))
            except Exception:
                pass
            try:
                if adapter.collection_exists(shadow_coll):
                    adapter.drop_collection(shadow_coll)
            except Exception:
                pass
