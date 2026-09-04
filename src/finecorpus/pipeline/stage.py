"""Stage abstraction for Read The Fine Corpus pipeline.

Each stage:
  - Declares its name, consumed contract name + SpecRange, produced contract name.
  - Validates input contract version (MUST reject unsupported versions loudly — §12).
  - Validates its own output before persisting.
  - Persists to ArtifactStore and returns the loaded-back artifact.

Design choices:
  - Stages are stateless callables; they hold only metadata (name, version ranges).
  - Input validation happens in run() before any processing — not inside _produce().
  - Output validation happens after _produce() returns; a stage that cannot produce a
    valid contract is a bug caught here, not downstream.

See docs/architecture/overview.md for the pipeline decomposition.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel, ValidationError

from finecorpus.contracts.versions import ContractVersionError, SpecRange, check_version
from finecorpus.pipeline.artifact_store import ArtifactStore


class StageError(Exception):
    """Raised when a stage encounters an unrecoverable error.

    Wraps ContractVersionError, ValidationError, and I/O errors so callers can
    distinguish stage failures from contract-version rejections if needed.
    """


class Stage(ABC):
    """Abstract pipeline stage.

    Subclasses implement _produce(input_data) -> dict.
    The base class handles:
      - Contract version validation (raises ContractVersionError on unsupported version).
      - Output schema validation (raises StageError if output is not contract-valid).
      - Artifact persistence and re-loading via ArtifactStore.
    """

    #: Human-readable stage name used for artifact filenames.
    name: str

    #: Human-readable name of the contract this stage consumes (first stage passes None).
    consumed_contract: str | None

    #: SpecRange this stage declares support for on the consumed contract.
    consumed_version_range: SpecRange | None

    #: Human-readable name of the contract this stage produces.
    produced_contract: str

    #: Pydantic model class for the output contract.
    output_model: type[BaseModel]

    def run(
        self,
        input_data: dict[str, Any] | None,
        store: ArtifactStore,
    ) -> dict[str, Any]:
        """Run the stage.

        1. Validate input contract version (if this stage has a consumed contract).
        2. Call _produce() to build the output dict.
        3. Validate output against output_model (hard error on failure).
        4. Persist via store.save() and re-load via store.load() for canonical form.

        Args:
            input_data: The raw dict from the previous stage's artifact.  None for Collect.
            store: ArtifactStore instance for this run.

        Returns:
            The loaded-back artifact dict (canonical JSON round-trip).

        Raises:
            ContractVersionError: If input_data schema_version is not supported.
            StageError: If output validation fails or I/O fails.
        """
        # 1. Validate input contract version
        # Only perform version checking when a consumed_version_range is declared.
        # Stages that consume pipeline-internal envelope contracts (not the six
        # official contracts from §12) set consumed_version_range=None to opt out
        # of the check_version call; the ArtifactStore's load() still ensures
        # schema_version is present and the JSON is well-formed.
        if (
            self.consumed_contract is not None
            and self.consumed_version_range is not None
            and input_data is not None
        ):
            schema_version = input_data.get("schema_version", "")
            if not schema_version:
                raise ContractVersionError(
                    contract=self.consumed_contract,
                    got="(missing)",
                    supported=self.consumed_version_range._describe(),
                )
            check_version(
                contract=self.consumed_contract,
                schema_version=str(schema_version),
                spec_range=self.consumed_version_range,
            )

        # 2. Produce output
        try:
            output_dict = self._produce(input_data)
        except (ContractVersionError, StageError):
            raise
        except Exception as exc:
            raise StageError(f"Stage '{self.name}' failed during _produce: {exc}") from exc

        # 3. Validate output against the output model
        try:
            self.output_model.model_validate(output_dict)
        except ValidationError as exc:
            raise StageError(
                f"Stage '{self.name}' produced invalid '{self.produced_contract}' output: {exc}"
            ) from exc

        # 4. Persist and re-load for canonical form
        try:
            store.save(self.name, output_dict)
            return store.load(self.name)
        except Exception as exc:
            raise StageError(f"Stage '{self.name}' artifact persistence failed: {exc}") from exc

    @abstractmethod
    def _produce(self, input_data: dict[str, Any] | None) -> dict[str, Any]:
        """Produce the output artifact dict.

        Implementations MUST NOT persist anything; the base class handles that.
        Failures inside here should raise StageError or let exceptions propagate.
        """
