"""Unit tests for the ``scroll_all`` adapter interface (D-41).

Covers the FakeAdapter implementation of ``scroll_all`` (iteration, filtering,
missing-collection error, sentinel score) and the export chunk-scroll path that
was previously untested because it reached into a private ``_client``.
"""

from __future__ import annotations

import json

import pytest

from finecorpus.index.adapter import CollectionNotFoundError
from tests.retrieval.helpers import FakeAdapter, make_chunk_payload


def _seed(adapter: FakeAdapter, coll: str, points: list[dict]) -> None:
    adapter.collections[coll] = {"points": points}


def test_scroll_all_yields_every_point() -> None:
    adapter = FakeAdapter()
    points = [make_chunk_payload(chunk_id=f"chk_{i}", kb_id="kb-a") for i in range(5)]
    _seed(adapter, "coll_a", points)

    results = list(adapter.scroll_all("coll_a"))
    assert len(results) == 5
    assert {r.chunk_id for r in results} == {f"chk_{i}" for i in range(5)}
    # scroll does not score points — score is a sentinel.
    assert all(r.score == 0.0 for r in results)


def test_scroll_all_applies_payload_filter() -> None:
    adapter = FakeAdapter()
    points = [
        make_chunk_payload(chunk_id="chk_a", kb_id="kb-a"),
        make_chunk_payload(chunk_id="chk_b", kb_id="kb-b"),
        make_chunk_payload(chunk_id="chk_c", kb_id="kb-a"),
    ]
    _seed(adapter, "coll_mixed", points)

    results = list(adapter.scroll_all("coll_mixed", {"tenancy.kb_id": "kb-a"}))
    assert {r.chunk_id for r in results} == {"chk_a", "chk_c"}


def test_scroll_all_filter_excludes_cross_tenant() -> None:
    """A filtered scroll must never yield off-filter points (tenancy boundary)."""
    adapter = FakeAdapter()
    points = [
        make_chunk_payload(chunk_id="mine", kb_id="kb-mine"),
        make_chunk_payload(chunk_id="theirs", kb_id="kb-theirs"),
    ]
    _seed(adapter, "coll_t", points)

    results = list(adapter.scroll_all("coll_t", {"tenancy.kb_id": "kb-mine"}))
    assert [r.chunk_id for r in results] == ["mine"]


def test_scroll_all_missing_collection_raises() -> None:
    adapter = FakeAdapter()
    with pytest.raises(CollectionNotFoundError):
        list(adapter.scroll_all("does_not_exist"))


def test_scroll_all_empty_collection() -> None:
    adapter = FakeAdapter()
    _seed(adapter, "empty", [])
    assert list(adapter.scroll_all("empty")) == []


# ---------------------------------------------------------------------------
# Export chunk-scroll path via FakeAdapter.scroll_all (previously untested)
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


def test_export_chunks_use_scroll_all(tmp_path) -> None:
    """export_kb writes chunk JSONL by iterating adapter.scroll_all (D-41).

    This exercises the export chunk portion through the first-class scroll API
    (no ``_client`` access) and asserts an accurate chunk count and file output.
    """
    from finecorpus.control.metadata import AliasRepository
    from finecorpus.index.adapter import alias_name
    from finecorpus.pipeline.export import export_kb

    kb_id = "kb-scroll-export"
    session_local = _make_engine_and_session()

    with session_local() as sess:
        repo = AliasRepository(sess)
        rec = repo.create(alias=alias_name(kb_id), kb_id=kb_id, workspace_id="ws-test")
        coll = f"rtfc_{kb_id.replace('-', '')}_00000001"
        rec.collection_name = coll
        sess.commit()

    adapter = FakeAdapter()
    points = [
        make_chunk_payload(chunk_id=f"chk_{i}", kb_id=kb_id, source_document_id=f"doc-{i}")
        for i in range(7)
    ]
    adapter.collections[coll] = {"points": points}

    out_dir = tmp_path / "export_out"
    with session_local() as session:
        manifest = export_kb(
            kb_id=kb_id,
            out_dir=out_dir,
            session=session,
            adapter=adapter,
        )

    assert manifest.chunk_count == 7

    chunk_files = sorted((out_dir / "chunks").glob("*.jsonl"))
    assert len(chunk_files) >= 1
    total = 0
    seen_chunk_ids = set()
    for cf in chunk_files:
        for line in cf.read_text().splitlines():
            if line.strip():
                rec = json.loads(line)
                seen_chunk_ids.add(rec["chunk_id"])
                total += 1
    assert total == 7
    assert seen_chunk_ids == {f"chk_{i}" for i in range(7)}
