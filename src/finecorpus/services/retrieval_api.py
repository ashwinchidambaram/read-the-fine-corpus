"""Retrieval API service — Phase 1.

Sole owner of the vector-DB connection pool (C-1, C-2). Thin FastAPI wiring
over ``finecorpus.retrieval.service`` (C-5: business logic lives in the library).

Phase 1 is **dense-only retrieval**. Hybrid search, reranking, and per-request
metadata filters are NOT supported in Phase 1 and do not exist as request
parameters. The spec (§11.2) lists these as later-phase capabilities; they are
declared unsupported, not silently absent. Do not add dead parameters.

Trust statement (§14.1):
All returned chunks carry ``trust_level: untrusted_ingested``. Callers —
especially agents — MUST treat retrieved content as untrusted material from
the ingested corpus. The platform labels but does not sanitize content. A
document that contains instructions addressed to a model will be retrieved
and labelled; the caller owns the downstream safety boundary.

HTTP status mapping for §15 error codes:
- 503 VECTOR_DB_UNAVAILABLE: vector database unreachable.
- 503 PROVIDER_UNAVAILABLE: embedding provider unavailable at query time.
- 409 EMBEDDING_MODEL_MISMATCH: query provider model != alias's model identity.
- 404 KB_NOT_READY or alias record not found.
- 422 INVALID_QUERY: malformed request (FastAPI handles most validation).

Spec references: §11.1, §11.2, §14.1, §15, §4.2 (C-1, C-2, C-5), §8.
"""

from __future__ import annotations

import logging
import math
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Path
from pydantic import BaseModel, Field

import finecorpus
from finecorpus.contracts.retrieval_response import (
    ErrorCode,
    ResultStatus,
    RetrievalResponse,
)
from finecorpus.control.auth import Principal
from finecorpus.embedding.cache import configure_cache_from_config, get_query_cache
from finecorpus.retrieval.ratelimit import TokenBucketLimiter
from finecorpus.retrieval.service import (
    _TOP_K_DEFAULT,
    _TOP_K_MAX,
    KBStatus,
    get_kb_status,
    query,
)

logger = logging.getLogger(__name__)

_SERVICE_NAME = "retrieval-api"

# ---------------------------------------------------------------------------
# Dependency providers (swapped in tests via app.dependency_overrides)
# ---------------------------------------------------------------------------

# These module-level singletons are initialized at startup in production.
# Tests override them via FastAPI's dependency injection or by patching.

_provider: Any = None  # EmbeddingProvider
_adapter: Any = None  # IndexAdapter
_session_factory: Any = None  # () -> Session context manager

# Auth/rate-limit singletons (Phase 4).  Initialized from config at startup;
# tests may patch these directly.
_auth_enabled: bool = False  # when False, no API key required (Phase 1–3 compat)
_key_repo_factory: Any = None  # () -> ApiKeyRepository (or None when auth disabled)
_rate_limiter: TokenBucketLimiter = TokenBucketLimiter(rate=None)  # disabled by default

# FastAPI dependency callables (built at startup when auth is configured).
# Initialized to permissive no-ops; replaced when auth.enabled=True.
_require_principal_dep: Any = None
_require_query_access_dep: Any = None


def _get_provider() -> Any:
    """Return the configured embedding provider."""
    if _provider is None:
        raise HTTPException(status_code=503, detail="Embedding provider not configured.")
    return _provider


def _get_adapter() -> Any:
    """Return the configured index adapter."""
    if _adapter is None:
        raise HTTPException(status_code=503, detail="Index adapter not configured.")
    return _adapter


def _get_session() -> Any:
    """Return the control-plane session factory."""
    if _session_factory is None:
        raise HTTPException(status_code=503, detail="Control-plane DB not configured.")
    return _session_factory


def _noop_principal() -> None:
    """No-op dependency: returns None when auth is disabled."""
    return None


def _noop_query_access(
    kb_id: Annotated[str, Path(description="Knowledge-base UUID.")],
    principal: Annotated[None, Depends(_noop_principal)],
) -> None:
    """No-op query access dependency: passes through when auth is disabled."""
    return None


# Mutable references to the active dependency callables (swapped at startup).
_require_principal_dep = _noop_principal
_require_query_access_dep = _noop_query_access


# ---------------------------------------------------------------------------
# Lifespan (startup/shutdown)
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan: initialize singletons, then shut down cleanly.

    Auth wiring note (PR #33 R1):
    The route decorator ``@app.post(..., ...)`` captures the dependency callable
    reference AT IMPORT TIME.  This means that reassigning the module-level
    variable ``_require_query_access_dep`` after the route is registered has no
    effect — the route closure already holds the old reference.

    The correct mechanism is ``app.dependency_overrides``.  When auth is enabled,
    we register the real auth dependency under the ``_noop_query_access`` key so
    that FastAPI's override map redirects every incoming request to the real dep.
    This is the ONLY mechanism that works at runtime after import-time capture.
    Tests that want to inject a custom dep must also use ``app.dependency_overrides``
    on ``_noop_query_access`` (the key the route was registered with).
    """
    global _provider, _adapter, _session_factory
    global _auth_enabled, _key_repo_factory, _rate_limiter
    global _require_principal_dep, _require_query_access_dep

    # In production, this reads from environment/config. In tests, the
    # caller patches the module-level singletons or uses dependency_overrides.

    # Cache + config initialization (from config when available)
    try:
        config_path = os.environ.get("RTFC_CONFIG")
        if config_path:
            from finecorpus.config.models import Config  # type: ignore[attr-defined]

            with open(config_path) as f:
                import yaml

                raw = yaml.safe_load(f)
            cfg = Config.model_validate(raw)
            configure_cache_from_config(cfg)

            # Rate limiter from config
            rate = cfg.retrieval.rate_limit.queries_per_second_per_tenant
            _rate_limiter = TokenBucketLimiter(rate=rate)

            # ------------------------------------------------------------------
            # Auth wiring (PR #33 R1): read auth.enabled from config and build
            # real deps when enabled.  The route captured _noop_query_access at
            # import time, so we register the real dep as an override on that key.
            # ------------------------------------------------------------------
            if cfg.auth.enabled:
                from contextlib import contextmanager

                from sqlalchemy.orm import Session

                from finecorpus.control.metadata import (
                    Base,
                    _ensure_all_models_imported,
                    create_engine,
                )
                from finecorpus.services.deps import (
                    make_require_principal,
                    make_require_query_access,
                )

                # Build the control-plane engine from the configured DSN.
                # storage.postgres.url is the canonical control-plane DSN.
                cp_dsn = cfg.storage.postgres.url
                if not cp_dsn:
                    logger.warning(
                        "auth.enabled=True but storage.postgres.url is not set; "
                        "auth will remain disabled."
                    )
                else:
                    _ensure_all_models_imported()
                    cp_engine = create_engine(cp_dsn)
                    Base.metadata.create_all(cp_engine)

                    from finecorpus.control.auth import ApiKeyRepository

                    @contextmanager
                    def _cp_session_ctx() -> Any:
                        with Session(cp_engine) as s:
                            yield s

                    def _key_repo_factory_impl() -> Any:  # type: ignore[return]
                        # Called per-request inside require_principal; yields a
                        # session-bound repo.  The session is managed by the dep.
                        # We return the repo after opening a short-lived session
                        # because make_require_principal calls repo_factory() once
                        # per request and the dep owns the session lifecycle.
                        # NOTE: this factory is called inside the dep which does
                        # NOT use a context manager — we open and hold a session
                        # for the duration of the dep call.  In production this
                        # would be a proper scoped session; for now open/close
                        # in the repo factory is sufficient.
                        sess = Session(cp_engine)
                        return ApiKeyRepository(sess)

                    _key_repo_factory = _key_repo_factory_impl

                    # Set the module flag (used by query() in service.py)
                    _auth_enabled = True

                    # Build and register real deps via dependency_overrides.
                    # This is the mechanism that actually works after import-time
                    # route registration (see docstring above).
                    _require_principal_dep = make_require_principal(
                        repo_factory=_key_repo_factory,
                        auth_enabled=True,
                    )
                    _require_query_access_dep = make_require_query_access(_require_principal_dep)
                    app.dependency_overrides[_noop_query_access] = _require_query_access_dep

                    logger.info(
                        "Auth enabled: real API-key deps registered via dependency_overrides"
                    )
        else:
            # Default in-process cache
            get_query_cache(reset=False)
    except Exception as exc:
        logger.warning("Cache initialization failed, using defaults: %s", exc)
        get_query_cache(reset=False)

    logger.info("%s started (version %s)", _SERVICE_NAME, finecorpus.__version__)
    yield

    # Clean up auth state on shutdown so the module is reset to its default
    # (Phase 1–3 compat) state.  This is especially important in test suites
    # where the same process runs multiple test cases against the same module
    # singleton: without cleanup, a test with auth.enabled=True would leave
    # _auth_enabled=True, causing the next test (without auth patches) to
    # reject all requests with PERMISSION_DENIED.
    # NOTE: global declarations for these names are already at the top of
    # this function; no duplicate global statement needed here.
    _auth_enabled = False  # noqa: F841 (global declared at top of function)
    _key_repo_factory = None  # noqa: F841
    app.dependency_overrides.pop(_noop_query_access, None)
    logger.info("%s shutting down", _SERVICE_NAME)


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="retrieval-api",
    version=finecorpus.__version__,
    description=(
        "Read The Fine Corpus — Phase 1 retrieval API.\n\n"
        "**Phase 1 scope: dense retrieval only.** Hybrid search, reranking, "
        "and per-request metadata filters are not available in Phase 1. "
        "Requesting these will result in an error.\n\n"
        "**Trust statement (§14.1):** All returned chunks are labelled "
        "``trust_level: untrusted_ingested``. Content is retrieved as-is from "
        "ingested documents and may contain adversarial or misleading text. "
        "The platform labels but does not sanitize content. Callers — especially "
        "agents — MUST treat retrieved chunks as untrusted material."
    ),
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class QueryRequest(BaseModel):
    """POST /v1/kb/{kb_id}/query request body.

    Phase 1 is dense-only. Hybrid search, reranking, and metadata filter
    parameters do not exist in this schema. Adding them here without backend
    support would be capability dishonesty (§11.2, spec Phase 1 scope).
    """

    query: str = Field(
        description=(
            "The query text. Will be embedded using the knowledge base's configured "
            "embedding model and searched against the index."
        ),
        min_length=1,
        max_length=8192,
    )
    top_k: int = Field(
        default=_TOP_K_DEFAULT,
        ge=1,
        le=_TOP_K_MAX,
        description=(
            f"Maximum number of results to return. Default: {_TOP_K_DEFAULT}. "
            f"Maximum: {_TOP_K_MAX}. Bounded server-side regardless of the value sent."
        ),
    )
    score_threshold: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description=(
            "Minimum relevance score (0.0–1.0) for a chunk to be included in results. "
            "When set, results below this threshold are excluded. If all candidates "
            "fall below the threshold, result_status will be 'filtered_to_zero' — "
            "the corpus has relevant content, but none above the requested quality bar. "
            "When absent, all top_k results are returned regardless of score."
        ),
    )
    explain: bool = Field(
        default=False,
        description=(
            "When true, populate the explain block in the response (§11.5).  "
            "The explain block contains the parsed query, every candidate considered "
            "(with raw score, provenance, and permission_resolved_at), exclusions with "
            "the exact filter that removed each candidate, and the retrieval strategy used.  "
            "Explain mode respects the same tenancy and permission rules as ordinary "
            "retrieval — it is NOT a bypass (§11.5)."
        ),
    )


class KBStatusResponse(BaseModel):
    """GET /v1/kb/{kb_id}/status response body.

    C-3 hygiene: collection_name is an internal implementation detail and is
    NOT exposed in the public status response.  Consumers should use alias +
    ready + model identity to determine queryability.
    """

    kb_id: str = Field(description="The knowledge-base ID.")
    alias: str = Field(
        description="The stable alias name used for all queries (e.g. 'rtfc_{kb_id}')."
    )
    embedding_provider: str | None = Field(
        default=None,
        description="Embedding provider identifier (e.g. 'openai', 'ollama', 'fake').",
    )
    embedding_model: str | None = Field(
        default=None,
        description="Embedding model identifier (e.g. 'text-embedding-3-small').",
    )
    embedding_dimensions: int | None = Field(
        default=None,
        description="Vector dimensionality of the index.",
    )
    config_version: str | None = Field(
        default=None,
        description=(
            "Ingestion config version hash that produced the current collection. "
            "Changes when the ingestion config changes and a reindex is promoted."
        ),
    )
    promoted_at: str | None = Field(
        default=None,
        description=(
            "ISO-8601 UTC timestamp of the last successful promotion. Null if never promoted."
        ),
    )
    ready: bool = Field(
        description=(
            "True iff a promoted collection exists and the KB is queryable. "
            "A KB that exists but has never been promoted returns ready=false."
        )
    )


# ---------------------------------------------------------------------------
# Error → HTTP status mapping (§15)
# ---------------------------------------------------------------------------

_ERROR_CODE_TO_HTTP: dict[ErrorCode, int] = {
    ErrorCode.EMBEDDING_MODEL_MISMATCH: 409,
    ErrorCode.VECTOR_DB_UNAVAILABLE: 503,
    ErrorCode.PROVIDER_UNAVAILABLE: 503,
    ErrorCode.KB_NOT_READY: 404,
    ErrorCode.PERMISSION_DENIED: 403,
    ErrorCode.INVALID_QUERY: 422,
    ErrorCode.CONTROL_PLANE_UNAVAILABLE: 503,
    ErrorCode.PAYLOAD_CORRUPT: 500,
    ErrorCode.RATE_LIMITED: 429,
}


def _response_to_http_status(response: RetrievalResponse) -> int:
    """Map a RetrievalResponse to the appropriate HTTP status code.

    - 200: matches, no_matches, filtered_to_zero (these are successful query outcomes).
    - error: mapped per the §15 error taxonomy.
    """
    if response.result_status != ResultStatus.error:
        return 200
    if response.error is None:
        return 500
    return _ERROR_CODE_TO_HTTP.get(response.error.code, 500)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.get(
    "/healthz",
    summary="Health check",
    description="Returns service status. Always 200 when the process is alive.",
    tags=["ops"],
)
def healthz() -> dict[str, str]:
    """Liveness probe. No dependency checks — returns 200 when the process is running."""
    return {"service": _SERVICE_NAME, "status": "ok", "version": finecorpus.__version__}


@app.get(
    "/v1/kb/{kb_id}/status",
    response_model=KBStatusResponse,
    summary="Knowledge-base index status",
    description=(
        "Returns the alias record summary for a knowledge base: model identity, "
        "current collection, and promotion timestamp. No secrets are included. "
        "Returns 404 if the knowledge base is not registered."
    ),
    tags=["retrieval"],
)
def kb_status(
    kb_id: Annotated[str, Path(description="Knowledge-base UUID.")],
) -> KBStatusResponse:
    """Return index status for a knowledge base.

    Safe to call before any ingestion is complete. Returns ``ready: false``
    until the first successful promotion.

    Returns 404 if no alias record exists for this KB.
    """
    session_factory = _get_session()
    with session_factory() as session:
        status: KBStatus | None = get_kb_status(kb_id=kb_id, session=session)

    if status is None:
        raise HTTPException(
            status_code=404,
            detail=f"Knowledge base '{kb_id}' is not registered.",
        )

    promoted_at_str: str | None = None
    if status.promoted_at is not None:
        promoted_at_str = status.promoted_at.isoformat()

    return KBStatusResponse(
        kb_id=status.kb_id,
        alias=status.alias,
        embedding_provider=status.embedding_provider,
        embedding_model=status.embedding_model,
        embedding_dimensions=status.embedding_dimensions,
        config_version=status.config_version,
        promoted_at=promoted_at_str,
        ready=status.ready,
    )


@app.post(
    "/v1/kb/{kb_id}/query",
    response_model=RetrievalResponse,
    summary="Query a knowledge base",
    description=(
        "Embed the query text and retrieve the most relevant chunks from the "
        "knowledge base using dense vector search.\n\n"
        "**Phase 1–4: dense-only.** Hybrid search, reranking, and per-request "
        "metadata filters are not supported.\n\n"
        "**Trust statement (§14.1):** All returned chunks carry "
        "``trust_level: untrusted_ingested``.\n\n"
        "**Error codes:**\n"
        "- ``PERMISSION_DENIED`` (403): Missing or invalid API key, or the key "
        "does not have access to this KB.\n"
        "- ``RATE_LIMITED`` (429): Per-tenant rate limit exceeded (M-101). "
        "Honour the ``Retry-After`` header.\n"
        "- ``EMBEDDING_MODEL_MISMATCH`` (409): Provider model mismatch.\n"
        "- ``PROVIDER_UNAVAILABLE`` (503): Embedding provider down.\n"
        "- ``VECTOR_DB_UNAVAILABLE`` (503): Vector database unreachable.\n"
        "- ``KB_NOT_READY`` (404): No promoted collection exists yet.\n\n"
        "**result_status values:**\n"
        "- ``matches``: At least one chunk matched.\n"
        "- ``no_matches``: No relevant content.\n"
        "- ``filtered_to_zero``: Relevant content exists below score_threshold.\n"
        "- ``error``: Fail-closed condition (see error.code)."
    ),
    tags=["retrieval"],
    responses={
        200: {"description": "Query completed (matches / no_matches / filtered_to_zero)."},
        401: {"description": "Missing or invalid API key."},
        403: {"description": "Principal lacks access to this KB (PERMISSION_DENIED)."},
        404: {"description": "KB not found or not ready yet."},
        409: {"description": "Embedding model mismatch (EMBEDDING_MODEL_MISMATCH)."},
        429: {"description": "Per-tenant rate limit exceeded (RATE_LIMITED)."},
        503: {
            "description": (
                "Service unavailable — embedding provider (PROVIDER_UNAVAILABLE) "
                "or vector database (VECTOR_DB_UNAVAILABLE) unreachable."
            )
        },
    },
)
def query_kb(
    kb_id: Annotated[str, Path(description="Knowledge-base UUID.")],
    body: QueryRequest,
    principal: Annotated[Principal | None, Depends(_require_query_access_dep)],
    x_break_glass_grant_id: Annotated[
        str | None,
        Header(
            alias="X-BreakGlass-Grant-ID",
            description=(
                "Break-glass grant ID for admin content reads (§2.3).  "
                "Admin-role principals MUST supply this header to read content.  "
                "When present and valid (active, unexpired, matches KB and admin), "
                "bypasses the permission_principals clause for this query.  "
                "kb/workspace tenancy clauses remain in effect.  "
                "Every read under a grant is written to the immutable audit log."
            ),
        ),
    ] = None,
) -> RetrievalResponse:
    """Query a knowledge base with dense vector retrieval.

    When auth is enabled:
    - Requires a valid API key (401 on missing/invalid).
    - Enforces RBAC: the principal must have query_kb permission for this KB (403).
    - Per-tenant rate limiting (M-101): 429 + Retry-After header on exceed.
    - Tenancy scope built from the authenticated principal (M-060: server-side only).

    All returned content is labelled ``trust_level: untrusted_ingested`` (§14.1).
    """
    # ------------------------------------------------------------------
    # Rate limit check (M-101) — before touching any DB or embedding.
    # Keyed by principal_id when available; falls back to kb_id for
    # unauthenticated mode (auth disabled).
    # ------------------------------------------------------------------
    tenant_key = principal.principal_id if principal is not None else kb_id
    if not _rate_limiter.acquire(tenant_key):
        from fastapi.responses import JSONResponse

        retry_after = _rate_limiter.retry_after_seconds(tenant_key)
        retry_after_int = max(1, math.ceil(retry_after))
        rate_limit_body = RetrievalResponse(
            schema_version="1.2.0",
            request_echo={"query": body.query, "filters_applied": []},  # type: ignore[arg-type]
            result_status="error",  # type: ignore[arg-type]
            results=[],
            error={  # type: ignore[arg-type]
                "code": ErrorCode.RATE_LIMITED,
                "message": (
                    f"Per-tenant rate limit exceeded.  Retry after {retry_after_int} second(s)."
                ),
                "retriable": True,
            },
        )
        return JSONResponse(  # type: ignore[return-value]
            status_code=429,
            content=rate_limit_body.model_dump(mode="json"),
            headers={"Retry-After": str(retry_after_int)},
        )

    provider = _get_provider()
    adapter = _get_adapter()
    session_factory = _get_session()
    cache = get_query_cache()

    with session_factory() as session:
        svc_response = query(
            kb_id=kb_id,
            query_text=body.query,
            provider=provider,
            adapter=adapter,
            session=session,
            top_k=body.top_k,
            score_threshold=body.score_threshold,
            cache=cache,
            principal=principal,
            auth_enabled=_auth_enabled,
            explain=body.explain,
            break_glass_grant_id=x_break_glass_grant_id,
        )

    http_status = _response_to_http_status(svc_response)
    if http_status != 200:
        # Raise HTTPException so FastAPI returns the correct status code.
        # The response body is the RetrievalResponse itself.
        raise HTTPException(
            status_code=http_status,
            detail=svc_response.model_dump(mode="json"),
        )

    return svc_response


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("finecorpus.services.retrieval_api:app", host="0.0.0.0", port=8000, reload=False)
