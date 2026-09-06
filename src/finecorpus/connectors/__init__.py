"""Connector framework for Read The Fine Corpus (§14).

A connector is a SOURCE that feeds documents plus permission metadata into the
Collect stage. This package is the framework/abstraction only — concrete
SharePoint / Confluence / Google Drive connectors are a separate work unit and
build on this surface.

Headline guarantee (§14.3): permission fidelity is enforced by the framework,
not by each connector. A connector that cannot supply reliable ACLs
(``PermissionFidelity.unavailable``) is BLOCKED from ingesting unless the run
carries an explicit acknowledged permission gap. See
:func:`~finecorpus.connectors.base.enforce_permission_fidelity`.

Public surface
--------------
- :class:`Connector` — ABC every concrete connector implements.
- :class:`ConnectorCapabilities` — static capability declaration.
- :class:`TokenStore` — control-plane seam for resolving secrets by reference (§14.2).
- :func:`enforce_permission_fidelity` / :class:`PermissionFidelityError` — the §14.3 gate.
- Data models: :class:`RawDocument`, :class:`DocumentPage`, :class:`DocumentRef`,
  :class:`PermissionRecord`, :class:`SyncCursor`, :class:`ConnectorConfig`.
- OAuth2: :class:`OAuth2Flow`, :class:`OAuth2Config`, :class:`OAuth2Token`.
- HTTP seam: :class:`HTTPClient`, :class:`HttpxClient`, :class:`HTTPResponse`.
- Registry: :func:`register_connector`, :func:`build_connector`, :func:`registered_connectors`.
- Test harness: :class:`RecordedHTTPClient` (see :mod:`finecorpus.connectors.fixtures`).
"""

from finecorpus.connectors.base import (
    Connector,
    ConnectorCapabilities,
    PermissionFidelityError,
    TokenStore,
    enforce_permission_fidelity,
)
from finecorpus.connectors.fixtures import RecordedHTTPClient
from finecorpus.connectors.http import HTTPClient, HTTPResponse, HttpxClient
from finecorpus.connectors.models import (
    ConnectorConfig,
    DocumentPage,
    DocumentRef,
    PermissionRecord,
    RawDocument,
    SyncCursor,
)
from finecorpus.connectors.oauth import OAuth2Config, OAuth2Error, OAuth2Flow, OAuth2Token
from finecorpus.connectors.registry import (
    ConnectorFactory,
    build_connector,
    register_connector,
    registered_connectors,
    unregister_connector,
)

__all__ = [
    # base
    "Connector",
    "ConnectorCapabilities",
    "TokenStore",
    "PermissionFidelityError",
    "enforce_permission_fidelity",
    # models
    "RawDocument",
    "DocumentPage",
    "DocumentRef",
    "PermissionRecord",
    "SyncCursor",
    "ConnectorConfig",
    # oauth
    "OAuth2Flow",
    "OAuth2Config",
    "OAuth2Token",
    "OAuth2Error",
    # http
    "HTTPClient",
    "HttpxClient",
    "HTTPResponse",
    # registry
    "register_connector",
    "unregister_connector",
    "registered_connectors",
    "build_connector",
    "ConnectorFactory",
    # test harness
    "RecordedHTTPClient",
]
