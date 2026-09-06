"""Connector registry — concrete connectors register by name.

Mirrors the embedding/llm registry pattern: a name → factory mapping plus a
builder that resolves a ``ConnectorConfig`` to a constructed :class:`Connector`.
Concrete connectors (SharePoint, Confluence, Google Drive) call
:func:`register_connector` at import time; the Collect stage / services call
:func:`build_connector` to instantiate one from config.

The registry stores *factories* (callables returning a ``Connector``) rather
than instances so construction stays lazy and per-run.
"""

from __future__ import annotations

from collections.abc import Callable

from finecorpus.connectors.base import Connector
from finecorpus.connectors.models import ConnectorConfig

# name -> factory(config) -> Connector
ConnectorFactory = Callable[[ConnectorConfig], Connector]

_REGISTRY: dict[str, ConnectorFactory] = {}


def register_connector(name: str, factory: ConnectorFactory) -> None:
    """Register a connector factory under ``name``.

    Raises
    ------
    ValueError
        If ``name`` is already registered (duplicate registration is a bug,
        not a silent override).
    """
    if name in _REGISTRY:
        raise ValueError(
            f"Connector '{name}' is already registered. Registration names must be unique."
        )
    _REGISTRY[name] = factory


def unregister_connector(name: str) -> None:
    """Remove ``name`` from the registry (primarily for tests)."""
    _REGISTRY.pop(name, None)


def registered_connectors() -> tuple[str, ...]:
    """Return the sorted names of all registered connectors."""
    return tuple(sorted(_REGISTRY))


def build_connector(config: ConnectorConfig) -> Connector:
    """Construct a connector for ``config.source_kind`` from the registry.

    Parameters
    ----------
    config:
        The connector instance configuration; ``config.source_kind`` selects
        the factory.

    Raises
    ------
    ValueError
        If no connector is registered for ``config.source_kind``.
    """
    name = config.source_kind.value
    factory = _REGISTRY.get(name)
    if factory is None:
        raise ValueError(
            f"No connector registered for source_kind='{name}'. "
            f"Registered: {registered_connectors()}."
        )
    return factory(config)


__all__ = [
    "ConnectorFactory",
    "register_connector",
    "unregister_connector",
    "registered_connectors",
    "build_connector",
]
