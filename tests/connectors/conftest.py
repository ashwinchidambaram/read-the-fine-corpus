"""Shared test doubles for the connector framework tests.

Defines an in-memory ``FakeConnector`` and ``FakeTokenStore`` so the framework
can be exercised with NO live network and NO real credentials.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from finecorpus.connectors.base import Connector, ConnectorCapabilities, TokenStore
from finecorpus.connectors.models import (
    ConnectorConfig,
    DocumentPage,
    DocumentRef,
    PermissionRecord,
    RawDocument,
    SyncCursor,
)
from finecorpus.contracts.inventory import SourceKind
from finecorpus.contracts.shared.blocks import PermissionFidelity


class FakeTokenStore(TokenStore):
    """In-memory token store for tests."""

    def __init__(self, tokens: dict[str, str] | None = None) -> None:
        self._tokens = tokens or {}

    def get_token(self, token_ref: str) -> str:
        return self._tokens[token_ref]


class FakeConnector(Connector):
    """A minimal in-memory connector for framework tests.

    Serves two hard-coded documents across two list pages, with configurable
    permission fidelity so the §14.3 gate can be exercised.
    """

    def __init__(
        self,
        *,
        fidelity: PermissionFidelity = PermissionFidelity.authoritative,
        connector_id: str = "fake",
    ) -> None:
        self._fidelity = fidelity
        self._connector_id = connector_id
        self.authenticated = False
        self._docs = [
            DocumentRef(source_id="d1", source_path="fake://d1", display_name="Doc 1"),
            DocumentRef(source_id="d2", source_path="fake://d2", display_name="Doc 2"),
        ]

    @property
    def capabilities(self) -> ConnectorCapabilities:
        return ConnectorCapabilities(
            connector_id=self._connector_id,
            supports_incremental=True,
            supplies_permissions=self._fidelity is not PermissionFidelity.unavailable,
            max_permission_fidelity=self._fidelity,
        )

    def authenticate(self, config: ConnectorConfig, token_store: TokenStore) -> None:
        self.authenticated = True

    def list_documents(self, cursor: SyncCursor | None = None) -> DocumentPage:
        if cursor is None:
            return DocumentPage(
                documents=[self._docs[0]],
                next_cursor=SyncCursor(token="page-2"),
                incremental_cursor=None,
            )
        if cursor.token == "page-2":
            return DocumentPage(
                documents=[self._docs[1]],
                next_cursor=None,
                incremental_cursor=SyncCursor(token="delta-100"),
            )
        raise AssertionError(f"unexpected cursor {cursor.token!r}")

    def fetch_document(self, doc_ref: DocumentRef) -> RawDocument:
        return RawDocument(
            ref=doc_ref,
            content=f"content-of-{doc_ref.source_id}".encode(),
            media_type="text/plain",
            fetched_at=datetime(2026, 1, 1, tzinfo=UTC),
        )

    def fetch_permissions(self, doc_ref: DocumentRef) -> PermissionRecord:
        return PermissionRecord(
            doc_ref=doc_ref,
            principals_read=["user:alice"] if self.capabilities.supplies_permissions else [],
            fidelity=self._fidelity,
            raw={"acl": "raw"},
        )


@pytest.fixture
def fake_config() -> ConnectorConfig:
    return ConnectorConfig(
        connector_id="fake-instance-1",
        source_kind=SourceKind.sharepoint,
        endpoint="https://tenant.example",
        token_ref="tok-1",
        secret_env_vars={"client_secret": "FINECORPUS_FAKE_SECRET"},
        options={"site_id": "abc"},
    )
