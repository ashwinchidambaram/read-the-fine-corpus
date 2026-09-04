"""finecorpus.pipeline — Pipeline stage abstractions and orchestration.

Each stage:
  - Declares its name, consumed contract + SpecRange, produced contract.
  - Validates input contract version via contracts.check_version (hard reject).
  - Validates its own output before persisting.
  - Persists artifacts as JSON files under <artifacts_root>/<run_id>/<stage>.json.

Public API:
  Stage          — base abstraction (consume, validate, produce, persist)
  ArtifactStore  — JSON-file artifact store
  run_pipeline   — orchestrate all five stages over a source directory
"""

from finecorpus.pipeline.artifact_store import ArtifactStore, ArtifactStoreError
from finecorpus.pipeline.orchestrator import run_pipeline
from finecorpus.pipeline.stage import Stage, StageError

__all__ = [
    "Stage",
    "StageError",
    "ArtifactStore",
    "ArtifactStoreError",
    "run_pipeline",
]
