"""Phase 5 tests for M-090 eval-set export.

Test inventory:
  test_eval_set_export_serialized — M-090: export includes
    eval_sets/<id>.json with accurate counts + manifest hash.
  test_eval_set_export_no_eval_sets — export with no eval sets writes
    README.txt and manifest reflects 0 sets.
  test_eval_set_export_multiple_sets — multiple eval sets each get their
    own JSON file in eval_sets/.
  test_eval_set_manifest_hashes_present — file_hashes in manifest includes
    eval_sets/<id>.json entries.
  test_eval_set_export_question_count — manifest eval_question_count is
    the sum of all questions across all eval sets.
"""

from __future__ import annotations

import json
import pathlib
import tempfile
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from finecorpus.control.eval_store import EvalSetRepository
from finecorpus.control.metadata import create_tables
from finecorpus.pipeline.export import ExportManifest, export_kb
from tests.retrieval.helpers import FakeAdapter

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

KB_ID = "kb-export-test"
WS_ID = "ws-export-test"
EVAL_SET_ID_1 = "es-export-001"
EVAL_SET_ID_2 = "es-export-002"
_NOW = datetime(2026, 9, 6, 12, 0, 0, tzinfo=UTC)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def engine():
    eng = create_engine("sqlite://")
    create_tables(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def session(engine):
    with Session(engine) as s:
        yield s


@pytest.fixture
def tmp_out():
    with tempfile.TemporaryDirectory() as d:
        yield pathlib.Path(d)


def _seed_eval_set(
    session: Session,
    *,
    eval_set_id: str = EVAL_SET_ID_1,
    kb_id: str = KB_ID,
    question_count: int = 2,
) -> None:
    """Seed an eval set with `question_count` questions."""
    repo = EvalSetRepository(session)
    repo.create(
        eval_set_id=eval_set_id,
        kb_id=kb_id,
        workspace_id=WS_ID,
        schema_version="1.0.0",
        origin="generated",
        confidence_level="reviewed",
        baseline_ref=None,
        created_at=_NOW,
    )
    for i in range(question_count):
        repo.add_question(
            question_id=f"q-export-{eval_set_id}-{i:03d}",
            eval_set_id=eval_set_id,
            kb_id=kb_id,
            text=f"Export test question {i}",
            question_type="factual_lookup",
            generation_method="generated_factual",
            review_status="reviewed_kept",
            source_segment_ids=[f"seg-{i:03d}"],
            source_unknown=False,
            expected_segment_ids=[f"seg-{i:03d}"],
        )
    session.commit()


def _make_adapter_with_alias(kb_id: str = KB_ID) -> FakeAdapter:
    """FakeAdapter with a registered alias for kb_id (no chunk data needed)."""
    from finecorpus.index.adapter import alias_name  # noqa: PLC0415

    adapter = FakeAdapter()
    alias = alias_name(kb_id)
    coll = f"rtfc_{kb_id.replace('-', '').lower()}_00000001"
    adapter.collections[coll] = {"points": []}
    adapter.aliases[alias] = coll
    return adapter


class _FakeAliasRepoForExport:
    """Minimal AliasRepository simulacrum for export tests."""

    def __init__(self, kb_id: str) -> None:
        from unittest.mock import MagicMock  # noqa: PLC0415

        mock_record = MagicMock()
        mock_record.collection_name = f"rtfc_{kb_id.replace('-', '').lower()}_00000001"
        self._record = mock_record

    def get(self, alias: str) -> object:
        return self._record


def _patch_alias_repo_for_export(kb_id: str):
    """Patch AliasRepository (imported inside export_kb via local import) to return a fake."""
    repo = _FakeAliasRepoForExport(kb_id)
    # The export_kb function does: from finecorpus.control.metadata import AliasRepository
    # Patch the class in the source module so the local import picks it up.
    return patch("finecorpus.control.metadata.AliasRepository", return_value=repo)


# ---------------------------------------------------------------------------
# test_eval_set_export_serialized (M-090)
# ---------------------------------------------------------------------------


class TestEvalSetExportSerialized:
    """M-090: export includes eval_sets/<id>.json with accurate counts and manifest hash."""

    def test_eval_set_export_serialized(self, engine, session, tmp_out):
        """Single eval set → eval_sets/<eval_set_id>.json exists with correct question count."""
        _seed_eval_set(session, eval_set_id=EVAL_SET_ID_1, question_count=3)

        adapter = _make_adapter_with_alias()

        with _patch_alias_repo_for_export(KB_ID):
            manifest = export_kb(
                KB_ID,
                out_dir=tmp_out,
                session=session,
                adapter=adapter,
            )

        # Check manifest type
        assert isinstance(manifest, ExportManifest)
        assert manifest.eval_set_count == 1
        assert manifest.eval_question_count == 3

        # Check file exists
        eval_file = tmp_out / "eval_sets" / f"{EVAL_SET_ID_1}.json"
        assert eval_file.exists(), f"Expected {eval_file} to exist"

        # Check content
        data = json.loads(eval_file.read_text())
        assert data["eval_set_id"] == EVAL_SET_ID_1
        assert data["kb_id"] == KB_ID
        assert data["question_count"] == 3
        assert len(data["questions"]) == 3

    def test_eval_set_manifest_hashes_present(self, engine, session, tmp_out):
        """file_hashes in manifest includes eval_sets/<id>.json entries."""
        _seed_eval_set(session, eval_set_id=EVAL_SET_ID_1, question_count=2)

        adapter = _make_adapter_with_alias()

        with _patch_alias_repo_for_export(KB_ID):
            manifest = export_kb(
                KB_ID,
                out_dir=tmp_out,
                session=session,
                adapter=adapter,
            )

        hash_key = f"eval_sets/{EVAL_SET_ID_1}.json"
        assert hash_key in manifest.file_hashes
        # Hash should be a non-empty hex string
        assert len(manifest.file_hashes[hash_key]) == 64  # SHA-256 hex = 64 chars
        assert all(c in "0123456789abcdef" for c in manifest.file_hashes[hash_key])

    def test_manifest_json_written_with_eval_counts(self, engine, session, tmp_out):
        """manifest.json on disk includes eval_set_count and eval_question_count."""
        _seed_eval_set(session, eval_set_id=EVAL_SET_ID_1, question_count=2)

        adapter = _make_adapter_with_alias()

        with _patch_alias_repo_for_export(KB_ID):
            export_kb(
                KB_ID,
                out_dir=tmp_out,
                session=session,
                adapter=adapter,
            )

        manifest_path = tmp_out / "manifest.json"
        assert manifest_path.exists()
        manifest_data = json.loads(manifest_path.read_text())
        assert manifest_data["eval_set_count"] == 1
        assert manifest_data["eval_question_count"] == 2


# ---------------------------------------------------------------------------
# test_eval_set_export_no_eval_sets
# ---------------------------------------------------------------------------


class TestEvalSetExportNoEvalSets:
    """Export with no eval sets configured — README + manifest with zeros."""

    def test_eval_set_export_no_eval_sets(self, engine, session, tmp_out):
        """No eval sets → eval_sets/ contains README.txt, manifest shows 0."""
        # Do NOT seed any eval sets
        adapter = _make_adapter_with_alias()

        with _patch_alias_repo_for_export(KB_ID):
            manifest = export_kb(
                KB_ID,
                out_dir=tmp_out,
                session=session,
                adapter=adapter,
            )

        assert manifest.eval_set_count == 0
        assert manifest.eval_question_count == 0

        # README should exist
        readme = tmp_out / "eval_sets" / "README.txt"
        assert readme.exists()


# ---------------------------------------------------------------------------
# test_eval_set_export_multiple_sets
# ---------------------------------------------------------------------------


class TestEvalSetExportMultipleSets:
    """Multiple eval sets → each gets its own JSON file."""

    def test_eval_set_export_multiple_sets(self, engine, session, tmp_out):
        _seed_eval_set(session, eval_set_id=EVAL_SET_ID_1, question_count=2)
        _seed_eval_set(session, eval_set_id=EVAL_SET_ID_2, question_count=5)

        adapter = _make_adapter_with_alias()

        with _patch_alias_repo_for_export(KB_ID):
            manifest = export_kb(
                KB_ID,
                out_dir=tmp_out,
                session=session,
                adapter=adapter,
            )

        assert manifest.eval_set_count == 2
        assert manifest.eval_question_count == 7

        file1 = tmp_out / "eval_sets" / f"{EVAL_SET_ID_1}.json"
        file2 = tmp_out / "eval_sets" / f"{EVAL_SET_ID_2}.json"
        assert file1.exists()
        assert file2.exists()

        data1 = json.loads(file1.read_text())
        data2 = json.loads(file2.read_text())
        assert data1["question_count"] == 2
        assert data2["question_count"] == 5

        # Both hashes present in manifest
        assert f"eval_sets/{EVAL_SET_ID_1}.json" in manifest.file_hashes
        assert f"eval_sets/{EVAL_SET_ID_2}.json" in manifest.file_hashes

    def test_eval_set_export_question_count_is_sum(self, engine, session, tmp_out):
        """eval_question_count is the sum across all eval sets."""
        _seed_eval_set(session, eval_set_id=EVAL_SET_ID_1, question_count=3)
        _seed_eval_set(session, eval_set_id=EVAL_SET_ID_2, question_count=4)

        adapter = _make_adapter_with_alias()

        with _patch_alias_repo_for_export(KB_ID):
            manifest = export_kb(
                KB_ID,
                out_dir=tmp_out,
                session=session,
                adapter=adapter,
            )

        assert manifest.eval_question_count == 7
