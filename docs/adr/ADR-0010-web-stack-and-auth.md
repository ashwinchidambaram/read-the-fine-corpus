# ADR-0010: Web stack and pluggable authentication

**Status:** Accepted
**Phase:** 6, Wave 1, WU-A (web foundation)
**Related:** §14.2 (auth, secrets), §6.2 (no credential leakage), ADR-0003 (stack), ADR-0008 (in-process rate limiting)

## Context

Read The Fine Corpus needs a browser UI for the Easy/Proficient knowledge-base
flows. Before building those flows, we need a web application skeleton and an
authentication foundation. Two decisions must be recorded now because they are
expensive to reverse: the rendering stack, and the authentication model.

Two project-wide constraints dominate:

1. **Self-hostable / air-gapped-friendly.** The platform runs in environments
   with no outbound network access and no tolerance for a heavy dependency
   chain. This has already shaped prior rulings (local providers, no CDN
   assets, minimal deps).
2. **Secret-handling discipline (§14.2).** Secrets arrive via environment
   variables only; nothing secret is ever written to `corpus.yaml` or logged.

## Decision

### 1. Server-rendered FastAPI + Jinja2 + HTMX (no Node/npm build chain)

The web UI is served by a FastAPI app (`finecorpus.web`) rendering Jinja2
templates, with HTMX for progressive interactivity. HTMX is **vendored
locally** (`src/finecorpus/web/static/htmx.min.js`), not loaded from a CDN.

Rationale:

- FastAPI and Starlette are already dependencies (the retrieval API uses them),
  so the server-rendered path adds only `jinja2` — a pure-Python, well-vetted
  templating library.
- No Node/npm toolchain, no bundler, no `node_modules`. The app ships as Python
  plus static files, which is trivially self-hostable and works air-gapped.
- Server-rendered HTML with HTMX covers the interactivity the KB flows need
  (form posts, partial swaps) without a client-side framework.

### 2. Pluggable authentication with an OIDC-ready seam (local accounts now)

Authentication is defined by an `AuthProvider` protocol
(`finecorpus.web.auth`) with `authenticate`, `get_session`, and `logout`. The
default implementation is `LocalAccountsAuthProvider` (username/password local
accounts). The app depends only on the protocol and injects the provider via
`create_app(config, *, auth_provider=...)`.

- **Fail-closed.** Every non-public route requires a validated session;
  `get_session` returns `None` for any token it cannot positively validate.
  Unauthenticated navigation is redirected to `/login`. Only `/healthz` and the
  login/logout routes are public.
- **Password hashing:** `hashlib.scrypt` (stdlib), per-account random salt,
  self-describing stored format, `hmac.compare_digest` for constant-time
  comparison. Chosen over `argon2-cffi` because argon2 is not already a
  dependency and stdlib scrypt avoids a new (native) dependency while remaining
  a memory-hard KDF — consistent with the no-heavy-dependency ruling.
- **Sessions:** opaque high-entropy random tokens (`secrets.token_urlsafe`)
  stored server-side via a `SessionStore` (in-memory default, documented seam
  for a persistent Postgres/Redis store). Tokens and passwords are never logged.
- **Session secret:** read from `RTFC_WEB_SESSION_SECRET` (env only), never
  written to config.
- **OIDC seam:** a documented extension point (`# OIDC seam:` in `auth.py`)
  shows exactly where an `OIDCAuthProvider` slots in. It would reuse the same
  `Session`/`SessionStore` and add an authorization-code callback route;
  because routes depend only on the `AuthProvider` protocol, no route or
  template changes are required.

## Rejected alternatives

- **Single-page application (React/Vue + bundler).** Rejected: introduces a
  Node/npm build chain and `node_modules`, contradicting the air-gapped /
  self-hostable and no-heavy-dependency rulings. HTMX gives sufficient
  interactivity server-side.
- **OIDC-only authentication now.** Rejected: forces every self-host operator
  to stand up an identity provider before they can log in, which is hostile to
  small/air-gapped deployments. Local accounts work out of the box; OIDC is a
  drop-in provider later via the seam.
- **`argon2-cffi` for hashing.** Rejected for this work unit: not already a
  dependency and pulls in a native (cffi) build. stdlib `hashlib.scrypt` is
  sufficient and dependency-free. This can be revisited if argon2 is adopted
  project-wide.
- **Client-side / stateless-JWT-only sessions.** Rejected: server-side opaque
  tokens allow immediate revocation (`logout` truly invalidates) and keep no
  claims on the client. A signed-cookie variant remains possible behind the
  same `SessionStore` seam if needed.

## Consequences

- New package `finecorpus.web`, a top-layer entry surface (sibling of
  `finecorpus.cli`); neither imports the other. It is added to the C-5
  layers contract top layer and to the C-5 forbidden container so core modules
  cannot import it.
- One new dependency: `jinja2`. FastAPI/Starlette/httpx were already present.
- The in-memory session store does not survive restarts and is single-replica;
  the persistent-store seam is the upgrade path when multi-replica is needed.
- Login rate-limiting is not yet wired; the existing
  `finecorpus.retrieval.ratelimit.TokenBucketLimiter` is the intended pattern
  and is left as a follow-up seam (foundation scope only).

## Known deferrals and hardening (WU-A review findings)

These were surfaced by the independent auth review and are consciously scoped
out of the foundation PR, to be closed in the WU-C login-flow work or Phase 7
hardening. None is an auth bypass; the core is fail-closed.

- **Session cookie `Secure` flag** — now driven by `config.web.cookie_secure`
  (default `false` for local HTTP; MUST be `true` for any HTTPS deployment,
  since a reverse proxy cannot add `Secure` to a cookie the app emitted
  without it). Fixed in this PR; documented here so operators set it.
- **CSRF protection** — not yet implemented on state-changing POSTs
  (`/login`, `/logout`). `SameSite=lax` gives partial cross-origin protection;
  a CSRF token lands with the real form flow in WU-C. Recorded here as a
  deliberate deferral (not an oversight).
- **Unauthenticated browser UX** — protected routes fail closed with `401`
  (+ `Location: /login`), not a redirect; converting that to a browser
  redirect to the login page is WU-C UX wiring.
- **`/docs`, `/redoc`, `/openapi.json`** — currently public (schema disclosure
  only, no protected data). Gate or disable them for locked-down deployments;
  tracked for WU-C/Phase 7.
- **Session secret** — read env-only (`RTFC_WEB_SESSION_SECRET`) and held in
  process memory; it is not yet wired to any signing (opaque 256-bit
  server-side tokens need none). It becomes load-bearing only if signed
  cookies are adopted behind the `SessionStore` seam.
