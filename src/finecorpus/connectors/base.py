"""Connector abstraction and the §14.3 fail-closed permission gate.

A *connector* is a SOURCE that feeds documents plus permission metadata into
the Collect stage.  This module defines:

- ``Connector`` — the abstract base every concrete connector subclasses. It
  mirrors the ``EmbeddingProvider`` ABC shape (capabilities + a small, fixed
  method surface) rather than inventing a parallel pattern.
- ``TokenStore`` — the documented control-plane seam for resolving a token by
  reference. Tokens are secrets and are NEVER written to config or logs (§14.2).
- ``enforce_permission_fidelity`` — the single hard check that implements the
  §14.3 fail-closed rule: a connector reporting ``unavailable`` fidelity must
  NOT ingest unless the run carries an explicit acknowledged permission gap.
- ``PermissionFidelityError`` — raised when that check blocks a run.

Design rationale for putting the check in the framework (not each connector):
§14.3 is a platform guarantee ("the platform MUST … refuse the connection").
Leaving it to each connector would make it forgeable by omission — a new
connector that simply never calls the check would silently launder
permissions.  Encoding it once here gives every connector a single, tested
gate to route through.

D-42 — the guarantee, stated accurately (scoped to the connector framework):

Within the connector framework, :meth:`Connector.collect` is the SOLE producer
of ingested items and is override-proof (``@typing.final`` plus an
``__init_subclass__`` guard that raises ``TypeError`` if a subclass tries to
define its own ``collect``). It ALWAYS routes permission data through
:func:`~finecorpus.connectors.gate.enforce_permission_fidelity` (via the gated
:meth:`PermissionRecord.to_source_permissions` converter) before fetching
content. A concrete connector implements only ``list_documents`` /
``fetch_document`` / ``fetch_permissions``; it never produces a
``SourcePermissions`` itself, and it cannot skip the gate by overriding
``collect``.

The ``SourcePermissions`` contract model itself remains freely constructible by
design, because non-connector pipeline stages (e.g. ``assess/stage.py``,
``plan/stage.py``) may legitimately produce it. So the gate is structural for
the connector ingestion path — not a global constructor lock on
``SourcePermissions``. A hand-built ``SourcePermissions`` outside the framework
is possible and is out of this framework's control; that is by design, not a
hole.
"""

from __future__ import annotations

import abc
import typing
from collections.abc import Iterator
from dataclasses import dataclass

from finecorpus.connectors.gate import (
    PermissionFidelityError,
    enforce_permission_fidelity,
)
from finecorpus.connectors.models import (
    CollectedItem,
    ConnectorConfig,
    DocumentPage,
    DocumentRef,
    PermissionRecord,
    RawDocument,
    SyncCursor,
)
from finecorpus.contracts.inventory import SourceRun
from finecorpus.contracts.shared.blocks import PermissionFidelity

# ---------------------------------------------------------------------------
# Capability declaration (mirrors embedding ProviderCapabilities, §2.1 shape)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConnectorCapabilities:
    """Static capability declaration for a connector.

    A plain data structure, populated at construction time — it MUST NOT make
    network calls, mirroring ``ProviderCapabilities``.

    Attributes
    ----------
    connector_id:
        Stable registry name (``"sharepoint"``, ``"confluence"``, ``"gdrive"``).
    supports_incremental:
        Whether the connector can produce a delta-sync cursor.
    supplies_permissions:
        Whether the connector can, in principle, supply source ACLs. A value of
        ``False`` means every run is inherently ``unavailable`` fidelity and
        therefore requires an acknowledged permission gap (§14.3).
    max_permission_fidelity:
        The best fidelity this connector can ever achieve. The framework uses
        this to reason about §14.3 up front, before any document is fetched.
    """

    connector_id: str
    supports_incremental: bool
    supplies_permissions: bool
    max_permission_fidelity: PermissionFidelity


# ---------------------------------------------------------------------------
# Token storage seam (§14.2)
# ---------------------------------------------------------------------------


class TokenStore(abc.ABC):
    """Control-plane seam for resolving connector credentials by reference.

    Concrete implementations live in the control plane (encrypted at rest,
    §14.2).  The framework depends only on this abstraction so tokens never
    have to be passed as literals through config, logs, or exports.

    Implementations MUST NOT log token values and MUST NOT include them in
    exception messages or reprs.
    """

    @abc.abstractmethod
    def get_token(self, token_ref: str) -> str:
        """Resolve ``token_ref`` (an opaque handle) to a secret token value.

        Raises
        ------
        KeyError
            If no token is stored under ``token_ref``.
        """


# ---------------------------------------------------------------------------
# §14.3 fail-closed permission gate
# ---------------------------------------------------------------------------
#
# The gate itself (``enforce_permission_fidelity`` + ``PermissionFidelityError``)
# lives in the leaf module :mod:`finecorpus.connectors.gate` so that both the
# data models and this base can route through it without an import cycle. They
# are re-exported here for backward compatibility (existing imports from
# ``finecorpus.connectors.base`` keep working).


# ---------------------------------------------------------------------------
# Connector abstract base
# ---------------------------------------------------------------------------


class Connector(abc.ABC):
    """Abstract base for all source connectors (§14).

    Concrete connectors (SharePoint, Confluence, Google Drive) subclass this
    and implement the abstract methods. The platform drives a connector through
    a fixed, small surface:

    1. ``authenticate`` once per run.
    2. ``list_documents`` paginated (optionally from a delta cursor).
    3. ``fetch_permissions`` then ``fetch_document`` per document.

    Permission fidelity is enforced by the framework in a SINGLE place:
    :meth:`collect`, the framework-owned, override-proof ingestion loop. It
    routes every document's permissions through the gated
    :meth:`PermissionRecord.to_source_permissions` converter (which calls
    :func:`enforce_permission_fidelity`) BEFORE fetching bytes. A concrete
    connector implements only ``list_documents`` / ``fetch_document`` /
    ``fetch_permissions`` and never produces a ``SourcePermissions`` itself.
    Because :meth:`collect` is ``@typing.final`` and guarded by
    :meth:`__init_subclass__`, a subclass cannot override it to skip the gate.
    """

    def __init_subclass__(cls, **kwargs: object) -> None:
        """Reject any subclass that tries to define its own ``collect`` (D-42).

        :meth:`collect` is the framework's sole, gated ingestion path.
        ``@typing.final`` documents that at type-check time; this guard makes it
        a RUNTIME fact: defining ``collect`` in a subclass raises ``TypeError``
        at class-definition time, so a connector cannot bypass the §14.3 gate by
        overriding the loop.
        """
        super().__init_subclass__(**kwargs)
        if "collect" in cls.__dict__:
            raise TypeError(
                f"{cls.__name__} may not override Connector.collect: it is the "
                "framework-owned, §14.3-gated ingestion path (D-42). Implement "
                "list_documents / fetch_document / fetch_permissions instead."
            )

    @property
    @abc.abstractmethod
    def capabilities(self) -> ConnectorCapabilities:
        """Static capability declaration. MUST NOT make network calls."""

    @abc.abstractmethod
    def authenticate(self, config: ConnectorConfig, token_store: TokenStore) -> None:
        """Establish an authenticated session for this run.

        Credentials are resolved via ``token_store`` from ``config.token_ref``
        / ``config.secret_env_vars`` — never read from ``config`` as literals
        (§14.2). Implementations MUST NOT log resolved token values.
        """

    @abc.abstractmethod
    def list_documents(self, cursor: SyncCursor | None = None) -> DocumentPage:
        """Return one page of document references.

        ``cursor=None`` starts a fresh listing. For incremental (delta) sync,
        pass the ``incremental_cursor`` persisted from the previous run so only
        changed documents are listed (§6.1).
        """

    @abc.abstractmethod
    def fetch_document(self, doc_ref: DocumentRef) -> RawDocument:
        """Fetch the raw bytes of a single document."""

    @abc.abstractmethod
    def fetch_permissions(self, doc_ref: DocumentRef) -> PermissionRecord:
        """Fetch source-side ACLs for a document (§14.3).

        The returned ``PermissionRecord.fidelity`` signals reliability. A
        connector that cannot obtain reliable ACLs MUST report
        ``PermissionFidelity.unavailable`` rather than fabricating open access.
        """

    def incremental_cursor(self, page: DocumentPage) -> SyncCursor | None:
        """Return the delta-sync watermark to persist after a full listing.

        Default: the page's ``incremental_cursor``. Connectors that compute the
        watermark differently may override.
        """
        return page.incremental_cursor

    # ------------------------------------------------------------------
    # D-42: framework-owned collect loop — the SOLE producer of ingestable
    # (RawDocument, SourcePermissions) pairs and the SINGLE §14.3 enforcement
    # entry point. Concrete connectors NEVER build a SourcePermissions
    # themselves; they implement only list/fetch/permissions. This loop routes
    # every item through the gated converter, and it is override-proof
    # (@typing.final + __init_subclass__ guard), so the §14.3 check is
    # structurally on the only path from a connector into the inventory.
    # ------------------------------------------------------------------

    @typing.final
    def collect(
        self,
        source_run: SourceRun,
        *,
        cursor: SyncCursor | None = None,
    ) -> Iterator[CollectedItem]:
        """Yield gated ``(RawDocument, SourcePermissions)`` pairs for a run.

        This is the framework-owned ingestion seam. It paginates
        ``list_documents`` (starting from ``cursor`` for delta sync), and for
        each document fetches permissions FIRST and converts them through
        :meth:`PermissionRecord.to_source_permissions` — which runs the §14.3
        gate. Only after the gate passes does it fetch the document bytes and
        yield a :class:`CollectedItem`.

        Because :class:`CollectedItem` carries a contract ``SourcePermissions``
        (not a raw ``PermissionRecord``), and the only way to obtain one is via
        the gated converter, a connector that implements the abstract methods
        STILL cannot emit restricted content as public: the gate is on the sole
        path. This method is ``@typing.final`` and guarded by
        :meth:`__init_subclass__`, so a subclass cannot override it to route
        around the gate. Fetching bytes only after the gate also means blocked
        documents are never downloaded.

        Raises
        ------
        PermissionFidelityError
            The first time a document's permissions are ``unavailable`` and the
            run has not acknowledged the gap — the run fails closed (§14.3).
        """
        connector_id = self.capabilities.connector_id
        page: DocumentPage | None = self.list_documents(cursor=cursor)
        while page is not None:
            for doc_ref in page.documents:
                record = self.fetch_permissions(doc_ref)
                # Gated conversion — raises PermissionFidelityError on an
                # unacknowledged unavailable-fidelity document. Runs BEFORE the
                # (potentially expensive) byte fetch, so blocked docs are never
                # downloaded.
                source_permissions = record.to_source_permissions(
                    connector_id=connector_id,
                    source_run=source_run,
                )
                raw = self.fetch_document(doc_ref)
                yield CollectedItem(document=raw, permissions=source_permissions)
            page = (
                self.list_documents(cursor=page.next_cursor)
                if page.next_cursor is not None
                else None
            )

    def __repr__(self) -> str:
        """Safe repr — never includes credential material (§14.2)."""
        caps = self.capabilities
        return (
            f"{self.__class__.__name__}("
            f"connector_id={caps.connector_id!r}, "
            f"supports_incremental={caps.supports_incremental}, "
            f"supplies_permissions={caps.supplies_permissions})"
        )


__all__ = [
    "ConnectorCapabilities",
    "TokenStore",
    "PermissionFidelityError",
    "enforce_permission_fidelity",
    "Connector",
    "CollectedItem",
]
