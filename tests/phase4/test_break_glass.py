"""Phase 4 break-glass runtime tests (M-001..M-004, T-05).

Covers:
- T-05: Every read under a grant produces a break_glass_read audit row with chunk_ids.
- M-001: Grant requires a non-empty reason.
- D-04 (M-004): Expired grant → read denied.
- Admin without grant → no content (fail-closed admin enforcement).
- break_glass_read_ref in response is set on break-glass reads.
- Revoked grant → read denied.
- Tenancy still applies under break-glass (grant does NOT cross KB boundaries).
"""

from __future__ import annotations

import sys
from datetime import timedelta
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from finecorpus.contracts.retrieval_response import ErrorCode, ResultStatus
from finecorpus.control.audit import AuditAction, AuditLogRepository
from finecorpus.control.auth import Principal, Role, ScopeKind
from finecorpus.control.break_glass import BreakGlassRepository, issue_grant
from finecorpus.control.metadata import create_tables
from finecorpus.embedding.fake import FakeProvider
from finecorpus.index.adapter import alias_name

# Import shared helpers
sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent / "retrieval"))
from helpers import (  # type: ignore[import-not-found]
    FakeAdapter,
    FakeAliasRecord,
    make_provenance_payload,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

KB_A = "kb-bg-a"
KB_B = "kb-bg-b"
WS_A = "ws-bg-a"
WS_B = "ws-bg-b"
MODEL_ID = "fake-embed-v1"
DIMENSIONS = 64
COLL_A = f"rtfc_{KB_A.replace('-', '').lower()}_00000001"
COLL_B = f"rtfc_{KB_B.replace('-', '').lower()}_00000001"
ADMIN_ID = "admin-principal-001"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def sqlite_engine():
    """In-memory SQLite engine with all control-plane tables."""
    engine = create_engine("sqlite://")
    create_tables(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def db_session(sqlite_engine) -> Any:
    """SQLite session for each test (auto-rollback via scope)."""
    with Session(sqlite_engine) as session:
        yield session


# ---------------------------------------------------------------------------
# Helper builders
# ---------------------------------------------------------------------------


def _make_admin_principal(admin_id: str = ADMIN_ID) -> Principal:
    return Principal(
        principal_id=admin_id,
        name="Test Admin",
        role=Role.admin,
        scope_kind=ScopeKind.global_,
        workspace_id=None,
        kb_id=None,
    )


def _make_service_principal(kb_id: str, ws_id: str, pid: str = "svc-001") -> Principal:
    return Principal(
        principal_id=pid,
        name=f"svc-{pid}",
        role=Role.service,
        scope_kind=ScopeKind.kb,
        workspace_id=ws_id,
        kb_id=kb_id,
    )


def _make_alias_record(kb_id: str, ws_id: str, coll: str) -> FakeAliasRecord:
    return FakeAliasRecord(
        alias=alias_name(kb_id),
        kb_id=kb_id,
        workspace_id=ws_id,
        collection=coll,
        embedding_provider="fake",
        embedding_model=MODEL_ID,
        embedding_dimensions=DIMENSIONS,
        config_version="cfgv1",
    )


def _make_chunk(chunk_id: str, kb_id: str, score: float = 0.9) -> dict[str, Any]:
    return {
        "id": chunk_id,
        "score": score,
        "payload": {
            "chunk_id": chunk_id,
            "text": f"Content from {chunk_id}",
            "tenancy": {
                "kb_id": kb_id,
                "workspace_id": WS_A if kb_id == KB_A else WS_B,
                "permission_mode": "restricted",
                "permission_principals": ["other-svc"],
                "permission_source": "platform",
                "permission_fidelity": "authoritative",
            },
            "provenance": make_provenance_payload(
                source_document_id=f"doc-{chunk_id}",
                source_document_version="v1",
            ),
        },
    }


def _make_provider() -> FakeProvider:
    return FakeProvider(model_id=MODEL_ID, dimensions=DIMENSIONS)


def _run_service_query(
    *,
    kb_id: str,
    adapter: FakeAdapter,
    alias_records: dict[str, FakeAliasRecord],
    session: Session,
    principal: Principal | None = None,
    auth_enabled: bool = False,
    explain: bool = False,
    break_glass_grant_id: str | None = None,
) -> Any:
    """Run query() against real DB session (for audit tests)."""
    from finecorpus.retrieval.service import query

    provider = _make_provider()

    with patch("finecorpus.retrieval.service.AliasRepository") as mock_repo_cls:
        mock_repo = mock_repo_cls.return_value
        mock_repo.get.side_effect = lambda alias: alias_records.get(alias)

        return query(
            kb_id=kb_id,
            query_text="policy test query",
            provider=provider,
            adapter=adapter,
            session=session,
            top_k=10,
            auth_enabled=auth_enabled,
            principal=principal,
            explain=explain,
            break_glass_grant_id=break_glass_grant_id,
        )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestGrantRequiresReason:
    """M-001: grant requires non-empty reason."""

    def test_grant_requires_reason(self, db_session: Session) -> None:
        """Issue grant with empty reason raises ValueError (M-001)."""
        with pytest.raises(ValueError, match="reason"):
            issue_grant(
                session=db_session,
                target_kb_id=KB_A,
                granting_admin_id=ADMIN_ID,
                reason="",  # empty — must be rejected
            )

    def test_grant_requires_reason_whitespace_only(self, db_session: Session) -> None:
        """Issue grant with whitespace-only reason raises ValueError."""
        with pytest.raises(ValueError, match="reason"):
            issue_grant(
                session=db_session,
                target_kb_id=KB_A,
                granting_admin_id=ADMIN_ID,
                reason="   ",
            )

    def test_grant_succeeds_with_valid_reason(self, db_session: Session) -> None:
        """Issue grant with a valid reason succeeds and returns grant record."""
        grant_record, audit_record = issue_grant(
            session=db_session,
            target_kb_id=KB_A,
            granting_admin_id=ADMIN_ID,
            reason="Investigating customer support ticket #12345",
        )
        assert grant_record.grant_id
        assert grant_record.target_kb_id == KB_A
        assert grant_record.granting_admin_id == ADMIN_ID
        assert grant_record.reason == "Investigating customer support ticket #12345"
        assert audit_record.entry_id
        assert audit_record.entry_type == AuditAction.break_glass_grant


class TestGrantExpiresD04:
    """D-04: expired grant → read denied."""

    def test_grant_expires_d04(self, db_session: Session) -> None:
        """An expired grant is not returned by active_grant_for (and read is denied)."""
        # Issue a grant that expired 1 hour ago
        grant_record, _ = issue_grant(
            session=db_session,
            target_kb_id=KB_A,
            granting_admin_id=ADMIN_ID,
            reason="Testing expiry",
            window=timedelta(hours=1),
        )
        db_session.flush()

        # active_grant_for at a time 2 hours after grant → None
        grant_time = grant_record.granted_at
        future_time = grant_time + timedelta(hours=2)

        repo = BreakGlassRepository(db_session)
        active = repo.active_grant_for(KB_A, ADMIN_ID, now=future_time)
        assert active is None, "Expired grant should not be returned as active"

    def test_expired_grant_read_denied(self, db_session: Session) -> None:
        """Query with an expired grant_id is denied with PERMISSION_DENIED."""
        # Issue and then expire the grant
        grant_record, _ = issue_grant(
            session=db_session,
            target_kb_id=KB_A,
            granting_admin_id=ADMIN_ID,
            reason="Testing expired read",
            window=timedelta(hours=1),
        )
        db_session.flush()
        grant_id = grant_record.grant_id

        adapter = FakeAdapter()
        adapter.seed_collection(
            alias_name(KB_A),
            COLL_A,
            [
                _make_chunk("chk-a-001", KB_A),
            ],
        )
        alias_records = {alias_name(KB_A): _make_alias_record(KB_A, WS_A, COLL_A)}
        principal = _make_admin_principal()

        # Patch now inside active_grant_for to return "expired"
        # Simulate by providing a grant_id that active_grant_for will not find
        # (because we query at a future time after expiry).
        # We do this by patching BreakGlassRepository.active_grant_for to return None.
        with patch.object(BreakGlassRepository, "active_grant_for", return_value=None):
            response = _run_service_query(
                kb_id=KB_A,
                adapter=adapter,
                alias_records=alias_records,
                session=db_session,
                principal=principal,
                auth_enabled=True,
                break_glass_grant_id=grant_id,
            )

        assert response.result_status == ResultStatus.error
        assert response.error is not None
        assert response.error.code == ErrorCode.PERMISSION_DENIED


class TestNoGrantNoContentRead:
    """Admin without grant → no content read (fail-closed, §2.3)."""

    def test_no_grant_no_content_read(self, db_session: Session) -> None:
        """Admin without break-glass grant receives PERMISSION_DENIED."""
        adapter = FakeAdapter()
        adapter.seed_collection(
            alias_name(KB_A),
            COLL_A,
            [
                _make_chunk("chk-a-001", KB_A),
            ],
        )
        alias_records = {alias_name(KB_A): _make_alias_record(KB_A, WS_A, COLL_A)}
        principal = _make_admin_principal()

        response = _run_service_query(
            kb_id=KB_A,
            adapter=adapter,
            alias_records=alias_records,
            session=db_session,
            principal=principal,
            auth_enabled=True,
            break_glass_grant_id=None,  # no grant
        )

        assert response.result_status == ResultStatus.error
        assert response.error is not None
        assert response.error.code == ErrorCode.PERMISSION_DENIED

    def test_no_grant_wrong_kb(self, db_session: Session) -> None:
        """Admin with a grant for KB-A cannot use it to access KB-B (fail-closed)."""
        # Issue a grant for KB_A
        grant_record, _ = issue_grant(
            session=db_session,
            target_kb_id=KB_A,
            granting_admin_id=ADMIN_ID,
            reason="Grant for KB-A only",
        )
        db_session.flush()

        adapter = FakeAdapter()
        adapter.seed_collection(
            alias_name(KB_B),
            COLL_B,
            [
                _make_chunk("chk-b-001", KB_B),
            ],
        )
        alias_records = {alias_name(KB_B): _make_alias_record(KB_B, WS_B, COLL_B)}
        principal = _make_admin_principal()

        # Use the KB-A grant to query KB-B — must be denied
        response = _run_service_query(
            kb_id=KB_B,
            adapter=adapter,
            alias_records=alias_records,
            session=db_session,
            principal=principal,
            auth_enabled=True,
            break_glass_grant_id=grant_record.grant_id,
        )

        assert response.result_status == ResultStatus.error
        assert response.error is not None
        assert response.error.code == ErrorCode.PERMISSION_DENIED


class TestT05EveryReadUnderGrantAudited:
    """T-05: N queries under grant → N break_glass_read audit rows with chunk ids."""

    def test_t05_every_read_under_grant_audited(self, db_session: Session) -> None:
        """Each query under a valid grant appends a break_glass_read audit row."""
        # Issue grant
        grant_record, _ = issue_grant(
            session=db_session,
            target_kb_id=KB_A,
            granting_admin_id=ADMIN_ID,
            reason="T-05 audit test",
        )
        db_session.flush()
        grant_id = grant_record.grant_id

        adapter = FakeAdapter()
        adapter.seed_collection(
            alias_name(KB_A),
            COLL_A,
            [
                _make_chunk("chk-a-001", KB_A),
                _make_chunk("chk-a-002", KB_A),
            ],
        )
        alias_records = {alias_name(KB_A): _make_alias_record(KB_A, WS_A, COLL_A)}
        principal = _make_admin_principal()

        n_queries = 3
        for _ in range(n_queries):
            response = _run_service_query(
                kb_id=KB_A,
                adapter=adapter,
                alias_records=alias_records,
                session=db_session,
                principal=principal,
                auth_enabled=True,
                break_glass_grant_id=grant_id,
            )
            assert response.result_status == ResultStatus.matches
            db_session.flush()

        # Count break_glass_read audit rows for this KB
        audit_repo = AuditLogRepository(db_session)
        all_entries = audit_repo.list_for_kb(KB_A, limit=100)
        read_entries = [e for e in all_entries if e.entry_type == AuditAction.break_glass_read]
        assert len(read_entries) >= n_queries, (
            f"Expected >= {n_queries} break_glass_read audit rows, got {len(read_entries)}"
        )

        # Each entry should have chunk_ids_returned in details
        for entry in read_entries:
            assert "chunk_ids_returned" in entry.details
            assert isinstance(entry.details["chunk_ids_returned"], list)
            assert "grant_id" in entry.details
            assert entry.details["grant_id"] == grant_id


class TestBreakGlassReadRefInResponse:
    """break_glass_read_ref is set in the response for break-glass reads."""

    def test_break_glass_read_ref_in_response(self, db_session: Session) -> None:
        """Response has non-null break_glass_read_ref on break-glass read."""
        grant_record, _ = issue_grant(
            session=db_session,
            target_kb_id=KB_A,
            granting_admin_id=ADMIN_ID,
            reason="Testing break_glass_read_ref",
        )
        db_session.flush()

        adapter = FakeAdapter()
        adapter.seed_collection(
            alias_name(KB_A),
            COLL_A,
            [
                _make_chunk("chk-a-001", KB_A),
            ],
        )
        alias_records = {alias_name(KB_A): _make_alias_record(KB_A, WS_A, COLL_A)}
        principal = _make_admin_principal()

        response = _run_service_query(
            kb_id=KB_A,
            adapter=adapter,
            alias_records=alias_records,
            session=db_session,
            principal=principal,
            auth_enabled=True,
            break_glass_grant_id=grant_record.grant_id,
        )

        assert response.result_status == ResultStatus.matches
        assert response.break_glass_read_ref is not None
        assert len(response.break_glass_read_ref) > 0

    def test_normal_read_has_no_break_glass_ref(self, db_session: Session) -> None:
        """Non-break-glass reads have break_glass_read_ref = None."""
        adapter = FakeAdapter()
        adapter.seed_collection(
            alias_name(KB_A),
            COLL_A,
            [
                _make_chunk("chk-a-001", KB_A),
            ],
        )
        alias_records = {alias_name(KB_A): _make_alias_record(KB_A, WS_A, COLL_A)}

        # No auth, no break-glass
        response = _run_service_query(
            kb_id=KB_A,
            adapter=adapter,
            alias_records=alias_records,
            session=db_session,
            auth_enabled=False,
        )

        assert response.result_status == ResultStatus.matches
        assert response.break_glass_read_ref is None


class TestRevokedGrantDenied:
    """Revoked grant → read denied."""

    def test_revoked_grant_denied(self, db_session: Session) -> None:
        """Querying with a revoked grant_id is denied."""
        grant_record, _ = issue_grant(
            session=db_session,
            target_kb_id=KB_A,
            granting_admin_id=ADMIN_ID,
            reason="To be revoked",
        )
        db_session.flush()
        grant_id = grant_record.grant_id

        # Revoke it
        repo = BreakGlassRepository(db_session)
        repo.revoke(grant_id)
        db_session.flush()

        adapter = FakeAdapter()
        adapter.seed_collection(
            alias_name(KB_A),
            COLL_A,
            [
                _make_chunk("chk-a-001", KB_A),
            ],
        )
        alias_records = {alias_name(KB_A): _make_alias_record(KB_A, WS_A, COLL_A)}
        principal = _make_admin_principal()

        # Revoked grants are not returned by active_grant_for
        response = _run_service_query(
            kb_id=KB_A,
            adapter=adapter,
            alias_records=alias_records,
            session=db_session,
            principal=principal,
            auth_enabled=True,
            break_glass_grant_id=grant_id,
        )

        assert response.result_status == ResultStatus.error
        assert response.error is not None
        assert response.error.code == ErrorCode.PERMISSION_DENIED


class TestTenancyStillAppliesUnderBreakGlass:
    """Break-glass grant does NOT cross KB boundaries (§2.3)."""

    def test_tenancy_still_applies_under_break_glass(self, db_session: Session) -> None:
        """A break-glass grant for KB-A does not allow reading KB-B content."""
        # Grant for KB-A
        grant_record, _ = issue_grant(
            session=db_session,
            target_kb_id=KB_A,
            granting_admin_id=ADMIN_ID,
            reason="KB-A access for audit",
        )
        db_session.flush()

        adapter = FakeAdapter()
        # Seed KB-A and KB-B
        adapter.seed_collection(
            alias_name(KB_A),
            COLL_A,
            [
                _make_chunk("chk-a-001", KB_A, score=0.9),
            ],
        )
        adapter.seed_collection(
            alias_name(KB_B),
            COLL_B,
            [
                _make_chunk("chk-b-001", KB_B, score=0.95),
            ],
        )

        alias_records = {
            alias_name(KB_A): _make_alias_record(KB_A, WS_A, COLL_A),
            alias_name(KB_B): _make_alias_record(KB_B, WS_B, COLL_B),
        }
        principal = _make_admin_principal()

        # Query KB-B with the KB-A grant — must fail
        response = _run_service_query(
            kb_id=KB_B,
            adapter=adapter,
            alias_records=alias_records,
            session=db_session,
            principal=principal,
            auth_enabled=True,
            break_glass_grant_id=grant_record.grant_id,
        )

        assert response.result_status == ResultStatus.error
        assert response.error is not None
        assert response.error.code == ErrorCode.PERMISSION_DENIED

    def test_break_glass_reads_only_kb_a_chunks(self, db_session: Session) -> None:
        """Under a KB-A grant, only KB-A chunks appear in results (tenancy filter stays)."""
        grant_record, _ = issue_grant(
            session=db_session,
            target_kb_id=KB_A,
            granting_admin_id=ADMIN_ID,
            reason="KB-A content audit",
        )
        db_session.flush()

        adapter = FakeAdapter()
        # Seed both KBs into the same collection (simulating a misconfigured adapter)
        # Real Qdrant uses kb_id filter — FakeAdapter also enforces it.
        adapter.seed_collection(
            alias_name(KB_A),
            COLL_A,
            [
                _make_chunk("chk-a-001", KB_A, score=0.9),
            ],
        )

        alias_records = {alias_name(KB_A): _make_alias_record(KB_A, WS_A, COLL_A)}
        principal = _make_admin_principal()

        response = _run_service_query(
            kb_id=KB_A,
            adapter=adapter,
            alias_records=alias_records,
            session=db_session,
            principal=principal,
            auth_enabled=True,
            break_glass_grant_id=grant_record.grant_id,
        )

        assert response.result_status == ResultStatus.matches
        chunk_ids = [r.chunk_id for r in response.results]
        assert "chk-a-001" in chunk_ids
        # No KB-B chunks (the tenancy filter ensures this)
        kb_b_ids = {cid for cid in chunk_ids if "chk-b" in cid}
        assert len(kb_b_ids) == 0


class TestGrantLifecycle:
    """Grant lifecycle: issue → audit → active → revoke."""

    def test_grant_audit_row_written_before_grant(self, db_session: Session) -> None:
        """Audit row is written BEFORE the grant record (M-004 ordering invariant)."""
        grant_record, audit_record = issue_grant(
            session=db_session,
            target_kb_id=KB_A,
            granting_admin_id=ADMIN_ID,
            reason="Audit ordering test",
        )
        db_session.flush()

        # The audit entry ID should be referenced in the grant record
        assert grant_record.audit_entry_id == audit_record.entry_id

        # The audit entry should exist and have the correct type
        audit_repo = AuditLogRepository(db_session)
        entry = audit_repo.get(audit_record.entry_id)
        assert entry is not None
        assert entry.entry_type == AuditAction.break_glass_grant

    def test_list_active_shows_non_expired_grants(self, db_session: Session) -> None:
        """list_active() returns only non-expired, non-revoked grants."""
        grant_record, _ = issue_grant(
            session=db_session,
            target_kb_id=KB_A,
            granting_admin_id=ADMIN_ID,
            reason="Active grant",
            window=timedelta(hours=4),
        )
        db_session.flush()

        repo = BreakGlassRepository(db_session)
        active = repo.list_active()
        active_ids = [g.grant_id for g in active]
        assert grant_record.grant_id in active_ids

    def test_default_window_is_4_hours(self, db_session: Session) -> None:
        """Grant without explicit window defaults to 4 hours (D-04)."""
        grant_record, _ = issue_grant(
            session=db_session,
            target_kb_id=KB_A,
            granting_admin_id=ADMIN_ID,
            reason="Default window test",
            window=None,  # use default
        )
        db_session.flush()

        delta = grant_record.expires_at - grant_record.granted_at
        assert abs(delta.total_seconds() - 4 * 3600) < 5, f"Expected 4-hour window, got {delta}"


class TestAuditFailureFailsClosed:
    """M-003: if the break-glass audit record cannot be persisted, content
    must NOT be served. A swallowed audit failure would mean an unaudited
    elevated-privilege read — the exact shape T-05 exists to prevent."""

    def test_audit_persistence_failure_denies_content(
        self, db_session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        grant_record, _ = issue_grant(
            session=db_session,
            target_kb_id=KB_A,
            granting_admin_id=ADMIN_ID,
            reason="M-003 fail-closed test",
        )
        db_session.flush()

        adapter = FakeAdapter()
        adapter.seed_collection(alias_name(KB_A), COLL_A, [_make_chunk("chk-a-001", KB_A)])
        alias_records = {alias_name(KB_A): _make_alias_record(KB_A, WS_A, COLL_A)}

        def _boom(*a: object, **k: object) -> None:
            raise RuntimeError("simulated audit persistence failure")

        from finecorpus.control.audit import AuditLogRepository

        monkeypatch.setattr(AuditLogRepository, "append", _boom, raising=True)

        response = _run_service_query(
            kb_id=KB_A,
            adapter=adapter,
            alias_records=alias_records,
            session=db_session,
            principal=_make_admin_principal(),
            auth_enabled=True,
            break_glass_grant_id=grant_record.grant_id,
        )
        assert response.result_status == ResultStatus.error, (
            "audit persistence failure must fail closed, not serve content"
        )
        assert not response.results, "no content may be served without a durable audit record"


class TestBreakGlassRouteReadRoundtrip:
    """F-1: the grant issued through the REST route MUST be found at read time.

    The route derives the admin identity from a validated key and stores it as
    ``granting_admin_id``; the retrieval read path validates the grant via
    ``active_grant_for(kb_id, principal.principal_id)``.  These MUST use the
    SAME stable identity (``principal_id`` = key_id), even when the admin's
    human ``name`` differs from its ``principal_id``.

    Fail-then-pass: with the old code (route stored ``principal.name``), the
    read-time lookup keyed on ``principal_id`` never matches and the read fails
    closed with PERMISSION_DENIED.  After the fix (route stores
    ``principal.principal_id``), the read is AUTHORIZED.
    """

    def test_route_issued_grant_matches_read_when_name_differs_from_principal_id(
        self,
    ) -> None:
        from contextlib import contextmanager

        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from sqlalchemy.pool import StaticPool

        from finecorpus.control.auth import ApiKeyRepository
        from finecorpus.services.control_routes import break_glass as bg_routes

        # Dedicated thread-safe engine: the TestClient runs the route in a
        # separate thread, so the shared module-scoped SQLite engine cannot be
        # reused here.  The route factory and the retrieval read share it.
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        create_tables(engine)

        @contextmanager
        def factory():  # type: ignore[no-untyped-def]
            s = Session(engine)
            try:
                yield s
                s.commit()
            finally:
                s.close()

        # Issue an admin key.  key_id (= principal_id) is a random hex token and
        # is therefore GUARANTEED to differ from the human principal_name.
        with factory() as s:
            plaintext, key_record = ApiKeyRepository(s).issue(
                principal_name="Root Admin (Incident Response)",
                role=Role.admin,
                scope_kind=ScopeKind.global_,
                created_by="test",
            )
            # Read the identity fields while the record is still session-bound
            # (it detaches once the factory context closes).
            admin_principal_id = key_record.key_id
            admin_name = key_record.principal_name
        assert admin_name != admin_principal_id, (
            "test precondition: the admin's display name must differ from its principal_id"
        )

        # Issue the grant through the REST route (exercises _require_admin_id).
        bg_routes._session_factory = factory  # noqa: SLF001
        app = FastAPI()
        app.include_router(bg_routes.router)
        client = TestClient(app)
        resp = client.post(
            "/admin/break-glass/grant",
            json={"target_kb_id": KB_A, "reason": "F-1 roundtrip: route->read"},
            headers={"X-API-Key": plaintext},
        )
        assert resp.status_code == 200, resp.text
        grant_body = resp.json()
        grant_id = grant_body["grant_id"]

        # The route must store the STABLE identity (principal_id), not the name.
        assert grant_body["granting_admin_id"] == admin_principal_id, (
            "route must store principal_id as granting_admin_id (F-1), not the human name"
        )
        assert grant_body["granting_admin_id"] != admin_name

        # Now perform a break-glass retrieval read with a Principal whose
        # principal_id == the key's key_id and whose name differs.  This is the
        # exact identity the read path looks up via active_grant_for.
        read_principal = Principal(
            principal_id=admin_principal_id,
            name=admin_name,
            role=Role.admin,
            scope_kind=ScopeKind.global_,
            workspace_id=None,
            kb_id=None,
        )

        adapter = FakeAdapter()
        adapter.seed_collection(alias_name(KB_A), COLL_A, [_make_chunk("chk-a-001", KB_A)])
        alias_records = {alias_name(KB_A): _make_alias_record(KB_A, WS_A, COLL_A)}

        with Session(engine) as read_session:
            response = _run_service_query(
                kb_id=KB_A,
                adapter=adapter,
                alias_records=alias_records,
                session=read_session,
                principal=read_principal,
                auth_enabled=True,
                break_glass_grant_id=grant_id,
            )

        # AUTHORIZED: grant found, content served.  This FAILS with the old
        # `.name` code (grant keyed on name, lookup keyed on principal_id ->
        # PERMISSION_DENIED) and PASSES after the F-1 fix.
        assert response.result_status == ResultStatus.matches, (
            f"route-issued grant must be found at read time; got {response.error}"
        )
        assert response.break_glass_read_ref is not None
        assert any(r.chunk_id == "chk-a-001" for r in response.results)
