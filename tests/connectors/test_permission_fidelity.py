"""§14.3 fail-closed permission gate — the headline requirement.

A connector reporting permission_fidelity=unavailable WITHOUT an acknowledged
gap MUST be blocked. With acknowledgement, it proceeds. Reliable fidelities
(authoritative/best_effort) always proceed. Also proves connector permission
data threads into the existing SourcePermissions / SourceRun contracts rather
than a parallel model.
"""

from __future__ import annotations

import pytest

from finecorpus.connectors.base import (
    PermissionFidelityError,
    enforce_permission_fidelity,
)
from finecorpus.contracts.inventory import SourceKind, SourcePermissions, SourceRun
from finecorpus.contracts.shared.blocks import PermissionFidelity
from tests.connectors.conftest import FakeConnector


def _run(*, ack: bool | None) -> SourceRun:
    return SourceRun(
        source_kind=SourceKind.sharepoint,
        connector_id="inst-1",
        acknowledged_permission_gap=ack,
    )


def test_unavailable_without_ack_is_blocked() -> None:
    with pytest.raises(PermissionFidelityError) as exc:
        enforce_permission_fidelity(
            connector_id="sharepoint",
            fidelity=PermissionFidelity.unavailable,
            source_run=_run(ack=None),
        )
    assert exc.value.fidelity is PermissionFidelity.unavailable
    # §14.2 — no credential material in the message.
    assert "token" not in str(exc.value).lower()


def test_unavailable_with_ack_proceeds() -> None:
    # Must not raise.
    enforce_permission_fidelity(
        connector_id="sharepoint",
        fidelity=PermissionFidelity.unavailable,
        source_run=_run(ack=True),
    )


@pytest.mark.parametrize(
    "fidelity",
    [PermissionFidelity.authoritative, PermissionFidelity.best_effort],
)
def test_reliable_fidelity_always_proceeds(fidelity: PermissionFidelity) -> None:
    enforce_permission_fidelity(
        connector_id="sharepoint",
        fidelity=fidelity,
        source_run=_run(ack=None),
    )


def test_connector_collect_blocks_unavailable() -> None:
    # The framework-owned collect loop is the SINGLE enforcement entry point
    # (guard_permissions was removed in favour of one narrative — D-42/m-3).
    conn = FakeConnector(fidelity=PermissionFidelity.unavailable, connector_id="sharepoint")
    with pytest.raises(PermissionFidelityError):
        list(conn.collect(_run(ack=None)))


def test_connector_gated_converter_allows_authoritative_and_threads_into_contract() -> None:
    conn = FakeConnector(fidelity=PermissionFidelity.authoritative, connector_id="sharepoint")
    run = _run(ack=None)
    doc = conn.list_documents().documents[0]
    perms = conn.fetch_permissions(doc)

    # Thread into the EXISTING contract model — no parallel permission model.
    # D-42: to_source_permissions REQUIRES the run context and runs the gate.
    source_perms = perms.to_source_permissions(connector_id="sharepoint", source_run=run)
    assert isinstance(source_perms, SourcePermissions)
    assert source_perms.fidelity is PermissionFidelity.authoritative
    assert source_perms.principals_read == ["user:alice"]
    assert source_perms.raw == {"acl": "raw"}
