"""Google Drive connector (§4.5, §14).

Talks to the Drive v3 REST API directly over the framework
:class:`~finecorpus.connectors.http.HTTPClient` seam — no `google-api-python-client`
SDK (self-hostable / air-gapped discipline, §6.4). All tests replay recorded
fixtures via ``RecordedHTTPClient``; CI never touches the network.

Surface:
- ``authenticate`` — resolves an OAuth2 access token by reference through the
  ``TokenStore`` seam (§14.2). Token *values* never come from config.
- ``list_documents`` — ``files.list`` with page-token pagination and an
  APPROXIMATE delta watermark persisted as the incremental cursor. NOTE: real
  Drive incremental sync uses ``changes.getStartPageToken`` + ``changes.list``,
  a separate endpoint family; the current watermark is a documented placeholder
  (see the ``list_documents`` TODO), not the real Drive changes shape.
- ``fetch_document`` — ``files.get?alt=media`` for raw bytes.
- ``fetch_permissions`` — ``permissions.list`` mapped into a
  :class:`PermissionRecord`. When the permissions call cannot be trusted (e.g.
  the token lacks the permissions scope and the API returns 403), the connector
  reports ``PermissionFidelity.unavailable`` — it never fabricates open access.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

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

_API_BASE = "https://www.googleapis.com/drive/v3"
# Fields kept small and deterministic so recorded fixtures stay stable.
_LIST_FIELDS = "nextPageToken,files(id,name,mimeType,modifiedTime)"
_PERM_FIELDS = "permissions(id,type,emailAddress,domain,role)"


class GoogleDriveConnector(Connector):
    """Google Drive source connector (OAuth2, per-file permissions)."""

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
        """Resolve the access token by reference (§14.2) — never from config."""
        if config.token_ref is None:
            raise ValueError("GoogleDriveConnector requires config.token_ref for the access token.")
        # Token VALUE is resolved through the control-plane seam and kept only
        # in memory for the run; never logged, never written to config.
        self._access_token = token_store.get_token(config.token_ref)

    def _auth_headers(self) -> dict[str, str]:
        if self._access_token is None:
            raise RuntimeError("authenticate() must be called before making requests.")
        return {"Authorization": f"Bearer {self._access_token}"}

    def _get(self, url: str) -> Any:
        resp = self._http.request("GET", url, headers=self._auth_headers())
        return resp

    def list_documents(self, cursor: SyncCursor | None = None) -> DocumentPage:
        params: dict[str, str] = {"fields": _LIST_FIELDS, "pageSize": "100"}
        folder_id = self._config.options.get("folder_id")
        if folder_id:
            params["q"] = f"'{folder_id}' in parents"
        if cursor is not None:
            params["pageToken"] = cursor.token
        url = f"{_API_BASE}/files?{urlencode(params)}"
        resp = self._get(url)
        payload = resp.json()

        documents: list[DocumentRef] = []
        for f in payload.get("files", []):
            documents.append(
                DocumentRef(
                    source_id=f["id"],
                    source_path=f"gdrive://{f['id']}",
                    display_name=f.get("name", f["id"]),
                    media_type=f.get("mimeType"),
                    source_modified_at=_parse_ts(f.get("modifiedTime")),
                    metadata={"mimeType": f.get("mimeType")},
                )
            )

        next_token = payload.get("nextPageToken")
        next_cursor = SyncCursor(token=next_token) if next_token else None
        # TODO(gdrive-delta): APPROXIMATION, not the real Drive delta API.
        # Genuine Drive incremental sync uses a distinct endpoint family:
        # `changes.getStartPageToken` to seed a start token, then
        # `changes.list?pageToken=...` returning a `newStartPageToken` on the
        # final page. `files.list` does NOT return `newStartPageToken`; reading
        # it here is a placeholder watermark so the incremental seam is wired
        # end-to-end. Implementing the real `changes.*` path (and reshaping the
        # fixture) is a follow-up; until then this cursor is best treated as a
        # documented approximation, not a trustworthy Drive change token.
        incremental_cursor = None
        if next_cursor is None and payload.get("newStartPageToken"):
            incremental_cursor = SyncCursor(token=payload["newStartPageToken"])
        return DocumentPage(
            documents=documents,
            next_cursor=next_cursor,
            incremental_cursor=incremental_cursor,
        )

    def fetch_document(self, doc_ref: DocumentRef) -> RawDocument:
        url = f"{_API_BASE}/files/{doc_ref.source_id}?alt=media"
        resp = self._get(url)
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
        query = urlencode({"fields": _PERM_FIELDS})
        url = f"{_API_BASE}/files/{doc_ref.source_id}/permissions?{query}"
        resp = self._get(url)
        if resp.status_code != 200:
            # Fail closed (§14.3): cannot trust ACLs → unavailable, no fabrication.
            return PermissionRecord(
                doc_ref=doc_ref,
                principals_read=[],
                fidelity=PermissionFidelity.unavailable,
                raw={"http_status": resp.status_code},
            )
        payload = resp.json()
        principals = [_principal(p) for p in payload.get("permissions", [])]
        return PermissionRecord(
            doc_ref=doc_ref,
            principals_read=principals,
            fidelity=PermissionFidelity.authoritative,
            raw=payload,
        )


def _principal(perm: dict[str, Any]) -> str:
    """Map a Drive permission entry to a canonical principal string."""
    ptype = perm.get("type", "user")
    if ptype == "anyone":
        return "anyone"
    if ptype == "domain":
        return f"domain:{perm.get('domain', '')}"
    if ptype == "group":
        return f"group:{perm.get('emailAddress', perm.get('id', ''))}"
    return f"user:{perm.get('emailAddress', perm.get('id', ''))}"


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _factory(config: ConnectorConfig) -> Connector:
    """Registry factory. Injects the production ``httpx`` client lazily.

    Constructed with the real :class:`HttpxClient` in production; tests build
    the connector directly with a ``RecordedHTTPClient`` (no network).
    """
    import httpx

    from finecorpus.connectors.http import HttpxClient

    return GoogleDriveConnector(config, HttpxClient(httpx.Client(timeout=30.0)))


register_connector(SourceKind.gdrive.value, _factory)

__all__ = ["GoogleDriveConnector"]
