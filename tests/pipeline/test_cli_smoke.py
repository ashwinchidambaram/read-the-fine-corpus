"""CLI smoke test: corpus pipeline run via subprocess over 3 fixture files.

Uses a temporary copy of 3 fixture files to keep the test fast.
"""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import sys

FIXTURE_CORPUS = pathlib.Path(__file__).parent.parent / "fixtures" / "golden" / "corpus"


class TestCliPipelineRun:
    """corpus pipeline run exits 0 and produces artifacts."""

    def test_pipeline_run_exits_zero(self, tmp_path):
        """corpus pipeline run produces artifacts and exits 0."""
        source = tmp_path / "source"
        source.mkdir()

        # Copy 3 fixture files
        for f in sorted(FIXTURE_CORPUS.glob("*.pdf"))[:3]:
            shutil.copy2(f, source / f.name)

        artifacts = tmp_path / "artifacts"
        artifacts.mkdir()

        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "finecorpus.cli.main",
                "pipeline",
                "run",
                "--source",
                str(source),
                "--artifacts",
                str(artifacts),
                "--run-id",
                "cli-smoke-test",
                "--workspace",
                "ws-smoke",
                "--kb",
                "kb-smoke",
            ],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, (
            f"CLI exited with {result.returncode}.\n"
            f"stdout: {result.stdout}\n"
            f"stderr: {result.stderr}"
        )

    def test_pipeline_run_produces_all_artifacts(self, tmp_path):
        """corpus pipeline run creates all 5 artifact files."""
        source = tmp_path / "source"
        source.mkdir()

        for f in sorted(FIXTURE_CORPUS.glob("*.pdf"))[:3]:
            shutil.copy2(f, source / f.name)

        artifacts = tmp_path / "artifacts"
        artifacts.mkdir()

        subprocess.run(
            [
                sys.executable,
                "-m",
                "finecorpus.cli.main",
                "pipeline",
                "run",
                "--source",
                str(source),
                "--artifacts",
                str(artifacts),
                "--run-id",
                "cli-smoke-artifacts",
                "--workspace",
                "ws-smoke",
                "--kb",
                "kb-smoke",
            ],
            capture_output=True,
            text=True,
            check=True,
        )

        run_dir = artifacts / "cli-smoke-artifacts"
        assert run_dir.exists(), f"Run directory not created: {run_dir}"

        for stage in ["collect", "assess", "decompose", "plan", "build"]:
            artifact = run_dir / f"{stage}.json"
            assert artifact.exists(), f"Artifact not created: {artifact}"

            # Each artifact is valid JSON with a schema_version
            data = json.loads(artifact.read_text())
            assert "schema_version" in data, f"Artifact {artifact} missing schema_version"

    def test_pipeline_run_output_mentions_stages(self, tmp_path):
        """corpus pipeline run stdout mentions all 5 stage names."""
        source = tmp_path / "source"
        source.mkdir()

        for f in sorted(FIXTURE_CORPUS.glob("*.pdf"))[:3]:
            shutil.copy2(f, source / f.name)

        artifacts = tmp_path / "artifacts"
        artifacts.mkdir()

        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "finecorpus.cli.main",
                "pipeline",
                "run",
                "--source",
                str(source),
                "--artifacts",
                str(artifacts),
                "--run-id",
                "cli-smoke-output",
                "--workspace",
                "ws-smoke",
                "--kb",
                "kb-smoke",
            ],
            capture_output=True,
            text=True,
            check=True,
        )

        for stage in ["collect", "assess", "decompose", "plan", "build"]:
            assert stage in result.stdout, (
                f"Stage '{stage}' not mentioned in CLI output:\n{result.stdout}"
            )
