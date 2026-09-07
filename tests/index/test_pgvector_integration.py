"""Integration tests for the pgvector IndexAdapter (Phase 7 WU-B).

Marker: ``pgvector_integration`` — a composite mark (named + skipif) from
:func:`conftest.pgvector_integration_mark`.  Auto-skipped when a PostgreSQL with
the pgvector extension is unreachable (the default compose image may not ship
pgvector; the orchestrator runs a pgvector-enabled overlay at phase close).

What these tests cover:
1. Full lifecycle: create_shadow → upsert → promote → search-via-alias → rollback.
2. Tenancy-filter enforcement: a cross-tenant query returns NOTHING (security
   boundary — parity with Qdrant, §11.4 M-060).
3. scroll_all iterates all points and honours the payload filter.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.orm import Session

from conftest import PGVECTOR_DSN, pgvector_integration_mark
from finecorpus.control.metadata import (
    AliasRepository,
    create_tables,
)
from finecorpus.index.adapter import ModelIdentity, alias_name
from finecorpus.index.lifecycle import (
    BuildState,
    create_shadow,
    promote,
    rollback,
)

# Reuse the chunk-point / vector builders from the Qdrant integration module.
from tests.index.test_integration import (
    POSTGRES_DSN,
    _fake_vector,
    _make_chunk_point,
    _unique_kb,
)


@pytest.fixture(scope="module")
def pg_adapter():
    from finecorpus.index.pgvector.backend import PgVectorAdapter

    return PgVectorAdapter(dsn=PGVECTOR_DSN)


@pytest.fixture(scope="module")
def db_engine():
    from finecorpus.control.metadata import create_engine as mk_engine

    engine = mk_engine(POSTGRES_DSN)
    create_tables(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def db_session(db_engine):
    with Session(db_engine) as session:
        yield session


@pgvector_integration_mark
class TestPgVectorLifecycle:
    def test_full_lifecycle_shadow_promote_search_rollback(self, pg_adapter, db_session) -> None:
        kb_id = _unique_kb()
        ws_id = "ws_pgvector_test"
        model = ModelIdentity(
            provider="fake", model="fake-embed-v1", dimensions=4, config_version="cfg_v1"
        )

        # --- N-1 build (rollback target) ---
        ctx1 = create_shadow(pg_adapter, kb_id, ws_id, build_id=1, model_identity=model)
        assert pg_adapter.collection_exists(ctx1.shadow_collection)
        pg_adapter.upsert_points(
            ctx1.shadow_collection,
            [
                _make_chunk_point("doc_old", i, f"old chunk {i}", kb_id, ws_id, model)
                for i in range(2)
            ],
        )
        assert pg_adapter.count_points(ctx1.shadow_collection) == 2

        repo = AliasRepository(db_session)
        if repo.get(alias_name(kb_id)) is None:
            repo.create(alias=alias_name(kb_id), kb_id=kb_id, workspace_id=ws_id)
            db_session.commit()

        promote(pg_adapter, db_session, ctx1, expected_min_chunks=1)
        assert ctx1.state == BuildState.LIVE
        assert pg_adapter.resolve_alias(ctx1.alias) == ctx1.shadow_collection

        # --- N build (new live) ---
        ctx2 = create_shadow(pg_adapter, kb_id, ws_id, build_id=2, model_identity=model)
        pg_adapter.upsert_points(
            ctx2.shadow_collection,
            [
                _make_chunk_point("doc_new", i, f"new chunk {i}", kb_id, ws_id, model)
                for i in range(3)
            ],
        )
        promote(pg_adapter, db_session, ctx2, expected_min_chunks=1)
        assert pg_adapter.resolve_alias(ctx2.alias) == ctx2.shadow_collection

        # Search via alias — never by collection name (C-3).
        results = pg_adapter.search(ctx2.alias, _fake_vector(4), top_k=5)
        assert len(results) == 3
        for r in results:
            assert r.payload["provenance"]["source_document_id"] == "doc_new"

        # --- Rollback restores N-1 and serves it ---
        rollback(pg_adapter, db_session, kb_id, available_model_providers={"fake"})
        assert pg_adapter.resolve_alias(ctx2.alias) == ctx1.shadow_collection
        rolled = pg_adapter.search(ctx2.alias, _fake_vector(4), top_k=5)
        assert {r.payload["provenance"]["source_document_id"] for r in rolled} == {"doc_old"}

        # Cleanup
        try:
            pg_adapter.delete_alias(ctx2.alias)
        except Exception:
            pass
        pg_adapter.drop_collection(ctx1.shadow_collection)
        pg_adapter.drop_collection(ctx2.shadow_collection)

    def test_tenancy_filter_blocks_cross_tenant(self, pg_adapter) -> None:
        """A cross-tenant query MUST return nothing (security boundary)."""
        kb_a = _unique_kb()
        kb_b = _unique_kb()
        model = ModelIdentity(provider="fake", model="m", dimensions=4, config_version="v1")

        coll = pg_adapter.create_collection(kb_a, 1, 4)
        als = alias_name(kb_a)
        # Seed points from TWO different KBs into the same collection so we can
        # prove the search filter (not just collection separation) enforces
        # tenancy.
        pg_adapter.upsert_points(
            coll,
            [
                _make_chunk_point("d_a", 0, "tenant A", kb_a, "ws", model),
                _make_chunk_point("d_b", 0, "tenant B", kb_b, "ws", model),
            ],
        )
        pg_adapter.create_alias(als, coll)

        # Query scoped to kb_a: only tenant-A point returns.
        mine = pg_adapter.search(
            als, _fake_vector(4), top_k=10, payload_filter={"tenancy.kb_id": kb_a}
        )
        assert {r.payload["tenancy"]["kb_id"] for r in mine} == {kb_a}

        # Query scoped to a THIRD, unrelated KB: nothing leaks.
        other = pg_adapter.search(
            als, _fake_vector(4), top_k=10, payload_filter={"tenancy.kb_id": str(uuid.uuid4())}
        )
        assert other == []

        # Cleanup
        pg_adapter.delete_alias(als)
        pg_adapter.drop_collection(coll)

    def test_scroll_all_full_and_filtered(self, pg_adapter) -> None:
        kb_a = _unique_kb()
        kb_b = _unique_kb()
        model = ModelIdentity(provider="fake", model="m", dimensions=4, config_version="v1")
        coll = pg_adapter.create_collection(kb_a, 3, 4)
        pg_adapter.upsert_points(
            coll,
            [
                _make_chunk_point("d_a1", 0, "a1", kb_a, "ws", model),
                _make_chunk_point("d_a2", 0, "a2", kb_a, "ws", model),
                _make_chunk_point("d_b1", 0, "b1", kb_b, "ws", model),
            ],
        )

        all_points = list(pg_adapter.scroll_all(coll))
        assert len(all_points) == 3

        filtered = list(pg_adapter.scroll_all(coll, {"tenancy.kb_id": kb_a}))
        assert {r.payload["tenancy"]["kb_id"] for r in filtered} == {kb_a}
        assert len(filtered) == 2

        pg_adapter.drop_collection(coll)

    def test_scroll_all_crosses_page_boundary_exactly_once(self, pg_adapter) -> None:
        """Keyset pagination must exercise the second+ page (point_id > %s::uuid)
        and return every row exactly once across the batch boundary — no skips or
        duplicates.  With batch_size=2 over 5 rows the scroll spans 3 pages."""
        kb = _unique_kb()
        model = ModelIdentity(provider="fake", model="m", dimensions=4, config_version="v1")
        coll = pg_adapter.create_collection(kb, 5, 4)
        pg_adapter.upsert_points(
            coll,
            [_make_chunk_point("d", i, f"row {i}", kb, "ws", model) for i in range(5)],
        )

        seen = [r.point_id for r in pg_adapter.scroll_all(coll, batch_size=2)]
        # Exactly 5 rows, each exactly once (no skip/dup at the page boundaries).
        assert len(seen) == 5
        assert len(set(seen)) == 5

        pg_adapter.drop_collection(coll)
