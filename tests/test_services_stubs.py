"""Tests for Phase 0 service stubs.

Uses FastAPI TestClient — no Docker required.
Each HTTP service exposes GET /healthz -> {"service": <name>, "status": "ok", "version": <ver>}.
"""

import pytest
from fastapi.testclient import TestClient

import finecorpus
from finecorpus.services.control_api import app as control_app
from finecorpus.services.embedding_service import app as embedding_app
from finecorpus.services.retrieval_api import app as retrieval_app


@pytest.fixture()
def retrieval_client() -> TestClient:
    return TestClient(retrieval_app)


@pytest.fixture()
def control_client() -> TestClient:
    return TestClient(control_app)


@pytest.fixture()
def embedding_client() -> TestClient:
    return TestClient(embedding_app)


# ---------------------------------------------------------------------------
# retrieval-api
# ---------------------------------------------------------------------------


class TestRetrievalApiHealthz:
    def test_status_200(self, retrieval_client: TestClient) -> None:
        response = retrieval_client.get("/healthz")
        assert response.status_code == 200

    def test_payload_keys(self, retrieval_client: TestClient) -> None:
        body = retrieval_client.get("/healthz").json()
        assert set(body.keys()) == {"service", "status", "version"}

    def test_service_name(self, retrieval_client: TestClient) -> None:
        body = retrieval_client.get("/healthz").json()
        assert body["service"] == "retrieval-api"

    def test_status_ok(self, retrieval_client: TestClient) -> None:
        body = retrieval_client.get("/healthz").json()
        assert body["status"] == "ok"

    def test_version(self, retrieval_client: TestClient) -> None:
        body = retrieval_client.get("/healthz").json()
        assert body["version"] == finecorpus.__version__


# ---------------------------------------------------------------------------
# control-api
# ---------------------------------------------------------------------------


class TestControlApiHealthz:
    def test_status_200(self, control_client: TestClient) -> None:
        response = control_client.get("/healthz")
        assert response.status_code == 200

    def test_payload_keys(self, control_client: TestClient) -> None:
        body = control_client.get("/healthz").json()
        assert set(body.keys()) == {"service", "status", "version"}

    def test_service_name(self, control_client: TestClient) -> None:
        body = control_client.get("/healthz").json()
        assert body["service"] == "control-api"

    def test_status_ok(self, control_client: TestClient) -> None:
        body = control_client.get("/healthz").json()
        assert body["status"] == "ok"

    def test_version(self, control_client: TestClient) -> None:
        body = control_client.get("/healthz").json()
        assert body["version"] == finecorpus.__version__


# ---------------------------------------------------------------------------
# embedding-service
# ---------------------------------------------------------------------------


class TestEmbeddingServiceHealthz:
    def test_status_200(self, embedding_client: TestClient) -> None:
        response = embedding_client.get("/healthz")
        assert response.status_code == 200

    def test_payload_keys(self, embedding_client: TestClient) -> None:
        body = embedding_client.get("/healthz").json()
        assert set(body.keys()) == {"service", "status", "version"}

    def test_service_name(self, embedding_client: TestClient) -> None:
        body = embedding_client.get("/healthz").json()
        assert body["service"] == "embedding-service"

    def test_status_ok(self, embedding_client: TestClient) -> None:
        body = embedding_client.get("/healthz").json()
        assert body["status"] == "ok"

    def test_version(self, embedding_client: TestClient) -> None:
        body = embedding_client.get("/healthz").json()
        assert body["version"] == finecorpus.__version__


# ---------------------------------------------------------------------------
# ingest-worker (no HTTP server — smoke test the module imports)
# ---------------------------------------------------------------------------


class TestIngestWorkerModule:
    def test_run_callable(self) -> None:
        """run() must be importable and callable (loop itself is not exercised)."""
        from finecorpus.services.ingest_worker import run

        assert callable(run)
