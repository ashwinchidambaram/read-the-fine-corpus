"""Phase 5 config-defaults tests.

Covers:
- sweep_min_corpus_docs default == 50 and ge=1 validator
- eval_injection_suspicion_threshold default == 0.5 and [0.0, 1.0] validator
- sweep_candidate_budget default == 40 and ge=1 validator
- sweep_sample_factor default == 2 and ge=1 validator
- Config.export() remains secret-free (no regression)
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from pydantic import ValidationError

from finecorpus.config import load_config
from finecorpus.config.models import AssessmentConfig

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def write_yaml(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "corpus.yaml"
    p.write_text(textwrap.dedent(content), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# AssessmentConfig direct model tests (fast, no file I/O)
# ---------------------------------------------------------------------------


class TestAssessmentDefaults:
    def test_sweep_min_corpus_docs_default(self) -> None:
        cfg = AssessmentConfig()
        assert cfg.sweep_min_corpus_docs == 50

    def test_sweep_min_corpus_docs_type_is_int(self) -> None:
        cfg = AssessmentConfig()
        assert isinstance(cfg.sweep_min_corpus_docs, int)

    def test_sweep_min_corpus_docs_custom_value(self) -> None:
        cfg = AssessmentConfig(sweep_min_corpus_docs=100)
        assert cfg.sweep_min_corpus_docs == 100

    def test_sweep_min_corpus_docs_rejects_zero(self) -> None:
        with pytest.raises(ValidationError):
            AssessmentConfig(sweep_min_corpus_docs=0)

    def test_sweep_min_corpus_docs_rejects_negative(self) -> None:
        with pytest.raises(ValidationError):
            AssessmentConfig(sweep_min_corpus_docs=-1)

    def test_sweep_min_corpus_docs_accepts_one(self) -> None:
        cfg = AssessmentConfig(sweep_min_corpus_docs=1)
        assert cfg.sweep_min_corpus_docs == 1

    def test_eval_injection_suspicion_threshold_default(self) -> None:
        cfg = AssessmentConfig()
        assert cfg.eval_injection_suspicion_threshold == pytest.approx(0.5)

    def test_eval_injection_suspicion_threshold_boundary_zero(self) -> None:
        cfg = AssessmentConfig(eval_injection_suspicion_threshold=0.0)
        assert cfg.eval_injection_suspicion_threshold == pytest.approx(0.0)

    def test_eval_injection_suspicion_threshold_boundary_one(self) -> None:
        cfg = AssessmentConfig(eval_injection_suspicion_threshold=1.0)
        assert cfg.eval_injection_suspicion_threshold == pytest.approx(1.0)

    def test_eval_injection_suspicion_threshold_rejects_above_one(self) -> None:
        with pytest.raises(ValidationError):
            AssessmentConfig(eval_injection_suspicion_threshold=1.1)

    def test_eval_injection_suspicion_threshold_rejects_below_zero(self) -> None:
        with pytest.raises(ValidationError):
            AssessmentConfig(eval_injection_suspicion_threshold=-0.1)

    def test_sweep_candidate_budget_default(self) -> None:
        cfg = AssessmentConfig()
        assert cfg.sweep_candidate_budget == 40

    def test_sweep_candidate_budget_custom(self) -> None:
        cfg = AssessmentConfig(sweep_candidate_budget=20)
        assert cfg.sweep_candidate_budget == 20

    def test_sweep_candidate_budget_rejects_zero(self) -> None:
        with pytest.raises(ValidationError):
            AssessmentConfig(sweep_candidate_budget=0)

    def test_sweep_candidate_budget_accepts_one(self) -> None:
        cfg = AssessmentConfig(sweep_candidate_budget=1)
        assert cfg.sweep_candidate_budget == 1

    def test_sweep_sample_factor_default(self) -> None:
        cfg = AssessmentConfig()
        assert cfg.sweep_sample_factor == 2

    def test_sweep_sample_factor_custom(self) -> None:
        cfg = AssessmentConfig(sweep_sample_factor=5)
        assert cfg.sweep_sample_factor == 5

    def test_sweep_sample_factor_rejects_zero(self) -> None:
        with pytest.raises(ValidationError):
            AssessmentConfig(sweep_sample_factor=0)

    def test_sweep_sample_factor_accepts_one(self) -> None:
        cfg = AssessmentConfig(sweep_sample_factor=1)
        assert cfg.sweep_sample_factor == 1


# ---------------------------------------------------------------------------
# Full Config load tests
# ---------------------------------------------------------------------------


class TestFullConfigDefaults:
    def test_sweep_min_corpus_docs_in_loaded_config(self, tmp_path: Path) -> None:
        cfg_file = write_yaml(tmp_path, "{}\n")
        cfg = load_config(cfg_file)
        assert cfg.assessment.sweep_min_corpus_docs == 50

    def test_eval_injection_suspicion_threshold_in_loaded_config(self, tmp_path: Path) -> None:
        cfg_file = write_yaml(tmp_path, "{}\n")
        cfg = load_config(cfg_file)
        assert cfg.assessment.eval_injection_suspicion_threshold == pytest.approx(0.5)

    def test_sweep_candidate_budget_in_loaded_config(self, tmp_path: Path) -> None:
        cfg_file = write_yaml(tmp_path, "{}\n")
        cfg = load_config(cfg_file)
        assert cfg.assessment.sweep_candidate_budget == 40

    def test_sweep_sample_factor_in_loaded_config(self, tmp_path: Path) -> None:
        cfg_file = write_yaml(tmp_path, "{}\n")
        cfg = load_config(cfg_file)
        assert cfg.assessment.sweep_sample_factor == 2

    def test_config_export_is_secret_free(self, tmp_path: Path) -> None:
        """Config.export() must not contain plaintext credential values."""
        cfg_file = write_yaml(tmp_path, "{}\n")
        cfg = load_config(cfg_file)
        exported = cfg.export()
        # Secret-bearing fields should not appear as values in export
        # (they are None by default; if somehow set they would be redacted)
        assert "sk-" not in str(exported)

    def test_sweep_min_corpus_docs_yaml_override(self, tmp_path: Path) -> None:
        cfg_file = write_yaml(
            tmp_path,
            """
            assessment:
              sweep_min_corpus_docs: 200
            """,
        )
        cfg = load_config(cfg_file)
        assert cfg.assessment.sweep_min_corpus_docs == 200
