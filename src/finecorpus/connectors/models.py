"""Connector framework data models (§14).

These pydantic models are the wire between a concrete connector (SharePoint,
Confluence, Google Drive, …) and the Collect stage.  They are deliberately
transport-agnostic: a connector fills them from whatever the source API
returns, and the framework threads them into the existing Inventory /
``SourceRun`` / ``SourcePermissions`` contracts.

Security invariants (§14.2):
- ``extra="forbid"`` on every model — unknown keys are rejected, so a stray
  ``access_token`` field cannot ride along into a config export.
- ``ConnectorConfig`` carries secrets ONLY by reference (env-var name or a
  control-plane token handle), never a literal credential value.  The
  framework never writes these models to logs or config exports verbatim.

The permission-fidelity fields reuse ``finecorpus.contracts`` enums directly
(``PermissionFidelity``) so there is exactly one source of truth for the §14.3
fail-closed rule — this framework does NOT invent a parallel one.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from finecorpus.connectors.gate import enforce_permission_fidelity
from finecorpus.contracts.inventory import SourceKind, SourcePermissions, SourceRun
from finecorpus.contracts.shared.blocks import PermissionFidelity

# ---------------------------------------------------------------------------
# Document reference + raw document
# ---------------------------------------------------------------------------


class DocumentRef(BaseModel):
    """A stable handle to a single document in the source system.

    Returned by ``list_documents`` and passed back to ``fetch_document`` /
    ``fetch_permissions``.  ``source_id`` is the connector's own opaque id
    (SharePoint item id, Confluence page id, Drive file id); the framework
    never interprets it.
    """

    model_config = ConfigDict(extra="forbid")

    source_id: str = Field(description="Opaque source-system id for this document.")
    source_path: str = Field(
        description="Canonical path/URI within the source system, for the InventoryItem."
    )
    display_name: str = Field(description="Human-facing name for reports and citations.")
    media_type: str | None = Field(
        default=None,
        description="MIME type when the source declares it; otherwise detected at fetch.",
    )
    source_modified_at: datetime | None = Field(
        default=None,
        description="Source-side last-modified timestamp; drives incremental change detection.",
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Free-form source metadata. Never secrets (§14.2).",
    )


class RawDocument(BaseModel):
    """The bytes of one document plus the metadata needed to inventory it.

    A connector's ``fetch_document`` returns this.  ``content`` is the raw
    source bytes; the Collect stage hashes them for the content-addressed
    ``document_id``.
    """

    model_config = ConfigDict(extra="forbid")

    ref: DocumentRef = Field(description="The reference this document was fetched from.")
    content: bytes = Field(description="Raw document bytes as returned by the source.")
    media_type: str = Field(description="Resolved MIME type for parser selection.")
    fetched_at: datetime = Field(description="When the connector fetched these bytes (UTC).")


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


class SyncCursor(BaseModel):
    """An opaque, source-side change cursor for incremental (delta) sync (§6.1).

    The framework treats ``token`` as opaque and round-trips it: it is stored
    on ``SourceRun.incremental_cursor`` and handed back to the connector on the
    next run so only changed documents are re-fetched.
    """

    model_config = ConfigDict(extra="forbid")

    token: str = Field(description="Opaque source-side cursor. Never interpreted by the framework.")


class DocumentPage(BaseModel):
    """One page of ``list_documents`` results.

    Pagination is cursor-based: ``next_cursor`` is None on the last page.
    ``incremental_cursor`` is the delta-sync watermark to persist once the
    whole listing completes (distinct from the intra-listing ``next_cursor``).
    """

    model_config = ConfigDict(extra="forbid")

    documents: list[DocumentRef] = Field(description="Document references on this page.")
    next_cursor: SyncCursor | None = Field(
        default=None,
        description="Cursor for the next page; None means this is the last page.",
    )
    incremental_cursor: SyncCursor | None = Field(
        default=None,
        description=(
            "Delta-sync watermark to persist after the full listing completes. "
            "Round-tripped into SourceRun.incremental_cursor."
        ),
    )


# ---------------------------------------------------------------------------
# Permissions
# ---------------------------------------------------------------------------


class PermissionRecord(BaseModel):
    """Source-side ACLs for one document, as the connector observed them (§14.3).

    This is the connector-facing shape.  ``to_source_permissions`` converts it
    into the existing ``finecorpus.contracts`` ``SourcePermissions`` model that
    the Inventory carries — there is no second permission model in the system.

    The ``fidelity`` field is the headline §14.3 signal: ``unavailable`` means
    the connector could not obtain reliable ACLs, which triggers the framework's
    fail-closed check unless the run carries an acknowledged permission gap.
    """

    model_config = ConfigDict(extra="forbid")

    doc_ref: DocumentRef = Field(description="The document these permissions apply to.")
    principals_read: list[str] = Field(
        default_factory=list,
        description="Source-side principals with read access (users, groups, roles).",
    )
    fidelity: PermissionFidelity = Field(
        description=(
            "Reliability of these ACLs (§14.3). 'unavailable' fails ingestion closed "
            "unless the run carries an acknowledged permission gap."
        )
    )
    raw: dict[str, Any] | None = Field(
        default=None,
        description="Verbatim source ACL payload retained for audit.",
    )

    def to_source_permissions(
        self,
        *,
        connector_id: str,
        source_run: SourceRun,
    ) -> SourcePermissions:
        """Convert into the contract ``SourcePermissions`` carried by the Inventory.

        This is the single seam where connector permission data enters the
        existing §14.3 machinery — nothing is re-modelled.

        D-42 (structural gate for the connector path): this conversion REQUIRES
        the run context and runs :func:`enforce_permission_fidelity` FIRST. There
        is therefore no argument-free path from connector permission data to a
        contract ``SourcePermissions``: a connector cannot mint a
        ``SourcePermissions`` for ``unavailable`` fidelity via this converter
        without an acknowledged gap. (This scopes to the connector framework;
        the ``SourcePermissions`` model itself is still freely constructible by
        non-connector pipeline stages such as assess/plan may — by design.)

        Parameters
        ----------
        connector_id:
            Identifies the connector for the §14.3 error message. Never a secret.
        source_run:
            The Collect run record carrying ``acknowledged_permission_gap``.

        Raises
        ------
        PermissionFidelityError
            When ``fidelity`` is ``unavailable`` and the run has not
            acknowledged the gap (§14.3).
        """
        enforce_permission_fidelity(
            connector_id=connector_id,
            fidelity=self.fidelity,
            source_run=source_run,
        )
        return SourcePermissions(
            principals_read=list(self.principals_read),
            fidelity=self.fidelity,
            raw=self.raw,
        )


# ---------------------------------------------------------------------------
# Collected item — the framework-owned collect loop's output (D-42)
# ---------------------------------------------------------------------------


class CollectedItem(BaseModel):
    """One ingestable document plus its GATED contract permissions (§14.3, D-42).

    Produced ONLY by :meth:`finecorpus.connectors.base.Connector.collect`. It
    carries a contract ``SourcePermissions`` — not a raw ``PermissionRecord`` —
    because the only way to obtain a ``SourcePermissions`` from connector data
    is through the gated ``PermissionRecord.to_source_permissions``. A connector
    therefore cannot hand the pipeline a ``CollectedItem`` whose permissions
    skipped the §14.3 check.
    """

    model_config = ConfigDict(extra="forbid")

    document: RawDocument = Field(description="The raw document bytes + metadata to inventory.")
    permissions: SourcePermissions = Field(
        description="Gated source-side ACLs (already passed the §14.3 fidelity check)."
    )


# ---------------------------------------------------------------------------
# Connector configuration
# ---------------------------------------------------------------------------


class ConnectorConfig(BaseModel):
    """Non-secret configuration for a connector instance.

    Secrets are carried ONLY by reference (§14.2):
    - ``token_ref`` is a control-plane token handle (an id, never the token).
    - ``secret_env_vars`` names the environment variables holding credentials.

    ``extra="forbid"`` guarantees a literal ``client_secret`` cannot be smuggled
    in, so a config export stays secret-free by construction (§6.4, §14.2).
    """

    model_config = ConfigDict(extra="forbid")

    connector_id: str = Field(
        description="Which configured connector instance this is. Not a credential (§14.2)."
    )
    source_kind: SourceKind = Field(description="Source system kind (sharepoint, confluence, …).")
    endpoint: str | None = Field(
        default=None,
        description="Base URL/tenant of the source system. Not a secret.",
    )
    token_ref: str | None = Field(
        default=None,
        description=(
            "Control-plane token handle (an opaque id). The actual token is resolved via "
            "the TokenStore seam and NEVER stored here (§14.2)."
        ),
    )
    secret_env_vars: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Map of logical credential name → environment variable name that holds it. "
            "Names only; never values (§14.2)."
        ),
    )
    options: dict[str, Any] = Field(
        default_factory=dict,
        description="Connector-specific non-secret options (site id, space key, folder id, …).",
    )


__all__ = [
    "DocumentRef",
    "RawDocument",
    "SyncCursor",
    "DocumentPage",
    "PermissionRecord",
    "CollectedItem",
    "ConnectorConfig",
]
