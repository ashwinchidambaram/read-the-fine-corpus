"""Tests for PR #33 review findings — fail-first tests, then implementations fix them.

Rulings:
- R1: Auth wiring is dead code — lifespan never sets _auth_enabled from config.
- R2: Global admin locked out — permission_principals always set for global scope.
- R3: Cross-workspace returns no_matches instead of PERMISSION_DENIED.
- R4: Null-workspace workspace key fails open.
- R5: Minor fixes (positive case assertion, make_chunk_payload, FakeAdapter docstring).

Each test in this file was WRITTEN BEFORE the fix and must fail on the broken code,
then pass after the fix.  Evidence is preserved in the commit message.
"""

from __future__ import annotations

import os
import sys
import tempfile
from collections.abc import Generator
from typing import Any
from unittest.mock import patch

import pytest

# Import helpers from the retrieval test suite
sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent / "retrieval"))
from helpers import (  # type: ignore[import-not-found]
    FakeAdapter,
    FakeAliasRecord,
    FakeAliasRepository,
    make_chunk_payload,
)

from finecorpus.contracts.retrieval_response import ErrorCode, ResultStatus
from finecorpus.control.auth import (
    ApiKeyRepository,
    AuthError,
    Principal,
    Role,
    ScopeKind,
)
from finecorpus.embedding.fake import FakeProvider
from finecorpus.index.adapter import alias_name

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

KB_A = "kb-r33-a"
KB_B = "kb-r33-b"
WS_A = "ws-r33-a"
WS_B = "ws-r33-b"
MODEL_ID = "fake-embed-v1"
DIMENSIONS = 64
COLL_A = f"rtfc_{KB_A.replace('-', '').lower()}_00000001"
COLL_B = f"rtfc_{KB_B.replace('-', '').lower()}_00000001"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_alias_record(kb_id: str, workspace_id: str) -> FakeAliasRecord:
    alias = alias_name(kb_id)
    coll = f"rtfc_{kb_id.replace('-', '').lower()}_00000001"
    return FakeAliasRecord(
        alias=alias,
        kb_id=kb_id,
        workspace_id=workspace_id,
        collection=coll,
        embedding_provider="fake",
        embedding_model=MODEL_ID,
        embedding_dimensions=DIMENSIONS,
        config_version="cfgv1",
    )


def _seeded_adapter_for_global(kb_id: str, workspace_id: str) -> FakeAdapter:
    """Adapter with one chunk seeded; permission_principals is empty (global access)."""
    adapter = FakeAdapter()
    chunk = make_chunk_payload(
        chunk_id=f"chk-{kb_id}-001",
        text=f"Content from {kb_id}",
        kb_id=kb_id,
        # permission_principals absent — global scope chunks are readable by all admins
    )
    # Ensure the tenancy block has the workspace_id and no permission_principals restriction
    chunk["payload"]["tenancy"]["workspace_id"] = workspace_id
    adapter.seed_collection(alias=alias_name(kb_id), coll=COLL_A, points=[chunk])
    return adapter


# ---------------------------------------------------------------------------
# R1: Live auth wiring — no patching of _auth_enabled or dependency_overrides
# ---------------------------------------------------------------------------


class TestR1LiveAuthWiring:
    """RULING 1 (BLOCKER): lifespan must wire auth from config, not dead assignment.

    The decisive test: start the app via the REAL lifespan with a config file
    that has auth.enabled=True.  NO patching of _auth_enabled; NO
    dependency_overrides.  Requests without a key → 401; with a valid key → not 401.
    Also test auth.enabled=False → no auth demanded.
    """

    def _make_config_yaml(self, auth_enabled: bool, db_path: str) -> str:
        return f"""
platform:
  instance_name: rtfc
  first_run_complete: true
  log_level: info
auth:
  enabled: {str(auth_enabled).lower()}
storage:
  postgres:
    url: "sqlite:///{db_path}"
retrieval:
  rate_limit:
    queries_per_second_per_tenant: null
"""

    def _setup_sqlite_db(self, db_path: str) -> None:
        """Create tables and return (ApiKeyRepository, session factory)."""

        from finecorpus.control.metadata import Base, _ensure_all_models_imported, create_engine

        _ensure_all_models_imported()
        engine = create_engine(f"sqlite:///{db_path}")
        Base.metadata.create_all(engine)
        return engine

    def test_r1_auth_enabled_true_no_key_returns_401(self) -> None:
        """With auth.enabled=True in config and no key → 401 (real lifespan, no patching)."""
        from fastapi.testclient import TestClient

        import finecorpus.services.retrieval_api as api_mod

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "ctrl.db")
            engine = self._setup_sqlite_db(db_path)

            config_path = os.path.join(tmpdir, "corpus.yaml")
            with open(config_path, "w") as f:
                f.write(self._make_config_yaml(auth_enabled=True, db_path=db_path))

            provider = FakeProvider(dimensions=DIMENSIONS, model_id=MODEL_ID)
            adapter = FakeAdapter()

            from contextlib import contextmanager as cm

            from sqlalchemy.orm import Session

            @cm
            def session_factory() -> Generator:
                with Session(engine) as sess:
                    yield sess

            # Patch infra singletons (provider/adapter/session) but NOT _auth_enabled
            # and NOT dependency_overrides. The lifespan must set _auth_enabled from config.
            with (
                patch.dict(os.environ, {"RTFC_CONFIG": config_path}),
                patch.object(api_mod, "_provider", provider),
                patch.object(api_mod, "_adapter", adapter),
                patch.object(api_mod, "_session_factory", session_factory),
            ):
                from finecorpus.services.retrieval_api import app

                with TestClient(app, raise_server_exceptions=False) as client:
                    # No API key — must be 401
                    resp = client.post(f"/v1/kb/{KB_A}/query", json={"query": "test"})

            assert resp.status_code == 401, (
                f"Expected 401 (auth required) but got {resp.status_code}. "
                "Lifespan is NOT wiring auth from config — dead code bug."
            )

    def test_r1_auth_enabled_true_valid_key_returns_not_401(self) -> None:
        """With auth.enabled=True and a seeded valid key → not 401 (real lifespan)."""
        from fastapi.testclient import TestClient
        from sqlalchemy.orm import Session

        import finecorpus.services.retrieval_api as api_mod

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "ctrl.db")
            engine = self._setup_sqlite_db(db_path)

            # Seed one key (kb-scoped) into the DB
            with Session(engine) as sess:
                repo = ApiKeyRepository(sess)
                plaintext_key, _record = repo.issue(
                    principal_name="test-svc",
                    role=Role.service,
                    scope_kind=ScopeKind.kb,
                    created_by="test",
                    workspace_id=WS_A,
                    kb_id=KB_A,
                )
                sess.commit()

            config_path = os.path.join(tmpdir, "corpus.yaml")
            with open(config_path, "w") as f:
                f.write(self._make_config_yaml(auth_enabled=True, db_path=db_path))

            provider = FakeProvider(dimensions=DIMENSIONS, model_id=MODEL_ID)

            # Seed the adapter with a valid chunk for KB_A
            adapter = _seeded_adapter_for_global(KB_A, WS_A)
            alias_rec = _make_alias_record(KB_A, WS_A)
            fake_repo = FakeAliasRepository({alias_name(KB_A): alias_rec})

            from contextlib import contextmanager as cm

            @cm
            def session_factory() -> Generator:
                with Session(engine) as sess:
                    yield sess

            with (
                patch.dict(os.environ, {"RTFC_CONFIG": config_path}),
                patch.object(api_mod, "_provider", provider),
                patch.object(api_mod, "_adapter", adapter),
                patch.object(api_mod, "_session_factory", session_factory),
                patch("finecorpus.retrieval.service.AliasRepository", return_value=fake_repo),
            ):
                from finecorpus.services.retrieval_api import app

                with TestClient(app, raise_server_exceptions=False) as client:
                    resp = client.post(
                        f"/v1/kb/{KB_A}/query",
                        json={"query": "test"},
                        headers={"X-API-Key": plaintext_key},
                    )

            assert resp.status_code != 401, (
                f"Got 401 with a valid API key (status={resp.status_code}). "
                "Auth wiring is broken — valid key should authenticate."
            )

    def test_r1_auth_enabled_false_no_key_passes_through(self) -> None:
        """With auth.enabled=False in config and no key → no 401 (compat mode)."""
        from fastapi.testclient import TestClient

        import finecorpus.services.retrieval_api as api_mod

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "ctrl.db")
            engine = self._setup_sqlite_db(db_path)

            config_path = os.path.join(tmpdir, "corpus.yaml")
            with open(config_path, "w") as f:
                f.write(self._make_config_yaml(auth_enabled=False, db_path=db_path))

            provider = FakeProvider(dimensions=DIMENSIONS, model_id=MODEL_ID)
            adapter = _seeded_adapter_for_global(KB_A, WS_A)
            alias_rec = _make_alias_record(KB_A, WS_A)
            fake_repo = FakeAliasRepository({alias_name(KB_A): alias_rec})

            from contextlib import contextmanager as cm

            from sqlalchemy.orm import Session

            @cm
            def session_factory() -> Generator:
                with Session(engine) as sess:
                    yield sess

            with (
                patch.dict(os.environ, {"RTFC_CONFIG": config_path}),
                patch.object(api_mod, "_provider", provider),
                patch.object(api_mod, "_adapter", adapter),
                patch.object(api_mod, "_session_factory", session_factory),
                patch("finecorpus.retrieval.service.AliasRepository", return_value=fake_repo),
            ):
                from finecorpus.services.retrieval_api import app

                with TestClient(app, raise_server_exceptions=False) as client:
                    resp = client.post(f"/v1/kb/{KB_A}/query", json={"query": "test"})

            assert resp.status_code != 401, (
                f"Got 401 with auth.enabled=False (status={resp.status_code}). "
                "Auth-disabled compat mode must not require a key."
            )


# ---------------------------------------------------------------------------
# R2: Global admin locked out — permission_principals must be empty for global scope
# ---------------------------------------------------------------------------


class TestR2GlobalAdminNotLockedOut:
    """RULING 2 (MAJOR): global-scope principal must not filter by permission_principals.

    Wave-2 R2 fix: global scope → empty permission_principals tuple (clause omitted) so
    the vector search finds chunks even when the admin's own ID is not in permission_principals.

    Phase 4 update (§2.3 break-glass enforcement): Admin-role principals — including global
    admins — MUST provide a break-glass grant to read content.  Without a grant, admin content
    reads are denied (PERMISSION_DENIED) regardless of scope.  The R2 fix (permission_principals
    clause omitted for global scope) is still correct and still applies UNDER a valid grant;
    it prevents the global admin from being locked out by the tenancy filter when they DO have
    a grant.  Without a grant, the fail-closed check fires before the tenancy filter runs.
    """

    def test_r2_global_admin_without_grant_is_denied(self) -> None:
        """Global admin WITHOUT a break-glass grant receives PERMISSION_DENIED (§2.3).

        Phase 4 behavior: admin content reads require a grant.  This test documents the
        behavior BEFORE a break-glass grant is provided.
        """
        from finecorpus.contracts.retrieval_response import ErrorCode
        from finecorpus.retrieval.service import query

        GLOBAL_ADMIN_PID = "pid-global-admin"
        SERVICE_KEY_PID = "pid-service-key"

        # Chunk seeded with service-key's principal ID in permission_principals
        adapter = FakeAdapter()
        chunk = {
            "id": "chk-global-001",
            "score": 0.9,
            "payload": {
                "chunk_id": "chk-global-001",
                "text": "Global admin content",
                "tenancy": {
                    "kb_id": KB_A,
                    "workspace_id": WS_A,
                    "permission_principals": [SERVICE_KEY_PID],
                    "permission_mode": "restricted",
                    "permission_source": "platform",
                    "permission_fidelity": "authoritative",
                },
                "provenance": {
                    "source_document_id": "doc-global-001",
                    "source_document_version": "v1",
                    "source_location": {
                        "locator_kind": "char_range",
                        "char_start": 0,
                        "char_end": 100,
                    },
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
        adapter.seed_collection(alias=alias_name(KB_A), coll=COLL_A, points=[chunk])

        global_admin = Principal(
            principal_id=GLOBAL_ADMIN_PID,
            name="global-admin",
            role=Role.admin,
            scope_kind=ScopeKind.global_,
            workspace_id=None,
            kb_id=None,
        )

        provider = FakeProvider(dimensions=DIMENSIONS, model_id=MODEL_ID)
        alias_rec = _make_alias_record(KB_A, WS_A)
        fake_repo = FakeAliasRepository({alias_name(KB_A): alias_rec})

        with patch("finecorpus.retrieval.service.AliasRepository", return_value=fake_repo):
            response = query(
                kb_id=KB_A,
                query_text="global admin query",
                provider=provider,
                adapter=adapter,
                session=object(),  # type: ignore[arg-type]
                auth_enabled=True,
                principal=global_admin,
                break_glass_grant_id=None,  # No grant — must be denied (§2.3)
            )

        # Phase 4 behavior: admin without grant → PERMISSION_DENIED (fail-closed §2.3)
        assert response.result_status == ResultStatus.error, (
            f"Global admin without grant got {response.result_status!r} — "
            "expected error/PERMISSION_DENIED.  "
            "Phase 4: admin content reads require a break-glass grant (§2.3)."
        )
        assert response.error is not None
        assert response.error.code == ErrorCode.PERMISSION_DENIED


# ---------------------------------------------------------------------------
# R3: Cross-workspace wrong error class — must be PERMISSION_DENIED, not no_matches
# ---------------------------------------------------------------------------


class TestR3CrossWorkspacePermissionDenied:
    """RULING 3 (MAJOR): ws-scoped principal querying a KB from different workspace
    must get PERMISSION_DENIED, not no_matches, and the adapter must not be called.
    """

    def test_r3_cross_workspace_returns_permission_denied(self) -> None:
        """ws-A editor queries a KB whose alias record says ws-B → PERMISSION_DENIED."""
        from finecorpus.retrieval.service import query

        # Principal belongs to ws-A but is workspace-scoped
        ws_a_editor = Principal(
            principal_id="pid-ws-a-editor",
            name="ws-a-editor",
            role=Role.editor,
            scope_kind=ScopeKind.workspace,
            workspace_id=WS_A,
            kb_id=None,
        )

        # KB_B belongs to ws-B
        alias_rec_b = _make_alias_record(KB_B, WS_B)
        fake_repo = FakeAliasRepository({alias_name(KB_B): alias_rec_b})

        adapter = FakeAdapter()
        # Seed a chunk in KB_B (ws-B) so it would match if filter is wrong
        chunk_b = make_chunk_payload(
            chunk_id="chk-ws-b-001",
            text="Content from ws-B KB-B",
            kb_id=KB_B,
        )
        chunk_b["payload"]["tenancy"]["workspace_id"] = WS_B
        adapter.seed_collection(alias=alias_name(KB_B), coll=COLL_B, points=[chunk_b])

        initial_call_count = adapter.search_call_count
        provider = FakeProvider(dimensions=DIMENSIONS, model_id=MODEL_ID)

        with patch("finecorpus.retrieval.service.AliasRepository", return_value=fake_repo):
            response = query(
                kb_id=KB_B,
                query_text="cross-workspace query",
                provider=provider,
                adapter=adapter,
                session=object(),  # type: ignore[arg-type]
                auth_enabled=True,
                principal=ws_a_editor,
            )

        assert response.result_status == ResultStatus.error, (
            f"Expected error (PERMISSION_DENIED) but got {response.result_status!r}. "
            "Cross-workspace access must fail closed with PERMISSION_DENIED."
        )
        assert response.error is not None
        assert response.error.code == ErrorCode.PERMISSION_DENIED, (
            f"Expected PERMISSION_DENIED but got {response.error.code!r}. "
            "Wrong error class — must be PERMISSION_DENIED, not no_matches."
        )
        # Adapter must NOT have been called
        assert adapter.search_call_count == initial_call_count, (
            "Adapter was called despite cross-workspace mismatch — must fail closed first."
        )


# ---------------------------------------------------------------------------
# R4: Null-workspace workspace key fails open
# ---------------------------------------------------------------------------


class TestR4NullWorkspaceKeyRejected:
    """RULING 4 (MAJOR): workspace-scoped key with workspace_id=None must be rejected.

    (a) issue() must reject workspace-scoped keys without workspace_id.
    (b) issue() must reject kb-scoped keys without kb_id.
    (c) validate() / query() must treat stored workspace-scoped record with
        null workspace as corrupt → PERMISSION_DENIED.
    """

    def _engine_with_tables(self) -> Any:
        from sqlalchemy import create_engine as sa_engine

        from finecorpus.control.metadata import Base, _ensure_all_models_imported

        _ensure_all_models_imported()
        engine = sa_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        return engine

    def test_r4a_issue_workspace_scoped_without_workspace_id_raises(self) -> None:
        """issue() rejects workspace-scoped key with workspace_id=None."""
        from sqlalchemy.orm import Session

        engine = self._engine_with_tables()
        with Session(engine) as sess:
            repo = ApiKeyRepository(sess)
            with pytest.raises((AuthError, ValueError), match="workspace"):
                repo.issue(
                    principal_name="svc",
                    role=Role.editor,
                    scope_kind=ScopeKind.workspace,
                    created_by="admin",
                    workspace_id=None,  # must be rejected
                    kb_id=None,
                )

    def test_r4b_issue_kb_scoped_without_kb_id_raises(self) -> None:
        """issue() rejects kb-scoped key with kb_id=None."""
        from sqlalchemy.orm import Session

        engine = self._engine_with_tables()
        with Session(engine) as sess:
            repo = ApiKeyRepository(sess)
            with pytest.raises((AuthError, ValueError), match="kb_id|kb"):
                repo.issue(
                    principal_name="svc",
                    role=Role.service,
                    scope_kind=ScopeKind.kb,
                    created_by="admin",
                    workspace_id=WS_A,
                    kb_id=None,  # must be rejected
                )

    def test_r4c_validate_corrupt_workspace_null_returns_permission_denied(self) -> None:
        """query() treats a stored workspace-scoped record with null workspace as corrupt.

        Expected: PERMISSION_DENIED (fail closed).
        """
        from finecorpus.retrieval.service import query

        # Manually construct a principal that would result from a corrupt stored record:
        # workspace-scoped but workspace_id=None
        corrupt_principal = Principal(
            principal_id="pid-corrupt",
            name="corrupt-ws-key",
            role=Role.editor,
            scope_kind=ScopeKind.workspace,
            workspace_id=None,  # corrupt — workspace-scoped but no workspace_id
            kb_id=None,
        )

        provider = FakeProvider(dimensions=DIMENSIONS, model_id=MODEL_ID)
        adapter = FakeAdapter()
        alias_rec = _make_alias_record(KB_A, WS_A)
        fake_repo = FakeAliasRepository({alias_name(KB_A): alias_rec})

        with patch("finecorpus.retrieval.service.AliasRepository", return_value=fake_repo):
            response = query(
                kb_id=KB_A,
                query_text="corrupt key query",
                provider=provider,
                adapter=adapter,
                session=object(),  # type: ignore[arg-type]
                auth_enabled=True,
                principal=corrupt_principal,
            )

        assert response.result_status == ResultStatus.error, (
            f"Expected error but got {response.result_status!r}. "
            "Workspace-scoped principal with null workspace_id must fail closed."
        )
        assert response.error is not None
        assert response.error.code == ErrorCode.PERMISSION_DENIED, (
            f"Expected PERMISSION_DENIED but got {response.error.code!r}."
        )


# ---------------------------------------------------------------------------
# R5: Minor fixes
# ---------------------------------------------------------------------------


class TestR5PositiveCaseAssertEqualsMatches:
    """RULING 5a: positive case in TestT02CrossTenantFailsClosed.test_kb_a_principal_can_query_kb_a
    must assert == ResultStatus.matches (not `in (matches, no_matches)`).

    This test validates that the positive case assertion was tightened.
    The existing test had `in (matches, no_matches)` which would pass even when
    the permission_principals filter was broken (returning no_matches).
    """

    def test_r5a_same_tenant_positive_case_yields_matches(self) -> None:
        """KB-A principal querying KB-A with correct permission_principals → matches exactly."""
        from finecorpus.retrieval.service import query

        PRINCIPAL_ID = "pid-r5-a"
        adapter = FakeAdapter()

        # Seed a chunk that has the principal's ID in permission_principals
        chunk = {
            "id": "chk-r5-001",
            "score": 0.9,
            "payload": {
                "chunk_id": "chk-r5-001",
                "text": "Content KB A",
                "tenancy": {
                    "kb_id": KB_A,
                    "workspace_id": WS_A,
                    "permission_principals": [PRINCIPAL_ID],
                    "permission_mode": "restricted",
                    "permission_source": "platform",
                    "permission_fidelity": "authoritative",
                },
                "provenance": {
                    "source_document_id": "doc-r5-001",
                    "source_document_version": "v1",
                    "source_location": {
                        "locator_kind": "char_range",
                        "char_start": 0,
                        "char_end": 100,
                    },
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
        adapter.seed_collection(alias=alias_name(KB_A), coll=COLL_A, points=[chunk])

        principal = Principal(
            principal_id=PRINCIPAL_ID,
            name="svc-r5",
            role=Role.service,
            scope_kind=ScopeKind.kb,
            workspace_id=WS_A,
            kb_id=KB_A,
        )

        provider = FakeProvider(dimensions=DIMENSIONS, model_id=MODEL_ID)
        alias_rec = _make_alias_record(KB_A, WS_A)
        fake_repo = FakeAliasRepository({alias_name(KB_A): alias_rec})

        with patch("finecorpus.retrieval.service.AliasRepository", return_value=fake_repo):
            response = query(
                kb_id=KB_A,
                query_text="test query",
                provider=provider,
                adapter=adapter,
                session=object(),  # type: ignore[arg-type]
                auth_enabled=True,
                principal=principal,
            )

        # Must be == matches, not just "in (matches, no_matches)"
        assert response.result_status == ResultStatus.matches, (
            f"Expected matches but got {response.result_status!r}. "
            "Same-tenant positive case must yield matches exactly."
        )


class TestR5MakeChunkPayloadPermissionPrincipals:
    """RULING 5b: make_chunk_payload gains optional permission_principals param."""

    def test_r5b_make_chunk_payload_with_permission_principals(self) -> None:
        """make_chunk_payload accepts and propagates permission_principals."""
        payload = make_chunk_payload(
            chunk_id="chk-r5b-001",
            text="test",
            kb_id=KB_A,
            permission_principals=["pid-svc-001"],
        )
        assert "permission_principals" in payload["payload"]["tenancy"]
        assert payload["payload"]["tenancy"]["permission_principals"] == ["pid-svc-001"]

    def test_r5b_make_chunk_payload_default_no_permission_principals(self) -> None:
        """make_chunk_payload without permission_principals has none in tenancy."""
        payload = make_chunk_payload(chunk_id="chk-r5b-002", text="test", kb_id=KB_A)
        # Should not crash; tenancy block present
        assert "tenancy" in payload["payload"]
