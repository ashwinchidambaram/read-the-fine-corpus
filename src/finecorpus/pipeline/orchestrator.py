"""Pipeline orchestration — chains all five stages for a single run.

run_pipeline() is the public entry point.  It chains:
  Collect → Assess → Decompose → Plan → Build

Each stage receives the previous stage's artifact dict.  All artifacts are
persisted to <artifacts_root>/<run_id>/<stage>.json.

Design notes:
  - run_id is caller-supplied (no defaults — determinism discipline).
  - collected_at is fixed at the start of the run so the Inventory timestamp
    is deterministic for the same logical inputs.
  - Stages fail loudly; orchestrator does not swallow errors.
"""

from __future__ import annotations

import pathlib
from datetime import UTC, datetime

from finecorpus.pipeline.artifact_store import ArtifactStore
from finecorpus.pipeline.assess import AssessStage
from finecorpus.pipeline.build import BuildStage
from finecorpus.pipeline.collect import CollectStage
from finecorpus.pipeline.decompose import DecomposeStage
from finecorpus.pipeline.plan import PlanStage


def run_pipeline(
    source_dir: str | pathlib.Path,
    artifacts_root: str | pathlib.Path,
    run_id: str,
    workspace_id: str,
    kb_id: str,
) -> dict[str, str]:
    """Run all five pipeline stages end-to-end.

    Args:
        source_dir: Directory to walk for documents (Collect input).
        artifacts_root: Root under which <run_id>/ is created.
        run_id: Caller-supplied run identity; must be non-empty.
        workspace_id: Tenancy workspace ULID (or identifier string).
        kb_id: Tenancy knowledge base ULID (or identifier string).

    Returns:
        A dict mapping stage name → absolute artifact path (as str).

    Raises:
        ArtifactStoreError: On I/O failure.
        ContractVersionError: If any stage receives an unsupported contract version.
        StageError: If any stage produces invalid output.
    """
    store = ArtifactStore(artifacts_root=artifacts_root, run_id=run_id)

    # Fix the collection timestamp for determinism across the full run.
    collected_at = datetime.now(tz=UTC)

    # Stage 1: Collect
    collect = CollectStage(
        source_dir=source_dir,
        workspace_id=workspace_id,
        kb_id=kb_id,
        collected_at=collected_at,
    )
    inventory_dict = collect.run(input_data=None, store=store)

    # Stage 2: Assess
    assess = AssessStage()
    parse_result_batch = assess.run(input_data=inventory_dict, store=store)

    # Stage 3: Decompose
    decompose = DecomposeStage()
    segment_set_batch = decompose.run(input_data=parse_result_batch, store=store)

    # Stage 4: Plan
    plan = PlanStage()
    ingestion_config = plan.run(input_data=segment_set_batch, store=store)

    # Stage 5: Build
    build = BuildStage()
    build.run(input_data=ingestion_config, store=store)

    # Return artifact paths for all five stages
    stage_names = ["collect", "assess", "decompose", "plan", "build"]
    return {name: str(store.artifact_path(name).resolve()) for name in stage_names}
