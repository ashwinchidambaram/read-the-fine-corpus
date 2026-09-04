"""Integration tests for the Qdrant adapter + lifecycle (§18.3 tests 1, 6).

Marker: ``qdrant_integration`` — auto-skipped when Qdrant is unreachable.
Also exercises Postgres via the control-plane metadata module.

What these tests cover:
1. Shadow build → promote → search-via-alias round trip.
2. Alias never resolves to a missing collection mid-swap (atomic swap).
3. Idempotent double-promote.
4. Failed validation blocks promotion; alias unchanged; shadow retained.
5. Rollback restores N-1 and serves correctly (§18.3 test 6).
6. Blocked-rollback path when model provider removed from config (OQ-L-7).
7. Startup reconcile repairs a simulated half-swap (OQ-L-1).
"""

from __future__ import annotations

import threading
import time
import uuid

import pytest
from sqlalchemy.orm import Session

from finecorpus.contracts.chunk_id import derive_point_id
from finecorpus.control.metadata import (
    AliasRepository,
    create_tables,
)
from finecorpus.index.adapter import (
    ModelIdentity,
    alias_name,
    build_point_payload,
)
from finecorpus.index.lifecycle import (
    BuildState,
    RollbackError,
    ValidationFailedError,
    create_shadow,
    promote,
    rollback,
    startup_reconcile,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

QDRANT_URL = "http://localhost:6333"
POSTGRES_DSN = "postgresql+psycopg://finecorpus:finecorpus@localhost:5432/finecorpus"


def _qdrant_reachable() -> bool:
    """Return True if Qdrant is accessible at localhost:6333."""
    try:
        from qdrant_client import QdrantClient

        c = QdrantClient(url=QDRANT_URL, timeout=2)
        c.get_collections()
        return True
    except Exception:
        return False


def _postgres_reachable() -> bool:
    """Return True if Postgres is accessible."""
    try:
        from sqlalchemy import create_engine, text

        eng = create_engine(POSTGRES_DSN, connect_args={"connect_timeout": 2})
        with eng.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


qdrant_integration = pytest.mark.skipif(
    not _qdrant_reachable(),
    reason="Qdrant not reachable at localhost:6333 (start with: docker compose up -d qdrant)",
)

postgres_integration = pytest.mark.skipif(
    not _postgres_reachable(),
    reason="Postgres not reachable (start with: docker compose up -d postgres)",
)

both_integration = pytest.mark.skipif(
    not (_qdrant_reachable() and _postgres_reachable()),
    reason="Qdrant or Postgres not reachable (start with: docker compose up -d qdrant postgres)",
)


@pytest.fixture(scope="module")
def qdrant_adapter():
    """QdrantAdapter connected to the compose Qdrant."""
    from finecorpus.index.qdrant.backend import QdrantAdapter

    return QdrantAdapter(url=QDRANT_URL, timeout=10)


@pytest.fixture(scope="module")
def db_engine():
    """SQLAlchemy engine connected to the compose Postgres."""
    from finecorpus.control.metadata import create_engine as mk_engine

    engine = mk_engine(POSTGRES_DSN)
    create_tables(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def db_session(db_engine):
    """Fresh session per test, rolled back after."""
    with Session(db_engine) as session:
        yield session


def _fake_vector(dims: int = 4) -> list[float]:
    """Generate a deterministic unit-ish vector for testing."""
    import math

    v = [0.1 * (i + 1) for i in range(dims)]
    mag = math.sqrt(sum(x * x for x in v))
    return [x / mag for x in v]


def _make_chunk_point(
    doc_id: str,
    chunk_index: int,
    text: str,
    kb_id: str,
    workspace_id: str,
    model_identity: ModelIdentity,
) -> dict:
    """Build a vector point dict suitable for upsert_points."""
    point_id = derive_point_id(
        document_id=doc_id,
        content_hash="abc123",
        config_version=model_identity.config_version,
        segment_path="intro",
        chunk_index=chunk_index,
    )
    from finecorpus.contracts.chunk_id import derive_chunk_id

    chunk_id = derive_chunk_id(
        document_id=doc_id,
        content_hash="abc123",
        config_version=model_identity.config_version,
        segment_path="intro",
        chunk_index=chunk_index,
    )
    payload = build_point_payload(
        chunk_id=chunk_id,
        provenance={
            "source_document_id": doc_id,
            "source_document_version": "abc123",
            "structural_path": [],
            "transformations": [],
            "confidence": 1.0,
            "ocr_confidence": None,
            "segment_type": "prose",
            "salience_tier": "primary",
            "salience_basis": "default",
            "salience_signals": [],
            "language": "en",
            "injection_suspicion": 0.0,
            "invisible_content_flags": [],
            "sensitivity_flags": [],
            "trust_level": "untrusted_ingested",
            "source_location": {
                "locator_kind": "char_range",
                "page_start": None,
                "page_end": None,
                "byte_start": None,
                "byte_end": None,
                "char_start": 0,
                "char_end": len(text),
                "cell_range": None,
                "dom_path": None,
                "bbox": None,
                "coordinate_note": None,
            },
        },
        tenancy={
            "workspace_id": workspace_id,
            "kb_id": kb_id,
            "permission_mode": "public_to_kb",
            "permission_principals": [],
            "permission_source": "platform",
            "permission_fidelity": "authoritative",
            "permission_resolved_at": None,
        },
        text=text,
        embedding_ref={
            "provider": model_identity.provider,
            "model": model_identity.model,
            "dimensions": model_identity.dimensions,
            "config_version": model_identity.config_version,
        },
    )
    return {
        "id": point_id,
        "vector": _fake_vector(model_identity.dimensions),
        "payload": payload,
    }


def _unique_kb() -> str:
    """Generate a unique KB ID for each test to avoid collisions."""
    return str(uuid.uuid4()).replace("-", "")[:12]


# ---------------------------------------------------------------------------
# Integration test 1: Shadow build → promote → search via alias
# ---------------------------------------------------------------------------


@both_integration
class TestShadowBuildPromoteSearch:
    def test_round_trip(self, qdrant_adapter, db_session) -> None:
        kb_id = _unique_kb()
        ws_id = "ws_integration_test"
        model = ModelIdentity(
            provider="fake",
            model="fake-embed-v1",
            dimensions=4,
            config_version="cfg_v1",
        )

        # Create shadow
        ctx = create_shadow(qdrant_adapter, kb_id, ws_id, build_id=1, model_identity=model)
        assert qdrant_adapter.collection_exists(ctx.shadow_collection)

        # Write chunks
        doc_id = "doc_001"
        points = [
            _make_chunk_point(doc_id, i, f"text chunk {i}", kb_id, ws_id, model) for i in range(3)
        ]
        qdrant_adapter.upsert_points(ctx.shadow_collection, points)
        assert qdrant_adapter.count_points(ctx.shadow_collection) == 3

        # Promote
        repo = AliasRepository(db_session)
        existing = repo.get(alias_name(kb_id))
        if existing is None:
            repo.create(alias=alias_name(kb_id), kb_id=kb_id, workspace_id=ws_id)
            db_session.commit()

        # Promote (the session already has the alias record pre-created above)
        promote(
            qdrant_adapter,
            db_session,
            ctx,
            expected_min_chunks=1,
        )

        assert ctx.state == BuildState.LIVE
        assert qdrant_adapter.resolve_alias(ctx.alias) == ctx.shadow_collection

        # Search via alias (C-3: never by collection name)
        results = qdrant_adapter.search(ctx.alias, _fake_vector(4), top_k=5)
        assert len(results) > 0
        for r in results:
            assert r.chunk_id.startswith("chk_")
            assert r.payload["provenance"]["source_document_id"] == doc_id

        # Cleanup
        try:
            qdrant_adapter.delete_alias(ctx.alias)
        except Exception:
            pass
        qdrant_adapter.drop_collection(ctx.shadow_collection)


# ---------------------------------------------------------------------------
# Integration test 2: Atomic swap — alias never resolves to missing collection
# ---------------------------------------------------------------------------


@qdrant_integration
class TestAtomicAliasSwap:
    def test_alias_never_misses_during_swap(self, qdrant_adapter) -> None:
        """The alias resolves correctly throughout a swap (no window of None)."""
        kb_id = _unique_kb()
        model = ModelIdentity(provider="fake", model="m", dimensions=4, config_version="v1")
        als = alias_name(kb_id)

        # Create and populate two collections
        coll1 = qdrant_adapter.create_collection(kb_id, 1, 4)
        coll2 = qdrant_adapter.create_collection(kb_id, 2, 4)
        qdrant_adapter.upsert_points(coll1, [_make_chunk_point("d1", 0, "t1", kb_id, "ws", model)])
        qdrant_adapter.upsert_points(coll2, [_make_chunk_point("d2", 0, "t2", kb_id, "ws", model)])

        # Start with alias on coll1
        qdrant_adapter.create_alias(als, coll1)
        assert qdrant_adapter.resolve_alias(als) == coll1

        # Monitor alias resolution in a background thread during swap
        errors: list[str] = []
        stop_flag = threading.Event()

        def poll_alias() -> None:
            while not stop_flag.is_set():
                target = qdrant_adapter.resolve_alias(als)
                if target not in (coll1, coll2):
                    errors.append(f"Alias resolved to unexpected target: {target!r}")
                time.sleep(0.001)

        t = threading.Thread(target=poll_alias, daemon=True)
        t.start()

        # Perform the swap
        qdrant_adapter.retarget_alias(als, coll2)
        time.sleep(0.05)  # Let the poller run a bit after the swap
        stop_flag.set()
        t.join(timeout=2)

        assert not errors, f"Alias was invalid during swap: {errors}"
        assert qdrant_adapter.resolve_alias(als) == coll2

        # Cleanup
        qdrant_adapter.delete_alias(als)
        qdrant_adapter.drop_collection(coll1)
        qdrant_adapter.drop_collection(coll2)


# ---------------------------------------------------------------------------
# Integration test 3: Idempotent double-promote
# ---------------------------------------------------------------------------


@both_integration
class TestIdempotentDoublePromote:
    def test_double_promote_is_idempotent(self, qdrant_adapter, db_session) -> None:
        kb_id = _unique_kb()
        ws_id = "ws_idem_test"
        model = ModelIdentity(provider="fake", model="m", dimensions=4, config_version="v1")

        ctx = create_shadow(qdrant_adapter, kb_id, ws_id, build_id=1, model_identity=model)
        qdrant_adapter.upsert_points(
            ctx.shadow_collection,
            [_make_chunk_point("d1", 0, "hello", kb_id, ws_id, model)],
        )

        # Create alias record
        repo = AliasRepository(db_session)
        if repo.get(ctx.alias) is None:
            repo.create(alias=ctx.alias, kb_id=kb_id, workspace_id=ws_id)
            db_session.commit()

        # First promote
        promote(qdrant_adapter, db_session, ctx)
        assert ctx.state == BuildState.LIVE

        # Second promote (idempotent: same shadow, same alias)
        ctx.state = BuildState.TRIGGERED  # Reset for test
        promote(qdrant_adapter, db_session, ctx)
        assert ctx.state == BuildState.LIVE

        # Alias still points at the shadow
        assert qdrant_adapter.resolve_alias(ctx.alias) == ctx.shadow_collection

        # Cleanup
        try:
            qdrant_adapter.delete_alias(ctx.alias)
        except Exception:
            pass
        qdrant_adapter.drop_collection(ctx.shadow_collection)


# ---------------------------------------------------------------------------
# Integration test 4: Failed validation blocks promotion
# ---------------------------------------------------------------------------


@both_integration
class TestFailedValidationBlocksPromotion:
    def test_empty_shadow_blocked(self, qdrant_adapter, db_session) -> None:
        kb_id = _unique_kb()
        ws_id = "ws_val_test"
        model = ModelIdentity(provider="fake", model="m", dimensions=4, config_version="v1")

        ctx = create_shadow(qdrant_adapter, kb_id, ws_id, build_id=1, model_identity=model)
        # No chunks written

        with pytest.raises(ValidationFailedError) as exc_info:
            promote(qdrant_adapter, db_session, ctx)

        # Alias is unchanged (not created)
        assert not qdrant_adapter.alias_exists(ctx.alias)

        # Shadow collection is retained (never dropped on validation failure)
        assert qdrant_adapter.collection_exists(ctx.shadow_collection)

        # State is VALIDATION_FAILED
        assert ctx.state == BuildState.VALIDATION_FAILED
        assert exc_info.value.gate in ("non_empty", "chunk_count_bounds")

        # Cleanup
        qdrant_adapter.drop_collection(ctx.shadow_collection)


# ---------------------------------------------------------------------------
# Integration test 5: Rollback restores N-1 (§18.3 test 6)
# ---------------------------------------------------------------------------


@both_integration
class TestRollbackRestoresNMinus1:
    def test_rollback_serves_n1_chunks(self, qdrant_adapter, db_session) -> None:
        """After rollback, the alias serves the N-1 collection's chunks."""
        kb_id = _unique_kb()
        ws_id = "ws_rollback_test"
        model = ModelIdentity(provider="fake", model="m", dimensions=4, config_version="v1")

        als = alias_name(kb_id)

        # Create N-1 collection (build 1)
        coll_n1 = qdrant_adapter.create_collection(
            kb_id,
            1,
            4,
            metadata={"provider": "fake", "model": "m", "dimensions": 4, "config_version": "v1"},
        )
        qdrant_adapter.upsert_points(
            coll_n1,
            [_make_chunk_point("doc_n1", 0, "N-1 content", kb_id, ws_id, model)],
        )

        # Create N collection (build 2)
        coll_n = qdrant_adapter.create_collection(
            kb_id,
            2,
            4,
            metadata={"provider": "fake", "model": "m", "dimensions": 4, "config_version": "v1"},
        )
        qdrant_adapter.upsert_points(
            coll_n,
            [_make_chunk_point("doc_n", 0, "N content", kb_id, ws_id, model)],
        )

        # Set alias to N (current live)
        qdrant_adapter.create_alias(als, coll_n)
        assert qdrant_adapter.resolve_alias(als) == coll_n

        # Set up alias record: current=coll_n, previous=coll_n1
        repo = AliasRepository(db_session)
        existing = repo.get(als)
        if existing is None:
            existing = repo.create(alias=als, kb_id=kb_id, workspace_id=ws_id)
            db_session.commit()
        existing.collection_name = coll_n
        existing.previous_collection = coll_n1
        existing.embedding_provider = "fake"
        existing.build_id = 2
        db_session.commit()

        # Rollback to N-1
        result = rollback(qdrant_adapter, db_session, kb_id, available_model_providers={"fake"})

        assert result == coll_n1
        assert qdrant_adapter.resolve_alias(als) == coll_n1

        # Search via alias — must return N-1 chunks, not N chunks
        results = qdrant_adapter.search(als, _fake_vector(4), top_k=5)
        doc_ids_in_results = {r.payload["provenance"]["source_document_id"] for r in results}
        assert "doc_n1" in doc_ids_in_results

        # Cleanup
        qdrant_adapter.delete_alias(als)
        qdrant_adapter.drop_collection(coll_n1)
        qdrant_adapter.drop_collection(coll_n)


# ---------------------------------------------------------------------------
# Integration test 6: Blocked rollback when model removed from config (OQ-L-7)
# ---------------------------------------------------------------------------


@both_integration
class TestBlockedRollbackModelUnavailable:
    def test_rollback_blocked_when_provider_missing(self, qdrant_adapter, db_session) -> None:
        kb_id = _unique_kb()
        ws_id = "ws_block_test"
        als = alias_name(kb_id)

        coll_n1 = qdrant_adapter.create_collection(kb_id, 1, 4)
        coll_n = qdrant_adapter.create_collection(kb_id, 2, 4)
        qdrant_adapter.upsert_points(
            coll_n1,
            [_make_chunk_point("d1", 0, "t", kb_id, ws_id, ModelIdentity("openai", "m", 4, "v1"))],
        )
        qdrant_adapter.create_alias(als, coll_n)

        repo = AliasRepository(db_session)
        existing = repo.get(als)
        if existing is None:
            existing = repo.create(alias=als, kb_id=kb_id, workspace_id=ws_id)
            db_session.commit()
        existing.collection_name = coll_n
        existing.previous_collection = coll_n1
        existing.embedding_provider = "openai"  # N-1 was built with openai
        existing.build_id = 2
        db_session.commit()

        # openai is NOT in available_model_providers
        with pytest.raises(RollbackError) as exc_info:
            rollback(qdrant_adapter, db_session, kb_id, available_model_providers={"ollama"})

        assert "openai" in str(exc_info.value)
        # Alias must remain unchanged
        assert qdrant_adapter.resolve_alias(als) == coll_n

        # Cleanup
        qdrant_adapter.delete_alias(als)
        qdrant_adapter.drop_collection(coll_n1)
        qdrant_adapter.drop_collection(coll_n)


# ---------------------------------------------------------------------------
# Integration test 7: Startup reconcile repairs half-swap (OQ-L-1)
# ---------------------------------------------------------------------------


@both_integration
class TestStartupReconcileRepairsHalfSwap:
    def test_reconcile_repairs_phase2_failure(self, qdrant_adapter, db_session) -> None:
        """Simulate: Phase 1 succeeded (Qdrant updated), Phase 2 failed (record stale)."""
        kb_id = _unique_kb()
        ws_id = "ws_reconcile_test"
        als = alias_name(kb_id)

        old_coll = qdrant_adapter.create_collection(
            kb_id,
            1,
            4,
            metadata={"provider": "fake", "model": "m", "dimensions": 4, "config_version": "v1"},
        )
        new_coll = qdrant_adapter.create_collection(
            kb_id,
            2,
            4,
            metadata={"provider": "fake", "model": "m", "dimensions": 4, "config_version": "v2"},
        )

        # Qdrant: alias already points at new_coll (Phase 1 done)
        qdrant_adapter.create_alias(als, new_coll)
        assert qdrant_adapter.resolve_alias(als) == new_coll

        # Control-plane: record still shows old_coll (Phase 2 failed)
        repo = AliasRepository(db_session)
        existing = repo.get(als)
        if existing is None:
            existing = repo.create(alias=als, kb_id=kb_id, workspace_id=ws_id)
            db_session.commit()
        existing.collection_name = old_coll
        existing.build_id = 1
        existing.embedding_provider = "fake"
        existing.embedding_model = "m"
        existing.embedding_dimensions = 4
        existing.config_version = "v1"
        db_session.commit()

        # Run reconcile
        result = startup_reconcile(qdrant_adapter, db_session, aliases_to_check=[als])

        assert result.inconsistencies_found == 1
        assert result.repaired == 1
        assert result.failed == 0

        # After reconcile, record should reflect new_coll
        repaired_record = repo.get(als)
        assert repaired_record is not None
        assert repaired_record.collection_name == new_coll

        # Cleanup
        qdrant_adapter.delete_alias(als)
        qdrant_adapter.drop_collection(old_coll)
        qdrant_adapter.drop_collection(new_coll)
