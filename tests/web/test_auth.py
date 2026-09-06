"""Unit tests for the web auth foundation (Phase 6, WU-A)."""

from __future__ import annotations

import time

from finecorpus.web.auth import (
    LocalAccountsAuthProvider,
    Session,
    hash_password,
    verify_password,
)
from finecorpus.web.sessions import InMemorySessionStore


def test_hash_password_never_stores_plaintext() -> None:
    stored = hash_password("hunter2")
    assert "hunter2" not in stored
    assert stored.startswith("scrypt$")
    # Two hashes of the same password differ (random salt).
    assert hash_password("hunter2") != stored


def test_verify_password_correct_and_incorrect() -> None:
    stored = hash_password("correct horse")
    assert verify_password("correct horse", stored) is True
    assert verify_password("wrong", stored) is False


def test_verify_password_malformed_fails_closed() -> None:
    assert verify_password("x", "not-a-valid-hash") is False
    assert verify_password("x", "") is False
    assert verify_password("x", "argon2$bogus") is False


def test_session_repr_hides_token() -> None:
    session = Session(token="topsecret", username="a", created_at=0.0, expires_at=1e12)
    assert "topsecret" not in repr(session)


def test_authenticate_success_creates_session() -> None:
    provider = LocalAccountsAuthProvider()
    provider.add_account("alice", "s3cret")
    session = provider.authenticate("alice", "s3cret")
    assert session is not None
    assert session.username == "alice"
    assert provider.get_session(session.token) is not None


def test_authenticate_wrong_password_returns_none() -> None:
    provider = LocalAccountsAuthProvider()
    provider.add_account("alice", "s3cret")
    assert provider.authenticate("alice", "nope") is None


def test_authenticate_unknown_user_returns_none() -> None:
    provider = LocalAccountsAuthProvider()
    assert provider.authenticate("ghost", "whatever") is None


def test_get_session_none_and_unknown_fail_closed() -> None:
    provider = LocalAccountsAuthProvider()
    assert provider.get_session(None) is None
    assert provider.get_session("") is None
    assert provider.get_session("bogus-token") is None


def test_logout_invalidates_session() -> None:
    provider = LocalAccountsAuthProvider()
    provider.add_account("alice", "s3cret")
    session = provider.authenticate("alice", "s3cret")
    assert session is not None
    provider.logout(session.token)
    assert provider.get_session(session.token) is None


def test_logout_unknown_token_is_noop() -> None:
    provider = LocalAccountsAuthProvider()
    provider.logout(None)
    provider.logout("nope")  # no exception


def test_expired_session_fails_closed() -> None:
    store = InMemorySessionStore()
    provider = LocalAccountsAuthProvider(session_store=store, session_ttl_seconds=60)
    provider.add_account("alice", "s3cret")
    session = provider.authenticate("alice", "s3cret")
    assert session is not None
    # Manually expire by inserting an already-expired session.
    expired = Session(
        token=session.token,
        username="alice",
        created_at=time.time() - 1000,
        expires_at=time.time() - 1,
    )
    store.save(expired)
    assert provider.get_session(session.token) is None
