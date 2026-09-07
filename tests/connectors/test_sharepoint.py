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


def test_anonymous_link_maps_to_anyone_token_which_is_NOT_special_cased_public() -> None:
    """Pin the intended semantics of the SharePoint anonymous-link mapping (MINOR-2).

    An anonymous sharing link (``link.scope == "anonymous"``) maps to the literal
    principal token ``"anyone"`` in ``principals_read``.

    FINDING (pinned here, not papered over): this token is an ORDINARY,
    opaque principal string.  Nothing downstream — not the pipeline
    (plan/assess/build), the §14.3 permission gate, the index backends, nor the
    retrieval matcher — special-cases ``"anyone"`` as genuinely-public.  At read
    time the retrieval filter requires ``tenancy.permission_principals`` to
    *contain the authenticated principal's principal_id* (retrieval/service.py
    ``_build_payload_filter``); it does NOT treat ``permission_mode`` /
    ``public_to_kb`` off the back of an ``"anyone"`` token, and no principal's
    ``principal_id`` is literally ``"anyone"``.

    Net effect: an anonymous-link SharePoint doc becomes UNreadable to normal
    principals (fail-CLOSED — not a data-leak, but genuinely-public content is
    not discoverable via the ``"anyone"`` token alone).  This test pins that
    behaviour so any future change to public semantics is a deliberate, tested
    decision rather than an accident.
    """
    from finecorpus.connectors.sharepoint import _principals

    # Anonymous link -> "anyone".
    anon = {"link": {"scope": "anonymous"}}
    assert _principals(anon) == ["anyone"], (
        "an anonymous sharing link must map to the 'anyone' token"
    )

    # A non-anonymous (organization/direct) link must NOT emit "anyone".
    org = {"link": {"scope": "organization"}}
    assert "anyone" not in _principals(org)

    # Pin: "anyone" is a plain principal token, not a public sentinel recognised
    # anywhere in the permission model.  The retrieval matcher only ever tests
    # membership of the reader's principal_id in permission_principals; there is
    # no "anyone"/public bypass.  If this ever becomes public-by-token, the
    # assertion below must be updated deliberately.
    from finecorpus.contracts.shared.blocks import PermissionMode

    assert not hasattr(PermissionMode, "anyone")
    assert "anyone" not in {m.value for m in PermissionMode}, (
        "'anyone' must not silently become a PermissionMode value"
    )
