"""The §14.3 fail-closed permission gate — the single hard check (D-42).

This is a *leaf* module: it imports only the ``finecorpus.contracts`` layer,
never ``models`` or ``base``.  Keeping the gate here lets BOTH the connector
data models (``PermissionRecord.to_source_permissions``) and the framework
collect loop (``Connector.collect``) route through the exact same check without
an import cycle.

Design rationale (D-42 — the gate is structural for the connector path).
§14.3 is a platform guarantee ("the platform MUST … refuse the connection"),
not a per-connector courtesy.  Within the connector framework the gate used to
be *enforced-if-called*: a caller could invoke
``PermissionRecord.to_source_permissions()`` directly, or a collect loop could
be overridden, and restricted content would launder through as public.  We close
that hole for the connector ingestion path two ways:

1. There is no argument-free way to build a contract ``SourcePermissions`` *from
   connector permission data*: ``PermissionRecord.to_source_permissions``
   REQUIRES a ``SourceRun`` and runs :func:`enforce_permission_fidelity` before
   it will emit anything.
2. The framework owns the collect loop (``Connector.collect``) that is the sole
   producer of ingestable ``(RawDocument, SourcePermissions)`` pairs; it routes
   every item through the same converter and is override-proof (``@typing.final``
   plus an ``__init_subclass__`` guard).

Scope, stated honestly: this does NOT lock down the ``SourcePermissions``
contract model globally.  That model is a plain public pydantic model that
non-connector pipeline stages (e.g. ``assess/stage.py``, ``plan/stage.py``) may
legitimately construct, and the framework neither can nor should prevent that.
The gate is structural for the connector ingestion PATH, not a global
constructor lock: a hand-built ``SourcePermissions`` outside the framework is
possible and out of this framework's control, by design.
"""

from __future__ import annotations

from finecorpus.contracts.inventory import SourceRun
from finecorpus.contracts.shared.blocks import PermissionFidelity


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


__all__ = [
    "PermissionFidelityError",
    "enforce_permission_fidelity",
]
