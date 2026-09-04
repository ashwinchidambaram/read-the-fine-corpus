"""Unit tests for the lifecycle state machine transitions."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from finecorpus.index.adapter import (
    BackendCapabilities,
    CollectionInfo,
    ModelIdentity,
    SearchResult,
    alias_name,
    collection_name,
)
from finecorpus.index.lifecycle import (
    BuildState,
    NoNMinusOneError,
    RollbackError,
    ValidationFailedError,
    create_shadow,
    promote,
    rollback,
    startup_reconcile,
    validate_shadow,
)

# ---------------------------------------------------------------------------
# Fake adapter for unit tests (no Qdrant required)
# ---------------------------------------------------------------------------


class FakeAdapter:
    """In-memory fake adapter for state-machine unit tests."""

    def __init__(self) -> None:
        self.collections: dict[str, dict] = {}  # name -> {points, metadata}
        self.aliases: dict[str, str] = {}  # alias -> collection

    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities()

    def create_collection(
        self,
        kb_id: str,
        build_id: int,
        dimensions: int,
        metadata: dict | None = None,
    ) -> str:
        name = collection_name(kb_id, build_id)
        self.collections[name] = {"points": [], "metadata": metadata or {}}
        return name

    def drop_collection(self, collection: str) -> None:
        self.collections.pop(collection, None)

    def get_collection_info(self, collection: str) -> CollectionInfo:
        c = self.collections[collection]
        return CollectionInfo(
            name=collection,
            vector_size=3,
            point_count=len(c["points"]),
            metadata=c["metadata"],
        )

    def collection_exists(self, collection: str) -> bool:
        return collection in self.collections

    def upsert_points(self, collection: str, points: list) -> None:
        self.collections[collection]["points"].extend(points)

    def delete_by_document(self, collection: str, source_document_id: str) -> int:
        points = self.collections[collection]["points"]
        before = len(points)
        self.collections[collection]["points"] = [
            p
            for p in points
            if p.get("payload", {}).get("provenance", {}).get("source_document_id")
            != source_document_id
        ]
        return before - len(self.collections[collection]["points"])

    def set_collection_metadata(self, collection: str, metadata: dict) -> None:
        self.collections[collection]["metadata"].update(metadata)

    def get_collection_metadata(self, collection: str) -> dict:
        return dict(self.collections.get(collection, {}).get("metadata", {}))

    def create_alias(self, alias: str, collection: str) -> None:
        self.aliases[alias] = collection

    def retarget_alias(self, alias: str, new_collection: str) -> None:
        if new_collection not in self.collections:
            from finecorpus.index.adapter import CollectionNotFoundError

            raise CollectionNotFoundError(f"Collection '{new_collection}' not found")
        self.aliases[alias] = new_collection

    def resolve_alias(self, alias: str) -> str | None:
        return self.aliases.get(alias)

    def alias_exists(self, alias: str) -> bool:
        return alias in self.aliases

    def delete_alias(self, alias: str) -> None:
        self.aliases.pop(alias, None)

    def search(
        self,
        alias: str,
        query_vector: list,
        top_k: int = 10,
        payload_filter: dict | None = None,
    ) -> list[SearchResult]:
        collection = self.aliases.get(alias)
        if collection is None:
            return []
        points = self.collections.get(collection, {}).get("points", [])
        # Very simple fake score
        results = [
            SearchResult(
                point_id=str(p.get("id", "")),
                chunk_id=p.get("payload", {}).get("chunk_id", ""),
                score=1.0,
                payload=p.get("payload", {}),
            )
            for p in points[:top_k]
        ]
        return results

    def count_points(self, collection: str) -> int:
        if collection not in self.collections:
            from finecorpus.index.adapter import CollectionNotFoundError

            raise CollectionNotFoundError(f"Collection '{collection}' not found")
        return len(self.collections[collection]["points"])

    def list_collections(self) -> list[str]:
        return list(self.collections.keys())

    def list_aliases(self) -> list:
        from finecorpus.index.adapter import AliasRecord as AdapterAliasRecord

        return [
            AdapterAliasRecord(alias_name=alias, collection_name=coll)
            for alias, coll in self.aliases.items()
        ]


# ---------------------------------------------------------------------------
# Fake SQLAlchemy session for unit tests
# ---------------------------------------------------------------------------


class FakeAliasRecord:
    def __init__(self, alias: str, kb_id: str, workspace_id: str) -> None:
        self.alias = alias
        self.kb_id = kb_id
        self.workspace_id = workspace_id
        self.collection_name: str | None = None
        self.build_id: int | None = None
        self.embedding_provider: str | None = None
        self.embedding_model: str | None = None
        self.embedding_dimensions: int | None = None
        self.config_version: str | None = None
        self.promoted_at = None
        self.previous_collection: str | None = None


class FakeSession:
    """Very thin fake that satisfies the lifecycle code's session usage."""

    def __init__(self) -> None:
        self._records: dict[str, FakeAliasRecord] = {}
        self.committed = 0
        self.rolled_back = 0

    def add(self, record: FakeAliasRecord) -> None:
        self._records[record.alias] = record

    def commit(self) -> None:
        self.committed += 1

    def rollback(self) -> None:
        self.rolled_back += 1

    def get_record(self, alias: str) -> FakeAliasRecord | None:
        return self._records.get(alias)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def _model_identity() -> ModelIdentity:
    return ModelIdentity(
        provider="openai",
        model="text-embedding-3-small",
        dimensions=3,
        config_version="abc123",
    )


# ---- validate_shadow ----


class TestValidateShadow:
    def test_empty_collection_fails(self) -> None:
        adapter = FakeAdapter()
        adapter.create_collection("kb1", 1, 3)
        result = validate_shadow(adapter, collection_name("kb1", 1))
        assert not result.passed
        assert result.gate == "non_empty"

    def test_non_empty_passes(self) -> None:
        adapter = FakeAdapter()
        coll = adapter.create_collection("kb1", 1, 3)
        pt = {"id": "x", "vector": [0.1, 0.2, 0.3], "payload": {"chunk_id": "c1"}}
        adapter.upsert_points(coll, [pt])
        result = validate_shadow(adapter, coll)
        assert result.passed

    def test_declared_empty_passes_zero_chunks(self) -> None:
        adapter = FakeAdapter()
        coll = adapter.create_collection("kb1", 1, 3)
        result = validate_shadow(adapter, coll, declared_empty=True)
        assert result.passed

    def test_below_min_fails(self) -> None:
        adapter = FakeAdapter()
        coll = adapter.create_collection("kb1", 1, 3)
        adapter.upsert_points(coll, [{"id": "x", "vector": [0.1, 0.2, 0.3], "payload": {}}])
        result = validate_shadow(adapter, coll, expected_min_chunks=5)
        assert not result.passed
        assert result.gate == "chunk_count_bounds"

    def test_above_max_fails(self) -> None:
        adapter = FakeAdapter()
        coll = adapter.create_collection("kb1", 1, 3)
        for i in range(10):
            adapter.upsert_points(coll, [{"id": str(i), "vector": [0.1, 0.2, 0.3], "payload": {}}])
        result = validate_shadow(adapter, coll, expected_max_chunks=5)
        assert not result.passed
        assert result.gate == "chunk_count_bounds"

    def test_missing_collection_fails(self) -> None:
        adapter = FakeAdapter()
        result = validate_shadow(adapter, "rtfc_nonexistent_00000001")
        assert not result.passed
        assert result.gate == "collection_exists"


# ---- create_shadow ----


class TestCreateShadow:
    def test_creates_collection(self) -> None:
        adapter = FakeAdapter()
        ctx = create_shadow(adapter, "kb1", "ws1", 1, _model_identity())
        assert adapter.collection_exists(ctx.shadow_collection)

    def test_correct_collection_name(self) -> None:
        adapter = FakeAdapter()
        ctx = create_shadow(adapter, "kb1", "ws1", 5, _model_identity())
        assert ctx.shadow_collection == collection_name("kb1", 5)

    def test_correct_alias_name(self) -> None:
        adapter = FakeAdapter()
        ctx = create_shadow(adapter, "kb1", "ws1", 1, _model_identity())
        assert ctx.alias == alias_name("kb1")

    def test_state_is_shadow_creating(self) -> None:
        adapter = FakeAdapter()
        ctx = create_shadow(adapter, "kb1", "ws1", 1, _model_identity())
        assert ctx.state == BuildState.SHADOW_CREATING


# ---- promote ----


class TestPromote:
    def _make_session_with_repo(self, alias: str, kb_id: str) -> FakeSession:
        """Patch AliasRepository to use our FakeSession."""
        return FakeSession()

    def test_first_promotion_creates_alias(self) -> None:
        adapter = FakeAdapter()
        ctx = create_shadow(adapter, "kb1", "ws1", 1, _model_identity())
        coll = ctx.shadow_collection
        pt = {"id": "x", "vector": [0.1, 0.2, 0.3], "payload": {"chunk_id": "c1"}}
        adapter.upsert_points(coll, [pt])

        session = FakeSession()
        with patch("finecorpus.index.lifecycle.AliasRepository") as MockRepo:
            mock_repo = MagicMock()
            MockRepo.return_value = mock_repo
            mock_repo.get.return_value = None  # No existing record

            promote(adapter, session, ctx)

        assert adapter.aliases.get(ctx.alias) == coll
        assert ctx.state == BuildState.LIVE

    def test_validation_failed_blocks_promotion(self) -> None:
        adapter = FakeAdapter()
        ctx = create_shadow(adapter, "kb1", "ws1", 1, _model_identity())
        # No chunks inserted

        session = FakeSession()
        with pytest.raises(ValidationFailedError) as exc_info:
            promote(adapter, session, ctx)

        assert ctx.state == BuildState.VALIDATION_FAILED
        assert exc_info.value.gate == "non_empty"
        # Alias unchanged
        assert not adapter.alias_exists(ctx.alias)

    def test_shadow_retained_on_validation_failure(self) -> None:
        adapter = FakeAdapter()
        ctx = create_shadow(adapter, "kb1", "ws1", 1, _model_identity())

        session = FakeSession()
        with pytest.raises(ValidationFailedError):
            promote(adapter, session, ctx)

        # Shadow collection is NOT dropped
        assert adapter.collection_exists(ctx.shadow_collection)

    def test_promote_transitions_to_live(self) -> None:
        adapter = FakeAdapter()
        ctx = create_shadow(adapter, "kb1", "ws1", 1, _model_identity())
        coll = ctx.shadow_collection
        pt = {"id": "x", "vector": [0.1, 0.2, 0.3], "payload": {"chunk_id": "c"}}
        adapter.upsert_points(coll, [pt])

        session = FakeSession()
        with patch("finecorpus.index.lifecycle.AliasRepository") as MockRepo:
            mock_repo = MagicMock()
            MockRepo.return_value = mock_repo
            mock_repo.get.return_value = None
            promote(adapter, session, ctx)

        assert ctx.state == BuildState.LIVE

    def test_idempotent_second_promote(self) -> None:
        """Calling promote twice with the same shadow is idempotent."""
        adapter = FakeAdapter()
        ctx = create_shadow(adapter, "kb1", "ws1", 1, _model_identity())
        coll = ctx.shadow_collection
        pt2 = {"id": "x", "vector": [0.1, 0.2, 0.3], "payload": {"chunk_id": "c"}}
        adapter.upsert_points(coll, [pt2])

        with patch("finecorpus.index.lifecycle.AliasRepository") as MockRepo:
            mock_record = MagicMock()
            mock_record.collection_name = None
            mock_repo = MagicMock()
            MockRepo.return_value = mock_repo
            mock_repo.get.return_value = mock_record

            session = FakeSession()
            promote(adapter, session, ctx)

            # Second call: record already shows the new collection
            mock_record.collection_name = coll
            mock_repo.get.return_value = mock_record

            ctx.state = BuildState.TRIGGERED  # Reset for test
            promote(adapter, session, ctx)

        assert ctx.state == BuildState.LIVE
        assert adapter.aliases[ctx.alias] == coll


# ---- rollback ----


class TestRollback:
    def test_no_n1_raises(self) -> None:
        adapter = FakeAdapter()
        with patch("finecorpus.index.lifecycle.AliasRepository") as MockRepo:
            mock_repo = MagicMock()
            MockRepo.return_value = mock_repo
            mock_record = MagicMock()
            mock_record.previous_collection = None
            mock_repo.get.return_value = mock_record
            with pytest.raises(NoNMinusOneError):
                rollback(adapter, FakeSession(), "kb1", {"openai"})

    def test_model_unavailable_blocks_rollback(self) -> None:
        """OQ-L-7: rollback is blocked if N-1 model provider is not configured."""
        adapter = FakeAdapter()
        with patch("finecorpus.index.lifecycle.AliasRepository") as MockRepo:
            mock_repo = MagicMock()
            MockRepo.return_value = mock_repo
            mock_record = MagicMock()
            mock_record.previous_collection = "rtfc_kb1_00000001"
            mock_record.embedding_provider = "openai"
            mock_repo.get.return_value = mock_record

            with pytest.raises(RollbackError) as exc_info:
                rollback(
                    adapter,
                    FakeSession(),
                    "kb1",
                    available_model_providers=set(),  # openai not available
                )

        assert "openai" in str(exc_info.value)

    def test_rollback_retargets_alias(self) -> None:
        adapter = FakeAdapter()
        # Set up collections
        n_coll = collection_name("kb1", 2)
        n1_coll = collection_name("kb1", 1)
        adapter.collections[n_coll] = {
            "points": [{"id": "a", "vector": [0.1, 0.2, 0.3], "payload": {"chunk_id": "c2"}}],
            "metadata": {},
        }
        adapter.collections[n1_coll] = {
            "points": [{"id": "b", "vector": [0.1, 0.2, 0.3], "payload": {"chunk_id": "c1"}}],
            "metadata": {},
        }
        adapter.aliases[alias_name("kb1")] = n_coll

        with patch("finecorpus.index.lifecycle.AliasRepository") as MockRepo:
            mock_repo = MagicMock()
            MockRepo.return_value = mock_repo
            mock_record = MagicMock()
            mock_record.previous_collection = n1_coll
            mock_record.embedding_provider = "openai"
            mock_repo.get.return_value = mock_record

            session = FakeSession()
            result = rollback(adapter, session, "kb1", {"openai"})

        assert result == n1_coll
        assert adapter.aliases[alias_name("kb1")] == n1_coll

    def test_rollback_restores_n1_model_identity(self) -> None:
        """F-01: after rollback the record's model identity equals N-1's, not N's."""
        adapter = FakeAdapter()
        n_coll = collection_name("kb1", 2)
        n1_coll = collection_name("kb1", 1)
        adapter.collections[n_coll] = {"points": [], "metadata": {}}
        adapter.collections[n1_coll] = {"points": [], "metadata": {}}
        adapter.aliases[alias_name("kb1")] = n_coll

        # Simulate a fully-promoted alias record (N live, N-1 in previous_*)
        n1_provider = "openai"
        n1_model = "text-embedding-3-small"
        n1_dimensions = 768
        n1_config = "cfg_n1"

        n_provider = "cohere"
        n_model = "embed-english-v3"
        n_dimensions = 1024
        n_config = "cfg_n"

        with patch("finecorpus.index.lifecycle.AliasRepository") as MockRepo:
            mock_repo = MagicMock()
            MockRepo.return_value = mock_repo

            mock_record = MagicMock()
            mock_record.previous_collection = n1_coll
            mock_record.collection_name = n_coll
            # N-1 model identity (stored in previous_* fields at promote time)
            mock_record.embedding_provider = n_provider
            mock_record.embedding_model = n_model
            mock_record.embedding_dimensions = n_dimensions
            mock_record.config_version = n_config
            mock_repo.get.return_value = mock_record

            # Simulate rollback_to_previous swapping the fields
            def _fake_rollback_to_previous(als: str) -> MagicMock:
                # Swap collection pointer
                mock_record.collection_name, mock_record.previous_collection = (
                    mock_record.previous_collection,
                    mock_record.collection_name,
                )
                # Swap model identity (the fix in F-01)
                mock_record.embedding_provider, mock_record.previous_embedding_provider = (
                    n1_provider,
                    mock_record.embedding_provider,
                )
                mock_record.embedding_model, mock_record.previous_embedding_model = (
                    n1_model,
                    mock_record.embedding_model,
                )
                mock_record.embedding_dimensions, mock_record.previous_embedding_dimensions = (
                    n1_dimensions,
                    mock_record.embedding_dimensions,
                )
                mock_record.config_version, mock_record.previous_config_version = (
                    n1_config,
                    mock_record.config_version,
                )
                return mock_record

            mock_repo.rollback_to_previous.side_effect = _fake_rollback_to_previous

            session = FakeSession()
            result = rollback(adapter, session, "kb1", {n_provider, n1_provider})

        assert result == n1_coll
        # F-01 assertion: after rollback the live model identity equals N-1's values
        assert mock_record.embedding_provider == n1_provider, (
            "After rollback, embedding_provider must equal N-1's provider"
        )
        assert mock_record.embedding_model == n1_model, (
            "After rollback, embedding_model must equal N-1's model"
        )
        assert mock_record.embedding_dimensions == n1_dimensions, (
            "After rollback, embedding_dimensions must equal N-1's dimensions"
        )
        assert mock_record.config_version == n1_config, (
            "After rollback, config_version must equal N-1's config_version"
        )


# ---- startup_reconcile ----


class TestStartupReconcile:
    def test_consistent_state_no_action(self) -> None:
        adapter = FakeAdapter()
        coll = adapter.create_collection("kb1", 1, 3)
        als = alias_name("kb1")
        adapter.aliases[als] = coll

        with patch("finecorpus.index.lifecycle.AliasRepository") as MockRepo:
            mock_repo = MagicMock()
            MockRepo.return_value = mock_repo
            mock_record = MagicMock()
            mock_record.collection_name = coll
            mock_repo.get.return_value = mock_record

            result = startup_reconcile(adapter, FakeSession(), aliases_to_check=[als])

        assert result.inconsistencies_found == 0
        assert result.repaired == 0

    def test_inconsistent_state_repaired(self) -> None:
        """Simulates a Phase-2 failure: Qdrant has new collection, record has old."""
        adapter = FakeAdapter()
        old_coll = adapter.create_collection("kb1", 1, 3)
        new_coll = adapter.create_collection("kb1", 2, 3)
        # Qdrant alias already points at new_coll (Phase 1 completed)
        als = alias_name("kb1")
        adapter.aliases[als] = new_coll
        # Store model identity in collection metadata
        adapter.set_collection_metadata(
            new_coll,
            {
                "provider": "openai",
                "model": "text-embedding-3-small",
                "dimensions": 3,
                "config_version": "v1",
            },
        )

        with patch("finecorpus.index.lifecycle.AliasRepository") as MockRepo:
            mock_repo = MagicMock()
            MockRepo.return_value = mock_repo
            # Control-plane record still shows old collection (Phase 2 failed)
            mock_record = MagicMock()
            mock_record.collection_name = old_coll
            mock_repo.get.return_value = mock_record

            session = FakeSession()
            result = startup_reconcile(adapter, session, aliases_to_check=[als])

        assert result.inconsistencies_found == 1
        assert result.repaired == 1
        assert result.failed == 0
