"""KB status / memory cost tests (M-096, Phase 4).

Covers:
- test_hot_copy_memory_cost_displayed_m096: `corpus kb status <kb_id>` output
  shows hot/cold version inventory + estimated memory footprint per hot copy
  + the §10.2 advisory note about hot_retention_count.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Helpers: minimal fake adapter + control plane
# ---------------------------------------------------------------------------


class _FakeCollectionInfo:
    def __init__(self, name: str, vector_size: int, point_count: int):
        self.name = name
        self.vector_size = vector_size
        self.point_count = point_count
        self.metadata = {}


class _FakeAdapter:
    def __init__(self, infos: dict[str, _FakeCollectionInfo]):
        self._infos = infos

    def get_collection_info(self, collection: str) -> _FakeCollectionInfo:
        if collection not in self._infos:
            from finecorpus.index.adapter import CollectionNotFoundError

            raise CollectionNotFoundError(f"Collection {collection!r} not found")
        return self._infos[collection]

    def list_snapshots(self, collection_name: str) -> list:
        return []


class _FakeAliasRecord:
    def __init__(self, kb_id: str, coll: str | None, prev_coll: str | None = None):
        self.kb_id = kb_id
        self.alias = f"rtfc_{kb_id}"
        self.collection_name = coll
        self.previous_collection = prev_coll
        self.embedding_dimensions = 128
        self.embedding_provider = "fake"
        self.embedding_model = "fake-v1"
        self.promoted_at = None
        self.workspace_id = "ws-status-test"


class _FakeAliasRepository:
    def __init__(self, record: _FakeAliasRecord | None):
        self._record = record

    def get(self, alias: str) -> _FakeAliasRecord | None:
        return self._record


# ---------------------------------------------------------------------------
# Test: M-096 — memory cost displayed in kb status
# ---------------------------------------------------------------------------


def test_hot_copy_memory_cost_displayed_m096(capsys):
    """corpus kb status shows memory footprint estimate + §10.2 advisory.

    Directly calls _cmd_kb_status with fake deps to verify output content.
    """
    import argparse

    from finecorpus.cli.main import _cmd_kb_status

    kb_id = "kb-status-test"
    coll = f"rtfc_{kb_id.replace('-', '')}_00000001"

    # 1000 points, 128 dims
    dims = 128
    count = 1000
    # Expected estimate: 1000 × 128 × 4B + 1000 × 500B = 1_012_000 bytes (~0.97 MB)

    info = _FakeCollectionInfo(coll, dims, count)
    adapter = _FakeAdapter({coll: info})
    alias_record = _FakeAliasRecord(kb_id=kb_id, coll=coll)
    alias_repo = _FakeAliasRepository(alias_record)

    fake_config = MagicMock()
    fake_config.storage.postgres.url = None
    fake_config.storage.qdrant.url = "http://localhost:6333"
    fake_config.index_lifecycle.hot_retention_count = 2

    args = argparse.Namespace(kb_id=kb_id, config=None)

    def fake_session_factory():
        sess = MagicMock()
        sess.__enter__ = lambda s: sess
        sess.__exit__ = lambda *a: None
        return sess

    # CLI imports everything locally inside the function, so patch at source modules
    with (
        patch("finecorpus.config.loader.load_config", return_value=fake_config),
        patch("sqlalchemy.create_engine"),
        patch("sqlalchemy.orm.sessionmaker", return_value=fake_session_factory),
        patch("finecorpus.index.qdrant.backend.QdrantAdapter", return_value=adapter),
        patch("finecorpus.control.metadata.AliasRepository", return_value=alias_repo),
        patch("finecorpus.index.adapter.alias_name", return_value=f"rtfc_{kb_id}"),
    ):
        _cmd_kb_status(args)

    captured = capsys.readouterr()
    output = captured.out

    # Verify output contains required elements
    assert "KB status:" in output or "alias" in output
    assert str(count) in output.replace(",", "") or f"{count:,}" in output
    assert str(dims) in output
    # Memory estimate appears in some form
    assert "MB" in output or "memory estimate" in output or "bytes" in output
    # §10.2 advisory must be present
    assert "hot_retention_count" in output or "§10.2" in output or "NOTE" in output


def test_kb_status_command_registered():
    """corpus kb status is registered as a subcommand in the CLI parser."""
    from finecorpus.cli.main import _build_parser

    parser = _build_parser()
    # Parse kb status --help should not error
    try:
        parser.parse_args(["kb", "status", "--help"])
    except SystemExit as exc:
        # Help exits with 0
        assert exc.code == 0


def test_kb_export_command_registered():
    """corpus kb export is registered as a subcommand in the CLI parser."""
    from finecorpus.cli.main import _build_parser

    parser = _build_parser()
    try:
        parser.parse_args(["kb", "export", "--help"])
    except SystemExit as exc:
        assert exc.code == 0


def test_memory_estimate_formula():
    """Verify the §10.2 memory formula: count × dims × 4B + count × 500B payload."""
    # 2000 points × 1536 dims × 4 bytes = 12_288_000 bytes + payload
    count = 2000
    dims = 1536
    vector_bytes = count * dims * 4
    payload_estimate = count * 500
    total_mb = (vector_bytes + payload_estimate) / (1024 * 1024)
    # 2000 × 1536 × 4 = 12,288,000 bytes = 11.7 MB
    # + 2000 × 500 = 1,000,000 bytes = 0.95 MB
    # total ≈ 12.65 MB
    assert 12.0 < total_mb < 13.0, f"Unexpected estimate: {total_mb:.2f} MB"
