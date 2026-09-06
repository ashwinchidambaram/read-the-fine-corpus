"""KB export tests (M-090, Phase 4).

Covers:
- test_kb_export_complete_m090: export of a seeded KB contains config + chunks +
  reports + manifest with accurate counts.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Minimal in-memory adapter for export testing (scrollable)
# ---------------------------------------------------------------------------


class _FakeScrollPoint:
    def __init__(self, payload: dict):
        self.payload = payload
        self.id = payload.get("chunk_id", "unknown")


class _FakeQdrantClient:
    """Minimal fake Qdrant client that supports scroll."""

    def __init__(self, points: list[dict]):
        self._points = points

    def scroll(
        self,
        collection_name: str,
        limit: int = 10,
        offset: Any = None,
        with_payload: bool = True,
        with_vectors: bool = False,
    ):
        start = 0 if offset is None else int(offset)
        batch = self._points[start : start + limit]
        next_offset = start + limit if (start + limit) < len(self._points) else None
        return [_FakeScrollPoint(p) for p in batch], next_offset


class _FakeAdapter:
    def __init__(self, points: list[dict]):
        self._client = _FakeQdrantClient(points)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_engine_and_session():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from finecorpus.control.metadata import Base, _ensure_all_models_imported

    _ensure_all_models_imported()
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _seed_kb(SessionLocal, kb_id: str, workspace_id: str = "ws-export-test"):
    """Register a KB alias in the control-plane DB."""
    from finecorpus.control.metadata import AliasRepository
    from finecorpus.index.adapter import alias_name

    with SessionLocal() as sess:
        alias_repo = AliasRepository(sess)
        als = alias_name(kb_id)
        existing = alias_repo.get(als)
        if existing is None:
            record = alias_repo.create(alias=als, kb_id=kb_id, workspace_id=workspace_id)
            # Simulate a promotion: set collection_name
            record.collection_name = f"rtfc_{kb_id.replace('-', '')}_{1:08d}"
            sess.commit()


# ---------------------------------------------------------------------------
# Test: complete export (M-090)
# ---------------------------------------------------------------------------


def test_kb_export_complete_m090(tmp_path):
    """Export of a seeded KB contains config + chunks + reports + manifest.

    Acceptance criterion M-090: the export bundle must contain:
    - config (if artifacts_root provided)
    - chunks (scrolled from live collection)
    - findings/exclusion reports (if present)
    - manifest.json with accurate chunk count
    """
    from finecorpus.pipeline.export import export_kb

    kb_id = "kb-export-test"
    SessionLocal = _make_engine_and_session()
    _seed_kb(SessionLocal, kb_id)

    # Seed fake chunks
    fake_chunks = [
        {
            "chunk_id": f"chk_{i}",
            "text": f"chunk text {i}",
            "trust_level": "untrusted_ingested",
            "provenance": {"document_id": f"doc-{i}"},
            "tenancy": {"kb_id": kb_id},
        }
        for i in range(7)
    ]
    adapter = _FakeAdapter(fake_chunks)

    # Create fake artifacts
    artifacts_root = tmp_path / "artifacts"
    run_id = "run-export-test"
    run_dir = artifacts_root / run_id
    run_dir.mkdir(parents=True)

    config_artifact = {
        "schema_version": "1.2.0",
        "kb_id": kb_id,
        "source": {"type": "local"},
    }
    (run_dir / "config.json").write_text(json.dumps(config_artifact), encoding="utf-8")

    findings_artifact = {
        "schema_version": "1.0.0",
        "run_id": run_id,
        "findings": [],
    }
    (run_dir / "report.json").write_text(json.dumps(findings_artifact), encoding="utf-8")

    out_dir = tmp_path / "export_out"

    with SessionLocal() as session:
        manifest = export_kb(
            kb_id=kb_id,
            out_dir=out_dir,
            session=session,
            adapter=adapter,
            artifacts_root=artifacts_root,
            run_id=run_id,
        )

    # --- Assertions ---

    # 1. Manifest exists and has correct chunk count
    assert manifest.kb_id == kb_id
    assert manifest.chunk_count == len(fake_chunks)

    manifest_path = out_dir / "manifest.json"
    assert manifest_path.exists(), "manifest.json must exist"

    manifest_data = json.loads(manifest_path.read_text())
    assert manifest_data["kb_id"] == kb_id
    assert manifest_data["chunk_count"] == len(fake_chunks)
    assert "exported_at" in manifest_data
    assert "file_hashes" in manifest_data

    # 2. Config artifact present
    assert (out_dir / "config.json").exists(), "config.json must be exported"
    cfg = json.loads((out_dir / "config.json").read_text())
    assert cfg["kb_id"] == kb_id

    # 3. Chunks directory with JSONL files
    chunks_dir = out_dir / "chunks"
    assert chunks_dir.exists(), "chunks/ directory must exist"
    chunk_files = list(chunks_dir.glob("*.jsonl"))
    assert len(chunk_files) >= 1, "At least one chunk file must be written"

    # Count total chunks across all files
    total_chunks = 0
    for cf in chunk_files:
        lines = [ln.strip() for ln in cf.read_text().splitlines() if ln.strip()]
        total_chunks += len(lines)
    assert total_chunks == len(fake_chunks), (
        f"Expected {len(fake_chunks)} chunks, got {total_chunks}"
    )

    # Verify each chunk record has trust_level
    first_file = sorted(chunk_files)[0]
    first_line = json.loads(first_file.read_text().splitlines()[0])
    assert "trust_level" in first_line

    # 4. Eval sets stub present
    eval_readme = out_dir / "eval_sets" / "README.txt"
    assert eval_readme.exists(), "eval_sets/README.txt stub must exist"
    assert "STUB" in eval_readme.read_text()

    # 5. File hashes in manifest match actual files
    for rel_path, expected_hash in manifest_data["file_hashes"].items():
        actual_path = out_dir / rel_path
        if actual_path.exists() and expected_hash:
            import hashlib

            actual_hash = hashlib.sha256(actual_path.read_bytes()).hexdigest()
            assert actual_hash == expected_hash, (
                f"Hash mismatch for {rel_path}: expected {expected_hash!r}, got {actual_hash!r}"
            )


def test_kb_export_unregistered_kb_raises(tmp_path):
    """export_kb raises ValueError if the KB is not registered."""
    from finecorpus.pipeline.export import export_kb

    SessionLocal = _make_engine_and_session()
    adapter = _FakeAdapter([])

    with SessionLocal() as session:
        with pytest.raises(ValueError, match="not registered"):
            export_kb(
                kb_id="kb-does-not-exist",
                out_dir=tmp_path / "out",
                session=session,
                adapter=adapter,
            )


def test_kb_export_no_collection_raises(tmp_path):
    """export_kb raises ValueError if the KB has never been promoted."""
    from finecorpus.control.metadata import AliasRepository
    from finecorpus.pipeline.export import export_kb

    SessionLocal = _make_engine_and_session()

    kb_id = "kb-no-collection"
    from finecorpus.index.adapter import alias_name as _alias_name

    with SessionLocal() as sess:
        alias_repo = AliasRepository(sess)
        alias_repo.create(alias=_alias_name(kb_id), kb_id=kb_id, workspace_id="ws-test")
        # Do NOT set collection_name (simulate never-promoted)
        sess.commit()

    adapter = _FakeAdapter([])
    with SessionLocal() as session:
        with pytest.raises(ValueError, match="no live collection"):
            export_kb(
                kb_id=kb_id,
                out_dir=tmp_path / "out",
                session=session,
                adapter=adapter,
            )
