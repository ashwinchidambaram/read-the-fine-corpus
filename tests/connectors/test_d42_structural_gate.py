"""D-42: prove the §14.3 permission gate is structural for the connector path.

The failure §14.3 warns about is a "permission-laundering machine": restricted
source content ingested and served as public. Before D-42 the gate was only
*enforced-if-called* — a caller that invoked ``to_source_permissions()``
directly, or a collect loop that could be overridden, could launder it.

These tests prove the hole is now closed for the connector ingestion path:

1. There is NO argument-free path from connector permission data to a contract
   ``SourcePermissions``. ``PermissionRecord.to_source_permissions`` REQUIRES the
   run context and runs the fidelity check inside it. A rogue connector that
   tries to skip the gate STILL cannot mint a ``SourcePermissions`` for
   unavailable-fidelity content without an ack.

2. The framework owns the sole collect loop (``Connector.collect``), which is
   OVERRIDE-PROOF (``@typing.final`` + an ``__init_subclass__`` guard that raises
   ``TypeError`` at class-definition time). A connector only implements
   list/fetch/permissions; it never produces a ``SourcePermissions`` itself, and
   it cannot define its own ``collect`` to route around the gate. So the ONLY
   producer of ingestable items runs the gate — and it fetches bytes only after
   the gate passes, so blocked docs are never even downloaded.

Scope boundary (stated honestly): the ``SourcePermissions`` contract model
itself remains freely constructible, because non-connector pipeline stages
(e.g. assess/plan) may legitimately produce it. The gate is structural for the
connector framework's ingestion path — NOT a global constructor lock. A
hand-built ``SourcePermissions`` outside the framework is possible and out of
this framework's control, by design.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from finecorpus.connectors.base import (
    Connector,
    ConnectorCapabilities,
    PermissionFidelityError,
    TokenStore,
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
from finecorpus.contracts.inventory import SourceKind, SourcePermissions, SourceRun
from finecorpus.contracts.shared.blocks import PermissionFidelity


class RogueConnector(Connector):
    """A hostile/negligent connector that NEVER calls ``guard_permissions``.

    It reports ``unavailable`` fidelity (it cannot obtain reliable ACLs) but
    tries to get its restricted document ingested anyway. It must NOT be able to.
    ``fetch_document`` records whether it was called, to prove blocked documents
    are never downloaded.
    """

    def __init__(self) -> None:
        self.fetched: list[str] = []
        self._doc = DocumentRef(
            source_id="restricted-1",
            source_path="rogue://restricted-1",
            display_name="Board Compensation Memo",
        )

    @property
    def capabilities(self) -> ConnectorCapabilities:
        return ConnectorCapabilities(
            connector_id="rogue",
            supports_incremental=False,
            supplies_permissions=False,
            max_permission_fidelity=PermissionFidelity.unavailable,
        )

    def authenticate(self, config: ConnectorConfig, token_store: TokenStore) -> None:
        return None

    def list_documents(self, cursor: SyncCursor | None = None) -> DocumentPage:
        return DocumentPage(documents=[self._doc], next_cursor=None, incremental_cursor=None)

    def fetch_document(self, doc_ref: DocumentRef) -> RawDocument:
        self.fetched.append(doc_ref.source_id)
        return RawDocument(
            ref=doc_ref,
            content=b"top secret",
            media_type="text/plain",
            fetched_at=datetime.now(UTC),
        )

    def fetch_permissions(self, doc_ref: DocumentRef) -> PermissionRecord:
        # It cannot obtain reliable ACLs, but pretends read access is wide open
        # by leaving the fidelity truthful-but-unavailable. The framework must
        # refuse to turn this into a public SourcePermissions.
        return PermissionRecord(
            doc_ref=doc_ref,
            principals_read=[],
            fidelity=PermissionFidelity.unavailable,
            raw={"note": "no reliable acl"},
        )


def _run(*, ack: bool | None) -> SourceRun:
    return SourceRun(
        source_kind=SourceKind.sharepoint,
        connector_id="rogue",
        acknowledged_permission_gap=ack,
    )


def test_direct_to_source_permissions_cannot_skip_the_gate() -> None:
    """A caller that goes straight to to_source_permissions() is still gated."""
    conn = RogueConnector()
    doc = conn.list_documents().documents[0]
    record = conn.fetch_permissions(doc)  # unavailable fidelity, guard NOT called
    with pytest.raises(PermissionFidelityError):
        # No argument-free bypass exists: the converter runs the §14.3 check.
        record.to_source_permissions(connector_id="rogue", source_run=_run(ack=None))


def test_framework_collect_loop_blocks_and_never_downloads_bytes() -> None:
    """The sole ingestion producer refuses the restricted doc and skips the fetch."""
    conn = RogueConnector()
    with pytest.raises(PermissionFidelityError):
        # Draining the generator drives the collect loop.
        list(conn.collect(_run(ack=None)))
    # Fail-closed BEFORE download: bytes for the blocked doc were never fetched.
    assert conn.fetched == []


def test_collect_yields_gated_contract_permissions_when_acknowledged() -> None:
    """With an acknowledged gap the loop proceeds and yields CONTRACT permissions.

    The yielded item carries a SourcePermissions (already gated) — not a raw
    PermissionRecord — so there is no un-gated permission object to launder.
    """
    conn = RogueConnector()
    items = list(conn.collect(_run(ack=True)))
    assert len(items) == 1
    item = items[0]
    assert isinstance(item, CollectedItem)
    assert isinstance(item.permissions, SourcePermissions)
    assert item.permissions.fidelity is PermissionFidelity.unavailable
    # The document was downloaded only because the gate passed (ack=True).
    assert conn.fetched == ["restricted-1"]


def test_collect_is_the_only_producer_of_collecteditem() -> None:
    """CollectedItem's permissions type is SourcePermissions, obtainable only via the gate.

    A connector cannot construct a CollectedItem carrying a raw, un-gated
    PermissionRecord: the field is typed to the contract SourcePermissions, and
    the only converter into that type runs the §14.3 check.
    """
    with pytest.raises(ValidationError):
        # Passing a raw PermissionRecord where a SourcePermissions is required
        # is a validation error — the un-gated shape cannot enter the pipeline.
        CollectedItem(
            document=RawDocument(
                ref=DocumentRef(source_id="d", source_path="p", display_name="n"),
                content=b"x",
                media_type="text/plain",
                fetched_at=datetime.now(UTC),
            ),
            permissions=PermissionRecord(  # type: ignore[arg-type]
                doc_ref=DocumentRef(source_id="d", source_path="p", display_name="n"),
                principals_read=[],
                fidelity=PermissionFidelity.unavailable,
            ),
        )


def test_overriding_collect_raises_typeerror_at_class_definition() -> None:
    """B-1: a subclass CANNOT override the gated ``collect`` — it is override-proof.

    ``@typing.final`` documents the intent for type-checkers; the
    ``__init_subclass__`` guard makes it a RUNTIME fact. Attempting to define a
    ``Connector`` subclass with its own ``collect`` (the reviewer's live bypass)
    raises ``TypeError`` at class-definition time, so no such class can exist.
    """
    with pytest.raises(TypeError, match="may not override Connector.collect"):

        class BypassConnector(Connector):
            @property
            def capabilities(self) -> ConnectorCapabilities:  # pragma: no cover
                raise NotImplementedError

            def authenticate(
                self, config: ConnectorConfig, token_store: TokenStore
            ) -> None:  # pragma: no cover
                raise NotImplementedError

            def list_documents(
                self, cursor: SyncCursor | None = None
            ) -> DocumentPage:  # pragma: no cover
                raise NotImplementedError

            def fetch_document(self, doc_ref: DocumentRef) -> RawDocument:  # pragma: no cover
                raise NotImplementedError

            def fetch_permissions(
                self, doc_ref: DocumentRef
            ) -> PermissionRecord:  # pragma: no cover
                raise NotImplementedError

            # The bypass the reviewer demonstrated: hand-build a
            # SourcePermissions and yield a CollectedItem without the gate.
            def collect(  # type: ignore[misc]
                self, source_run: SourceRun, *, cursor: SyncCursor | None = None
            ):  # pragma: no cover
                yield CollectedItem(
                    document=RawDocument(
                        ref=DocumentRef(source_id="x", source_path="p", display_name="n"),
                        content=b"leaked",
                        media_type="text/plain",
                        fetched_at=datetime.now(UTC),
                    ),
                    permissions=SourcePermissions(
                        principals_read=[],
                        fidelity=PermissionFidelity.authoritative,
                    ),
                )


def test_source_permissions_is_freely_constructible_outside_the_framework() -> None:
    """B-2: honest scope boundary — the contract model itself is NOT locked down.

    ``SourcePermissions`` is a plain public contract model that non-connector
    pipeline stages (e.g. assess/plan) may legitimately construct. The connector
    framework does NOT — and must not — prevent that. This test documents that a
    hand-built ``SourcePermissions`` is possible and OUT OF the connector
    framework's control; it does NOT assert impossibility. The connector gate is
    structural for the ingestion PATH, not a global constructor lock.
    """
    # Directly constructible with no gate, no connector, no SourceRun.
    sp = SourcePermissions(principals_read=[], fidelity=PermissionFidelity.authoritative)
    assert isinstance(sp, SourcePermissions)
    assert sp.fidelity is PermissionFidelity.authoritative
