"""OAuth2 token handling for connectors (authorization-code + refresh-token).

Transport-agnostic and testable without a live provider: the flow takes an
injected :class:`~finecorpus.connectors.http.HTTPClient`, so tests replay
recorded fixtures and CI never needs real credentials.

Secrets handling (§14.2):
- Access/refresh tokens are treated as secrets: they are returned to the caller
  (which persists them via the control-plane ``TokenStore`` seam) but are NEVER
  logged, put in exceptions, or written to config.
- ``OAuth2Config`` carries ``client_secret`` only for the token exchange; it is
  never persisted by this module and never appears in ``__repr__``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from urllib.parse import urlencode

from finecorpus.connectors.http import HTTPClient

# ---------------------------------------------------------------------------
# Config + token models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OAuth2Config:
    """Static OAuth2 client configuration for a provider.

    ``client_secret`` is a credential: this dataclass is used transiently for a
    token exchange and MUST NOT be persisted or logged. ``__repr__`` is
    overridden to redact it.
    """

    authorization_endpoint: str
    token_endpoint: str
    client_id: str
    client_secret: str
    redirect_uri: str
    scopes: tuple[str, ...] = ()

    def __repr__(self) -> str:
        """Redact the client secret (§14.2)."""
        return (
            f"OAuth2Config(client_id={self.client_id!r}, "
            f"token_endpoint={self.token_endpoint!r}, "
            f"client_secret=***REDACTED***)"
        )


@dataclass(frozen=True)
class OAuth2Token:
    """An OAuth2 token set. Values are secrets (§14.2).

    ``__repr__`` redacts token material so a stray log line cannot leak it.
    """

    access_token: str
    refresh_token: str | None = None
    token_type: str = "Bearer"
    expires_at: float | None = None  # epoch seconds; None = unknown
    scopes: tuple[str, ...] = field(default_factory=tuple)

    def is_expired(self, *, now: float | None = None, leeway_s: float = 60.0) -> bool:
        """Return True when the token is at/near expiry (with ``leeway_s`` slack)."""
        if self.expires_at is None:
            return False
        current = now if now is not None else time.time()
        return current >= (self.expires_at - leeway_s)

    def authorization_header(self) -> dict[str, str]:
        """Return the ``Authorization`` header for API calls."""
        return {"Authorization": f"{self.token_type} {self.access_token}"}

    def __repr__(self) -> str:
        """Redact token material (§14.2)."""
        return (
            f"OAuth2Token(token_type={self.token_type!r}, "
            f"has_refresh_token={self.refresh_token is not None}, "
            f"expires_at={self.expires_at!r}, access_token=***REDACTED***)"
        )


class OAuth2Error(Exception):
    """Raised when a token exchange or refresh fails.

    MUST NOT include any token or client-secret material (§14.2).
    """


# ---------------------------------------------------------------------------
# Flow
# ---------------------------------------------------------------------------


class OAuth2Flow:
    """Authorization-code + refresh-token flow over an injected HTTP client.

    No global state, no live provider assumptions: construct with an
    :class:`HTTPClient` (recorded in tests, ``httpx`` in production).
    """

    def __init__(self, config: OAuth2Config, http_client: HTTPClient) -> None:
        self._config = config
        self._http = http_client

    def authorization_url(self, *, state: str) -> str:
        """Build the provider authorization URL to redirect the user to."""
        params = {
            "response_type": "code",
            "client_id": self._config.client_id,
            "redirect_uri": self._config.redirect_uri,
            "state": state,
        }
        if self._config.scopes:
            params["scope"] = " ".join(self._config.scopes)
        return f"{self._config.authorization_endpoint}?{urlencode(params)}"

    def exchange_code(self, code: str) -> OAuth2Token:
        """Exchange an authorization code for a token set."""
        return self._token_request(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self._config.redirect_uri,
                "client_id": self._config.client_id,
                "client_secret": self._config.client_secret,
            }
        )

    def refresh(self, token: OAuth2Token) -> OAuth2Token:
        """Obtain a fresh token set using the refresh token.

        Providers may omit ``refresh_token`` from the refresh response; in that
        case the original refresh token is carried forward.
        """
        if not token.refresh_token:
            raise OAuth2Error("Cannot refresh: token has no refresh_token.")
        refreshed = self._token_request(
            {
                "grant_type": "refresh_token",
                "refresh_token": token.refresh_token,
                "client_id": self._config.client_id,
                "client_secret": self._config.client_secret,
            }
        )
        if refreshed.refresh_token is None:
            # Carry forward the original refresh token when the provider omits it.
            refreshed = OAuth2Token(
                access_token=refreshed.access_token,
                refresh_token=token.refresh_token,
                token_type=refreshed.token_type,
                expires_at=refreshed.expires_at,
                scopes=refreshed.scopes,
            )
        return refreshed

    def ensure_fresh(self, token: OAuth2Token, *, now: float | None = None) -> OAuth2Token:
        """Return a non-expired token, refreshing transparently if needed."""
        if token.is_expired(now=now):
            return self.refresh(token)
        return token

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _token_request(self, form: dict[str, str]) -> OAuth2Token:
        """POST a token request and parse the response into an OAuth2Token."""
        resp = self._http.request(
            "POST",
            self._config.token_endpoint,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data=form,
        )
        if resp.status_code != 200:
            # Deliberately omit body: it may echo credential material (§14.2).
            raise OAuth2Error(
                f"Token endpoint returned HTTP {resp.status_code} "
                f"for grant_type={form.get('grant_type')!r}."
            )
        payload = resp.json()
        expires_in = payload.get("expires_in")
        expires_at = (time.time() + float(expires_in)) if expires_in is not None else None
        scope = payload.get("scope")
        scopes: tuple[str, ...] = tuple(scope.split()) if isinstance(scope, str) else ()
        return OAuth2Token(
            access_token=payload["access_token"],
            refresh_token=payload.get("refresh_token"),
            token_type=payload.get("token_type", "Bearer"),
            expires_at=expires_at,
            scopes=scopes,
        )


__all__ = [
    "OAuth2Config",
    "OAuth2Token",
    "OAuth2Error",
    "OAuth2Flow",
]
