"""Atlassian connector (Confluence + Jira) — recorded-fixture tests, NO network.

Proves, for BOTH products: auth-by-reference, pagination, fetch, and the
permission mapping / fail-closed behaviour. Confluence page-level read
restrictions map to authoritative principals; an EMPTY restriction set (space-
inherited) degrades to best_effort rather than laundering it as an authoritative
empty read-list; a 403 → unavailable. Jira issues are ALWAYS
unavailable fidelity (issue-level security is not a reliable filterable ACL), so
a Jira run fails closed unless the operator acknowledges the gap.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from finecorpus.connectors.atlassian import AtlassianConnector
from finecorpus.connectors.base import PermissionFidelityError
from finecorpus.connectors.fixtures import RecordedHTTPClient, UnmatchedRequestError
from finecorpus.connectors.models import ConnectorConfig
from finecorpus.contracts.inventory import SourceKind, SourcePermissions, SourceRun
from finecorpus.contracts.shared.blocks import PermissionFidelity
from tests.connectors.conftest import FakeTokenStore

_FIX = Path(__file__).parent / "fixtures"


def _load(*names: str) -> list[dict]:
    interactions: list[dict] = []
    for name in names:
        interactions.extend(json.loads((_FIX / name).read_text()))
    return interactions


def _confluence_cfg() -> ConnectorConfig:
    return ConnectorConfig(
        connector_id="conf-1",
        source_kind=SourceKind.confluence,
        endpoint="https://acme.atlassian.net/wiki",
        token_ref="atl-tok",
        options={"space_key": "ENG"},
    )


def _jira_cfg() -> ConnectorConfig:
    return ConnectorConfig(
        connector_id="jira-1",
        source_kind=SourceKind.jira,
        endpoint="https://acme.atlassian.net",
        token_ref="atl-tok",
        options={"jql": "order by created"},
    )


def _run(kind: SourceKind, *, ack: bool | None = None) -> SourceRun:
    return SourceRun(source_kind=kind, connector_id="x", acknowledged_permission_gap=ack)


def _authed(cfg: ConnectorConfig, http: RecordedHTTPClient) -> AtlassianConnector:
    conn = AtlassianConnector(cfg, http)
    conn.authenticate(cfg, FakeTokenStore({"atl-tok": "atl.api.token"}))
    return conn


# --- capabilities ----------------------------------------------------------


def test_capabilities_differ_by_product() -> None:
    conf = AtlassianConnector(_confluence_cfg(), RecordedHTTPClient([]))
    jira = AtlassianConnector(_jira_cfg(), RecordedHTTPClient([]))
    assert conf.capabilities.supplies_permissions is True
    assert conf.capabilities.max_permission_fidelity is PermissionFidelity.authoritative
    # Jira cannot supply a reliable filterable ACL → always unavailable.
    assert jira.capabilities.supplies_permissions is False
    assert jira.capabilities.max_permission_fidelity is PermissionFidelity.unavailable


def test_bad_product_rejected() -> None:
    cfg = ConnectorConfig(
        connector_id="x", source_kind=SourceKind.confluence, options={"product": "bitbucket"}
    )
    with pytest.raises(ValueError, match="confluence.*jira"):
        AtlassianConnector(cfg, RecordedHTTPClient([]))


# --- Confluence ------------------------------------------------------------


def test_confluence_list_and_fetch() -> None:
    conn = _authed(
        _confluence_cfg(),
        RecordedHTTPClient(_load("confluence_list.json", "confluence_fetch.json")),
    )
    page = conn.list_documents()
    assert [d.source_id for d in page.documents] == ["page-1", "page-2"]
    assert page.next_cursor is None  # 2 < limit 25 → single page
    raw = conn.fetch_document(page.documents[0])
    assert raw.content == b"<p>Welcome to the team.</p>"
    assert raw.media_type == "text/html"


def test_confluence_restrictions_map_to_authoritative() -> None:
    conn = _authed(
        _confluence_cfg(),
        RecordedHTTPClient(_load("confluence_list.json", "confluence_restrictions.json")),
    )
    doc = conn.list_documents().documents[0]
    record = conn.fetch_permissions(doc)
    assert record.fidelity is PermissionFidelity.authoritative
    assert record.principals_read == ["user:acc-alice", "group:confluence-admins"]
    sp = record.to_source_permissions(connector_id="conf-1", source_run=_run(SourceKind.confluence))
    assert isinstance(sp, SourcePermissions)


def test_confluence_empty_restrictions_downgrades_to_best_effort() -> None:
    # M-1: a page with NO page-level read restriction inherits its space's ACL,
    # which this connector does not fetch. Reporting an empty read-list as
    # `authoritative` would launder the space-scoped ACL. Fail honest: the
    # empty-restriction case is `best_effort`, not `authoritative`.
    conn = _authed(
        _confluence_cfg(),
        RecordedHTTPClient(_load("confluence_list.json", "confluence_restrictions_empty.json")),
    )
    doc = conn.list_documents().documents[0]
    record = conn.fetch_permissions(doc)
    assert record.fidelity is PermissionFidelity.best_effort
    assert record.principals_read == []
    # best_effort still passes the §14.3 gate (it is a mirrorable fidelity).
    sp = record.to_source_permissions(connector_id="conf-1", source_run=_run(SourceKind.confluence))
    assert isinstance(sp, SourcePermissions)
    assert sp.fidelity is PermissionFidelity.best_effort


def test_confluence_restrictions_denied_is_unavailable_and_blocks() -> None:
    conn = _authed(
        _confluence_cfg(),
        RecordedHTTPClient(_load("confluence_list.json", "confluence_restrictions_denied.json")),
    )
    doc = conn.list_documents().documents[0]
    record = conn.fetch_permissions(doc)
    assert record.fidelity is PermissionFidelity.unavailable
    with pytest.raises(PermissionFidelityError):
        record.to_source_permissions(
            connector_id="conf-1", source_run=_run(SourceKind.confluence, ack=None)
        )


# --- Jira ------------------------------------------------------------------


def test_jira_list_and_fetch() -> None:
    conn = _authed(_jira_cfg(), RecordedHTTPClient(_load("jira_search.json", "jira_issue.json")))
    page = conn.list_documents()
    assert [d.source_id for d in page.documents] == ["PROJ-1", "PROJ-2"]
    assert page.next_cursor is None  # startAt+50 >= total 2
    raw = conn.fetch_document(page.documents[0])
    assert b"Users cannot log in" in raw.content
    assert raw.media_type == "application/json"


def test_jira_permissions_always_unavailable_and_blocks() -> None:
    conn = _authed(_jira_cfg(), RecordedHTTPClient(_load("jira_search.json")))
    doc = conn.list_documents().documents[0]
    record = conn.fetch_permissions(doc)
    # Fail closed: Jira never fabricates a filterable ACL.
    assert record.fidelity is PermissionFidelity.unavailable
    with pytest.raises(PermissionFidelityError):
        record.to_source_permissions(connector_id="jira-1", source_run=_run(SourceKind.jira))
    # With an acknowledged gap it proceeds (recorded as an acknowledged gap).
    sp = record.to_source_permissions(
        connector_id="jira-1", source_run=_run(SourceKind.jira, ack=True)
    )
    assert sp.fidelity is PermissionFidelity.unavailable


def test_no_network_beyond_recorded_interactions() -> None:
    conn = _authed(_jira_cfg(), RecordedHTTPClient([]))
    with pytest.raises(UnmatchedRequestError):
        conn.list_documents()
