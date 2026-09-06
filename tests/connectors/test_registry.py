"""Registry round-trip: register a connector, build it from config, unregister."""

from __future__ import annotations

import pytest

from finecorpus.connectors.models import ConnectorConfig
from finecorpus.connectors.registry import (
    build_connector,
    register_connector,
    registered_connectors,
    unregister_connector,
)
from finecorpus.contracts.inventory import SourceKind
from tests.connectors.conftest import FakeConnector


def test_registry_round_trip() -> None:
    # sharepoint is the source_kind used by fake_config below.
    unregister_connector("sharepoint")
    register_connector("sharepoint", lambda cfg: FakeConnector(connector_id=cfg.connector_id))
    try:
        assert "sharepoint" in registered_connectors()
        cfg = ConnectorConfig(connector_id="inst-1", source_kind=SourceKind.sharepoint)
        conn = build_connector(cfg)
        assert isinstance(conn, FakeConnector)
        assert conn.capabilities.connector_id == "inst-1"
    finally:
        unregister_connector("sharepoint")


def test_duplicate_registration_rejected() -> None:
    unregister_connector("dup")
    register_connector("dup", lambda cfg: FakeConnector())
    try:
        with pytest.raises(ValueError, match="already registered"):
            register_connector("dup", lambda cfg: FakeConnector())
    finally:
        unregister_connector("dup")


def test_build_unknown_source_kind_raises() -> None:
    unregister_connector("gdrive")
    cfg = ConnectorConfig(connector_id="x", source_kind=SourceKind.gdrive)
    with pytest.raises(ValueError, match="No connector registered"):
        build_connector(cfg)
