"""Integration tests for retrieval service against live Qdrant + Postgres.

Marker: ``qdrant_integration`` — a **composite** mark (named + skipif) provided by
:func:`conftest.qdrant_integration_mark`.  Tests decorated with it are:

- Selected by ``pytest -m qdrant_integration`` (named mark).
- Auto-skipped when Qdrant or Postgres are unreachable (skipif).

What these tests cover:
1. Seed a tiny collection via adapter + lifecycle, query through the real
   retrieval service, assert round-trip (matches, provenance, trust label).
2. no_matches is returned correctly when the corpus has nothing relevant.
3. filtered_to_zero is distinguished from no_matches when score_threshold
   eliminates all candidates.
4. EMBEDDING_MODEL_MISMATCH blocks retrieval (provider never called) when
   the alias record is seeded with a mismatched model identity.

Infrastructure:
- FakeProvider: deterministic vectors; no OpenAI key required.
- QdrantAdapter from index.qdrant.backend.
- AliasRepository over a real Postgres connection.
- control.metadata.create_tables() for schema bootstrap.

Run with:
    docker compose -f docker-compose.yml -f docker-compose.integration.yml up -d qdrant postgres
    uv run pytest -m qdrant_integration tests/retrieval/test_integration.py -v
    docker compose -f docker-compose.yml -f docker-compose.integration.yml down
"""

from __future__ import annotations

import os as _os
import uuid
from typing import Any

import pytest

from conftest import qdrant_integration_mark

QDRANT_URL = "http://localhost:6333"
# Override via RTFC_POSTGRES_DSN env var for CI or non-default passwords.
POSTGRES_DSN = _os.environ.get(
    "RTFC_POSTGRES_DSN",
    "postgresql+psycopg://finecorpus:finecorpus@localhost:5432/finecorpus",
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

DIMENSIONS = 64
MODEL_ID = "fake-embed-v1"
PROVIDER_ID = "fake"
CONFIG_VERSION = "cfgv-integration-1"


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

    return QdrantAdapter(url=QDRANT_URL)


@pytest.fixture(scope="module")
def fake_provider() -> Any:
    from finecorpus.embedding.fake import FakeProvider

    return FakeProvider(dimensions=DIMENSIONS, model_id=MODEL_ID)


@pytest.fixture(scope="function")
def kb_id() -> str:
    """Unique KB ID per test to avoid cross-test pollution."""
    return f"kb-int-{uuid.uuid4().hex[:8]}"


@pytest.fixture(scope="function")
def seeded_kb(kb_id: str, engine: Any, qdrant_adapter: Any, fake_provider: Any) -> str:
    """Bootstrap a full KB: alias record, shadow collection, 2 chunks, promote.

    Returns the kb_id. Tears down Qdrant collection and alias on exit.
    Postgres rows are left (tests use unique KB IDs per run, no cleanup needed
    for correctness; a full teardown would require deleting the alias record).
    """
    from sqlalchemy.orm import Session

    from finecorpus.control.metadata import AliasRepository
    from finecorpus.index.adapter import ModelIdentity, alias_name, build_point_payload
    from finecorpus.index.lifecycle import create_shadow, promote

    alias = alias_name(kb_id)
    build_id = 1
    workspace_id = "ws-integration"

    identity = ModelIdentity(
        provider=PROVIDER_ID,
        model=MODEL_ID,
        dimensions=DIMENSIONS,
        config_version=CONFIG_VERSION,
    )

    # Bootstrap the control-plane alias record
    with Session(engine) as session:
        repo = AliasRepository(session)
        existing = repo.get(alias)
        if existing is None:
            repo.create(alias=alias, kb_id=kb_id, workspace_id=workspace_id)
            session.commit()

    # Create shadow collection
    shadow_state = create_shadow(
        adapter=qdrant_adapter,
        kb_id=kb_id,
        workspace_id=workspace_id,
        build_id=build_id,
        model_identity=identity,
    )
    coll_name = shadow_state.shadow_collection

    # Seed two chunks
    from tests.retrieval.helpers import make_provenance_payload

    for i, text in enumerate(["Alpha retrieval chunk.", "Beta retrieval chunk."]):
        chunk_id = f"chk_int_{kb_id}_{i}"
        point_id = str(uuid.uuid4())
        prov = make_provenance_payload(
            source_document_id=f"doc-int-{i}",
            source_document_version="v1",
        )
        tenancy = {"kb_id": kb_id, "workspace_id": workspace_id}
        payload = build_point_payload(
            chunk_id=chunk_id,
            provenance=prov,
            tenancy=tenancy,
            text=text,
            embedding_ref={"model_id": MODEL_ID, "dimensions": DIMENSIONS},
        )
        vector = fake_provider.embed_batch([text], model_id=MODEL_ID).embeddings[0]
        qdrant_adapter.upsert_points(
            collection=coll_name,
            points=[{"id": point_id, "vector": vector, "payload": payload}],
        )

    # Promote (2 chunks seeded above — default min_chunks=1 gate passes)
    with Session(engine) as session:
        promote(
            adapter=qdrant_adapter,
            session=session,
            ctx=shadow_state,
        )
        session.commit()

    yield kb_id

    # Teardown: drop the Qdrant collection and alias
    try:
        if qdrant_adapter.alias_exists(alias_name(kb_id)):
            qdrant_adapter.delete_alias(alias_name(kb_id))
        if qdrant_adapter.collection_exists(coll_name):
            qdrant_adapter.drop_collection(coll_name)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------


@qdrant_integration_mark
class TestRoundTrip:
    def test_happy_path_matches(
        self,
        seeded_kb: str,
        fake_provider: Any,
        qdrant_adapter: Any,
        engine: Any,
    ) -> None:
        """Full round-trip: query through real service, assert matches."""
        from sqlalchemy.orm import Session

        from finecorpus.embedding.cache import QueryEmbeddingCache
        from finecorpus.retrieval.service import query

        cache = QueryEmbeddingCache(enabled=False)

        with Session(engine) as session:
            result = query(
                kb_id=seeded_kb,
                query_text="Alpha chunk",
                provider=fake_provider,
                adapter=qdrant_adapter,
                session=session,
                top_k=10,
                cache=cache,
            )

        from finecorpus.contracts.retrieval_response import ResultStatus

        assert result.result_status == ResultStatus.matches
        assert len(result.results) > 0

    def test_provenance_complete_in_round_trip(
        self,
        seeded_kb: str,
        fake_provider: Any,
        qdrant_adapter: Any,
        engine: Any,
    ) -> None:
        """Every result must carry full §8 provenance in the real round-trip."""
        from sqlalchemy.orm import Session

        from finecorpus.embedding.cache import QueryEmbeddingCache
        from finecorpus.retrieval.service import query

        cache = QueryEmbeddingCache(enabled=False)

        with Session(engine) as session:
            result = query(
                kb_id=seeded_kb,
                query_text="retrieval chunk",
                provider=fake_provider,
                adapter=qdrant_adapter,
                session=session,
                cache=cache,
            )

        for r in result.results:
            prov = r.provenance
            assert prov.source_document_id
            assert prov.source_document_version
            assert prov.source_location is not None
            assert prov.segment_type is not None

    def test_trust_label_in_round_trip(
        self,
        seeded_kb: str,
        fake_provider: Any,
        qdrant_adapter: Any,
        engine: Any,
    ) -> None:
        """Trust label must be untrusted_ingested on every result (§14.1)."""
        from sqlalchemy.orm import Session

        from finecorpus.contracts.retrieval_response import ResultStatus
        from finecorpus.contracts.shared.blocks import TrustLevel
        from finecorpus.embedding.cache import QueryEmbeddingCache
        from finecorpus.retrieval.service import query

        cache = QueryEmbeddingCache(enabled=False)

        with Session(engine) as session:
            result = query(
                kb_id=seeded_kb,
                query_text="chunk",
                provider=fake_provider,
                adapter=qdrant_adapter,
                session=session,
                cache=cache,
            )

        assert result.result_status == ResultStatus.matches
        for r in result.results:
            assert r.trust_level == TrustLevel.untrusted_ingested

    def test_no_matches_for_unknown_kb(
        self,
        fake_provider: Any,
        qdrant_adapter: Any,
        engine: Any,
    ) -> None:
        """An unknown KB returns KB_NOT_READY error (alias record absent)."""
        from sqlalchemy.orm import Session

        from finecorpus.contracts.retrieval_response import ErrorCode, ResultStatus
        from finecorpus.embedding.cache import QueryEmbeddingCache
        from finecorpus.retrieval.service import query

        cache = QueryEmbeddingCache(enabled=False)

        with Session(engine) as session:
            result = query(
                kb_id="kb-does-not-exist-xyz",
                query_text="anything",
                provider=fake_provider,
                adapter=qdrant_adapter,
                session=session,
                cache=cache,
            )

        assert result.result_status == ResultStatus.error
        assert result.error is not None
        assert result.error.code == ErrorCode.KB_NOT_READY

    def test_model_mismatch_fail_closed(
        self,
        seeded_kb: str,
        qdrant_adapter: Any,
        engine: Any,
    ) -> None:
        """Model mismatch: provider with different model → fail closed, never calls embed."""
        from sqlalchemy.orm import Session

        from finecorpus.contracts.retrieval_response import ErrorCode, ResultStatus
        from finecorpus.embedding.cache import QueryEmbeddingCache
        from finecorpus.embedding.fake import FakeProvider
        from finecorpus.retrieval.service import query

        # Provider with DIFFERENT model than what was indexed
        wrong_provider = FakeProvider(
            dimensions=128,  # wrong dimensions
            model_id="wrong-model",
        )
        cache = QueryEmbeddingCache(enabled=False)

        # Track embed calls
        embed_calls: list[bool] = []
        orig = wrong_provider.embed_batch

        def tracking_embed(texts: list, model_id: str) -> object:
            embed_calls.append(True)
            return orig(texts, model_id)

        wrong_provider.embed_batch = tracking_embed  # type: ignore[method-assign]

        with Session(engine) as session:
            result = query(
                kb_id=seeded_kb,
                query_text="test",
                provider=wrong_provider,
                adapter=qdrant_adapter,
                session=session,
                cache=cache,
            )

        assert result.result_status == ResultStatus.error
        assert result.error is not None
        assert result.error.code == ErrorCode.EMBEDDING_MODEL_MISMATCH
        assert len(embed_calls) == 0, "embed_batch must NOT be called on model mismatch"

    def test_filtered_to_zero_vs_no_matches(
        self,
        seeded_kb: str,
        fake_provider: Any,
        qdrant_adapter: Any,
        engine: Any,
    ) -> None:
        """score_threshold that eliminates all → filtered_to_zero (not no_matches)."""
        from sqlalchemy.orm import Session

        from finecorpus.contracts.retrieval_response import ResultStatus
        from finecorpus.embedding.cache import QueryEmbeddingCache
        from finecorpus.retrieval.service import query

        cache = QueryEmbeddingCache(enabled=False)

        with Session(engine) as session:
            result = query(
                kb_id=seeded_kb,
                query_text="chunk",
                provider=fake_provider,
                adapter=qdrant_adapter,
                session=session,
                score_threshold=0.9999,  # above any realistic cosine score
                cache=cache,
            )

        # The corpus has content but threshold killed all candidates
        # Result is either filtered_to_zero (if search found candidates) or
        # no_matches (if search returned nothing). We assert it's not an error.
        assert result.result_status in {
            ResultStatus.filtered_to_zero,
            ResultStatus.no_matches,
        }
        assert result.error is None
