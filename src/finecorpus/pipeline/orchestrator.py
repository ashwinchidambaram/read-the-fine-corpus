"""Pipeline orchestration — chains all five stages for a single run.

run_pipeline() is the public entry point.  It chains:
  Collect → Assess → Decompose → Plan → Build → [Promote]

Each stage receives the previous stage's artifact dict.  All artifacts are
persisted to <artifacts_root>/<run_id>/<stage>.json.

Design notes:
  - run_id is caller-supplied (no defaults — determinism discipline).
  - collected_at is fixed at the start of the run so the Inventory timestamp
    is deterministic for the same logical inputs.
  - Stages fail loudly; orchestrator does not swallow errors.
  - promote=True triggers alias promotion after Build succeeds; actual promote
    requires a SQLAlchemy Session (db_session parameter). When promote=True but
    no db_session is provided, the stage raises RuntimeError.
  - embedding_provider / index_adapter: when provided, the real Build stage
    runs. When omitted, Build falls back to the Phase 0 skeleton.
"""

from __future__ import annotations

import pathlib
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from finecorpus.pipeline.artifact_store import ArtifactStore
from finecorpus.pipeline.assess import AssessStage
from finecorpus.pipeline.build import BuildStage
from finecorpus.pipeline.collect import CollectStage
from finecorpus.pipeline.decompose import DecomposeStage
from finecorpus.pipeline.plan import PlanStage

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from finecorpus.embedding.base import EmbeddingProvider
    from finecorpus.index.adapter import IndexAdapter


def run_pipeline(
    source_dir: str | pathlib.Path,
    artifacts_root: str | pathlib.Path,
    run_id: str,
    workspace_id: str,
    kb_id: str,
    run_started_at: datetime | None = None,
    embedding_provider: EmbeddingProvider | None = None,
    index_adapter: IndexAdapter | None = None,
    build_id: int = 1,
    promote: bool = False,
    db_session: Session | None = None,
) -> dict[str, str]:
    """Run all five pipeline stages end-to-end.

    Args:
        source_dir: Directory to walk for documents (Collect input).
        artifacts_root: Root under which <run_id>/ is created.
        run_id: Caller-supplied run identity; must be non-empty.
        workspace_id: Tenancy workspace ULID (or identifier string).
        kb_id: Tenancy knowledge base ULID (or identifier string).
        run_started_at: Optional caller-supplied run timestamp (UTC).  When
            provided, all stages use this value instead of calling wall-clock,
            making the full run deterministic.  Defaults to now() when None.
        embedding_provider: EmbeddingProvider to use for embedding chunks in
            the Build stage. When None, Build falls back to Phase 0 skeleton.
        index_adapter: IndexAdapter to write chunks to in the Build stage.
            When None, Build falls back to Phase 0 skeleton.
        build_id: Build ID for the shadow collection name derivation.
            Monotonically increasing; caller supplies (default 1 for tests).
        promote: If True, run lifecycle promotion after Build completes
            (requires db_session to be provided). Default False.
        db_session: SQLAlchemy Session for the lifecycle promotion phase.
            Required when promote=True.

    Returns:
        A dict mapping stage name → absolute artifact path (as str).

    Raises:
        ArtifactStoreError: On I/O failure.
        ContractVersionError: If any stage receives an unsupported contract version.
        StageError: If any stage produces invalid output.
        RuntimeError: If promote=True but db_session is None.
    """
    if promote and db_session is None:
        raise RuntimeError(
            "promote=True requires db_session to be provided for lifecycle promotion."
        )

    store = ArtifactStore(artifacts_root=artifacts_root, run_id=run_id)

    # Fix a single run timestamp for determinism across the full run.
    # All stages use this value — no stage calls wall-clock independently.
    collected_at = run_started_at or datetime.now(tz=UTC)

    # Stage 1: Collect
    collect = CollectStage(
        source_dir=source_dir,
        workspace_id=workspace_id,
        kb_id=kb_id,
        collected_at=collected_at,
    )
    inventory_dict = collect.run(input_data=None, store=store)

    # Stage 2: Assess
    assess = AssessStage(run_id=run_id, run_started_at=collected_at)
    parse_result_batch = assess.run(input_data=inventory_dict, store=store)

    # Stage 3: Decompose (artifacts_root passed for frozen-artifact cache)
    decompose = DecomposeStage(
        run_started_at=collected_at, artifacts_root=artifacts_root, run_id=run_id
    )
    segment_set_batch = decompose.run(input_data=parse_result_batch, store=store)

    # Stage 4: Plan
    plan = PlanStage(run_started_at=collected_at)
    ingestion_config = plan.run(input_data=segment_set_batch, store=store)

    # Stage 5: Build
    build = BuildStage(
        run_started_at=collected_at,
        embedding_provider=embedding_provider,
        index_adapter=index_adapter,
        build_id=build_id,
        artifacts_root=artifacts_root,
        run_id=run_id,
        workspace_id=workspace_id,
        kb_id=kb_id,
    )
    build.run(input_data=ingestion_config, store=store)

    # Optional: promote the shadow collection via the lifecycle layer
    if promote and embedding_provider is not None and index_adapter is not None:
        assert db_session is not None  # already checked above
        _do_promote(
            artifacts_root=artifacts_root,
            run_id=run_id,
            store=store,
            index_adapter=index_adapter,
            db_session=db_session,
            workspace_id=workspace_id,
            kb_id=kb_id,
            build_id=build_id,
        )

    # Return artifact paths for all five stages
    stage_names = ["collect", "assess", "decompose", "plan", "build"]
    return {name: str(store.artifact_path(name).resolve()) for name in stage_names}


def _do_promote(
    artifacts_root: str | pathlib.Path,
    run_id: str,
    store: ArtifactStore,
    index_adapter: IndexAdapter,
    db_session: Session,
    workspace_id: str,
    kb_id: str,
    build_id: int,
) -> None:
    """Run the two-phase promotion for the just-built shadow collection.

    Reads the BuildResult to extract the shadow_collection and chunk_count,
    then delegates to lifecycle.promote().
    """

    from finecorpus.index.adapter import ModelIdentity, alias_name
    from finecorpus.index.lifecycle import BuildContext, BuildState, promote
    from finecorpus.pipeline.build.stage import BuildResult

    build_artifact_raw = store.load_with_model_validation("build", BuildResult)
    build_artifact = BuildResult.model_validate(build_artifact_raw.model_dump())
    shadow = build_artifact.shadow_collection
    chunk_count = build_artifact.chunk_count

    if not shadow:
        raise RuntimeError("promote=True but Build stage produced no shadow_collection.")

    # Reconstruct model identity from the shadow collection metadata
    meta = index_adapter.get_collection_metadata(shadow)
    model_identity = ModelIdentity(
        provider=meta.get("provider", ""),
        model=meta.get("model", ""),
        dimensions=int(meta.get("dimensions", 0)),
        config_version=meta.get("config_version", ""),
    )

    ctx = BuildContext(
        kb_id=kb_id,
        workspace_id=workspace_id,
        build_id=build_id,
        shadow_collection=shadow,
        alias=alias_name(kb_id),
        model_identity=model_identity,
        state=BuildState.INGESTING,
    )

    from finecorpus.control.metadata import AliasRepository

    repo = AliasRepository(db_session)
    als = alias_name(kb_id)
    if repo.get(als) is None:
        repo.create(alias=als, kb_id=kb_id, workspace_id=workspace_id)
        db_session.commit()

    promote(
        adapter=index_adapter,
        session=db_session,
        ctx=ctx,
        expected_min_chunks=max(1, chunk_count),
        declared_empty=(chunk_count == 0),
    )
