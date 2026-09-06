"""SharePoint (Microsoft Graph) connector — recorded-fixture tests, NO network.

Proves: OAuth token exchange (fixture), delta-sync pagination via nextLink +
deltaLink watermark, fetch, permission→SourcePermissions mapping, and the
fail-closed path (permissions 403 → unavailable → blocked without ack).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from finecorpus.connectors.base import PermissionFidelityError
from finecorpus.connectors.fixtures import RecordedHTTPClient, UnmatchedRequestError
from finecorpus.connectors.models import ConnectorConfig
from finecorpus.connectors.oauth import OAuth2Config, OAuth2Flow
from finecorpus.connectors.sharepoint import SharePointConnector
from finecorpus.contracts.inventory import SourceKind, SourcePermissions, SourceRun
from finecorpus.contracts.shared.blocks import PermissionFidelity
from tests.connectors.conftest import FakeTokenStore

_FIX = Path(__file__).parent / "fixtures"


def _load(*names: str) -> list[dict]:
    interactions: list[dict] = []
    for name in names:
        interactions.extend(json.loads((_FIX / name).read_text()))
    return interactions


def _config() -> ConnectorConfig:
    return ConnectorConfig(
        connector_id="sp-1",
        source_kind=SourceKind.sharepoint,
        endpoint="https://graph.microsoft.com",
        token_ref="sp-tok",
        options={"drive_id": "drive-1"},
    )


def _run(*, ack: bool | None = None) -> SourceRun:
    return SourceRun(
        source_kind=SourceKind.sharepoint,
        connector_id="sp-1",
        acknowledged_permission_gap=ack,
    )


def _authed(http: RecordedHTTPClient) -> SharePointConnector:
    conn = SharePointConnector(_config(), http)
    conn.authenticate(_config(), FakeTokenStore({"sp-tok": "graph.access"}))
    return conn


def test_oauth_token_exchange_from_fixture() -> None:
    # Graph tokens come from the AAD v2 endpoint; reuse the generic oauth fixture shape.
    http = RecordedHTTPClient(
        [
            {
                "match": {
                    "method": "POST",
                    "url": "https://login.microsoftonline.com/tenant/oauth2/v2.0/token",
                },
                "response": {
                    "status_code": 200,
                    "json": {
                        "access_token": "graph.access",
                        "token_type": "Bearer",
                        "expires_in": 3599,
                        "scope": "Files.Read.All Sites.Read.All",
                    },
                },
            }
        ]
    )
    flow = OAuth2Flow(
        OAuth2Config(
            authorization_endpoint="https://login.microsoftonline.com/tenant/oauth2/v2.0/authorize",
            token_endpoint="https://login.microsoftonline.com/tenant/oauth2/v2.0/token",
            client_id="cid",
            client_secret="secret",
            redirect_uri="https://app/cb",
            scopes=("Files.Read.All", "Sites.Read.All"),
        ),
        http,
    )
    token = flow.exchange_code("code")
    assert token.access_token == "graph.access"


def test_authenticate_without_token_ref_raises() -> None:
    cfg = ConnectorConfig(
        connector_id="s", source_kind=SourceKind.sharepoint, options={"drive_id": "d"}
    )
    conn = SharePointConnector(cfg, RecordedHTTPClient([]))
    with pytest.raises(ValueError, match="token_ref"):
        conn.authenticate(cfg, FakeTokenStore({}))


def test_delta_pagination_and_watermark_skips_folders_and_tombstones() -> None:
    conn = _authed(RecordedHTTPClient(_load("sharepoint_delta.json")))
    ids: list[str] = []
    page = conn.list_documents()
    ids.extend(d.source_id for d in page.documents)
    while page.next_cursor is not None:
        page = conn.list_documents(cursor=page.next_cursor)
        ids.extend(d.source_id for d in page.documents)
    incremental = conn.incremental_cursor(page)

    # Folders (folder-x) and deleted tombstones (item-deleted) are excluded.
    assert ids == ["item-1", "item-2"]
    assert incremental is not None
    assert incremental.token.endswith("token=DELTA9")


def test_fetch_document_returns_raw_bytes() -> None:
    conn = _authed(RecordedHTTPClient(_load("sharepoint_delta.json", "sharepoint_fetch.json")))
    doc = conn.list_documents().documents[0]
    raw = conn.fetch_document(doc)
    assert raw.content == b"policy document bytes"
    assert raw.ref.source_id == "item-1"


def test_permission_mapping_to_source_permissions() -> None:
    conn = _authed(
        RecordedHTTPClient(_load("sharepoint_delta.json", "sharepoint_permissions.json"))
    )
    doc = conn.list_documents().documents[0]
    record = conn.fetch_permissions(doc)
    assert record.fidelity is PermissionFidelity.authoritative
    assert record.principals_read == ["user:user-guid-alice", "group:group-guid-eng"]
    sp = record.to_source_permissions(connector_id="sp-1", source_run=_run())
    assert isinstance(sp, SourcePermissions)


def test_permissions_denied_reports_unavailable_and_blocks() -> None:
    conn = _authed(
        RecordedHTTPClient(_load("sharepoint_delta.json", "sharepoint_permissions_denied.json"))
    )
    doc = conn.list_documents().documents[0]
    record = conn.fetch_permissions(doc)
    assert record.fidelity is PermissionFidelity.unavailable
    with pytest.raises(PermissionFidelityError):
        record.to_source_permissions(connector_id="sp-1", source_run=_run(ack=None))


def test_no_network_beyond_recorded_interactions() -> None:
    conn = _authed(RecordedHTTPClient([]))
    with pytest.raises(UnmatchedRequestError):
        conn.list_documents()
