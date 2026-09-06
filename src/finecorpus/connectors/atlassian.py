"""Atlassian connector — Confluence pages + Jira issues (§4.5, §14).

A single connector class serves both Atlassian products; the product is chosen
by ``config.source_kind`` (``confluence`` or ``jira``). It talks to the Atlassian
Cloud REST APIs directly over the framework
:class:`~finecorpus.connectors.http.HTTPClient` seam (no `atlassian-python-api`
SDK — self-hostable / air-gapped discipline, §6.4). Tests replay recorded
fixtures via ``RecordedHTTPClient``; CI never touches the network.

Auth is OAuth2 (Atlassian 3LO) or an API token, both resolved by reference
through the ``TokenStore`` seam (§14.2) and sent as a Bearer token.

Permission fidelity (§14.3):
- **Confluence** exposes per-page *restrictions* (``/rest/api/content/{id}/restriction``).
  When present they map to ``PermissionRecord`` principals with
  ``authoritative`` fidelity. When the restrictions call fails, the connector
  reports ``unavailable`` — it does NOT fabricate open access.
- **Jira** has project/issue-level security that the REST issue payload does not
  reliably expose as a filterable ACL. Rather than launder issues as public,
  the Jira path reports ``PermissionFidelity.unavailable`` for every issue, so a
  Jira run fails closed unless the operator acknowledges the permission gap.
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

_CONFLUENCE = "confluence"
_JIRA = "jira"


class AtlassianConnector(Connector):
    """Confluence + Jira source connector (Atlassian OAuth2 / API token)."""

    def __init__(self, config: ConnectorConfig, http_client: HTTPClient) -> None:
        self._config = config
        self._http = http_client
        self._token: str | None = None
        product = config.options.get("product") or config.source_kind.value
        if product not in (_CONFLUENCE, _JIRA):
            raise ValueError(
                f"AtlassianConnector product must be 'confluence' or 'jira', got {product!r}."
            )
        self._product = product

    @property
    def capabilities(self) -> ConnectorCapabilities:
        # Confluence can supply authoritative per-page restrictions; Jira cannot
        # reliably supply a filterable ACL, so its best fidelity is 'unavailable'.
        if self._product == _CONFLUENCE:
            max_fidelity = PermissionFidelity.authoritative
            supplies = True
        else:
            max_fidelity = PermissionFidelity.unavailable
            supplies = False
        return ConnectorCapabilities(
            connector_id=self._config.connector_id,
            supports_incremental=False,
            supplies_permissions=supplies,
            max_permission_fidelity=max_fidelity,
        )

    def authenticate(self, config: ConnectorConfig, token_store: TokenStore) -> None:
        if config.token_ref is None:
            raise ValueError("AtlassianConnector requires config.token_ref for the API token.")
        self._token = token_store.get_token(config.token_ref)

    def _base(self) -> str:
        endpoint = self._config.endpoint
        if not endpoint:
            raise ValueError("AtlassianConnector requires config.endpoint (site base URL).")
        return endpoint.rstrip("/")

    def _auth_headers(self) -> dict[str, str]:
        if self._token is None:
            raise RuntimeError("authenticate() must be called before making requests.")
        return {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}

    def _get(self, url: str) -> Any:
        return self._http.request("GET", url, headers=self._auth_headers())

    # -- listing --------------------------------------------------------

    def list_documents(self, cursor: SyncCursor | None = None) -> DocumentPage:
        if self._product == _CONFLUENCE:
            return self._list_confluence(cursor)
        return self._list_jira(cursor)

    def _list_confluence(self, cursor: SyncCursor | None) -> DocumentPage:
        start = int(cursor.token) if cursor is not None else 0
        limit = 25
        space_key = self._config.options.get("space_key")
        params: dict[str, str] = {"limit": str(limit), "start": str(start), "expand": "version"}
        if space_key:
            params["spaceKey"] = str(space_key)
        url = f"{self._base()}/rest/api/content?{urlencode(params)}"
        payload = self._get(url).json()

        documents: list[DocumentRef] = []
        for page in payload.get("results", []):
            pid = page["id"]
            documents.append(
                DocumentRef(
                    source_id=pid,
                    source_path=f"{self._base()}/pages/{pid}",
                    display_name=page.get("title", pid),
                    media_type="text/html",
                    source_modified_at=_parse_ts(
                        page.get("version", {}).get("when") if page.get("version") else None
                    ),
                    metadata={"product": _CONFLUENCE, "type": page.get("type")},
                )
            )
        returned = len(payload.get("results", []))
        next_cursor = SyncCursor(token=str(start + limit)) if returned == limit else None
        return DocumentPage(documents=documents, next_cursor=next_cursor, incremental_cursor=None)

    def _list_jira(self, cursor: SyncCursor | None) -> DocumentPage:
        start = int(cursor.token) if cursor is not None else 0
        limit = 50
        jql = self._config.options.get("jql", "order by created")
        params = {"jql": str(jql), "startAt": str(start), "maxResults": str(limit)}
        url = f"{self._base()}/rest/api/2/search?{urlencode(params)}"
        payload = self._get(url).json()

        documents: list[DocumentRef] = []
        for issue in payload.get("issues", []):
            key = issue["key"]
            fields = issue.get("fields", {})
            documents.append(
                DocumentRef(
                    source_id=key,
                    source_path=f"{self._base()}/browse/{key}",
                    display_name=fields.get("summary", key),
                    media_type="application/json",
                    source_modified_at=_parse_ts(fields.get("updated")),
                    metadata={"product": _JIRA},
                )
            )
        total = int(payload.get("total", 0))
        next_start = start + limit
        next_cursor = SyncCursor(token=str(next_start)) if next_start < total else None
        return DocumentPage(documents=documents, next_cursor=next_cursor, incremental_cursor=None)

    # -- fetch ----------------------------------------------------------

    def fetch_document(self, doc_ref: DocumentRef) -> RawDocument:
        product = doc_ref.metadata.get("product", self._product)
        if product == _CONFLUENCE:
            query = urlencode({"expand": "body.storage"})
            url = f"{self._base()}/rest/api/content/{doc_ref.source_id}?{query}"
            payload = self._get(url).json()
            body = payload.get("body", {}).get("storage", {}).get("value", "")
            content = body.encode("utf-8")
            media_type = "text/html"
        else:
            url = f"{self._base()}/rest/api/2/issue/{doc_ref.source_id}"
            resp = self._get(url)
            content = resp.body
            media_type = "application/json"
        return RawDocument(
            ref=doc_ref,
            content=content,
            media_type=media_type,
            fetched_at=datetime.now(UTC),
        )

    # -- permissions ----------------------------------------------------

    def fetch_permissions(self, doc_ref: DocumentRef) -> PermissionRecord:
        product = doc_ref.metadata.get("product", self._product)
        if product == _JIRA:
            # Jira issue security is not reliably exposed as a filterable ACL;
            # fail closed rather than launder issues as public (§14.3).
            return PermissionRecord(
                doc_ref=doc_ref,
                principals_read=[],
                fidelity=PermissionFidelity.unavailable,
                raw={"reason": "jira issue-level security not exposed as filterable ACL"},
            )
        return self._confluence_permissions(doc_ref)

    def _confluence_permissions(self, doc_ref: DocumentRef) -> PermissionRecord:
        url = f"{self._base()}/rest/api/content/{doc_ref.source_id}/restriction/byOperation/read"
        resp = self._get(url)
        if resp.status_code != 200:
            return PermissionRecord(
                doc_ref=doc_ref,
                principals_read=[],
                fidelity=PermissionFidelity.unavailable,
                raw={"http_status": resp.status_code},
            )
        payload = resp.json()
        restrictions = payload.get("restrictions", {})
        principals: list[str] = []
        for user in restrictions.get("user", {}).get("results", []):
            principals.append(f"user:{user.get('accountId', user.get('username', ''))}")
        for group in restrictions.get("group", {}).get("results", []):
            principals.append(f"group:{group.get('name', group.get('id', ''))}")
        # An empty page-level read-restriction set means the page INHERITS its
        # space permissions, which this connector does not fetch. Reporting an
        # empty principals list as `authoritative` would launder a space-scoped
        # ACL as an authoritative empty read-list — downstream could not tell
        # "space-inherited" from "restricted to nobody". Fail honest: downgrade
        # fidelity to `best_effort` so the incompleteness is signalled (§14.3).
        # (Fetching space membership is a larger follow-up.)
        if not principals:
            return PermissionRecord(
                doc_ref=doc_ref,
                principals_read=[],
                fidelity=PermissionFidelity.best_effort,
                raw=payload,
            )
        return PermissionRecord(
            doc_ref=doc_ref,
            principals_read=principals,
            fidelity=PermissionFidelity.authoritative,
            raw=payload,
        )


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _factory(config: ConnectorConfig) -> Connector:
    """Registry factory; injects the production ``httpx`` client."""
    import httpx

    from finecorpus.connectors.http import HttpxClient

    return AtlassianConnector(config, HttpxClient(httpx.Client(timeout=30.0)))


# One class, registered under both Atlassian source kinds.
register_connector(SourceKind.confluence.value, _factory)
register_connector(SourceKind.jira.value, _factory)

__all__ = ["AtlassianConnector"]
