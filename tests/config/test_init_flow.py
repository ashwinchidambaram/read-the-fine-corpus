"""Tests for finecorpus.config.init_flow — corpus init command (M-103, spec §4.6).

Test coverage
-------------
1. Scripted flow — openai provider: config written with correct defaults.
2. Scripted flow — ollama provider: config written with given base_url and model.
3. Scripted flow — both providers: config written; default is openai.
4. Config file correctness: OpenAI model written correctly.
5. Secret never written: OPENAI_API_KEY must NOT appear in corpus.yaml.
6. Preflight invoked (monkeypatched): run_preflight is called after write.
7. Non-interactive missing-flag error: InitError raised when --provider absent.
8. Non-interactive invalid provider: InitError raised for unknown provider.
9. first_run_complete is true after successful preflight.
10. first_run_complete is NOT stamped after failed preflight.
11. Interactive flow via stdin (basic smoke).
12. run_preflight=False skips preflight (config-only path).
"""

from __future__ import annotations

import io
import shutil
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import yaml

from finecorpus.config.init_flow import InitError, run_init_flow

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def tmp_dir(tmp_path: Path) -> Path:
    """A temp directory with corpus.example.yaml copied in."""
    repo_root = Path(__file__).resolve().parent.parent.parent
    example_src = repo_root / "corpus.example.yaml"
    if not example_src.exists():
        pytest.skip("corpus.example.yaml not found at repo root")
    shutil.copy(example_src, tmp_path / "corpus.example.yaml")
    return tmp_path


def _make_fake_report(passed: bool = True) -> Any:
    """Return a mock PreflightReport."""
    report = MagicMock()
    report.passed = passed
    report.has_failures = not passed
    report.__str__ = MagicMock(return_value="OK      config_parse         parsed ok\n\nPASS")
    return report


def _load_generated_yaml(path: Path) -> dict[str, Any]:
    """Read and parse the generated yaml file."""
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _run_scripted(
    tmp_dir: Path,
    *,
    provider: str,
    **kwargs: Any,
) -> tuple[Any, str]:
    """Run init_flow in non-interactive mode with preflight mocked out."""
    stdout = io.StringIO()
    stdin = io.StringIO()
    # Patch the imported names at their definition site (inside finecorpus.config.loader
    # and finecorpus.config.preflight / finecorpus.embedding.preflight_check),
    # since init_flow does local imports inside run_init_flow().
    with (
        patch("finecorpus.config.loader.load_config"),
        patch("finecorpus.config.preflight.run_preflight") as mock_pf,
        patch("finecorpus.embedding.preflight_check.make_embedding_check") as mock_emb,
    ):
        mock_pf.return_value = _make_fake_report(True)
        mock_emb.return_value = MagicMock()
        result = run_init_flow(
            config_path=tmp_dir / "corpus.yaml",
            example_path=tmp_dir / "corpus.example.yaml",
            provider=provider,
            non_interactive=True,
            stdout=stdout,
            stdin=stdin,
            **kwargs,
        )
    return result, stdout.getvalue()


# ---------------------------------------------------------------------------
# Test 1: openai scripted flow — config written with correct defaults
# ---------------------------------------------------------------------------


def test_openai_scripted_config_written(tmp_dir: Path) -> None:
    """OpenAI scripted flow writes corpus.yaml with correct provider settings."""
    result, _ = _run_scripted(tmp_dir, provider="openai")

    assert result.config_path == (tmp_dir / "corpus.yaml").resolve()
    assert result.provider_choice == "openai"
    cfg = _load_generated_yaml(tmp_dir / "corpus.yaml")
    assert cfg["providers"]["embedding"]["default"] == "openai"


# ---------------------------------------------------------------------------
# Test 2: ollama scripted flow — config written with provided url and model
# ---------------------------------------------------------------------------


def test_ollama_scripted_config_written(tmp_dir: Path) -> None:
    """Ollama scripted flow writes corpus.yaml with the given base_url and model."""
    custom_url = "http://192.168.1.5:11434"
    custom_model = "bge-m3"

    result, _ = _run_scripted(
        tmp_dir,
        provider="ollama",
        ollama_base_url=custom_url,
        ollama_model=custom_model,
    )

    assert result.provider_choice == "ollama"
    cfg = _load_generated_yaml(tmp_dir / "corpus.yaml")
    assert cfg["providers"]["embedding"]["default"] == "ollama"
    assert cfg["providers"]["embedding"]["local"]["endpoint"] == custom_url
    assert cfg["providers"]["embedding"]["local"]["model"] == custom_model


# ---------------------------------------------------------------------------
# Test 3: both providers — default is openai
# ---------------------------------------------------------------------------


def test_both_providers_default_is_openai(tmp_dir: Path) -> None:
    """'both' provider choice sets default to openai and writes local section."""
    result, _ = _run_scripted(tmp_dir, provider="both")

    assert result.provider_choice == "both"
    cfg = _load_generated_yaml(tmp_dir / "corpus.yaml")
    assert cfg["providers"]["embedding"]["default"] == "openai"


# ---------------------------------------------------------------------------
# Test 4: config correctness — cloud model written correctly
# ---------------------------------------------------------------------------


def test_openai_model_written_to_yaml(tmp_dir: Path) -> None:
    """The selected OpenAI model is correctly reflected in the written config."""
    _run_scripted(tmp_dir, provider="openai", openai_model="text-embedding-3-large")

    cfg = _load_generated_yaml(tmp_dir / "corpus.yaml")
    assert cfg["providers"]["embedding"]["cloud"]["model"] == "text-embedding-3-large"


# ---------------------------------------------------------------------------
# Test 5: secret never written — OPENAI_API_KEY must NOT appear in yaml
# ---------------------------------------------------------------------------


def test_api_key_never_written_to_yaml(tmp_dir: Path) -> None:
    """The OPENAI_API_KEY must not appear in the written corpus.yaml."""
    _, out = _run_scripted(tmp_dir, provider="openai")

    yaml_text = (tmp_dir / "corpus.yaml").read_text(encoding="utf-8")
    # No API key patterns should appear in the file
    assert "sk-" not in yaml_text, "OpenAI sk- key prefix found in corpus.yaml"
    assert "OPENAI_API_KEY" not in yaml_text, "OPENAI_API_KEY literal found in corpus.yaml"
    # The export instruction must appear in stdout output, not in the file
    assert "OPENAI_API_KEY" in out, "Expected export instruction in stdout"


# ---------------------------------------------------------------------------
# Test 6: preflight is invoked (monkeypatched)
# ---------------------------------------------------------------------------


def test_preflight_is_invoked(tmp_dir: Path) -> None:
    """run_preflight must be called once after corpus.yaml is written."""
    stdout = io.StringIO()
    stdin = io.StringIO()
    with (
        patch("finecorpus.config.loader.load_config"),
        patch("finecorpus.config.preflight.run_preflight") as mock_pf,
        patch("finecorpus.embedding.preflight_check.make_embedding_check") as mock_emb,
    ):
        mock_pf.return_value = _make_fake_report(True)
        mock_emb.return_value = MagicMock()
        run_init_flow(
            config_path=tmp_dir / "corpus.yaml",
            example_path=tmp_dir / "corpus.example.yaml",
            provider="ollama",
            non_interactive=True,
            stdout=stdout,
            stdin=stdin,
        )

    mock_pf.assert_called_once()


# ---------------------------------------------------------------------------
# Test 7: non-interactive missing flag → InitError
# ---------------------------------------------------------------------------


def test_non_interactive_missing_provider_raises_init_error(tmp_dir: Path) -> None:
    """--non-interactive without --provider must raise InitError."""
    with pytest.raises(InitError, match="--provider is required"):
        run_init_flow(
            config_path=tmp_dir / "corpus.yaml",
            example_path=tmp_dir / "corpus.example.yaml",
            non_interactive=True,
            # provider not set
        )


# ---------------------------------------------------------------------------
# Test 8: non-interactive invalid provider → InitError
# ---------------------------------------------------------------------------


def test_non_interactive_invalid_provider_raises_init_error(tmp_dir: Path) -> None:
    """--non-interactive with an unknown provider value must raise InitError."""
    with pytest.raises(InitError, match="Invalid --provider"):
        run_init_flow(
            config_path=tmp_dir / "corpus.yaml",
            example_path=tmp_dir / "corpus.example.yaml",
            provider="vertex_ai",  # not a valid choice
            non_interactive=True,
        )


# ---------------------------------------------------------------------------
# Test 9: first_run_complete stamped on successful preflight
# ---------------------------------------------------------------------------


def test_first_run_stamped_after_successful_preflight(tmp_dir: Path) -> None:
    """first_run_complete: true must be written when preflight passes."""
    stdout = io.StringIO()
    stdin = io.StringIO()
    with (
        patch("finecorpus.config.loader.load_config"),
        patch("finecorpus.config.preflight.run_preflight") as mock_pf,
        patch("finecorpus.embedding.preflight_check.make_embedding_check") as mock_emb,
    ):
        mock_pf.return_value = _make_fake_report(passed=True)
        mock_emb.return_value = MagicMock()
        result = run_init_flow(
            config_path=tmp_dir / "corpus.yaml",
            example_path=tmp_dir / "corpus.example.yaml",
            provider="ollama",
            non_interactive=True,
            stdout=stdout,
            stdin=stdin,
        )

    assert result.first_run_stamped is True
    cfg = _load_generated_yaml(tmp_dir / "corpus.yaml")
    assert cfg["platform"]["first_run_complete"] is True


# ---------------------------------------------------------------------------
# Test 10: first_run_complete NOT stamped when preflight fails
# ---------------------------------------------------------------------------


def test_first_run_not_stamped_after_failed_preflight(tmp_dir: Path) -> None:
    """first_run_complete must NOT be set when preflight fails."""
    stdout = io.StringIO()
    stdin = io.StringIO()
    with (
        patch("finecorpus.config.loader.load_config"),
        patch("finecorpus.config.preflight.run_preflight") as mock_pf,
        patch("finecorpus.embedding.preflight_check.make_embedding_check") as mock_emb,
    ):
        mock_pf.return_value = _make_fake_report(passed=False)
        mock_emb.return_value = MagicMock()
        result = run_init_flow(
            config_path=tmp_dir / "corpus.yaml",
            example_path=tmp_dir / "corpus.example.yaml",
            provider="ollama",
            non_interactive=True,
            stdout=stdout,
            stdin=stdin,
        )

    assert result.first_run_stamped is False
    cfg = _load_generated_yaml(tmp_dir / "corpus.yaml")
    # first_run_complete must remain false in the file
    assert cfg["platform"]["first_run_complete"] is False


# ---------------------------------------------------------------------------
# Test 11: interactive flow via stdin (basic smoke)
# ---------------------------------------------------------------------------


def test_interactive_flow_via_stdin(tmp_dir: Path) -> None:
    """Interactive mode reads provider from stdin and writes config."""
    # Simulate user typing: "ollama\n" for provider, then Enter for defaults
    stdin = io.StringIO("ollama\n\n\n")
    stdout = io.StringIO()
    with (
        patch("finecorpus.config.loader.load_config"),
        patch("finecorpus.config.preflight.run_preflight") as mock_pf,
        patch("finecorpus.embedding.preflight_check.make_embedding_check") as mock_emb,
    ):
        mock_pf.return_value = _make_fake_report(True)
        mock_emb.return_value = MagicMock()
        result = run_init_flow(
            config_path=tmp_dir / "corpus.yaml",
            example_path=tmp_dir / "corpus.example.yaml",
            non_interactive=False,
            stdout=stdout,
            stdin=stdin,
        )

    assert result.provider_choice == "ollama"
    cfg = _load_generated_yaml(tmp_dir / "corpus.yaml")
    assert cfg["providers"]["embedding"]["default"] == "ollama"


# ---------------------------------------------------------------------------
# Test 12: run_preflight=False skips preflight (config-only path)
# ---------------------------------------------------------------------------


def test_run_preflight_false_skips_preflight(tmp_dir: Path) -> None:
    """Setting run_preflight=False must skip calling run_preflight."""
    stdout = io.StringIO()
    stdin = io.StringIO()
    with patch("finecorpus.config.preflight.run_preflight") as mock_pf:
        result = run_init_flow(
            config_path=tmp_dir / "corpus.yaml",
            example_path=tmp_dir / "corpus.example.yaml",
            provider="ollama",
            non_interactive=True,
            run_preflight=False,
            stdout=stdout,
            stdin=stdin,
        )

    mock_pf.assert_not_called()
    assert result.first_run_stamped is False
    assert result.preflight_passed is False  # not run, so not passed
