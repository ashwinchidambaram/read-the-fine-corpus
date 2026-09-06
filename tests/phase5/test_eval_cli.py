"""Phase 5 tests for the `corpus eval` CLI command group.

These tests invoke the real ``_cmd_eval_*`` handlers against an on-disk SQLite
control plane (a temp-file DSN so the handler's engine and the test's
verification engine share the same database).  The LLM provider is the real
FakeLLMProvider, wired through the real ``build_llm_provider_from_config`` path
by setting ``internal_llm.default.provider="fake"`` in the Config.

Proven behaviours:
  - generate: persists a provisional eval set + questions (all unreviewed).
  - import: round-trips an eval-set JSON file; a question with no review_status
    is persisted as unreviewed (provisional — no default).
  - review: flips a question's review_status; invalid status is rejected.
  - status: prints the PROVISIONAL banner for a provisional set.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

import finecorpus.cli.main as cli_main
from finecorpus.config.models import Config
from finecorpus.control.eval_store import EvalSetRepository
from finecorpus.control.metadata import create_engine as fc_create_engine
from finecorpus.control.metadata import create_tables

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

KB_ID = "kb-eval-cli"
WS_ID = "ws-eval-cli"


@pytest.fixture()
def dsn(tmp_path: Path) -> str:
    """A file-backed SQLite DSN shared by handler and verification engines."""
    db_path = tmp_path / "control.db"
    return f"sqlite:///{db_path}"


@pytest.fixture()
def fake_config(dsn: str) -> Config:
    """Config with a fake internal LLM provider and file-backed control DSN."""
    cfg = Config()
    new_default = cfg.internal_llm.default.model_copy(
        update={"provider": "fake", "model": "fake-llm-v1"}
    )
    new_internal = cfg.internal_llm.model_copy(update={"default": new_default})
    new_pg = cfg.storage.postgres.model_copy(update={"url": dsn})
    new_storage = cfg.storage.model_copy(update={"postgres": new_pg})
    return cfg.model_copy(update={"internal_llm": new_internal, "storage": new_storage})


@pytest.fixture()
def patch_load_config(monkeypatch: pytest.MonkeyPatch, fake_config: Config):
    """Patch the loader so all handlers receive fake_config."""

    def _fake_load_config(path=None):  # noqa: ANN001, ANN202
        return fake_config

    monkeypatch.setattr("finecorpus.config.loader.load_config", _fake_load_config)
    return fake_config


@pytest.fixture()
def verify_session(dsn: str):
    """A session on the SAME control DB the handlers wrote to."""
    engine = fc_create_engine(dsn)
    create_tables(engine)
    with Session(engine) as s:
        yield s
    engine.dispose()


def _write_decompose_artifact(artifacts_root: Path, run_id: str) -> None:
    """Write a minimal two-document decompose artifact with segments."""
    run_dir = artifacts_root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    decompose = {
        "schema_version": "1.0.0",
        "segment_sets": [
            {
                "document_id": "doc-A",
                "segments": [
                    {"segment_id": "seg-a-1", "text": "Economics content about markets."},
                    {"segment_id": "seg-a-2", "text": "More economics content here."},
                ],
            },
            {
                "document_id": "doc-B",
                "segments": [
                    {"segment_id": "seg-b-1", "text": "Politics content about elections."},
                    {"segment_id": "seg-b-2", "text": "More politics content here."},
                ],
            },
        ],
    }
    (run_dir / "decompose.json").write_text(json.dumps(decompose), encoding="utf-8")


# ---------------------------------------------------------------------------
# generate
# ---------------------------------------------------------------------------


class TestEvalGenerate:
    def test_generate_persists_provisional_questions(
        self,
        patch_load_config: Config,
        tmp_path: Path,
        verify_session: Session,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        artifacts_root = tmp_path / "artifacts"
        run_id = "run-gen-001"
        _write_decompose_artifact(artifacts_root, run_id)

        args = argparse.Namespace(
            kb_id=KB_ID,
            artifacts=str(artifacts_root),
            run_id=run_id,
            count_per_type=1,
            config=None,
        )
        rc = cli_main._cmd_eval_generate(args)
        assert rc == 0

        # Persisted: one eval set, all questions unreviewed + provisional set.
        repo = EvalSetRepository(verify_session)
        sets = repo.list_for_kb(KB_ID)
        assert len(sets) == 1
        assert sets[0].confidence_level == "provisional"

        questions = repo.get_questions(sets[0].eval_set_id)
        assert len(questions) >= 1
        assert all(q.review_status == "unreviewed" for q in questions)

        out = capsys.readouterr().out
        assert "PROVISIONAL" in out

    def test_generate_fails_closed_without_llm_provider(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        dsn: str,
    ) -> None:
        """No configured LLM provider → non-zero exit, no fabrication."""
        cfg = Config()  # internal_llm.default.provider is None
        new_pg = cfg.storage.postgres.model_copy(update={"url": dsn})
        new_storage = cfg.storage.model_copy(update={"postgres": new_pg})
        cfg = cfg.model_copy(update={"storage": new_storage})
        monkeypatch.setattr("finecorpus.config.loader.load_config", lambda path=None: cfg)

        artifacts_root = tmp_path / "artifacts"
        run_id = "run-gen-noprov"
        _write_decompose_artifact(artifacts_root, run_id)

        args = argparse.Namespace(
            kb_id=KB_ID,
            artifacts=str(artifacts_root),
            run_id=run_id,
            count_per_type=1,
            config=None,
        )
        rc = cli_main._cmd_eval_generate(args)
        assert rc == 1


# ---------------------------------------------------------------------------
# import
# ---------------------------------------------------------------------------


class TestEvalImport:
    def test_import_round_trips(
        self,
        patch_load_config: Config,
        tmp_path: Path,
        verify_session: Session,
    ) -> None:
        eval_file = tmp_path / "eval_set.json"
        eval_file.write_text(
            json.dumps(
                {
                    "eval_set_id": "es-import-001",
                    "workspace_id": WS_ID,
                    "schema_version": "1.0.0",
                    "origin": "imported_eval_set",
                    "confidence_level": "reviewed",
                    "baseline_ref": None,
                    "questions": [
                        {
                            "question_id": "q-imp-1",
                            "text": "Imported reviewed question?",
                            "question_type": "factual_lookup",
                            "generation_method": "imported",
                            "review_status": "reviewed_kept",
                            "source_segment_ids": ["seg-x"],
                            "source_unknown": False,
                            "expected_segment_ids": ["seg-x"],
                        },
                        {
                            # No review_status → MUST persist as unreviewed.
                            "question_id": "q-imp-2",
                            "text": "Imported question with no status?",
                            "question_type": "interpretive",
                            "generation_method": "imported",
                            "source_segment_ids": [],
                            "source_unknown": True,
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )

        args = argparse.Namespace(kb_id=KB_ID, file=str(eval_file), config=None)
        rc = cli_main._cmd_eval_import(args)
        assert rc == 0

        repo = EvalSetRepository(verify_session)
        questions = {q.question_id: q for q in repo.get_questions("es-import-001")}
        assert len(questions) == 2
        # review_status retained from the file
        assert questions["q-imp-1"].review_status == "reviewed_kept"
        # absent review_status persisted as unreviewed (provisional; no default)
        assert questions["q-imp-2"].review_status == "unreviewed"


# ---------------------------------------------------------------------------
# review
# ---------------------------------------------------------------------------


def _seed_one_question(session: Session) -> str:
    """Seed a set with one unreviewed question. Returns question_id."""
    repo = EvalSetRepository(session)
    repo.create(
        eval_set_id="es-review-001",
        kb_id=KB_ID,
        workspace_id=WS_ID,
        schema_version="1.0.0",
        origin="generated",
        confidence_level="provisional",
        baseline_ref=None,
        created_at=datetime(2026, 9, 6, tzinfo=UTC),
    )
    repo.add_question(
        question_id="q-review-1",
        eval_set_id="es-review-001",
        kb_id=KB_ID,
        text="A question to review.",
        question_type="factual_lookup",
        generation_method="generated_factual",
        review_status="unreviewed",
        source_segment_ids=["seg-r"],
        source_unknown=False,
        expected_segment_ids=["seg-r"],
    )
    session.commit()
    return "q-review-1"


class TestEvalReview:
    def test_review_flips_status(
        self,
        patch_load_config: Config,
        verify_session: Session,
    ) -> None:
        qid = _seed_one_question(verify_session)

        args = argparse.Namespace(
            kb_id=KB_ID,
            question_id=qid,
            status="reviewed_kept",
            reviewer="alice",
            config=None,
        )
        rc = cli_main._cmd_eval_review(args)
        assert rc == 0

        verify_session.expire_all()
        repo = EvalSetRepository(verify_session)
        q = next(q for q in repo.get_questions("es-review-001") if q.question_id == qid)
        assert q.review_status == "reviewed_kept"
        assert q.reviewed_by == "alice"
        assert q.reviewed_at is not None

    def test_review_rejects_invalid_status(
        self,
        patch_load_config: Config,
        verify_session: Session,
    ) -> None:
        qid = _seed_one_question(verify_session)

        args = argparse.Namespace(
            kb_id=KB_ID,
            question_id=qid,
            status="totally_bogus_status",
            reviewer="alice",
            config=None,
        )
        rc = cli_main._cmd_eval_review(args)
        assert rc == 1

        # Status unchanged.
        verify_session.expire_all()
        repo = EvalSetRepository(verify_session)
        q = next(q for q in repo.get_questions("es-review-001") if q.question_id == qid)
        assert q.review_status == "unreviewed"


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


class TestEvalStatus:
    def test_status_prints_provisional_banner(
        self,
        patch_load_config: Config,
        verify_session: Session,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _seed_one_question(verify_session)

        args = argparse.Namespace(kb_id=KB_ID, config=None)
        rc = cli_main._cmd_eval_status(args)
        assert rc == 0

        out = capsys.readouterr().out
        assert "es-review-001" in out
        assert "PROVISIONAL" in out
        assert "eval sets: 1" in out
