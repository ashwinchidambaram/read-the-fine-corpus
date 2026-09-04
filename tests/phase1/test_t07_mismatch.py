"""T-07 — Embedding model mismatch fails closed (§18.3 test 7).

Acceptance criterion (§19 Phase 1 / §18.3 test 7):
  "Embedding model mismatch — a query against an index built with a different
  model fails closed rather than returning results."

What this test does
-------------------
Full-stack version of the mismatch check:

1. Ingests and promotes a collection using FakeProvider(dims=384, model='fake-a').
2. Reconfigures the query path to use FakeProvider(dims=768, model='fake-b')
   (different model identity — both dims and model_id differ).
3. Issues a query through ``retrieval.service.query()`` (the REAL service, not
   a unit mock).
4. Asserts:
   - result_status == "error"
   - error.code == EMBEDDING_MODEL_MISMATCH
   - zero results returned
   - provider embed_batch was NEVER called (fail-closed before embedding)

Difference from the unit test in tests/retrieval/test_integration.py
----------------------------------------------------------------------
``tests/retrieval/test_integration.py::TestRoundTrip::test_model_mismatch_fail_closed``
seeds data directly and patches the alias record — it is a unit-level integration
test of the service function logic.

This test goes end-to-end through the REAL alias lifecycle (create_shadow →
promote), so it verifies that the alias record produced by actual promotion
carries the correct model identity fields that the mismatch check depends on.
No mocks, no patched sessions — the full control-plane round-trip.

Marker: ``qdrant_integration`` (composite mark from conftest) — auto-skipped
when Qdrant or Postgres are unreachable.

Run:
    docker compose -f docker-compose.yml -f docker-compose.integration.yml up -d
    uv run pytest tests/phase1/test_t07_mismatch.py -v -m qdrant_integration
    docker compose -f docker-compose.yml -f docker-compose.integration.yml down
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from conftest import qdrant_integration_mark

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

QDRANT_URL = "http://localhost:6333"
POSTGRES_DSN = "postgresql+psycopg://finecorpus:finecorpus@localhost:5432/finecorpus"

# Provider A — used to ingest and promote
DIMS_A = 384
MODEL_ID_A = "fake-a"

# Provider B — wrong model, used at query time to trigger the mismatch
DIMS_B = 768
MODEL_ID_B = "fake-b"

PROVIDER_ID = "fake"
CONFIG_VERSION = "cfgv-t07-mismatch"

CORPUS_TEXTS = [
    "Mismatch test chunk one.",
    "Mismatch test chunk two.",
    "Embedding model identity must be checked before embed is called.",
]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def engine() -> Any:
    from sqlalchemy import create_engine

    from finecorpus.control.metadata import create_tables

    eng = create_engine(POSTGRES_DSN)
    create_tables(eng)
    return eng


@pytest.fixture(scope="module")
def qdrant_adapter() -> Any:
    from finecorpus.index.qdrant.backend import QdrantAdapter

    return QdrantAdapter(url=QDRANT_URL, timeout=10)


@pytest.fixture(scope="function")
def kb_id() -> str:
    """Unique KB ID per test to avoid cross-test pollution."""
    return f"kb-t07-{uuid.uuid4().hex[:8]}"


@pytest.fixture(scope="function")
def seeded_kb_with_model_a(
    kb_id: str,
    engine: Any,
    qdrant_adapter: Any,
) -> str:
    """Build and promote a KB using model 'fake-a'.

    Returns the kb_id.  Tears down Qdrant collection and alias on exit.
    """
    from sqlalchemy.orm import Session

    from finecorpus.control.metadata import AliasRepository
    from finecorpus.embedding.fake import FakeProvider
    from finecorpus.index.adapter import ModelIdentity, alias_name, build_point_payload
    from finecorpus.index.lifecycle import create_shadow, promote

    provider_a = FakeProvider(dimensions=DIMS_A, model_id=MODEL_ID_A)
    workspace_id = "ws-t07"
    identity_a = ModelIdentity(
        provider=PROVIDER_ID,
        model=MODEL_ID_A,
        dimensions=DIMS_A,
        config_version=CONFIG_VERSION,
    )
    alias = alias_name(kb_id)

    # Bootstrap control-plane alias record
    with Session(engine) as session:
        repo = AliasRepository(session)
        if repo.get(alias) is None:
            repo.create(alias=alias, kb_id=kb_id, workspace_id=workspace_id)
            session.commit()

    # Create shadow collection with model A dimensions
    shadow_state = create_shadow(
        adapter=qdrant_adapter,
        kb_id=kb_id,
        workspace_id=workspace_id,
        build_id=1,
        model_identity=identity_a,
    )
    coll_name = shadow_state.shadow_collection

    # Seed chunks
    for i, text in enumerate(CORPUS_TEXTS):
        chunk_id = f"chk_t07_{kb_id}_{i}"
        point_id = str(uuid.uuid4())
        prov = {
            "source_document_id": f"doc-t07-{i}",
            "source_document_version": "v1",
            "source_location": {
                "locator_kind": "char_range",
                "char_start": 0,
                "char_end": len(text),
            },
            "structural_path": [],
            "transformations": [],
            "confidence": 1.0,
            "ocr_confidence": None,
            "segment_type": "prose",
            "salience_tier": "primary",
            "salience_basis": "default",
            "salience_signals": [
                {"kind": "default", "implied_tier": "supporting", "won": True, "detail": "none"}
            ],
            "language": "en",
            "injection_suspicion": 0.0,
            "invisible_content_flags": [],
            "sensitivity_flags": [],
            "trust_level": "untrusted_ingested",
        }
        tenancy = {"kb_id": kb_id, "workspace_id": workspace_id}
        payload = build_point_payload(
            chunk_id=chunk_id,
            provenance=prov,
            tenancy=tenancy,
            text=text,
            embedding_ref={"model_id": MODEL_ID_A, "dimensions": DIMS_A},
        )
        vector = provider_a.embed_batch([text], model_id=MODEL_ID_A).embeddings[0]
        qdrant_adapter.upsert_points(
            collection=coll_name,
            points=[{"id": point_id, "vector": vector, "payload": payload}],
        )

    # Promote
    with Session(engine) as session:
        promote(
            adapter=qdrant_adapter,
            session=session,
            ctx=shadow_state,
        )
        session.commit()

    yield kb_id

    # Teardown
    try:
        if qdrant_adapter.alias_exists(alias):
            qdrant_adapter.delete_alias(alias)
        if qdrant_adapter.collection_exists(coll_name):
            qdrant_adapter.drop_collection(coll_name)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@qdrant_integration_mark
class TestMismatchFailsClosed:
    """T-07: query with wrong model fails closed through the real alias lifecycle."""

    def test_mismatch_returns_error_status(
        self,
        seeded_kb_with_model_a: str,
        engine: Any,
        qdrant_adapter: Any,
    ) -> None:
        """Querying with model B against a collection indexed with model A must fail closed."""
        from sqlalchemy.orm import Session

        from finecorpus.contracts.retrieval_response import ResultStatus
        from finecorpus.embedding.cache import QueryEmbeddingCache
        from finecorpus.embedding.fake import FakeProvider
        from finecorpus.retrieval.service import query

        # Provider B — different model_id AND different dimensions from what was indexed.
        # This is the exact object passed to query() below; no wrapping or patching.
        provider_b = FakeProvider(dimensions=DIMS_B, model_id=MODEL_ID_B)
        cache = QueryEmbeddingCache(enabled=False)

        with Session(engine) as session:
            result = query(
                kb_id=seeded_kb_with_model_a,
                query_text="mismatch test query",
                provider=provider_b,
                adapter=qdrant_adapter,
                session=session,
                cache=cache,
            )

        assert result.result_status == ResultStatus.error, (
            f"Expected result_status=error on model mismatch, got {result.result_status!r}"
        )

    def test_mismatch_error_code_is_embedding_model_mismatch(
        self,
        seeded_kb_with_model_a: str,
        engine: Any,
        qdrant_adapter: Any,
    ) -> None:
        """Error code must be EMBEDDING_MODEL_MISMATCH (§15, provider-abstraction §3.3)."""
        from sqlalchemy.orm import Session

        from finecorpus.contracts.retrieval_response import ErrorCode
        from finecorpus.embedding.cache import QueryEmbeddingCache
        from finecorpus.embedding.fake import FakeProvider
        from finecorpus.retrieval.service import query

        provider_b = FakeProvider(dimensions=DIMS_B, model_id=MODEL_ID_B)
        cache = QueryEmbeddingCache(enabled=False)

        with Session(engine) as session:
            result = query(
                kb_id=seeded_kb_with_model_a,
                query_text="mismatch test query",
                provider=provider_b,
                adapter=qdrant_adapter,
                session=session,
                cache=cache,
            )

        assert result.error is not None, "Error envelope must be present on mismatch"
        assert result.error.code == ErrorCode.EMBEDDING_MODEL_MISMATCH, (
            f"Expected EMBEDDING_MODEL_MISMATCH, got {result.error.code!r}"
        )

    def test_mismatch_returns_zero_results(
        self,
        seeded_kb_with_model_a: str,
        engine: Any,
        qdrant_adapter: Any,
    ) -> None:
        """Zero results must be returned on mismatch — fail-closed means no data leaks."""
        from sqlalchemy.orm import Session

        from finecorpus.embedding.cache import QueryEmbeddingCache
        from finecorpus.embedding.fake import FakeProvider
        from finecorpus.retrieval.service import query

        provider_b = FakeProvider(dimensions=DIMS_B, model_id=MODEL_ID_B)
        cache = QueryEmbeddingCache(enabled=False)

        with Session(engine) as session:
            result = query(
                kb_id=seeded_kb_with_model_a,
                query_text="mismatch test query",
                provider=provider_b,
                adapter=qdrant_adapter,
                session=session,
                cache=cache,
            )

        assert result.results == [], "No results must be returned on embedding model mismatch"

    def test_mismatch_embed_never_called(
        self,
        seeded_kb_with_model_a: str,
        engine: Any,
        qdrant_adapter: Any,
    ) -> None:
        """Provider embed_batch must NOT be called on mismatch (fail-closed before embed).

        This is the key §15 / provider-abstraction §3.3 invariant: the service
        detects the mismatch by comparing model identity from the alias record
        (set at ingest time via real promotion) against the provider's declared
        capabilities — BEFORE calling embed.
        """
        from sqlalchemy.orm import Session

        from finecorpus.embedding.cache import QueryEmbeddingCache
        from finecorpus.embedding.fake import FakeProvider
        from finecorpus.retrieval.service import query

        provider_b = FakeProvider(dimensions=DIMS_B, model_id=MODEL_ID_B)
        cache = QueryEmbeddingCache(enabled=False)

        # Track embed_batch calls
        embed_calls: list[str] = []
        original_embed = provider_b.embed_batch

        def tracking_embed(texts: list[str], model_id: str) -> object:
            embed_calls.append(f"embed_batch called with {len(texts)} texts")
            return original_embed(texts, model_id)

        provider_b.embed_batch = tracking_embed  # type: ignore[method-assign]

        with Session(engine) as session:
            query(
                kb_id=seeded_kb_with_model_a,
                query_text="mismatch test query — embed must not be called",
                provider=provider_b,
                adapter=qdrant_adapter,
                session=session,
                cache=cache,
            )

        assert len(embed_calls) == 0, (
            f"embed_batch was called {len(embed_calls)} time(s) during a mismatch — "
            "the service MUST detect the mismatch from the alias record BEFORE "
            "calling the provider. Calls: {embed_calls}"
        )

    def test_mismatch_alias_record_reflects_model_a(
        self,
        seeded_kb_with_model_a: str,
        engine: Any,
    ) -> None:
        """Verify the alias record (written by real promotion) holds model A identity.

        This confirms the end-to-end chain: ingest with model A → promote (writes
        alias record) → alias record carries model A metadata → mismatch detected.
        """
        from sqlalchemy.orm import Session

        from finecorpus.control.metadata import AliasRepository
        from finecorpus.index.adapter import alias_name

        alias = alias_name(seeded_kb_with_model_a)
        with Session(engine) as session:
            repo = AliasRepository(session)
            record = repo.get(alias)

        assert record is not None, "Alias record must exist after promotion"
        assert record.embedding_model == MODEL_ID_A, (
            f"Alias record model should be {MODEL_ID_A!r}, got {record.embedding_model!r}"
        )
        assert record.embedding_dimensions == DIMS_A, (
            f"Alias record dimensions should be {DIMS_A}, got {record.embedding_dimensions}"
        )
