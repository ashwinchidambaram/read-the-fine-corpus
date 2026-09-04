"""API tests for finecorpus.services.retrieval_api.

Tests the FastAPI application via TestClient (no live services required).
Verifies HTTP status codes, response schema matches the contract, and
the /status endpoint.

The test strategy patches the module-level singletons (_provider, _adapter,
_session_factory) via the app's dependency-override pattern, or by direct
attribute injection into the retrieval_api module for the simple cases.
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import finecorpus
from finecorpus.embedding.cache import QueryEmbeddingCache
from finecorpus.embedding.fake import FakeProvider
from finecorpus.index.adapter import alias_name

from .helpers import (
    FakeAdapter,
    FakeAliasRecord,
    FakeAliasRepository,
    make_alias_record,
    make_chunk_payload,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

KB_ID = "kb-api-test"
ALIAS = alias_name(KB_ID)
COLL = f"rtfc_{KB_ID.replace('-', '').lower()}_00000001"
MODEL_ID = "fake-embed-v1"
DIMENSIONS = 64

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def provider() -> FakeProvider:
    return FakeProvider(dimensions=DIMENSIONS, model_id=MODEL_ID)


@pytest.fixture()
def alias_record() -> FakeAliasRecord:
    return make_alias_record(KB_ID, model_id=MODEL_ID, dimensions=DIMENSIONS)


@pytest.fixture()
def adapter_with_chunks(alias_record: FakeAliasRecord) -> FakeAdapter:
    adapter = FakeAdapter()
    adapter.seed_collection(
        alias=ALIAS,
        coll=COLL,
        points=[
            make_chunk_payload(
                chunk_id="chk_api_001",
                text="Policy chunk text.",
                kb_id=KB_ID,
                score=0.88,
            ),
        ],
    )
    return adapter


@pytest.fixture()
def fresh_cache() -> QueryEmbeddingCache:
    return QueryEmbeddingCache(ttl_seconds=3600, max_entries=128, enabled=True)


@contextmanager
def _api_client(
    provider: Any,
    adapter: Any,
    alias_record: FakeAliasRecord | None,
    cache: QueryEmbeddingCache,
) -> Generator[TestClient, None, None]:
    """Build a TestClient with all module-level singletons patched."""
    import finecorpus.services.retrieval_api as api_mod

    records = {alias_record.alias: alias_record} if alias_record else {}
    fake_repo = FakeAliasRepository(records)

    @contextmanager
    def fake_session_factory() -> Generator:
        yield object()

    with (
        patch.object(api_mod, "_provider", provider),
        patch.object(api_mod, "_adapter", adapter),
        patch.object(api_mod, "_session_factory", fake_session_factory),
        patch.object(api_mod, "get_query_cache", return_value=cache),
        patch(
            "finecorpus.retrieval.service.AliasRepository",
            return_value=fake_repo,
        ),
    ):
        from finecorpus.services.retrieval_api import app

        with TestClient(app) as client:
            yield client


# ---------------------------------------------------------------------------
# /healthz
# ---------------------------------------------------------------------------


class TestHealthz:
    def test_status_200(self) -> None:
        from finecorpus.services.retrieval_api import app

        with TestClient(app) as client:
            response = client.get("/healthz")
        assert response.status_code == 200

    def test_payload_keys(self) -> None:
        from finecorpus.services.retrieval_api import app

        with TestClient(app) as client:
            body = client.get("/healthz").json()
        assert set(body.keys()) == {"service", "status", "version"}

    def test_service_name(self) -> None:
        from finecorpus.services.retrieval_api import app

        with TestClient(app) as client:
            body = client.get("/healthz").json()
        assert body["service"] == "retrieval-api"

    def test_version(self) -> None:
        from finecorpus.services.retrieval_api import app

        with TestClient(app) as client:
            body = client.get("/healthz").json()
        assert body["version"] == finecorpus.__version__


# ---------------------------------------------------------------------------
# POST /v1/kb/{kb_id}/query — HTTP status codes
# ---------------------------------------------------------------------------


class TestQueryStatusCodes:
    def test_200_on_matches(
        self,
        provider: FakeProvider,
        adapter_with_chunks: FakeAdapter,
        alias_record: FakeAliasRecord,
        fresh_cache: QueryEmbeddingCache,
    ) -> None:
        with _api_client(provider, adapter_with_chunks, alias_record, fresh_cache) as client:
            response = client.post(
                f"/v1/kb/{KB_ID}/query",
                json={"query": "policy"},
            )
        assert response.status_code == 200

    def test_200_on_no_matches(
        self,
        provider: FakeProvider,
        alias_record: FakeAliasRecord,
        fresh_cache: QueryEmbeddingCache,
    ) -> None:
        adapter = FakeAdapter()
        adapter.seed_collection(alias=ALIAS, coll=COLL, points=[])

        with _api_client(provider, adapter, alias_record, fresh_cache) as client:
            response = client.post(
                f"/v1/kb/{KB_ID}/query",
                json={"query": "anything"},
            )
        assert response.status_code == 200
        body = response.json()
        assert body["result_status"] == "no_matches"

    def test_200_on_filtered_to_zero(
        self,
        provider: FakeProvider,
        adapter_with_chunks: FakeAdapter,
        alias_record: FakeAliasRecord,
        fresh_cache: QueryEmbeddingCache,
    ) -> None:
        with _api_client(provider, adapter_with_chunks, alias_record, fresh_cache) as client:
            response = client.post(
                f"/v1/kb/{KB_ID}/query",
                json={"query": "policy", "score_threshold": 0.9999},
            )
        assert response.status_code == 200
        body = response.json()
        assert body["result_status"] == "filtered_to_zero"

    def test_409_on_model_mismatch(
        self,
        adapter_with_chunks: FakeAdapter,
        fresh_cache: QueryEmbeddingCache,
    ) -> None:
        # Provider model != alias record model
        provider = FakeProvider(dimensions=DIMENSIONS, model_id=MODEL_ID)
        bad_record = make_alias_record(KB_ID, model_id="different-model", dimensions=128)

        with _api_client(provider, adapter_with_chunks, bad_record, fresh_cache) as client:
            response = client.post(
                f"/v1/kb/{KB_ID}/query",
                json={"query": "test"},
            )
        assert response.status_code == 409

    def test_503_on_provider_unavailable(
        self,
        adapter_with_chunks: FakeAdapter,
        alias_record: FakeAliasRecord,
        fresh_cache: QueryEmbeddingCache,
    ) -> None:
        failing_provider = FakeProvider(
            dimensions=DIMENSIONS, model_id=MODEL_ID, fail_on_embed=True
        )
        with _api_client(
            failing_provider, adapter_with_chunks, alias_record, fresh_cache
        ) as client:
            response = client.post(
                f"/v1/kb/{KB_ID}/query",
                json={"query": "test"},
            )
        assert response.status_code == 503

    def test_503_on_vector_db_unavailable(
        self,
        provider: FakeProvider,
        alias_record: FakeAliasRecord,
        fresh_cache: QueryEmbeddingCache,
    ) -> None:
        failing_adapter = FakeAdapter(fail_on_search=True)
        # Must have alias seeded so mismatch check passes (adapter.search raises later)
        failing_adapter.seed_collection(alias=ALIAS, coll=COLL, points=[])

        with _api_client(provider, failing_adapter, alias_record, fresh_cache) as client:
            response = client.post(
                f"/v1/kb/{KB_ID}/query",
                json={"query": "test"},
            )
        assert response.status_code == 503

    def test_404_on_kb_not_ready(
        self,
        provider: FakeProvider,
        adapter_with_chunks: FakeAdapter,
        fresh_cache: QueryEmbeddingCache,
    ) -> None:
        # No alias record → KB_NOT_READY → 404
        with _api_client(provider, adapter_with_chunks, None, fresh_cache) as client:
            response = client.post(
                f"/v1/kb/{KB_ID}/query",
                json={"query": "test"},
            )
        assert response.status_code == 404

    def test_422_on_empty_query(
        self,
        provider: FakeProvider,
        adapter_with_chunks: FakeAdapter,
        alias_record: FakeAliasRecord,
        fresh_cache: QueryEmbeddingCache,
    ) -> None:
        with _api_client(provider, adapter_with_chunks, alias_record, fresh_cache) as client:
            response = client.post(
                f"/v1/kb/{KB_ID}/query",
                json={"query": ""},  # min_length=1
            )
        assert response.status_code == 422

    def test_422_on_top_k_out_of_range(
        self,
        provider: FakeProvider,
        adapter_with_chunks: FakeAdapter,
        alias_record: FakeAliasRecord,
        fresh_cache: QueryEmbeddingCache,
    ) -> None:
        with _api_client(provider, adapter_with_chunks, alias_record, fresh_cache) as client:
            response = client.post(
                f"/v1/kb/{KB_ID}/query",
                json={"query": "test", "top_k": 0},  # ge=1
            )
        assert response.status_code == 422


# ---------------------------------------------------------------------------
# POST /v1/kb/{kb_id}/query — response schema
# ---------------------------------------------------------------------------


class TestQueryResponseSchema:
    def test_schema_version_present(
        self,
        provider: FakeProvider,
        adapter_with_chunks: FakeAdapter,
        alias_record: FakeAliasRecord,
        fresh_cache: QueryEmbeddingCache,
    ) -> None:
        with _api_client(provider, adapter_with_chunks, alias_record, fresh_cache) as client:
            body = client.post(f"/v1/kb/{KB_ID}/query", json={"query": "policy"}).json()
        assert "schema_version" in body
        assert body["schema_version"]

    def test_result_status_present(
        self,
        provider: FakeProvider,
        adapter_with_chunks: FakeAdapter,
        alias_record: FakeAliasRecord,
        fresh_cache: QueryEmbeddingCache,
    ) -> None:
        with _api_client(provider, adapter_with_chunks, alias_record, fresh_cache) as client:
            body = client.post(f"/v1/kb/{KB_ID}/query", json={"query": "policy"}).json()
        assert body["result_status"] in {"matches", "no_matches", "filtered_to_zero", "error"}

    def test_request_echo_present(
        self,
        provider: FakeProvider,
        adapter_with_chunks: FakeAdapter,
        alias_record: FakeAliasRecord,
        fresh_cache: QueryEmbeddingCache,
    ) -> None:
        with _api_client(provider, adapter_with_chunks, alias_record, fresh_cache) as client:
            body = client.post(f"/v1/kb/{KB_ID}/query", json={"query": "policy"}).json()
        assert "request_echo" in body
        assert body["request_echo"]["query"] == "policy"
        assert isinstance(body["request_echo"]["filters_applied"], list)

    def test_results_have_trust_label(
        self,
        provider: FakeProvider,
        adapter_with_chunks: FakeAdapter,
        alias_record: FakeAliasRecord,
        fresh_cache: QueryEmbeddingCache,
    ) -> None:
        with _api_client(provider, adapter_with_chunks, alias_record, fresh_cache) as client:
            body = client.post(f"/v1/kb/{KB_ID}/query", json={"query": "policy"}).json()
        if body["result_status"] == "matches":
            for result in body["results"]:
                assert result["trust_level"] == "untrusted_ingested"

    def test_results_have_provenance(
        self,
        provider: FakeProvider,
        adapter_with_chunks: FakeAdapter,
        alias_record: FakeAliasRecord,
        fresh_cache: QueryEmbeddingCache,
    ) -> None:
        with _api_client(provider, adapter_with_chunks, alias_record, fresh_cache) as client:
            body = client.post(f"/v1/kb/{KB_ID}/query", json={"query": "policy"}).json()
        if body["result_status"] == "matches":
            for result in body["results"]:
                assert "provenance" in result
                prov = result["provenance"]
                assert "source_document_id" in prov
                assert "source_location" in prov
                assert "segment_type" in prov

    def test_results_have_chunk_id_and_text(
        self,
        provider: FakeProvider,
        adapter_with_chunks: FakeAdapter,
        alias_record: FakeAliasRecord,
        fresh_cache: QueryEmbeddingCache,
    ) -> None:
        with _api_client(provider, adapter_with_chunks, alias_record, fresh_cache) as client:
            body = client.post(f"/v1/kb/{KB_ID}/query", json={"query": "policy"}).json()
        if body["result_status"] == "matches":
            for result in body["results"]:
                assert "chunk_id" in result
                assert "text" in result

    def test_top_k_default(
        self,
        provider: FakeProvider,
        alias_record: FakeAliasRecord,
        fresh_cache: QueryEmbeddingCache,
    ) -> None:
        # Seed 20 chunks
        adapter = FakeAdapter()
        adapter.seed_collection(
            alias=ALIAS,
            coll=COLL,
            points=[
                make_chunk_payload(chunk_id=f"chk_{i:03d}", score=0.9, kb_id=KB_ID)
                for i in range(20)
            ],
        )
        with _api_client(provider, adapter, alias_record, fresh_cache) as client:
            body = client.post(f"/v1/kb/{KB_ID}/query", json={"query": "test"}).json()
        # Default top_k=10; we seeded 20 so should get 10
        assert len(body.get("results", [])) <= 10

    def test_validates_against_retrieval_response_contract(
        self,
        provider: FakeProvider,
        adapter_with_chunks: FakeAdapter,
        alias_record: FakeAliasRecord,
        fresh_cache: QueryEmbeddingCache,
    ) -> None:
        """The response must be parseable as a RetrievalResponse."""
        from finecorpus.contracts.retrieval_response import RetrievalResponse

        with _api_client(provider, adapter_with_chunks, alias_record, fresh_cache) as client:
            body = client.post(f"/v1/kb/{KB_ID}/query", json={"query": "policy"}).json()
        # Should not raise
        parsed = RetrievalResponse.model_validate(body)
        assert parsed.result_status is not None


# ---------------------------------------------------------------------------
# GET /v1/kb/{kb_id}/status
# ---------------------------------------------------------------------------


class TestKBStatus:
    def test_200_when_ready(
        self,
        provider: FakeProvider,
        adapter_with_chunks: FakeAdapter,
        alias_record: FakeAliasRecord,
        fresh_cache: QueryEmbeddingCache,
    ) -> None:
        with _api_client(provider, adapter_with_chunks, alias_record, fresh_cache) as client:
            response = client.get(f"/v1/kb/{KB_ID}/status")
        assert response.status_code == 200

    def test_status_body_shape(
        self,
        provider: FakeProvider,
        adapter_with_chunks: FakeAdapter,
        alias_record: FakeAliasRecord,
        fresh_cache: QueryEmbeddingCache,
    ) -> None:
        with _api_client(provider, adapter_with_chunks, alias_record, fresh_cache) as client:
            body = client.get(f"/v1/kb/{KB_ID}/status").json()
        assert "kb_id" in body
        assert "alias" in body
        assert "ready" in body
        assert "embedding_model" in body
        assert "embedding_dimensions" in body

    def test_status_ready_when_promoted(
        self,
        provider: FakeProvider,
        adapter_with_chunks: FakeAdapter,
        alias_record: FakeAliasRecord,
        fresh_cache: QueryEmbeddingCache,
    ) -> None:
        with _api_client(provider, adapter_with_chunks, alias_record, fresh_cache) as client:
            body = client.get(f"/v1/kb/{KB_ID}/status").json()
        assert body["ready"] is True

    def test_status_not_ready_when_no_collection(
        self,
        provider: FakeProvider,
        adapter_with_chunks: FakeAdapter,
        fresh_cache: QueryEmbeddingCache,
    ) -> None:
        unpromoted = FakeAliasRecord(alias=ALIAS, kb_id=KB_ID, collection=None)
        with _api_client(provider, adapter_with_chunks, unpromoted, fresh_cache) as client:
            body = client.get(f"/v1/kb/{KB_ID}/status").json()
        assert body["ready"] is False

    def test_404_when_kb_not_registered(
        self,
        provider: FakeProvider,
        adapter_with_chunks: FakeAdapter,
        fresh_cache: QueryEmbeddingCache,
    ) -> None:
        with _api_client(provider, adapter_with_chunks, None, fresh_cache) as client:
            response = client.get(f"/v1/kb/{KB_ID}/status")
        assert response.status_code == 404

    def test_model_identity_in_status(
        self,
        provider: FakeProvider,
        adapter_with_chunks: FakeAdapter,
        alias_record: FakeAliasRecord,
        fresh_cache: QueryEmbeddingCache,
    ) -> None:
        with _api_client(provider, adapter_with_chunks, alias_record, fresh_cache) as client:
            body = client.get(f"/v1/kb/{KB_ID}/status").json()
        assert body["embedding_model"] == MODEL_ID
        assert body["embedding_dimensions"] == DIMENSIONS
        assert body["embedding_provider"] == "fake"

    def test_no_secrets_in_status(
        self,
        provider: FakeProvider,
        adapter_with_chunks: FakeAdapter,
        alias_record: FakeAliasRecord,
        fresh_cache: QueryEmbeddingCache,
    ) -> None:
        """Status endpoint must not expose any credential material (§14.2)."""
        with _api_client(provider, adapter_with_chunks, alias_record, fresh_cache) as client:
            body = client.get(f"/v1/kb/{KB_ID}/status").json()
        # Ensure no suspicious keys
        body_str = str(body).lower()
        for forbidden in ("key", "secret", "token", "password", "credential"):
            assert forbidden not in body_str, (
                f"Status response contains suspicious field containing '{forbidden}'"
            )
