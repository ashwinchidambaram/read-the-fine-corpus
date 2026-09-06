"""Security regression: break-glass routes must derive admin identity from a
validated admin API key — never from a client-set X-Admin-ID header
(spoofable-field auth bypass, CRITICAL)."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from finecorpus.control.auth import ApiKeyRepository, Role, ScopeKind
from finecorpus.control.metadata import create_tables
from finecorpus.services.control_routes import break_glass as bg_routes


def _app_with_db() -> tuple[FastAPI, Any, str]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    create_tables(engine)

    @contextmanager
    def factory():
        s = Session(engine)
        try:
            yield s
            s.commit()
        finally:
            s.close()

    with factory() as s:
        repo = ApiKeyRepository(s)
        plaintext, _ = repo.issue(
            principal_name="root-admin",
            role=Role.admin,
            scope_kind=ScopeKind.global_,
            created_by="test",
        )

    bg_routes._session_factory = factory  # noqa: SLF001
    app = FastAPI()
    app.include_router(bg_routes.router)
    return app, factory, plaintext


def test_spoofed_admin_header_rejected_without_key() -> None:
    app, _, _ = _app_with_db()
    client = TestClient(app)
    resp = client.post(
        "/admin/break-glass/grant",
        json={"target_kb_id": "kb-x", "reason": "spoof attempt"},
        headers={"X-Admin-ID": "i-am-totally-an-admin"},
    )
    assert resp.status_code in (401, 403), (
        "client-set X-Admin-ID must never authenticate a break-glass grant"
    )


def test_valid_admin_key_grants_and_identity_is_server_derived() -> None:
    app, factory, plaintext = _app_with_db()
    client = TestClient(app)
    resp = client.post(
        "/admin/break-glass/grant",
        json={"target_kb_id": "kb-x", "reason": "incident triage"},
        headers={"X-API-Key": plaintext, "X-Admin-ID": "spoofed-name-ignored"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    granting = body.get("granting_admin_id", "")
    assert "spoofed" not in granting, (
        "granting_admin_id must be server-derived from the validated key"
    )


def test_list_and_revoke_roundtrip_no_detached_instance() -> None:
    app, _, plaintext = _app_with_db()
    client = TestClient(app)
    hdr = {"X-API-Key": plaintext}
    g = client.post(
        "/admin/break-glass/grant",
        json={"target_kb_id": "kb-x", "reason": "incident triage"},
        headers=hdr,
    )
    assert g.status_code == 200, g.text
    grant_id = g.json()["grant_id"]

    active = client.get("/admin/break-glass/active", headers=hdr)
    assert active.status_code == 200, active.text
    assert any(x["grant_id"] == grant_id for x in active.json()["grants"])

    rev = client.request("DELETE", f"/admin/break-glass/{grant_id}", headers=hdr)
    assert rev.status_code == 200, rev.text
    assert rev.json()["revoked"] is True

    active2 = client.get("/admin/break-glass/active", headers=hdr)
    assert not any(x["grant_id"] == grant_id for x in active2.json()["grants"])
