"""Google Drive connector — recorded-fixture tests, NO live network.

Proves: OAuth token exchange (fixture), page-token pagination, fetch, the
permission→PermissionRecord→SourcePermissions mapping, and the fail-closed path
(permissions 403 → unavailable fidelity → blocked without an acknowledged gap).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from finecorpus.connectors.base import PermissionFidelityError
from finecorpus.connectors.fixtures import RecordedHTTPClient, UnmatchedRequestError
from finecorpus.connectors.gdrive import GoogleDriveConnector
from finecorpus.connectors.models import ConnectorConfig
from finecorpus.connectors.oauth import OAuth2Config, OAuth2Flow
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
        connector_id="gdrive-1",
        source_kind=SourceKind.gdrive,
        endpoint="https://www.googleapis.com",
        token_ref="gdrive-tok",
        options={"folder_id": "folder-root"},
    )


def _run(*, ack: bool | None = None) -> SourceRun:
    return SourceRun(
        source_kind=SourceKind.gdrive,
        connector_id="gdrive-1",
        acknowledged_permission_gap=ack,
    )


def _authed(http: RecordedHTTPClient) -> GoogleDriveConnector:
    conn = GoogleDriveConnector(_config(), http)
    conn.authenticate(_config(), FakeTokenStore({"gdrive-tok": "ya29.access"}))
    return conn


# --- auth ------------------------------------------------------------------


def test_oauth_token_exchange_from_fixture() -> None:
    http = RecordedHTTPClient.from_file(_FIX / "gdrive_oauth_token.json")
    flow = OAuth2Flow(
        OAuth2Config(
            authorization_endpoint="https://accounts.google.com/o/oauth2/v2/auth",
            token_endpoint="https://oauth2.googleapis.com/token",
            client_id="cid",
            client_secret="secret",
            redirect_uri="https://app/cb",
            scopes=("https://www.googleapis.com/auth/drive.readonly",),
        ),
        http,
    )
    token = flow.exchange_code("auth-code")
    assert token.access_token == "ya29.drive-access-token"
    assert http.calls == [{"method": "POST", "url": "https://oauth2.googleapis.com/token"}]


def test_authenticate_resolves_token_by_reference() -> None:
    conn = GoogleDriveConnector(_config(), RecordedHTTPClient([]))
    conn.authenticate(_config(), FakeTokenStore({"gdrive-tok": "ya29.access"}))
    # No network call happened during auth (token resolved via the store seam).
    assert conn.capabilities.connector_id == "gdrive-1"


def test_authenticate_without_token_ref_raises() -> None:
    cfg = ConnectorConfig(connector_id="g", source_kind=SourceKind.gdrive)
    conn = GoogleDriveConnector(cfg, RecordedHTTPClient([]))
    with pytest.raises(ValueError, match="token_ref"):
        conn.authenticate(cfg, FakeTokenStore({}))


# --- pagination + fetch ----------------------------------------------------


def test_pagination_via_page_token_and_delta_watermark() -> None:
    # NOTE: the delta watermark here is the documented APPROXIMATION (see the
    # list_documents TODO) — real Drive delta uses the changes.* endpoints. This
    # asserts the placeholder watermark is wired through end-to-end.
    conn = _authed(RecordedHTTPClient(_load("gdrive_list.json")))
    ids: list[str] = []
    page = conn.list_documents()
    ids.extend(d.source_id for d in page.documents)
    incremental = None
    while page.next_cursor is not None:
        page = conn.list_documents(cursor=page.next_cursor)
        ids.extend(d.source_id for d in page.documents)
    incremental = conn.incremental_cursor(page)

    assert ids == ["file-a", "file-b"]
    assert incremental is not None
    assert incremental.token == "delta-500"


def test_fetch_document_returns_raw_bytes() -> None:
    conn = _authed(RecordedHTTPClient(_load("gdrive_list.json", "gdrive_fetch.json")))
    doc = conn.list_documents().documents[0]
    raw = conn.fetch_document(doc)
    assert raw.content == b"quarterly plan body bytes"
    assert raw.ref.source_id == "file-a"


# --- permission mapping ----------------------------------------------------


def test_permission_mapping_to_source_permissions() -> None:
    conn = _authed(RecordedHTTPClient(_load("gdrive_list.json", "gdrive_permissions.json")))
    doc = conn.list_documents().documents[0]
    record = conn.fetch_permissions(doc)
    assert record.fidelity is PermissionFidelity.authoritative
    assert record.principals_read == [
        "user:alice@example.com",
        "group:eng@example.com",
        "domain:example.com",
    ]
    # Threads into the EXISTING contract via the gated converter.
    sp = record.to_source_permissions(connector_id="gdrive-1", source_run=_run())
    assert isinstance(sp, SourcePermissions)
    assert sp.fidelity is PermissionFidelity.authoritative


# --- fail-closed path ------------------------------------------------------


def test_permissions_denied_reports_unavailable_and_blocks() -> None:
    conn = _authed(RecordedHTTPClient(_load("gdrive_list.json", "gdrive_permissions_denied.json")))
    doc = conn.list_documents().documents[0]
    record = conn.fetch_permissions(doc)
    # Never fabricate authoritative: a 403 on the ACL call → unavailable.
    assert record.fidelity is PermissionFidelity.unavailable
    assert record.principals_read == []
    # Fails closed with no acknowledged gap.
    with pytest.raises(PermissionFidelityError):
        record.to_source_permissions(connector_id="gdrive-1", source_run=_run(ack=None))
    # Proceeds once the operator acknowledges the gap.
    sp = record.to_source_permissions(connector_id="gdrive-1", source_run=_run(ack=True))
    assert sp.fidelity is PermissionFidelity.unavailable


def test_no_network_beyond_recorded_interactions() -> None:
    conn = _authed(RecordedHTTPClient([]))  # empty → any request raises
    with pytest.raises(UnmatchedRequestError):
        conn.list_documents()
