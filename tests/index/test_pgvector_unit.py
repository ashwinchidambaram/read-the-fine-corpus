"""Unit tests for the pgvector adapter's SQL/filter translation (no live DB).

The tenancy-filter translation is a SECURITY boundary (§11.4 M-060): it MUST be
as strict as Qdrant's.  These tests exercise ``_filter_to_sql`` / ``_jsonb_path``
and the identifier validators directly, without a database, so the security-
critical logic is covered even where pgvector Postgres is unavailable.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any

import pytest

from finecorpus.index.adapter import IndexError
from finecorpus.index.pgvector.backend import (
    PgVectorAdapter,
    _filter_to_sql,
    _jsonb_path,
    _validate_identifier,
)


def test_filter_to_sql_empty() -> None:
    where, params = _filter_to_sql(None)
    assert where == ""
    assert params == []
    where, params = _filter_to_sql({})
    assert where == ""
    assert params == []


def test_filter_to_sql_scalar_equality() -> None:
    where, params = _filter_to_sql({"tenancy.kb_id": "kb-123"})
    assert where == "payload #>> '{tenancy,kb_id}' = %s"
    assert params == ["kb-123"]


def test_filter_to_sql_multiple_clauses_are_and_joined() -> None:
    where, params = _filter_to_sql({"tenancy.kb_id": "kb-1", "tenancy.workspace_id": "ws-1"})
    # Order preserved from dict insertion; both mandatory (AND).
    assert " AND " in where
    assert "payload #>> '{tenancy,kb_id}' = %s" in where
    assert "payload #>> '{tenancy,workspace_id}' = %s" in where
    assert params == ["kb-1", "ws-1"]


def test_filter_to_sql_contains_sentinel_array_membership() -> None:
    """The __contains__ sentinel → JSONB array-contains (mirrors Qdrant array match)."""
    where, params = _filter_to_sql({"tenancy.permission_principals": {"__contains__": "user-7"}})
    assert where == "payload #> '{tenancy,permission_principals}' @> to_jsonb(%s::text)"
    assert params == ["user-7"]


def test_filter_to_sql_values_are_parameterized_not_interpolated() -> None:
    """A malicious value must land in params, never in the SQL text."""
    evil = "kb'; DROP TABLE rtfc_pgvector_collections; --"
    where, params = _filter_to_sql({"tenancy.kb_id": evil})
    assert evil not in where
    assert params == [evil]


def test_jsonb_path_rejects_injection_in_key() -> None:
    with pytest.raises(IndexError):
        _jsonb_path("tenancy.kb_id'; DROP TABLE x; --")


def test_jsonb_path_valid() -> None:
    assert _jsonb_path("tenancy.kb_id") == "{tenancy,kb_id}"
    assert _jsonb_path("provenance.source_document_id") == "{provenance,source_document_id}"


def test_validate_identifier_accepts_collection_names() -> None:
    assert _validate_identifier("rtfc_abc123_00000001") == "rtfc_abc123_00000001"


def test_validate_identifier_rejects_injection() -> None:
    with pytest.raises(IndexError):
        _validate_identifier("foo; DROP TABLE bar")
    with pytest.raises(IndexError):
        _validate_identifier("has-hyphen")


# ---------------------------------------------------------------------------
# Fake-cursor harness for search() — asserts the HNSW recall mitigation
# (raised hnsw.ef_search) is issued, without a live database.
# ---------------------------------------------------------------------------


class _FakeCursor:
    """Records every executed statement; returns no rows for the SELECT."""

    def __init__(self, log: list[tuple[str, Any]]) -> None:
        self._log = log

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self._log.append((sql, params))

    def fetchall(self) -> list[Any]:
        return []


class _FakeConn:
    def __init__(self, log: list[tuple[str, Any]]) -> None:
        self._log = log

    @contextmanager
    def transaction(self):  # type: ignore[no-untyped-def]
        yield self

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self._log)


def _adapter_with_fake_conn(log: list[tuple[str, Any]]) -> PgVectorAdapter:
    """Build an adapter that skips __init__/DB connect and wires a fake conn."""
    adapter = object.__new__(PgVectorAdapter)
    adapter._conn = _FakeConn(log)  # type: ignore[attr-defined]
    return adapter


@pytest.mark.parametrize(
    ("top_k", "expected_ef"),
    [
        (1, 100),  # floor: max(1*4, 100) = 100
        (10, 100),  # max(40, 100) = 100
        (50, 200),  # 50*4 = 200
        (500, 1000),  # 500*4 = 2000, capped at 1000
    ],
)
def test_search_sets_hnsw_ef_search_scaled_to_top_k(
    monkeypatch: pytest.MonkeyPatch, top_k: int, expected_ef: int
) -> None:
    """search() MUST raise the HNSW candidate pool via SET LOCAL hnsw.ef_search,
    scaled to top_k (floor 100, cap 1000) — the filtered-recall mitigation."""
    log: list[tuple[str, Any]] = []
    adapter = _adapter_with_fake_conn(log)
    # Stub alias resolution so we don't touch a DB.
    monkeypatch.setattr(adapter, "_resolve_for_query", lambda alias: "rtfc_kb_00000001")

    results = adapter.search(
        "rtfc_kb__alias",
        [0.1, 0.2, 0.3, 0.4],
        top_k=top_k,
        payload_filter={"tenancy.kb_id": "kb-1"},
    )
    assert results == []

    # Postgres SET does not accept bound parameters, so ef_search is INLINED in
    # the statement text (verified against live pgvector — a bound `%s` raises
    # "syntax error at or near $1"). Assert the inlined value, not a param tuple.
    ef_stmts = [sql for sql, _params in log if "hnsw.ef_search" in sql and "SET LOCAL" in sql]
    assert ef_stmts, f"expected a SET LOCAL hnsw.ef_search; got statements: {log}"
    assert f"SET LOCAL hnsw.ef_search = {expected_ef}" in ef_stmts[0]


def test_search_issues_iterative_scan_guarded(monkeypatch: pytest.MonkeyPatch) -> None:
    """iterative_scan is set defensively; if the GUC is unknown, search still runs."""
    log: list[tuple[str, Any]] = []
    adapter = _adapter_with_fake_conn(log)
    monkeypatch.setattr(adapter, "_resolve_for_query", lambda alias: "rtfc_kb_00000001")

    adapter.search("rtfc_kb__alias", [0.1, 0.2, 0.3, 0.4], top_k=10)
    assert any("hnsw.iterative_scan" in sql for sql, _ in log)


def test_search_survives_unsupported_iterative_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An older pgvector that errors on the iterative_scan GUC must not fail search."""

    class _RaisingCursor(_FakeCursor):
        def execute(self, sql: str, params: Any = None) -> None:
            if "hnsw.iterative_scan" in sql:
                raise RuntimeError('unrecognized configuration parameter "hnsw.iterative_scan"')
            super().execute(sql, params)

    class _RaisingConn(_FakeConn):
        def cursor(self) -> _RaisingCursor:
            return _RaisingCursor(self._log)

    log: list[tuple[str, Any]] = []
    adapter = object.__new__(PgVectorAdapter)
    adapter._conn = _RaisingConn(log)  # type: ignore[attr-defined]
    monkeypatch.setattr(adapter, "_resolve_for_query", lambda alias: "rtfc_kb_00000001")

    results = adapter.search("rtfc_kb__alias", [0.1, 0.2, 0.3, 0.4], top_k=10)
    assert results == []
    # ef_search still issued; the SELECT still ran.
    assert any("hnsw.ef_search" in sql for sql, _ in log)
    assert any(sql.strip().startswith("SELECT point_id") for sql, _ in log)
