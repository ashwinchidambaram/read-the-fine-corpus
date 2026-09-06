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

Honesty note (do not overstate): today the gate is *enforced-if-called* — a
caller that invokes ``PermissionRecord.to_source_permissions`` directly, or a
Collect loop that forgets ``guard_permissions``, can still bypass it.  Making
the guarantee truly structural requires wiring this gate into the single
ingestion seam so the only path from a connector's permission data to a
contract ``SourcePermissions`` passes through the check — that wiring lands
with the concrete connectors (decision-ledger D-42).
"""

from __future__ import annotations

import abc
from dataclasses import dataclass

from finecorpus.connectors.models import (
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


class PermissionFidelityError(Exception):
    """Raised when §14.3 blocks ingestion for unreliable permission data.

    The platform refuses to ingest source content whose ACLs it cannot trust,
    unless an operator has explicitly acknowledged the gap. MUST NOT include
    any credential material (§14.2).
    """

    def __init__(self, connector_id: str, fidelity: PermissionFidelity) -> None:
        super().__init__(
            f"Connector '{connector_id}' reported permission_fidelity="
            f"'{fidelity.value}', which cannot be mirrored into filterable "
            f"permission fields. Per §14.3 the platform refuses to ingest "
            f"restricted content as public. Set "
            f"SourceRun.acknowledged_permission_gap=True to ingest anyway "
            f"(the run is then recorded as an acknowledged gap)."
        )
        self.connector_id = connector_id
        self.fidelity = fidelity


def enforce_permission_fidelity(
    *,
    connector_id: str,
    fidelity: PermissionFidelity,
    source_run: SourceRun,
) -> None:
    """Fail closed when permission data is unreliable and unacknowledged (§14.3).

    This is the single hard check the whole framework routes through. It is a
    pure function so it is trivial to unit-test and impossible to bypass by a
    connector that "forgets" to guard.

    Rule (§14.3):
    - ``authoritative`` / ``best_effort`` → allowed (mirrorable ACL data).
    - ``unavailable`` → allowed ONLY if
      ``source_run.acknowledged_permission_gap is True``; otherwise BLOCKED.

    Parameters
    ----------
    connector_id:
        For the error message; never a secret.
    fidelity:
        The fidelity the connector achieved for this run/document.
    source_run:
        The Collect run record carrying ``acknowledged_permission_gap``.

    Raises
    ------
    PermissionFidelityError
        When fidelity is ``unavailable`` and the run has not acknowledged it.
    """
    if fidelity is PermissionFidelity.unavailable and not source_run.acknowledged_permission_gap:
        raise PermissionFidelityError(connector_id=connector_id, fidelity=fidelity)


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

    Permission fidelity is enforced by the framework via ``guard_permissions``,
    which routes through :func:`enforce_permission_fidelity`. Concrete
    connectors (or the Collect stage on their behalf) MUST call
    ``guard_permissions`` for every document; the gate is enforced-if-called,
    not yet structurally unbypassable (see the module docstring and D-42) —
    the concrete-connector wiring makes it the single ingestion path.
    """

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
    # Framework-enforced §14.3 gate — connectors route through this.
    # ------------------------------------------------------------------

    def guard_permissions(
        self,
        permissions: PermissionRecord,
        source_run: SourceRun,
    ) -> PermissionRecord:
        """Apply the §14.3 fail-closed check, then return the record unchanged.

        Call this on every ``fetch_permissions`` result before ingesting the
        corresponding document. It raises :class:`PermissionFidelityError` when
        the record is ``unavailable`` fidelity and the run has not acknowledged
        the gap — so a connector cannot launder restricted content as public.
        """
        enforce_permission_fidelity(
            connector_id=self.capabilities.connector_id,
            fidelity=permissions.fidelity,
            source_run=source_run,
        )
        return permissions

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
]
