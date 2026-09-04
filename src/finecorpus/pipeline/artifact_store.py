"""Artifact persistence for the Read The Fine Corpus pipeline.

Artifacts are JSON files stored at:
  <artifacts_root>/<run_id>/<stage_name>.json

Design decisions:
  - run_id is caller-supplied (no wall-clock/random defaults — determinism discipline).
  - Loading re-validates the contract version field's presence; full schema validation
    is done by the consuming stage's check_version call.
  - JSON is the canonical format: durable, inspectable, diff-friendly (§5).
  - artifacts_root is an explicit constructor argument; no implicit defaults.

See docs/architecture/overview.md §5 (durable, inspectable artifacts).
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

from pydantic import ValidationError


class ArtifactStoreError(Exception):
    """Raised when artifact I/O fails or a loaded artifact is structurally invalid."""


class ArtifactStore:
    """JSON-file artifact store for a single pipeline run.

    Args:
        artifacts_root: Root directory under which run directories are created.
        run_id: Caller-supplied run identity; no defaults generated here.
    """

    def __init__(self, artifacts_root: str | pathlib.Path, run_id: str) -> None:
        if not run_id:
            raise ArtifactStoreError("run_id must be a non-empty string (no defaults generated)")
        self._root = pathlib.Path(artifacts_root)
        self._run_id = run_id
        self._run_dir = self._root / run_id
        self._run_dir.mkdir(parents=True, exist_ok=True)

    @property
    def run_dir(self) -> pathlib.Path:
        """Directory where this run's artifacts live."""
        return self._run_dir

    @property
    def run_id(self) -> str:
        return self._run_id

    def artifact_path(self, stage_name: str) -> pathlib.Path:
        """Return the canonical path for a stage artifact."""
        return self._run_dir / f"{stage_name}.json"

    def save(self, stage_name: str, data: dict[str, Any]) -> pathlib.Path:
        """Persist data as a JSON artifact.

        Args:
            stage_name: Used as the filename stem.
            data: Must be JSON-serializable (pydantic model_dump output).

        Returns:
            Path of the written file.

        Raises:
            ArtifactStoreError: On serialization or I/O failure.
        """
        path = self.artifact_path(stage_name)
        try:
            payload = json.dumps(data, indent=2, default=str, ensure_ascii=False)
            path.write_text(payload, encoding="utf-8")
        except (TypeError, ValueError, OSError) as exc:
            raise ArtifactStoreError(
                f"Failed to save artifact for stage '{stage_name}' at {path}: {exc}"
            ) from exc
        return path

    def load(self, stage_name: str) -> dict[str, Any]:
        """Load and return an artifact dict.

        Re-validates that the JSON parses cleanly and contains a schema_version field.
        Full contract-version checking is the consuming stage's responsibility.

        Args:
            stage_name: Used to derive the filename.

        Returns:
            The artifact dict.

        Raises:
            ArtifactStoreError: If the file is missing, invalid JSON, or missing
                schema_version (malformed input must be rejected loudly — §18.2).
        """
        path = self.artifact_path(stage_name)
        if not path.exists():
            raise ArtifactStoreError(
                f"Artifact for stage '{stage_name}' not found at {path}. "
                "Has the stage run yet for this run_id?"
            )
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ArtifactStoreError(
                f"Failed to read artifact for stage '{stage_name}' at {path}: {exc}"
            ) from exc

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ArtifactStoreError(
                f"Artifact for stage '{stage_name}' at {path} is not valid JSON: {exc}"
            ) from exc

        if not isinstance(data, dict):
            raise ArtifactStoreError(
                f"Artifact for stage '{stage_name}' at {path} is not a JSON object "
                f"(got {type(data).__name__}). Malformed input rejected."
            )

        if "schema_version" not in data:
            raise ArtifactStoreError(
                f"Artifact for stage '{stage_name}' at {path} is missing 'schema_version'. "
                "Malformed input rejected (§18.2)."
            )

        return data

    def exists(self, stage_name: str) -> bool:
        """Return True if an artifact for stage_name exists in this run."""
        return self.artifact_path(stage_name).exists()

    def load_with_model_validation(
        self,
        stage_name: str,
        model_class: type,
    ) -> Any:
        """Load an artifact and fully validate it against a pydantic model.

        Args:
            stage_name: Stage name for filename lookup.
            model_class: Pydantic model class to validate against.

        Returns:
            Validated pydantic model instance.

        Raises:
            ArtifactStoreError: If loading or validation fails.
        """
        data = self.load(stage_name)
        try:
            return model_class.model_validate(data)
        except ValidationError as exc:
            raise ArtifactStoreError(
                f"Artifact for stage '{stage_name}' failed schema validation "
                f"against {model_class.__name__}: {exc}"
            ) from exc
