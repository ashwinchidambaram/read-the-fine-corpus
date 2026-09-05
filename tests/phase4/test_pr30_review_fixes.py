"""Regression tests for the PR #30 review findings.

Finding 1 (MAJOR): index parity drift — the ``create_tables()`` path emitted no
indexes on ``job_queue`` / ``service_principal_keys``, so dedupe coalescing and
key_prefix uniqueness silently did not exist outside Alembic-provisioned
Postgres. Dedupe must hold on the create_all path (app-level pre-check plus the
DB index as the concurrent-race backstop).

Finding 2 (MAJOR): ``ApiKeyRepository.issue()`` accepted arbitrary role/scope
strings at runtime; ``validate()`` then crashed with a bare ``ValueError``
instead of the typed ``AuthError``.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import Session

from finecorpus.control.auth import ApiKeyRepository, AuthError, Role, ScopeKind
from finecorpus.control.jobs import JobQueueRepository
from finecorpus.control.metadata import create_tables


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite://")
    create_tables(engine)
    return Session(engine)


class TestDedupeOnCreateAllPath:
    def test_enqueue_twice_same_dedupe_key_coalesces(self, session: Session) -> None:
        repo = JobQueueRepository(session)
        j1 = repo.enqueue(
            kb_id="kb1",
            workspace_id="ws1",
            job_type="reindex_full",
            payload={},
            dedupe_key="k1",
        )
        j2 = repo.enqueue(
            kb_id="kb1",
            workspace_id="ws1",
            job_type="reindex_full",
            payload={},
            dedupe_key="k1",
        )
        assert j2.job_id == j1.job_id, (
            "second enqueue with the same active (kb_id, dedupe_key) must coalesce"
        )

    def test_reenqueue_after_completion_creates_new_job(self, session: Session) -> None:
        repo = JobQueueRepository(session)
        j1 = repo.enqueue(
            kb_id="kb1",
            workspace_id="ws1",
            job_type="reindex_full",
            payload={},
            dedupe_key="k1",
        )
        claimed = repo.claim("w1")
        assert claimed is not None
        repo.complete(claimed.job_id)
        j2 = repo.enqueue(
            kb_id="kb1",
            workspace_id="ws1",
            job_type="reindex_full",
            payload={},
            dedupe_key="k1",
        )
        assert j2.job_id != j1.job_id, "completed jobs must not block re-enqueue"

    def test_create_all_emits_job_queue_indexes(self) -> None:
        engine = create_engine("sqlite://")
        create_tables(engine)
        insp = inspect(engine)
        names = {ix["name"] for ix in insp.get_indexes("job_queue")}
        assert any("dedupe" in n for n in names), (
            f"create_all must emit the dedupe index; got {names}"
        )

    def test_create_all_enforces_key_prefix_uniqueness(self) -> None:
        engine = create_engine("sqlite://")
        create_tables(engine)
        insp = inspect(engine)
        uniques = {
            ix["name"] for ix in insp.get_indexes("service_principal_keys") if ix.get("unique")
        }
        cols_unique = any(
            "key_prefix" in ix["column_names"]
            for ix in insp.get_indexes("service_principal_keys")
            if ix.get("unique")
        )
        assert uniques and cols_unique, "key_prefix must be unique on the create_all path"


class TestRoleValidation:
    def test_issue_rejects_invalid_role(self, session: Session) -> None:
        repo = ApiKeyRepository(session)
        with pytest.raises(ValueError, match="[Rr]ole"):
            repo.issue(
                principal_name="t",
                role="totally_bogus",  # type: ignore[arg-type]
                scope_kind=ScopeKind.kb,
                kb_id="kb1",
                created_by="admin",
            )

    def test_issue_rejects_invalid_scope_kind(self, session: Session) -> None:
        repo = ApiKeyRepository(session)
        with pytest.raises(ValueError, match="[Ss]cope"):
            repo.issue(
                principal_name="t",
                role=Role.service,
                scope_kind="bogus_scope",  # type: ignore[arg-type]
                kb_id="kb1",
                created_by="admin",
            )

    def test_validate_wraps_corrupt_stored_role_in_auth_error(self, session: Session) -> None:
        repo = ApiKeyRepository(session)
        plaintext, record = repo.issue(
            principal_name="t",
            role=Role.service,
            scope_kind=ScopeKind.kb,
            kb_id="kb1",
            created_by="admin",
        )
        # Simulate a corrupt row written by a buggy/older writer.
        record.role = "corrupted_role_value"
        session.flush()
        with pytest.raises(AuthError):
            repo.validate(plaintext)
