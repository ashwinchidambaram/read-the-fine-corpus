"""Web application factory (Phase 6, WU-A) — FastAPI + Jinja2 + HTMX skeleton.

This is the **foundation** for the server-rendered UI, not the full
Easy/Proficient experience. It provides:

- ``create_app(config, *, auth_provider=None)`` — the ASGI app factory.
- A public ``GET /healthz`` liveness endpoint.
- Login (``GET``/``POST /login``) and ``POST /logout`` routes.
- An authenticated ``GET /`` landing page.

Fail-closed: every non-public route requires a valid session (via the
``require_session`` dependency). Unauthenticated requests to a protected route
are denied with ``401 Unauthorized`` (carrying a ``Location: /login`` header
for clients that choose to follow it); there is no path that serves protected
content without a validated session. Turning that 401 into a browser redirect
to the login page is UX wiring that lands with the real login flow (WU-C).

Secret handling (§14.2): the session cookie secret is read from the
``RTFC_WEB_SESSION_SECRET`` environment variable at construction time and is
never written to config or logged. If unset, an ephemeral per-process secret is
generated (sessions will not survive a restart, which is acceptable for the
in-memory default store).

Import-linter: ``finecorpus.web`` is a top-layer entry surface (sibling of
``finecorpus.cli``). It may import config/services/retrieval/etc. but must not
be imported by any of them.

Spec references: §14.2 (auth, secrets), §6.2 (no credential leakage).
"""

import logging
import os
import secrets
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

from fastapi import Cookie, Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import finecorpus
from finecorpus.web.auth import AuthProvider, LocalAccountsAuthProvider, Session

if TYPE_CHECKING:
    from finecorpus.config import Config

logger = logging.getLogger(__name__)

_SESSION_COOKIE = "rtfc_session"
_SESSION_SECRET_ENV = "RTFC_WEB_SESSION_SECRET"

_PACKAGE_DIR = Path(__file__).resolve().parent
_TEMPLATES_DIR = _PACKAGE_DIR / "templates"
_STATIC_DIR = _PACKAGE_DIR / "static"


def _resolve_session_secret() -> str:
    """Read the session secret from env, or generate an ephemeral one.

    Never logs the secret. An ephemeral secret means cookies do not survive a
    restart — acceptable for the in-memory session store default.
    """
    secret = os.environ.get(_SESSION_SECRET_ENV)
    if secret:
        return secret
    logger.warning(
        "%s not set; generating an ephemeral per-process session secret. "
        "Sessions will not survive a restart.",
        _SESSION_SECRET_ENV,
    )
    return secrets.token_urlsafe(32)


def create_app(
    config: "Config | None" = None,
    *,
    auth_provider: AuthProvider | None = None,
) -> FastAPI:
    """Build and return the web ASGI application.

    Args:
        config: Root configuration. When ``None``, defaults are used (host/port
            and session TTL come from ``config.web``).
        auth_provider: The pluggable auth backend. Defaults to a
            ``LocalAccountsAuthProvider`` (local username/password accounts).
            Injecting an ``OIDCAuthProvider`` here later requires no route
            changes — routes depend only on the ``AuthProvider`` protocol.
    """
    ttl = config.web.session_ttl_seconds if config is not None else 60 * 60 * 8
    cookie_secure = config.web.cookie_secure if config is not None else False
    provider: AuthProvider = auth_provider or LocalAccountsAuthProvider(session_ttl_seconds=ttl)

    # Session secret is resolved (env-only) so it exists in process memory; it
    # is intentionally not attached to the app in any loggable place.
    _resolve_session_secret()

    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

    app = FastAPI(
        title="finecorpus-web",
        version=finecorpus.__version__,
        description=(
            "Read The Fine Corpus — server-rendered web UI (Phase 6 foundation). "
            "FastAPI + Jinja2 + HTMX, no client build chain (air-gapped-friendly)."
        ),
    )
    app.state.auth_provider = provider
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    # -- auth dependency (fail-closed) --------------------------------------

    def _current_session(
        token: Annotated[str | None, Cookie(alias=_SESSION_COOKIE)] = None,
    ) -> Session | None:
        """Return the validated session, or ``None`` (never raises)."""
        return provider.get_session(token)

    def require_session(
        session: Annotated[Session | None, Depends(_current_session)],
    ) -> Session:
        """Enforce a valid session (fail-closed).

        Denies unauthenticated requests with ``401 Unauthorized`` and a
        ``Location: /login`` header. No route bypasses this. (Converting the 401
        into a browser redirect is deferred to the WU-C login flow.)
        """
        if session is None:
            raise HTTPException(
                status_code=401,
                detail="Authentication required.",
                headers={"Location": "/login"},
            )
        return session

    # -- public routes -------------------------------------------------------

    @app.get("/healthz", include_in_schema=True)
    def healthz() -> dict[str, str]:
        """Unauthenticated liveness probe."""
        return {"status": "ok"}

    @app.get("/login", response_class=HTMLResponse)
    def login_form(
        request: Request,
        session: Annotated[Session | None, Depends(_current_session)],
        error: str | None = None,
    ) -> Response:
        """Render the login page. Redirects to / if already authenticated."""
        if session is not None:
            return RedirectResponse(url="/", status_code=303)
        return templates.TemplateResponse(request, "login.html", {"error": error})

    @app.post("/login")
    def login_submit(
        request: Request,
        username: Annotated[str, Form()],
        password: Annotated[str, Form()],
    ) -> Response:
        """Authenticate credentials and set the session cookie on success.

        Never logs the username/password. On failure returns the login page
        with a generic error (no user enumeration).
        """
        session = provider.authenticate(username, password)
        if session is None:
            return templates.TemplateResponse(
                request,
                "login.html",
                {"error": "Invalid username or password."},
                status_code=401,
            )
        response = RedirectResponse(url="/", status_code=303)
        response.set_cookie(
            key=_SESSION_COOKIE,
            value=session.token,
            httponly=True,
            samesite="lax",
            secure=cookie_secure,  # config.web.cookie_secure — set true for HTTPS deployments
            max_age=ttl,
            path="/",
        )
        return response

    @app.post("/logout")
    def logout(
        token: Annotated[str | None, Cookie(alias=_SESSION_COOKIE)] = None,
    ) -> Response:
        """Invalidate the session and clear the cookie."""
        provider.logout(token)
        response = RedirectResponse(url="/login", status_code=303)
        response.delete_cookie(key=_SESSION_COOKIE, path="/")
        return response

    # -- protected routes ----------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def landing(
        request: Request,
        session: Annotated[Session, Depends(require_session)],
    ) -> Response:
        """Authenticated landing page (skeleton)."""
        return templates.TemplateResponse(
            request,
            "landing.html",
            {"username": session.username, "version": finecorpus.__version__},
        )

    return app


__all__ = ["create_app"]
