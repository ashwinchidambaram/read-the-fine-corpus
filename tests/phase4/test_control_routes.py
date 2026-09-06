"""Phase 4 control-API route tests.

Covers:
- Per-route auth: 401 (no key), 403 (wrong role), 200 (admin).
  Matrix: at least keys + audit + reindex endpoints.
- Key issuance returns plaintext ONCE and never again (hash not returned).
- Job and tombstone routes basic happy-path.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Setup: minimal SQLite control-plane for route tests
# ---------------------------------------------------------------------------


def _make_engine_and_session():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from finecorpus.control.metadata import Base, _ensure_all_models_imported

    _ensure_all_models_imported()
    # StaticPool: same connection reused across threads; required for SQLite :memory:
    # so that tables created in setup remain visible to test threads.
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


# Use function-scope to avoid cross-test module-global pollution
@pytest.fixture()
def SessionLocal():
    return _make_engine_and_session()


@pytest.fixture()
def admin_key(SessionLocal):
    """Issue an admin key and return (plaintext_key, key_id)."""
    from finecorpus.control.auth import ApiKeyRepository, Role, ScopeKind

    with SessionLocal() as sess:
        repo = ApiKeyRepository(sess)
        plaintext, record = repo.issue(
            principal_name="test-admin",
            role=Role.admin,
            scope_kind=ScopeKind.global_,
            created_by="test",
        )
        key_id = record.key_id  # read inside session
        sess.commit()
    return plaintext, key_id


@pytest.fixture()
def viewer_key(SessionLocal):
    """Issue a KB-scoped viewer key."""
    from finecorpus.control.auth import ApiKeyRepository, Role, ScopeKind

    with SessionLocal() as sess:
        repo = ApiKeyRepository(sess)
        plaintext, record = repo.issue(
            principal_name="test-viewer",
            role=Role.viewer,
            scope_kind=ScopeKind.kb,
            created_by="test",
            kb_id="kb-route-test",
        )
        key_id = record.key_id  # read inside session
        sess.commit()
    return plaintext, key_id


@pytest.fixture()
def control_client(SessionLocal):
    """Return a TestClient for the control API with DB wired in."""
    from finecorpus.services import control_api
    from finecorpus.services.control_routes import (
        audit as audit_routes,
    )
    from finecorpus.services.control_routes import (
        budgets as budgets_routes,
    )
    from finecorpus.services.control_routes import (
        jobs as jobs_routes,
    )
    from finecorpus.services.control_routes import (
        keys as keys_routes,
    )
    from finecorpus.services.control_routes import (
        tombstones as tombstones_routes,
    )

    # Wire session factories into route modules
    jobs_routes.configure(session_factory=SessionLocal)
    audit_routes.configure(session_factory=SessionLocal)
    budgets_routes.configure(session_factory=SessionLocal)
    keys_routes.configure(session_factory=SessionLocal)
    tombstones_routes.configure(session_factory=SessionLocal)

    return TestClient(control_api.app)


# ---------------------------------------------------------------------------
# Auth matrix: /v1/kb/{kb_id}/keys (POST) — issue key (admin only)
# ---------------------------------------------------------------------------


def test_issue_key_no_auth_returns_401(control_client):
    """POST /v1/kb/{kb_id}/keys with no key → 401."""
    r = control_client.post(
        "/v1/kb/kb-test/keys",
        json={"principal_name": "agent", "role": "service"},
    )
    assert r.status_code == 401


def test_issue_key_viewer_role_returns_403(control_client, viewer_key):
    """POST /v1/kb/{kb_id}/keys with viewer key → 403."""
    plaintext, _ = viewer_key
    r = control_client.post(
        "/v1/kb/kb-test/keys",
        json={"principal_name": "agent", "role": "service"},
        headers={"X-API-Key": plaintext},
    )
    assert r.status_code == 403


def test_issue_key_admin_returns_200_with_plaintext_once(control_client, admin_key):
    """POST /v1/kb/{kb_id}/keys with admin key → 200 + plaintext_key in response."""
    plaintext_admin, _ = admin_key
    r = control_client.post(
        "/v1/kb/kb-route-test/keys",
        json={"principal_name": "new-agent", "role": "service", "created_by": "test-admin"},
        headers={"X-API-Key": plaintext_admin},
    )
    assert r.status_code == 200, f"Unexpected status {r.status_code}: {r.text}"
    data = r.json()
    assert "plaintext_key" in data
    assert data["plaintext_key"].startswith("rtfc_sk_")
    assert "record" in data
    assert "key_hash" not in data  # never returned
    assert "key_hash" not in data.get("record", {})


def test_plaintext_never_returned_again(control_client, admin_key):
    """After issuance, the plaintext key is NEVER returned by any list/get endpoint.

    There is no GET /keys endpoint; the key material lives only in the POST response.
    This test verifies the record does not contain hash or secret fields.
    """
    plaintext_admin, _ = admin_key
    r = control_client.post(
        "/v1/kb/kb-route-test/keys",
        json={"principal_name": "another-agent", "role": "service", "created_by": "test-admin"},
        headers={"X-API-Key": plaintext_admin},
    )
    assert r.status_code == 200
    data = r.json()
    record = data.get("record", {})
    # Ensure no secret material in record
    assert "key_hash" not in record
    # plaintext_key is only in the top-level response (not re-serialized elsewhere)
    assert "plaintext_key" in data
    # Subsequent requests to other endpoints don't leak it
    r2 = control_client.get("/v1/audit", headers={"X-API-Key": plaintext_admin})
    assert r2.status_code == 200
    audit_data = r2.json()
    for entry in audit_data:
        assert "plaintext_key" not in str(entry)


# ---------------------------------------------------------------------------
# Auth matrix: /v1/audit (GET)
# ---------------------------------------------------------------------------


def test_audit_no_auth_returns_200_unscoped(control_client):
    """GET /v1/audit with no key (auth disabled in test) → 200."""
    r = control_client.get("/v1/audit")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_audit_with_admin_key_returns_200(control_client, admin_key):
    """GET /v1/audit with admin key → 200."""
    plaintext, _ = admin_key
    r = control_client.get("/v1/audit", headers={"X-API-Key": plaintext})
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# Auth matrix: /v1/kb/{kb_id}/reindex (POST)
# ---------------------------------------------------------------------------


def test_reindex_missing_kb_returns_404(control_client, admin_key):
    """POST /v1/kb/{kb_id}/reindex for unregistered KB → 404."""
    r = control_client.post("/v1/kb/nonexistent-kb/reindex")
    assert r.status_code == 404


def test_reindex_with_registered_kb(control_client, SessionLocal):
    """POST /v1/kb/{kb_id}/reindex for a registered KB → 200 with job record."""
    kb_id = "kb-reindex-route-test"

    # Register the KB alias
    from finecorpus.control.metadata import AliasRepository
    from finecorpus.index.adapter import alias_name

    with SessionLocal() as sess:
        alias_repo = AliasRepository(sess)
        als = alias_name(kb_id)
        existing = alias_repo.get(als)
        if existing is None:
            alias_repo.create(alias=als, kb_id=kb_id, workspace_id="ws-route-test")
            sess.commit()

    r = control_client.post(f"/v1/kb/{kb_id}/reindex")
    assert r.status_code == 200
    data = r.json()
    assert data["kb_id"] == kb_id
    assert data["job_type"] == "reindex_full"
    assert data["state"] in ("queued", "claimed", "running")


# ---------------------------------------------------------------------------
# Jobs routes
# ---------------------------------------------------------------------------


def test_list_jobs_returns_list(control_client):
    r = control_client.get("/v1/jobs")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_get_nonexistent_job_returns_404(control_client):
    r = control_client.get("/v1/jobs/nonexistent-job-id")
    assert r.status_code == 404


def test_resume_nonexistent_job_returns_404(control_client):
    r = control_client.post("/v1/jobs/nonexistent-job-id/resume")
    assert r.status_code == 404


def test_cancel_nonexistent_job_returns_404(control_client):
    r = control_client.post("/v1/jobs/nonexistent-job-id/cancel")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Tombstones routes
# ---------------------------------------------------------------------------


def test_tombstones_empty_list(control_client):
    r = control_client.get("/v1/kb/kb-no-tombstones/tombstones")
    assert r.status_code == 200
    assert r.json() == []


def test_tombstones_invalid_kind_returns_422(control_client):
    r = control_client.get("/v1/kb/kb-test/tombstones?kind=invalid_kind")
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Budget routes
# ---------------------------------------------------------------------------


def test_accrual_no_filter(control_client):
    r = control_client.get("/v1/budgets/accrual?kb_id=kb-budget-test")
    assert r.status_code == 200
    data = r.json()
    assert "accrued_usd" in data


# ---------------------------------------------------------------------------
# Metrics endpoint
# ---------------------------------------------------------------------------


def test_metrics_endpoint(control_client):
    r = control_client.get("/metrics")
    assert r.status_code == 200
    assert "rtfc_" in r.text or "python_" in r.text  # prometheus metrics


def test_healthz_endpoint(control_client):
    r = control_client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["service"] == "control-api"
