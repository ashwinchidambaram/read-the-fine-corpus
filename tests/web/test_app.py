"""Integration tests for the web app factory (Phase 6, WU-A).

Uses FastAPI's TestClient. Verifies the fail-closed auth contract end-to-end.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from finecorpus.web import create_app
from finecorpus.web.auth import LocalAccountsAuthProvider


@pytest.fixture
def provider() -> LocalAccountsAuthProvider:
    p = LocalAccountsAuthProvider()
    p.add_account("alice", "s3cret")
    return p


@pytest.fixture
def client(provider: LocalAccountsAuthProvider) -> TestClient:
    app = create_app(config=None, auth_provider=provider)
    # follow_redirects=False so we can assert redirect behaviour explicitly.
    return TestClient(app, follow_redirects=False)


def test_app_factory_boots() -> None:
    app = create_app()
    assert app is not None
    assert app.title == "finecorpus-web"


def test_healthz_is_public(client: TestClient) -> None:
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_protected_route_denied_when_unauthenticated(client: TestClient) -> None:
    resp = client.get("/")
    assert resp.status_code == 401
    # Fail-closed: no protected content served.
    assert "Signed in as" not in resp.text


def test_session_cookie_secure_follows_config(provider: LocalAccountsAuthProvider) -> None:
    """The session cookie's Secure flag is driven by config.web.cookie_secure.

    Default (config=None) → no Secure (dev/HTTP); cookie_secure=True → Secure set
    so the session cookie is never sent over plaintext HTTP in a TLS deployment.
    """
    from finecorpus.config.models import Config  # noqa: PLC0415

    default_client = TestClient(
        create_app(config=None, auth_provider=provider), follow_redirects=False
    )
    r_default = default_client.post("/login", data={"username": "alice", "password": "s3cret"})
    assert "secure" not in r_default.headers["set-cookie"].lower()

    cfg = Config()
    cfg.web.cookie_secure = True
    secure_client = TestClient(
        create_app(config=cfg, auth_provider=provider), follow_redirects=False
    )
    r_secure = secure_client.post("/login", data={"username": "alice", "password": "s3cret"})
    assert "secure" in r_secure.headers["set-cookie"].lower()


def test_valid_login_creates_session_and_grants_access(client: TestClient) -> None:
    resp = client.post("/login", data={"username": "alice", "password": "s3cret"})
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"
    assert "rtfc_session" in resp.cookies
    # The session cookie is now stored on the client; access the landing page.
    landing = client.get("/")
    assert landing.status_code == 200
    assert "alice" in landing.text


def test_invalid_password_rejected(client: TestClient) -> None:
    resp = client.post("/login", data={"username": "alice", "password": "wrong"})
    assert resp.status_code == 401
    assert "Invalid username or password" in resp.text
    assert "rtfc_session" not in resp.cookies


def test_logout_invalidates_session(client: TestClient) -> None:
    client.post("/login", data={"username": "alice", "password": "s3cret"})
    assert client.get("/").status_code == 200
    logout = client.post("/logout")
    assert logout.status_code == 303
    assert logout.headers["location"] == "/login"
    # After logout the session is gone — protected route is denied again.
    assert client.get("/").status_code == 401


def test_forged_cookie_fails_closed(client: TestClient) -> None:
    client.cookies.set("rtfc_session", "totally-made-up-token")
    resp = client.get("/")
    assert resp.status_code == 401


def test_login_page_is_public(client: TestClient) -> None:
    resp = client.get("/login")
    assert resp.status_code == 200
    assert "Sign in" in resp.text


def test_password_never_echoed_on_failure(client: TestClient) -> None:
    resp = client.post("/login", data={"username": "alice", "password": "supersecretpw"})
    assert "supersecretpw" not in resp.text
