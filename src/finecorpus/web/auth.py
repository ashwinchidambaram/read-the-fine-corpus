"""Pluggable authentication for the web application (Phase 6, WU-A).

This module defines the ``AuthProvider`` protocol — the single seam through
which the web app authenticates users and validates sessions — and a default
``LocalAccountsAuthProvider`` backed by local username/password accounts.

Design principles
-----------------
- **Fail-closed.** ``get_session()`` returns ``None`` for any token that is
  absent, malformed, expired, or revoked. Routes that require auth must treat
  ``None`` as "deny". No path silently grants access.
- **Constant-time verification.** Password verification uses
  ``hashlib.scrypt`` (stdlib) with a per-account random salt and
  ``hmac.compare_digest`` for the final comparison, so a wrong password takes
  the same code path as a right one and comparison is not short-circuiting.
- **No secrets in logs.** Passwords and session tokens are never logged,
  echoed, or placed in exception messages.

Hashing choice — ``hashlib.scrypt`` (stdlib) vs ``argon2-cffi``
---------------------------------------------------------------
The spec permits ``argon2-cffi`` *if it were already a dependency*, else it
directs us to prefer ``hashlib.scrypt`` from the standard library to avoid a
new dependency. ``argon2-cffi`` is **not** currently a dependency, and this
project's rulings favour a self-hostable, air-gapped-friendly build with no
heavy/native dependency chain. ``hashlib.scrypt`` is a memory-hard KDF built
into CPython (OpenSSL-backed), needs no new package, and is more than adequate
for local-accounts password storage. We therefore use ``hashlib.scrypt``.

OIDC seam
---------
See the ``# OIDC seam:`` block near the bottom of this file. An
``OIDCAuthProvider`` would implement the same ``AuthProvider`` protocol; the
web app depends only on the protocol, so swapping providers requires no route
changes.

Spec references: §14.2 (auth), §6.2/§14.2 (never leak credentials).
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

# --- scrypt cost parameters -------------------------------------------------
# Interactive-login tuned parameters (RFC 7914 guidance). n must be a power of
# two; these give a ~64 MiB memory cost per verification, appropriate for a
# self-hosted login path.
_SCRYPT_N = 2**14  # CPU/memory cost
_SCRYPT_R = 8  # block size
_SCRYPT_P = 1  # parallelization
_SCRYPT_DKLEN = 32  # derived-key length in bytes
_SALT_BYTES = 16
_TOKEN_BYTES = 32  # opaque session-token entropy


@dataclass(frozen=True)
class Session:
    """An authenticated session.

    ``token`` is the opaque bearer credential handed to the client (as a
    cookie). It is high-entropy and server-side-validated; it is never logged.
    """

    token: str = field(repr=False)
    username: str
    created_at: float
    expires_at: float

    def is_valid(self, *, now: float | None = None) -> bool:
        """True when the session has not expired."""
        current = time.time() if now is None else now
        return current < self.expires_at


# ---------------------------------------------------------------------------
# Password hashing (stdlib scrypt) — no plaintext ever stored
# ---------------------------------------------------------------------------


def hash_password(password: str) -> str:
    """Hash a password with a fresh random salt.

    Returns a self-describing string ``scrypt$n$r$p$<salt_hex>$<hash_hex>`` so
    the parameters travel with the stored hash. The plaintext is never stored
    or returned.
    """
    salt = secrets.token_bytes(_SALT_BYTES)
    derived = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
        maxmem=0,
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${derived.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time verification of ``password`` against a stored hash.

    Returns ``False`` for any malformed stored value rather than raising, so a
    corrupted record fails closed. Uses ``hmac.compare_digest`` for the final
    comparison to avoid timing side channels.
    """
    try:
        scheme, n_s, r_s, p_s, salt_hex, hash_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        n, r, p = int(n_s), int(r_s), int(p_s)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except (ValueError, AttributeError):
        return False

    derived = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=n,
        r=r,
        p=p,
        dklen=len(expected),
        maxmem=0,
    )
    return hmac.compare_digest(derived, expected)


# ---------------------------------------------------------------------------
# AuthProvider protocol — the pluggable seam
# ---------------------------------------------------------------------------


@runtime_checkable
class AuthProvider(Protocol):
    """The authentication seam the web app depends on.

    Every implementation MUST be fail-closed: ``get_session`` returns ``None``
    for any token it cannot positively validate. Implementations MUST NOT log
    passwords or tokens.
    """

    def authenticate(self, username: str, password: str) -> Session | None:
        """Verify credentials and mint a session, or return ``None`` on failure.

        Returning ``None`` covers unknown users, wrong passwords, and disabled
        accounts alike — callers must not distinguish these (avoids user
        enumeration).
        """
        ...

    def get_session(self, token: str | None) -> Session | None:
        """Validate an opaque session token, or return ``None`` (fail-closed)."""
        ...

    def logout(self, token: str | None) -> None:
        """Invalidate a session token. Idempotent; a no-op for unknown tokens."""
        ...


# ---------------------------------------------------------------------------
# LocalAccountsAuthProvider — default implementation
# ---------------------------------------------------------------------------


@dataclass
class _Account:
    username: str
    password_hash: str  # scrypt$... — never plaintext


class LocalAccountsAuthProvider:
    """Auth backed by local username/password accounts held in memory.

    Sessions are opaque random tokens stored server-side (see
    ``finecorpus.web.sessions.SessionStore``). Password hashes use
    ``hashlib.scrypt``; plaintext is never retained.

    The account set is provided at construction time (e.g. seeded from an
    operator-provisioned source). Persistence of accounts is out of scope for
    this foundation work unit; the in-memory store carries a documented seam.
    """

    def __init__(
        self,
        *,
        session_store: SessionStore | None = None,
        session_ttl_seconds: int = 60 * 60 * 8,
    ) -> None:
        from finecorpus.web.sessions import InMemorySessionStore

        self._accounts: dict[str, _Account] = {}
        self._store: SessionStore = session_store or InMemorySessionStore()
        self._ttl = session_ttl_seconds

    # -- account management --------------------------------------------------

    def add_account(self, username: str, password: str) -> None:
        """Register a local account. Stores only the scrypt hash, not the password."""
        self._accounts[username] = _Account(
            username=username, password_hash=hash_password(password)
        )

    def has_account(self, username: str) -> bool:
        return username in self._accounts

    # -- AuthProvider protocol ----------------------------------------------

    def authenticate(self, username: str, password: str) -> Session | None:
        account = self._accounts.get(username)
        if account is None:
            # Perform a dummy verification to keep timing uniform against a
            # non-existent user (mitigates user enumeration via timing).
            verify_password(password, _DUMMY_HASH)
            return None
        if not verify_password(password, account.password_hash):
            return None
        now = time.time()
        token = secrets.token_urlsafe(_TOKEN_BYTES)
        session = Session(
            token=token,
            username=username,
            created_at=now,
            expires_at=now + self._ttl,
        )
        self._store.save(session)
        return session

    def get_session(self, token: str | None) -> Session | None:
        if not token:
            return None
        session = self._store.get(token)
        if session is None:
            return None
        if not session.is_valid():
            # Expired — evict and fail closed.
            self._store.delete(token)
            return None
        return session

    def logout(self, token: str | None) -> None:
        if token:
            self._store.delete(token)


# A fixed dummy hash for constant-time handling of unknown usernames. It is a
# hash of an unguessable random string; no real password verifies against it.
_DUMMY_HASH = hash_password(secrets.token_urlsafe(32))


# ---------------------------------------------------------------------------
# OIDC seam
# ---------------------------------------------------------------------------
#
# OIDC seam: To support Single Sign-On, add an ``OIDCAuthProvider`` here (or in
# a dedicated ``finecorpus.web.oidc`` module) that implements the same
# ``AuthProvider`` protocol above. It would:
#
#   * Replace ``authenticate(username, password)`` with a redirect-based
#     authorization-code flow: the login route would 302 to the IdP, and a new
#     ``GET /auth/callback`` route would exchange the code for tokens, verify
#     the ID token (issuer, audience, signature, nonce, expiry), and then call
#     an internal ``_mint_session(claims)`` — mirroring what ``authenticate``
#     does today — to create the same server-side ``Session``.
#   * Keep ``get_session(token)`` and ``logout(token)`` identical in shape:
#     the app's session cookie and ``SessionStore`` are reused unchanged, so
#     the entire web layer stays provider-agnostic.
#   * Read IdP config (issuer URL, client_id, and a client_secret supplied via
#     env only — never written to a config file) at construction time.
#
# Because ``create_app(config, *, auth_provider=...)`` injects the provider and
# the routes depend only on the ``AuthProvider`` protocol, swapping local
# accounts for OIDC requires NO changes to app.py routes or templates.
# ---------------------------------------------------------------------------


# Re-exported for callers that type-annotate against the store.
from finecorpus.web.sessions import SessionStore  # noqa: E402

__all__ = [
    "AuthProvider",
    "LocalAccountsAuthProvider",
    "Session",
    "SessionStore",
    "hash_password",
    "verify_password",
]
