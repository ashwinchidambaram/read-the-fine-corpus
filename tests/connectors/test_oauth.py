"""OAuth2 token refresh against a recorded fixture — no live network."""

from __future__ import annotations

from pathlib import Path

import pytest

from finecorpus.connectors.fixtures import RecordedHTTPClient, UnmatchedRequestError
from finecorpus.connectors.oauth import OAuth2Config, OAuth2Error, OAuth2Flow, OAuth2Token

_FIXTURE = Path(__file__).parent / "fixtures" / "oauth_refresh.json"


def _config() -> OAuth2Config:
    return OAuth2Config(
        authorization_endpoint="https://provider.example/oauth/authorize",
        token_endpoint="https://provider.example/oauth/token",
        client_id="client-123",
        client_secret="secret-should-not-leak",
        redirect_uri="https://app.example/callback",
        scopes=("read.documents", "read.permissions"),
    )


def test_refresh_from_recorded_fixture() -> None:
    http = RecordedHTTPClient.from_file(_FIXTURE)
    flow = OAuth2Flow(_config(), http)
    old = OAuth2Token(access_token="old", refresh_token="old-refresh", expires_at=0.0)

    refreshed = flow.refresh(old)

    assert refreshed.access_token == "new-access-token-abc"
    assert refreshed.refresh_token == "new-refresh-token-xyz"
    assert refreshed.scopes == ("read.documents", "read.permissions")
    # Exactly one call, to the token endpoint — no unexpected network activity.
    assert http.calls == [{"method": "POST", "url": "https://provider.example/oauth/token"}]


def test_ensure_fresh_refreshes_expired_token() -> None:
    http = RecordedHTTPClient.from_file(_FIXTURE)
    flow = OAuth2Flow(_config(), http)
    expired = OAuth2Token(access_token="old", refresh_token="r", expires_at=1.0)
    fresh = flow.ensure_fresh(expired, now=10_000.0)
    assert fresh.access_token == "new-access-token-abc"


def test_ensure_fresh_noop_for_valid_token() -> None:
    http = RecordedHTTPClient([])  # no interactions -> any call would raise
    flow = OAuth2Flow(_config(), http)
    valid = OAuth2Token(access_token="still-good", refresh_token="r", expires_at=10_000.0)
    same = flow.ensure_fresh(valid, now=0.0)
    assert same is valid
    assert http.calls == []  # proves no network call happened


def test_refresh_without_refresh_token_raises() -> None:
    flow = OAuth2Flow(_config(), RecordedHTTPClient([]))
    with pytest.raises(OAuth2Error, match="no refresh_token"):
        flow.refresh(OAuth2Token(access_token="a", refresh_token=None))


def test_token_and_config_repr_redact_secrets() -> None:
    cfg = _config()
    assert "secret-should-not-leak" not in repr(cfg)
    assert "REDACTED" in repr(cfg)
    tok = OAuth2Token(access_token="super-secret-token", refresh_token="r")
    assert "super-secret-token" not in repr(tok)
    assert "REDACTED" in repr(tok)


def test_unmatched_request_raises_no_network() -> None:
    http = RecordedHTTPClient([])
    flow = OAuth2Flow(_config(), http)
    with pytest.raises(UnmatchedRequestError):
        flow.exchange_code("some-code")
