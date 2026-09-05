"""Phase 4 tenancy isolation tests (M-060, M-061, T-02).

Covers:
- T-02 (M-061): Cross-tenant retrieval fails closed — KB-A principal querying KB-B
  returns PERMISSION_DENIED, zero results, and the adapter never receives a search.
- M-060: Tenancy filter is server-side and not client-overridable.
- Workspace filter applied when principal is present.
- Auth-disabled compat mode: query passes through with only kb_id filter.
"""

from __future__ import annotations

import sys
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any
from unittest.mock import patch

from fastapi.testclient import TestClient

from finecorpus.contracts.retrieval_response import ErrorCode, ResultStatus
from finecorpus.control.auth import Principal, Role, ScopeKind
from finecorpus.embedding.fake import FakeProvider
from finecorpus.index.adapter import alias_name

# Import helpers from the retrieval test suite
sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent / "retrieval"))
from helpers import (  # type: ignore[import-not-found]
    FakeAdapter,
    FakeAliasRecord,
    FakeAliasRepository,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

KB_A = "kb-tenant-a"
KB_B = "kb-tenant-b"
WS_A = "ws-tenant-a"
WS_B = "ws-tenant-b"
PRINCIPAL_A_ID = "pid-tenant-a"
PRINCIPAL_B_ID = "pid-tenant-b"
MODEL_ID = "fake-embed-v1"
DIMENSIONS = 64
COLL_A = f"rtfc_{KB_A.replace('-', '').lower()}_00000001"
COLL_B = f"rtfc_{KB_B.replace('-', '').lower()}_00000001"


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _make_principal(
    principal_id: str,
    kb_id: str,
    workspace_id: str,
    *,
    role: Role = Role.service,
) -> Principal:
    return Principal(
        principal_id=principal_id,
        name=f"svc-{principal_id}",
        role=role,
        scope_kind=ScopeKind.kb,
        workspace_id=workspace_id,
        kb_id=kb_id,
    )


def _make_alias_record_with_ws(kb_id: str, workspace_id: str) -> FakeAliasRecord:
    """Build a FakeAliasRecord with an explicit workspace_id."""
    alias = alias_name(kb_id)
    coll = f"rtfc_{kb_id.replace('-', '').lower()}_00000001"
    rec = FakeAliasRecord(
        alias=alias,
        kb_id=kb_id,
        workspace_id=workspace_id,
        collection=coll,
        embedding_provider="fake",
        embedding_model=MODEL_ID,
        embedding_dimensions=DIMENSIONS,
        config_version="cfgv1",
    )
    return rec


def _seeded_adapter() -> FakeAdapter:
    """Return an adapter seeded with one chunk per KB, matching their workspace/principal."""
    adapter = FakeAdapter()

    # KB-A chunk: permission_principals includes PRINCIPAL_A_ID, workspace_id=WS_A
    chunk_a = {
        "id": "chk-a-001",
        "score": 0.9,
        "payload": {
            "chunk_id": "chk-a-001",
            "text": "Chunk from KB-A",
            "tenancy": {
                "kb_id": KB_A,
                "workspace_id": WS_A,
                "permission_principals": [PRINCIPAL_A_ID],
                "permission_mode": "restricted",
                "permission_source": "platform",
                "permission_fidelity": "authoritative",
            },
            "provenance": {
                "source_document_id": "doc-a-001",
                "source_document_version": "v1",
                "source_location": {"locator_kind": "char_range", "char_start": 0, "char_end": 100},
                "structural_path": [],
                "transformations": [],
                "confidence": 1.0,
                "segment_type": "prose",
                "salience_tier": "primary",
                "salience_basis": "default",
                "salience_signals": [],
                "language": "en",
                "injection_suspicion": 0.0,
                "invisible_content_flags": [],
                "sensitivity_flags": [],
                "trust_level": "untrusted_ingested",
            },
        },
    }
    adapter.seed_collection(alias=alias_name(KB_A), coll=COLL_A, points=[chunk_a])

    # KB-B chunk: permission_principals includes PRINCIPAL_B_ID, workspace_id=WS_B
    chunk_b = {
        "id": "chk-b-001",
        "score": 0.9,
        "payload": {
            "chunk_id": "chk-b-001",
            "text": "Chunk from KB-B",
            "tenancy": {
                "kb_id": KB_B,
                "workspace_id": WS_B,
                "permission_principals": [PRINCIPAL_B_ID],
                "permission_mode": "restricted",
                "permission_source": "platform",
                "permission_fidelity": "authoritative",
            },
            "provenance": {
                "source_document_id": "doc-b-001",
                "source_document_version": "v1",
                "source_location": {"locator_kind": "char_range", "char_start": 0, "char_end": 100},
                "structural_path": [],
                "transformations": [],
                "confidence": 1.0,
                "segment_type": "prose",
                "salience_tier": "primary",
                "salience_basis": "default",
                "salience_signals": [],
                "language": "en",
                "injection_suspicion": 0.0,
                "invisible_content_flags": [],
                "sensitivity_flags": [],
                "trust_level": "untrusted_ingested",
            },
        },
    }
    adapter.seed_collection(alias=alias_name(KB_B), coll=COLL_B, points=[chunk_b])

    return adapter


# ---------------------------------------------------------------------------
# Service-level tests (no FastAPI) — direct retrieval.service.query() calls
# ---------------------------------------------------------------------------


class TestT02CrossTenantFailsClosed:
    """T-02 (M-061): Cross-tenant retrieval must fail closed.

    A principal authenticated for KB-A must not retrieve content from KB-B.
    The service must return PERMISSION_DENIED and the adapter must not even
    be called with an unfiltered search.
    """

    def test_t02_cross_tenant_fails_closed(self) -> None:
        """KB-A principal querying KB-B → PERMISSION_DENIED, zero results, adapter not called."""
        from finecorpus.retrieval.service import query

        adapter = _seeded_adapter()
        principal_a = _make_principal(PRINCIPAL_A_ID, KB_A, WS_A)
        provider = FakeProvider(dimensions=DIMENSIONS, model_id=MODEL_ID)

        alias_rec_b = _make_alias_record_with_ws(KB_B, WS_B)
        alias_rec_a = _make_alias_record_with_ws(KB_A, WS_A)
        fake_repo = FakeAliasRepository(
            {alias_name(KB_A): alias_rec_a, alias_name(KB_B): alias_rec_b}
        )

        initial_call_count = adapter.search_call_count

        with patch(
            "finecorpus.retrieval.service.AliasRepository",
            return_value=fake_repo,
        ):
            response = query(
                kb_id=KB_B,  # principal_a is authorized for KB_A only
                query_text="test query",
                provider=provider,
                adapter=adapter,
                session=object(),  # type: ignore[arg-type]
                auth_enabled=True,
                principal=principal_a,
            )

        # Must fail closed with PERMISSION_DENIED
        assert response.result_status == ResultStatus.error
        assert response.error is not None
        assert response.error.code == ErrorCode.PERMISSION_DENIED
        assert response.results == []

        # The adapter must never have received a search call
        # (fail-closed happens before index access)
        assert adapter.search_call_count == initial_call_count, (
            "Adapter search was called on cross-tenant access — must fail closed first"
        )

    def test_kb_a_principal_can_query_kb_a(self) -> None:
        """KB-A principal can successfully query KB-A (same-tenant positive case)."""
        from finecorpus.retrieval.service import query

        adapter = _seeded_adapter()
        principal_a = _make_principal(PRINCIPAL_A_ID, KB_A, WS_A)
        provider = FakeProvider(dimensions=DIMENSIONS, model_id=MODEL_ID)

        alias_rec_a = _make_alias_record_with_ws(KB_A, WS_A)
        fake_repo = FakeAliasRepository({alias_name(KB_A): alias_rec_a})

        with patch(
            "finecorpus.retrieval.service.AliasRepository",
            return_value=fake_repo,
        ):
            response = query(
                kb_id=KB_A,
                query_text="test query",
                provider=provider,
                adapter=adapter,
                session=object(),  # type: ignore[arg-type]
                auth_enabled=True,
                principal=principal_a,
            )

        # Should get results (the adapter finds the chunk for KB_A with principal_a's id)
        assert response.result_status in (ResultStatus.matches, ResultStatus.no_matches)
        assert response.error is None


class TestTenancyFilterNotClientOverridableMO60:
    """M-060: The tenancy filter is server-side and MUST NOT be client-overridable."""

    def test_client_cannot_supply_tenancy_fields(self) -> None:
        """The QueryRequest model has no tenancy fields — it cannot be overridden.

        This test verifies at the model level that QueryRequest does not expose
        any tenancy-related fields (workspace_id, permission_principals, kb_id).
        A client could only influence tenancy via the URL path kb_id (intentional)
        — not through the request body.
        """
        from finecorpus.services.retrieval_api import QueryRequest

        # QueryRequest must not have tenancy-adjacent fields in its schema
        schema = QueryRequest.model_json_schema()
        properties = schema.get("properties", {})

        forbidden_fields = {"workspace_id", "permission_principals", "tenant_id", "principal_id"}
        for field in forbidden_fields:
            assert field not in properties, (
                f"QueryRequest exposes tenancy field '{field}' — "
                "this violates M-060 (tenancy must be server-side only)"
            )

    def test_tenancy_scope_built_from_principal_not_body(self) -> None:
        """The TenancyScope is built from the principal, never from the request body.

        Verify _build_tenancy_filter includes workspace_id from the principal
        (not from anything the client could supply) when a principal is present.
        """
        from finecorpus.retrieval.service import TenancyScope, _build_tenancy_filter

        scope_with_principal = TenancyScope(
            kb_id="kb-test",
            workspace_id="ws-server-derived",
            permission_principals=("pid-server-derived",),
        )
        f = _build_tenancy_filter(scope_with_principal)

        assert f["tenancy.kb_id"] == "kb-test"
        assert f["tenancy.workspace_id"] == "ws-server-derived"
        assert f["tenancy.permission_principals"] == {"__contains__": "pid-server-derived"}

    def test_auth_disabled_scope_has_only_kb_id(self) -> None:
        """When auth is disabled the tenancy filter contains only kb_id (Phase 1–3 compat)."""
        from finecorpus.retrieval.service import TenancyScope, _build_tenancy_filter

        scope_no_auth = TenancyScope(kb_id="kb-test")
        f = _build_tenancy_filter(scope_no_auth)

        assert set(f.keys()) == {"tenancy.kb_id"}


class TestWorkspaceFilterAppliedWhenPrincipalPresent:
    """Workspace filter is injected when a principal with workspace_id is present."""

    def test_workspace_filter_in_tenancy_scope(self) -> None:
        """TenancyScope includes workspace_id and permission_principals from principal."""
        from finecorpus.retrieval.service import TenancyScope, _build_tenancy_filter

        scope = TenancyScope(
            kb_id="kb-ws-test",
            workspace_id="ws-from-principal",
            permission_principals=("pid-from-principal",),
        )
        f = _build_tenancy_filter(scope)

        assert "tenancy.workspace_id" in f
        assert f["tenancy.workspace_id"] == "ws-from-principal"
        assert "tenancy.permission_principals" in f

    def test_permission_denied_when_no_principal_and_auth_enabled(self) -> None:
        """query() returns PERMISSION_DENIED when auth is enabled and principal is None."""
        from finecorpus.retrieval.service import query

        provider = FakeProvider(dimensions=DIMENSIONS, model_id=MODEL_ID)
        adapter = _seeded_adapter()

        response = query(
            kb_id=KB_A,
            query_text="test",
            provider=provider,
            adapter=adapter,
            session=object(),  # type: ignore[arg-type]
            auth_enabled=True,
            principal=None,  # no principal — fail closed
        )

        assert response.result_status == ResultStatus.error
        assert response.error is not None
        assert response.error.code == ErrorCode.PERMISSION_DENIED

    def test_no_permission_denied_when_auth_disabled(self) -> None:
        """query() does NOT return PERMISSION_DENIED when auth is disabled (principal=None)."""
        from finecorpus.retrieval.service import query

        provider = FakeProvider(dimensions=DIMENSIONS, model_id=MODEL_ID)

        alias_rec_a = _make_alias_record_with_ws(KB_A, WS_A)
        fake_repo = FakeAliasRepository({alias_name(KB_A): alias_rec_a})
        adapter = _seeded_adapter()

        with patch(
            "finecorpus.retrieval.service.AliasRepository",
            return_value=fake_repo,
        ):
            response = query(
                kb_id=KB_A,
                query_text="test",
                provider=provider,
                adapter=adapter,
                session=object(),  # type: ignore[arg-type]
                auth_enabled=False,  # auth disabled → compat mode
                principal=None,
            )

        # Should not be PERMISSION_DENIED
        assert response.error is None or response.error.code != ErrorCode.PERMISSION_DENIED


# ---------------------------------------------------------------------------
# API-level tests via FastAPI TestClient
# ---------------------------------------------------------------------------


@contextmanager
def _api_client_with_auth(
    provider: Any,
    adapter: Any,
    alias_records: dict[str, FakeAliasRecord],
    *,
    auth_enabled: bool = True,
    principal: Principal | None = None,
    rate: int | None = None,
) -> Generator[TestClient, None, None]:
    """Build a TestClient with auth patched in."""
    import finecorpus.services.retrieval_api as api_mod
    from finecorpus.embedding.cache import QueryEmbeddingCache
    from finecorpus.retrieval.ratelimit import TokenBucketLimiter

    fake_repo = FakeAliasRepository(alias_records)

    @contextmanager
    def fake_session_factory() -> Generator:  # type: ignore[misc]
        yield object()

    # Build a require_query_access dep that returns the supplied principal
    # (simulates a pre-authenticated principal for HTTP-level tests).
    def _fixed_principal() -> Principal | None:
        return principal

    def _fixed_query_access(
        kb_id: str,
        p: Principal | None = None,
    ) -> Principal | None:
        from fastapi import HTTPException

        from finecorpus.control.auth import Action, AuthError, authorize

        if auth_enabled and p is None:
            raise HTTPException(status_code=401, detail="Missing API key.")
        if p is not None:
            try:
                authorize(p, Action.query_kb, kb_id=kb_id)
            except AuthError as exc:
                raise HTTPException(status_code=403, detail=str(exc)) from None
        return p

    limiter = TokenBucketLimiter(rate=rate)

    with (
        patch.object(api_mod, "_provider", provider),
        patch.object(api_mod, "_adapter", adapter),
        patch.object(api_mod, "_session_factory", fake_session_factory),
        patch.object(api_mod, "get_query_cache", return_value=QueryEmbeddingCache(enabled=True)),
        patch.object(api_mod, "_auth_enabled", auth_enabled),
        patch.object(api_mod, "_rate_limiter", limiter),
        patch.object(api_mod, "_require_query_access_dep", _fixed_principal),
        patch(
            "finecorpus.retrieval.service.AliasRepository",
            return_value=fake_repo,
        ),
    ):
        from finecorpus.services.retrieval_api import app

        with TestClient(app) as client:
            yield client


class TestApiLevelAuth:
    """API-level 401/403/429 tests via FastAPI TestClient.

    Uses FastAPI's ``app.dependency_overrides`` to inject test deps because
    the route decorator captures the dep reference at import time.
    """

    def _base_patches(
        self,
        api_mod: Any,
        provider: Any,
        adapter: Any,
        fake_repo: Any,
        *,
        rate: int | None = None,
        auth_enabled: bool = True,
    ) -> tuple[Any, ...]:
        """Return a tuple of patch context managers for the base singletons."""
        from finecorpus.embedding.cache import QueryEmbeddingCache
        from finecorpus.retrieval.ratelimit import TokenBucketLimiter

        @contextmanager
        def fake_session_factory() -> Generator:  # type: ignore[misc]
            yield object()

        limiter = TokenBucketLimiter(rate=rate)
        return (
            patch.object(api_mod, "_provider", provider),
            patch.object(api_mod, "_adapter", adapter),
            patch.object(api_mod, "_session_factory", fake_session_factory),
            patch.object(
                api_mod, "get_query_cache", return_value=QueryEmbeddingCache(enabled=True)
            ),
            patch.object(api_mod, "_auth_enabled", auth_enabled),
            patch.object(api_mod, "_rate_limiter", limiter),
            patch("finecorpus.retrieval.service.AliasRepository", return_value=fake_repo),
        )

    def test_401_missing_key_when_auth_enabled(self) -> None:
        """No API key when auth is enabled → 401.

        Uses dependency_overrides to inject a dep that raises 401.
        """
        from fastapi import HTTPException

        import finecorpus.services.retrieval_api as api_mod

        provider = FakeProvider(dimensions=DIMENSIONS, model_id=MODEL_ID)
        adapter = _seeded_adapter()
        alias_a = _make_alias_record_with_ws(KB_A, WS_A)
        fake_repo = FakeAliasRepository({alias_name(KB_A): alias_a})

        def _auth_required() -> None:
            raise HTTPException(status_code=401, detail="Missing API key.")

        patches = self._base_patches(api_mod, provider, adapter, fake_repo, auth_enabled=True)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
            from finecorpus.services.retrieval_api import app

            # Override the dep that is currently baked into the route
            app.dependency_overrides[api_mod._noop_query_access] = _auth_required
            try:
                with TestClient(app, raise_server_exceptions=False) as client:
                    resp = client.post(f"/v1/kb/{KB_A}/query", json={"query": "test"})
            finally:
                app.dependency_overrides.pop(api_mod._noop_query_access, None)

        assert resp.status_code == 401

    def test_403_wrong_kb_when_auth_enabled(self) -> None:
        """Principal for KB-A accessing KB-B → 403 (via service-level authorize)."""
        import finecorpus.services.retrieval_api as api_mod

        provider = FakeProvider(dimensions=DIMENSIONS, model_id=MODEL_ID)
        adapter = _seeded_adapter()
        principal_a = _make_principal(PRINCIPAL_A_ID, KB_A, WS_A)
        alias_a = _make_alias_record_with_ws(KB_A, WS_A)
        alias_b = _make_alias_record_with_ws(KB_B, WS_B)
        fake_repo = FakeAliasRepository({alias_name(KB_A): alias_a, alias_name(KB_B): alias_b})

        # The dep returns principal_a (KB_A scoped); the service query() enforces
        # the KB containment and returns PERMISSION_DENIED → 403.
        def _returns_principal_a() -> Principal | None:
            return principal_a

        patches = self._base_patches(api_mod, provider, adapter, fake_repo, auth_enabled=True)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
            from finecorpus.services.retrieval_api import app

            app.dependency_overrides[api_mod._noop_query_access] = _returns_principal_a
            try:
                with TestClient(app, raise_server_exceptions=False) as client:
                    resp = client.post(
                        f"/v1/kb/{KB_B}/query",  # KB-B, but principal for KB-A
                        json={"query": "test"},
                    )
            finally:
                app.dependency_overrides.pop(api_mod._noop_query_access, None)

        assert resp.status_code == 403

    def test_429_rate_limited_with_retry_after_header(self) -> None:
        """Exhausted rate limit → 429 with Retry-After header (M-101)."""
        import finecorpus.services.retrieval_api as api_mod
        from finecorpus.retrieval.ratelimit import TokenBucketLimiter

        provider = FakeProvider(dimensions=DIMENSIONS, model_id=MODEL_ID)
        adapter = _seeded_adapter()
        principal_a = _make_principal(PRINCIPAL_A_ID, KB_A, WS_A)
        alias_a = _make_alias_record_with_ws(KB_A, WS_A)
        fake_repo = FakeAliasRepository({alias_name(KB_A): alias_a})

        def _returns_principal_a() -> Principal | None:
            return principal_a

        # Rate limit: 2 QPS with burst=1 (2 tokens total) — exhausts fast
        tight_limiter = TokenBucketLimiter(rate=2, burst_factor=1)

        @contextmanager
        def fake_session_factory() -> Generator:  # type: ignore[misc]
            yield object()

        from finecorpus.embedding.cache import QueryEmbeddingCache

        with (
            patch.object(api_mod, "_provider", provider),
            patch.object(api_mod, "_adapter", adapter),
            patch.object(api_mod, "_session_factory", fake_session_factory),
            patch.object(
                api_mod, "get_query_cache", return_value=QueryEmbeddingCache(enabled=True)
            ),
            patch.object(api_mod, "_auth_enabled", True),
            patch.object(api_mod, "_rate_limiter", tight_limiter),
            patch("finecorpus.retrieval.service.AliasRepository", return_value=fake_repo),
        ):
            from finecorpus.services.retrieval_api import app

            app.dependency_overrides[api_mod._noop_query_access] = _returns_principal_a
            try:
                with TestClient(app, raise_server_exceptions=False) as client:
                    responses = []
                    for _ in range(5):
                        r = client.post(f"/v1/kb/{KB_A}/query", json={"query": "test"})
                        responses.append(r)
            finally:
                app.dependency_overrides.pop(api_mod._noop_query_access, None)

        status_codes = [r.status_code for r in responses]
        assert 429 in status_codes, f"Expected 429 in {status_codes}"

        # The 429 response must include a Retry-After header
        rate_limited_resp = next(r for r in responses if r.status_code == 429)
        assert "retry-after" in rate_limited_resp.headers, (
            "429 response must include Retry-After header (ADR-0008)"
        )
        retry_after_val = int(rate_limited_resp.headers["retry-after"])
        assert retry_after_val >= 1, "Retry-After must be >= 1 second"
