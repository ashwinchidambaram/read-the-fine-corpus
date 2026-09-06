"""SharePoint connector via Microsoft Graph (§4.5, §14).

Uses the Microsoft Graph REST API directly over the framework
:class:`~finecorpus.connectors.http.HTTPClient` seam — no `msgraph` /
`office365` SDK (self-hostable / air-gapped discipline, §6.4). Tests replay
recorded fixtures via ``RecordedHTTPClient``; CI never touches the network.

Surface:
- ``authenticate`` — resolves an OAuth2 access token by reference through the
  ``TokenStore`` seam (§14.2).
- ``list_documents`` — drive-item ``delta`` query: page via ``@odata.nextLink``
  and persist ``@odata.deltaLink`` as the incremental watermark (§6.1).
- ``fetch_document`` — item ``/content`` for raw bytes.
- ``fetch_permissions`` — item ``/permissions`` mapped into a
  :class:`PermissionRecord`. A 403/failed permissions call yields
  ``PermissionFidelity.unavailable`` (fail closed, §14.3) — never fabricated.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from finecorpus.connectors.base import Connector, ConnectorCapabilities, TokenStore
from finecorpus.connectors.http import HTTPClient
from finecorpus.connectors.models import (
    ConnectorConfig,
    DocumentPage,
    DocumentRef,
    PermissionRecord,
    RawDocument,
    SyncCursor,
)
from finecorpus.connectors.registry import register_connector
from finecorpus.contracts.inventory import SourceKind
from finecorpus.contracts.shared.blocks import PermissionFidelity

_GRAPH_BASE = "https://graph.microsoft.com/v1.0"


class SharePointConnector(Connector):
    """SharePoint source connector over Microsoft Graph (delta sync, per-item ACLs)."""

    def __init__(self, config: ConnectorConfig, http_client: HTTPClient) -> None:
        self._config = config
        self._http = http_client
        self._access_token: str | None = None

    @property
    def capabilities(self) -> ConnectorCapabilities:
        return ConnectorCapabilities(
            connector_id=self._config.connector_id,
            supports_incremental=True,
            supplies_permissions=True,
            max_permission_fidelity=PermissionFidelity.authoritative,
        )

    def authenticate(self, config: ConnectorConfig, token_store: TokenStore) -> None:
        if config.token_ref is None:
            raise ValueError("SharePointConnector requires config.token_ref for the access token.")
        self._access_token = token_store.get_token(config.token_ref)

    def _auth_headers(self) -> dict[str, str]:
        if self._access_token is None:
            raise RuntimeError("authenticate() must be called before making requests.")
        return {"Authorization": f"Bearer {self._access_token}"}

    def _drive_id(self) -> str:
        drive_id = self._config.options.get("drive_id")
        if not drive_id:
            raise ValueError("SharePointConnector requires options.drive_id.")
        return str(drive_id)

    def list_documents(self, cursor: SyncCursor | None = None) -> DocumentPage:
        # Delta query: the cursor token IS the next full URL (nextLink/deltaLink),
        # which is exactly how Graph paginates. On a fresh run we build the base
        # delta URL for the configured drive root.
        if cursor is not None:
            url = cursor.token
        else:
            url = f"{_GRAPH_BASE}/drives/{self._drive_id()}/root/delta"
        resp = self._http.request("GET", url, headers=self._auth_headers())
        payload = resp.json()

        documents: list[DocumentRef] = []
        for item in payload.get("value", []):
            # Delta includes folders and deleted tombstones; skip non-files.
            if "file" not in item or "deleted" in item:
                continue
            documents.append(
                DocumentRef(
                    source_id=item["id"],
                    source_path=_web_url(item),
                    display_name=item.get("name", item["id"]),
                    media_type=item.get("file", {}).get("mimeType"),
                    source_modified_at=_parse_ts(item.get("lastModifiedDateTime")),
                    metadata={"drive_id": self._drive_id()},
                )
            )

        next_link = payload.get("@odata.nextLink")
        delta_link = payload.get("@odata.deltaLink")
        next_cursor = SyncCursor(token=next_link) if next_link else None
        # deltaLink appears only on the final page — persist it as the watermark.
        incremental_cursor = SyncCursor(token=delta_link) if delta_link else None
        return DocumentPage(
            documents=documents,
            next_cursor=next_cursor,
            incremental_cursor=incremental_cursor,
        )

    def fetch_document(self, doc_ref: DocumentRef) -> RawDocument:
        url = f"{_GRAPH_BASE}/drives/{self._drive_id()}/items/{doc_ref.source_id}/content"
        resp = self._http.request("GET", url, headers=self._auth_headers())
        media_type = (
            doc_ref.media_type or resp.headers.get("content-type") or "application/octet-stream"
        )
        return RawDocument(
            ref=doc_ref,
            content=resp.body,
            media_type=media_type,
            fetched_at=datetime.now(UTC),
        )

    def fetch_permissions(self, doc_ref: DocumentRef) -> PermissionRecord:
        url = f"{_GRAPH_BASE}/drives/{self._drive_id()}/items/{doc_ref.source_id}/permissions"
        resp = self._http.request("GET", url, headers=self._auth_headers())
        if resp.status_code != 200:
            return PermissionRecord(
                doc_ref=doc_ref,
                principals_read=[],
                fidelity=PermissionFidelity.unavailable,
                raw={"http_status": resp.status_code},
            )
        payload = resp.json()
        principals: list[str] = []
        for perm in payload.get("value", []):
            principals.extend(_principals(perm))
        return PermissionRecord(
            doc_ref=doc_ref,
            principals_read=principals,
            fidelity=PermissionFidelity.authoritative,
            raw=payload,
        )


def _principals(perm: dict[str, Any]) -> list[str]:
    """Map one Graph permission entry to zero or more principal strings."""
    out: list[str] = []
    granted = perm.get("grantedToV2") or perm.get("grantedTo")
    if isinstance(granted, dict):
        user = granted.get("user")
        if isinstance(user, dict):
            out.append(f"user:{user.get('id', user.get('displayName', ''))}")
        group = granted.get("group")
        if isinstance(group, dict):
            out.append(f"group:{group.get('id', group.get('displayName', ''))}")
    for identity in perm.get("grantedToIdentitiesV2", []):
        user = identity.get("user")
        if isinstance(user, dict):
            out.append(f"user:{user.get('id', user.get('displayName', ''))}")
    if perm.get("link", {}).get("scope") == "anonymous":
        out.append("anyone")
    return out


def _web_url(item: dict[str, Any]) -> str:
    return item.get("webUrl") or f"sharepoint://{item['id']}"


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _factory(config: ConnectorConfig) -> Connector:
    """Registry factory; injects the production ``httpx`` client."""
    import httpx

    from finecorpus.connectors.http import HttpxClient

    return SharePointConnector(config, HttpxClient(httpx.Client(timeout=30.0)))


register_connector(SourceKind.sharepoint.value, _factory)

__all__ = ["SharePointConnector"]
