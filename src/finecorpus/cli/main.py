"""corpus CLI — thin wiring over the finecorpus library.

Entry point registered in pyproject.toml:
  [project.scripts]
  corpus = "finecorpus.cli.main:main"

Commands:
  corpus init           -- first-run provider prompt (M-103, spec §4.6)
  corpus pipeline run   -- run_pipeline over a source directory
  corpus preflight      -- run_preflight over a config file
  corpus report         -- generate findings + exclusion reports from artifacts

Logic stays in the library (constraint C-5).  This module is allowed to:
  - Parse arguments.
  - Call library functions.
  - Print results.
  - sys.exit() on error.

It is NOT allowed to contain business logic.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any


def _cmd_init(args: argparse.Namespace) -> int:
    """Wire corpus init → finecorpus.config.init_flow.run_init_flow."""
    from finecorpus.config.init_flow import InitError, run_init_flow

    try:
        result = run_init_flow(
            config_path=Path(args.config) if args.config else None,
            example_path=Path(args.example) if args.example else None,
            provider=args.provider,
            ollama_base_url=args.ollama_base_url,
            ollama_model=args.ollama_model,
            openai_model=args.openai_model,
            non_interactive=args.non_interactive,
        )
    except InitError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    return 0 if result.preflight_passed else 1


def _check_budget_direct_mode(args: argparse.Namespace) -> bool:
    """Check BudgetGuard before Build in direct (non-queued) pipeline run mode.

    Returns True if the budget cap was hit and the run should be aborted.
    Returns False if the run should proceed.

    When no control-plane DSN is configured → always returns False (no enforcement).
    """
    import pathlib

    # Only activate when a control-plane DSN is configured
    config = None
    control_dsn = None
    try:
        from finecorpus.config.loader import load_config

        config_path = getattr(args, "config", None) or "corpus.yaml"
        config = load_config(config_path)
        control_dsn = config.storage.postgres.url
    except Exception:  # noqa: BLE001
        return False  # Config unavailable → no enforcement

    if control_dsn is None:
        return False  # No DSN → behave as Phase 3

    # Resolve projected cost from artifacts
    projected_usd = 0.0
    try:
        import json as _json

        from finecorpus.contracts.ingestion_config import IngestionConfig
        from finecorpus.contracts.segment_set_batch import SegmentSetBatch
        from finecorpus.pipeline.costing import estimate_ingestion_cost, resolve_costing_providers

        artifacts_root = pathlib.Path(args.artifacts)
        run_id = args.run_id
        plan_path = artifacts_root / run_id / "plan.json"
        decompose_path = artifacts_root / run_id / "decompose.json"

        if plan_path.exists() and decompose_path.exists():
            ic = IngestionConfig.model_validate(_json.loads(plan_path.read_text(encoding="utf-8")))
            sb = SegmentSetBatch.model_validate(
                _json.loads(decompose_path.read_text(encoding="utf-8"))
            )
            providers = resolve_costing_providers(ic)
            if not providers.embedding_unavailable and providers.embedding_provider is not None:
                est = estimate_ingestion_cost(
                    segment_set_batch=sb,
                    ingestion_config=ic,
                    embedding_provider=providers.embedding_provider,
                    llm_provider_or_none=providers.llm_provider,
                )
                projected_usd = float(est.total_cost_usd)
    except Exception:  # noqa: BLE001
        pass  # Best-effort; if we can't estimate, guard with 0

    # Connect to control plane and check
    try:
        from sqlalchemy.orm import Session

        from finecorpus.control.cost_ledger import BudgetDecision, BudgetGuard, CostLedgerRepository
        from finecorpus.control.metadata import create_engine, create_tables

        engine = create_engine(control_dsn)
        create_tables(engine)

        budgets = config.budgets  # type: ignore[union-attr]
        kb_cap = float(budgets.per_kb_cap_usd) if budgets.per_kb_cap_usd is not None else None
        ws_cap = (
            float(budgets.per_workspace_cap_usd)
            if budgets.per_workspace_cap_usd is not None
            else None
        )

        if kb_cap is None and ws_cap is None:
            engine.dispose()
            return False  # No caps configured

        with Session(engine) as session:
            ledger_repo = CostLedgerRepository(session)
            guard = BudgetGuard(kb_cap_usd=kb_cap, workspace_cap_usd=ws_cap, repo=ledger_repo)
            decision = guard.check(
                kb_id=args.kb,
                workspace_id=args.workspace,
                projected_usd=projected_usd,
            )

        engine.dispose()

        if decision == BudgetDecision.allow:
            return False

        # Cap hit — inform user and abort
        if decision == BudgetDecision.pause_kb_cap:
            print(
                f"ERROR: Per-KB budget cap hit (estimated ${projected_usd:.4f} would exceed cap).",
                file=sys.stderr,
            )
            print(
                "  To resume: raise per_kb_cap_usd in your config or use 'corpus jobs resume'.",
                file=sys.stderr,
            )
        else:
            print(
                f"ERROR: Per-workspace budget cap hit "
                f"(estimated ${projected_usd:.4f} would exceed cap).",
                file=sys.stderr,
            )
            print(
                "  To resume: raise per_workspace_cap_usd in your config "
                "or use 'corpus jobs resume'.",
                file=sys.stderr,
            )
        return True

    except Exception as exc:  # noqa: BLE001
        # Budget check failed (e.g. DB unreachable) — log and proceed without enforcement
        import logging

        logging.getLogger("finecorpus.cli").warning(
            "budget guard check failed: %s; proceeding without enforcement", exc
        )
        return False


def _requires_confirmation(cost_estimate: Any, args: argparse.Namespace) -> bool:
    """Return True if the cost estimate requires interactive confirmation.

    Phase 4 extension: also checks budgets.ingestion_confirmation_threshold_usd
    from config (§16).  If the estimate is above the threshold, confirmation is
    required regardless of whether a control-plane DSN is configured.

    Note: this function does NOT check the ``--yes`` flag itself.  The caller
    (``_cmd_pipeline_run``) is responsible for gating the call with
    ``if not getattr(args, "yes", False)``.  This function returns a policy
    decision based solely on the cost estimate and the threshold config.

    Policy:
      - CostEstimateUnavailable (declared non-local, unavailable) → always confirm.
      - Estimate above ingestion_confirmation_threshold_usd → confirm.
      - Estimate below threshold → skip (proceed without prompt).
    """
    from decimal import Decimal

    from finecorpus.pipeline.costing import CostEstimateUnavailable

    if isinstance(cost_estimate, CostEstimateUnavailable):
        return True  # Honest unavailable → always confirm

    # Load threshold from config if available; fall back to legacy always-confirm
    threshold: Decimal | None = None
    try:
        from finecorpus.config.loader import load_config

        config_path = getattr(args, "config", None) or "corpus.yaml"
        cfg = load_config(config_path)
        threshold = cfg.budgets.ingestion_confirmation_threshold_usd
    except Exception:  # noqa: BLE001
        # Config unavailable — use legacy behaviour (always confirm when estimate exists)
        return True

    total = getattr(cost_estimate, "total_cost_usd", Decimal("0"))
    if threshold is not None and Decimal(str(total)) <= threshold:
        return False  # Below threshold — proceed without prompt
    return True


def _cmd_pipeline_sweep(args: argparse.Namespace) -> int:
    """Wire corpus pipeline sweep → eval_sweep.estimate_sweep_cost + run_sweep.

    Phase 5 §9.3 configuration-sweep orchestration:
      1. Load config and the corpus inventory (documents from KB).
      2. Call estimate_sweep_cost (M-047 — shown BEFORE any execution).
      3. Gate on budgets.sweep_confirmation_threshold_usd (M-048).
      4. On confirmation, call run_sweep and print the ranked table.
    """
    from decimal import Decimal

    # Resolve config
    config_path = getattr(args, "config", None) or "corpus.yaml"
    try:
        from finecorpus.config.loader import load_config

        cfg = load_config(config_path)
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: Could not load config — {exc}", file=sys.stderr)
        return 1

    kb_id: str = args.kb_id
    confirmed: bool = bool(getattr(args, "yes", False))

    # --- Resolve dependencies (embedding provider + index adapter) ---
    try:
        # We need an IngestionConfig to resolve providers; use a minimal reference config.
        # The sweep uses the base_config from the KB's current live config.
        # For the CLI, we use the FakeProvider path for now (no live KB required).
        # A full wiring would load the KB's current config from the control plane.
        from finecorpus.embedding.fake import FakeProvider

        provider = FakeProvider(dimensions=384, model_id="cli-sweep-fake")
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: Could not initialize embedding provider — {exc}", file=sys.stderr)
        return 1

    # --- Build a minimal base_config for the sweep ---
    try:
        from finecorpus.pipeline.plan.config_builder import build_default_ingestion_config

        base_config = build_default_ingestion_config(
            kb_id=kb_id,
            workspace_id=getattr(cfg, "workspace_id", "default"),
        )
    except Exception as exc:  # noqa: BLE001
        print(
            f"ERROR: Could not build base ingestion config — {exc}",
            file=sys.stderr,
        )
        return 1

    # --- Build an empty documents list (no live corpus available in CLI) ---
    documents: list = []

    # --- Estimate sweep cost (M-047 — shown BEFORE execution) ---
    from finecorpus.services.eval_sweep import SweepCostEstimate, estimate_sweep_cost, run_sweep

    print()
    print("=" * 60)
    print("  SWEEP COST ESTIMATE (pre-execution, M-047)")
    print("=" * 60)

    estimate: SweepCostEstimate | None = None
    try:
        from sqlalchemy.orm import Session as _Session

        from finecorpus.control.metadata import create_engine as _fc_create_engine
        from finecorpus.control.metadata import create_tables

        dsn = cfg.storage.postgres.url
        if dsn:
            engine = _fc_create_engine(dsn)
            create_tables(engine)
            with _Session(engine) as session:
                estimate = estimate_sweep_cost(
                    kb_id,
                    base_config,
                    documents,
                    session=session,
                    config=cfg,
                    embed_caps=provider,
                    llm_caps=None,
                )
            engine.dispose()
        else:
            print("  (No control-plane DSN; estimate not available.)", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001
        print(f"  (Cost estimate unavailable: {exc})", file=sys.stderr)

    if estimate is not None:
        print(f"  Sampled docs        : {estimate.n_sample_docs}")
        print(f"  Candidates          : {estimate.n_candidates}")
        print(f"  TOTAL EST COST      : ${float(estimate.total_est_cost_usd):.4f}")
        print(f"  Basis: {estimate.basis[:120]}")
        print("=" * 60)
        print()

        # --- M-048: Confirmation gate ---
        threshold: Decimal = cfg.budgets.sweep_confirmation_threshold_usd
        if Decimal(str(estimate.total_est_cost_usd)) >= threshold and not confirmed:
            print(
                f"  Sweep estimated cost (${float(estimate.total_est_cost_usd):.4f}) "
                f"is at or above the confirmation threshold (${float(threshold):.2f})."
            )
            print("  Re-run with --yes / -y to confirm and proceed.")
            try:
                answer = input("Proceed with sweep? [y/N] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print("\nAborted.", file=sys.stderr)
                return 1
            if answer not in ("y", "yes"):
                print("Sweep cancelled by user.", file=sys.stderr)
                return 1
            confirmed = True

    # --- Run sweep ---
    try:
        from sqlalchemy.orm import Session as _Session2

        from finecorpus.control.metadata import create_engine as _fc_create_engine2

        dsn = cfg.storage.postgres.url
        if not dsn:
            print(
                "ERROR: No control-plane DSN configured; sweep requires a database.",
                file=sys.stderr,
            )
            return 1

        engine = _fc_create_engine2(dsn)
        create_tables(engine)

        # Minimal adapter — real sweep would use QdrantAdapter
        try:
            from finecorpus.index.qdrant.backend import QdrantAdapter

            adapter = QdrantAdapter(url=cfg.storage.qdrant.url)
        except Exception as exc:  # noqa: BLE001
            print(f"ERROR: Could not connect to Qdrant — {exc}", file=sys.stderr)
            engine.dispose()
            return 1

        with _Session2(engine) as session:
            result = run_sweep(
                kb_id=kb_id,
                base_config=base_config,
                documents=documents,
                eval_set=[],
                session=session,
                config=cfg,
                adapter=adapter,
                provider=provider,
                confirmed=confirmed,
                workspace_id=getattr(cfg, "workspace_id", "default"),
            )
        engine.dispose()

    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: Sweep failed — {exc}", file=sys.stderr)
        return 1

    if result.declined:
        print(f"Sweep declined (M-050): {result.reason}")
        return 0

    if result.needs_confirmation:
        print(
            "Sweep requires confirmation (M-048). Re-run with --yes to proceed.",
            file=sys.stderr,
        )
        return 1

    from finecorpus.services.eval_sweep import render_ranked_table

    print(render_ranked_table(result))
    return 0


def _cmd_pipeline_run(args: argparse.Namespace) -> int:
    """Wire corpus pipeline run → finecorpus.pipeline.run_pipeline.

    Phase 3 addition: cost gate.  Before the Build stage executes, loads the
    Plan artifact (IngestionConfig + SegmentSetBatch), calls
    ``estimate_ingestion_cost``, prints the estimate, and requires ``--yes``
    or interactive confirmation to proceed.

    Phase 4 extension: confirmation threshold from budgets config (§16).
    When no control-plane DSN is configured, behaves as Phase 3.  When a DSN
    is configured, also records actuals to cost_ledger and consults BudgetGuard
    before Build.
    """
    from finecorpus.contracts.versions import ContractVersionError
    from finecorpus.pipeline import run_pipeline
    from finecorpus.pipeline.artifact_store import ArtifactStoreError
    from finecorpus.pipeline.stage import StageError

    # Cost gate: estimate before Build if a plan artifact exists.
    if not getattr(args, "yes", False):
        cost_estimate = _try_load_cost_estimate(args)
        if cost_estimate is not None:
            _print_cost_estimate(cost_estimate)
            # Confirmation required? (threshold-aware, Phase 4)
            if _requires_confirmation(cost_estimate, args):
                try:
                    answer = input("Proceed with ingestion? [y/N] ").strip().lower()
                except (EOFError, KeyboardInterrupt):
                    print("\nAborted.", file=sys.stderr)
                    return 1
                if answer not in ("y", "yes"):
                    print("Ingestion cancelled by user.", file=sys.stderr)
                    return 1

    # Phase 4: budget guard in direct-run mode
    # When a control-plane DSN is configured, consult BudgetGuard before Build.
    budget_paused = _check_budget_direct_mode(args)
    if budget_paused:
        return 1

    try:
        artifact_paths = run_pipeline(
            source_dir=args.source,
            artifacts_root=args.artifacts,
            run_id=args.run_id,
            workspace_id=args.workspace,
            kb_id=args.kb,
        )
    except ContractVersionError as exc:
        print(f"ERROR: Contract version rejected — {exc}", file=sys.stderr)
        return 2
    except StageError as exc:
        print(f"ERROR: Stage failure — {exc}", file=sys.stderr)
        return 3
    except ArtifactStoreError as exc:
        print(f"ERROR: Artifact store — {exc}", file=sys.stderr)
        return 4
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print("Pipeline complete. Artifacts:")
    for stage, path in artifact_paths.items():
        print(f"  {stage:10s}: {path}")
    return 0


def _try_load_cost_estimate(args: argparse.Namespace) -> Any | None:
    """Attempt to load plan + decompose artifacts and compute a cost estimate.

    Returns one of:
    - ``IngestionCostEstimate`` — honest estimate for a resolved provider.
    - ``CostEstimateUnavailable`` — when a declared non-local provider cannot be
      constructed (e.g. openai declared but no API key in env).  The CLI must
      print an explicit "unavailable" message rather than fabricating $0.00.
    - ``None`` — artifacts not yet available; skip the gate entirely.

    Policy (Ruling 2):
    - Resolve the real embedding provider declared by the IngestionConfig via
      ``resolve_costing_providers`` — never hardcode FakeProvider.
    - If the declared provider is non-local and unavailable (no key), return
      ``CostEstimateUnavailable`` — do NOT fabricate $0.00.
    - Local/fake providers are always constructible; they get the zero-marginal-cost label.
    - All provider resolution logic lives in ``pipeline/costing.py`` (C-5).
    """
    import pathlib

    try:
        from finecorpus.contracts.ingestion_config import IngestionConfig
        from finecorpus.contracts.segment_set_batch import SegmentSetBatch
        from finecorpus.pipeline.costing import (
            CostEstimateUnavailable,
            estimate_ingestion_cost,
            resolve_costing_providers,
        )
    except ImportError:
        return None

    artifacts_root = pathlib.Path(args.artifacts)
    run_id = args.run_id
    plan_path = artifacts_root / run_id / "plan.json"
    decompose_path = artifacts_root / run_id / "decompose.json"

    if not plan_path.exists() or not decompose_path.exists():
        # Artifacts not yet produced — skip the cost gate
        return None

    try:
        import json as _json

        ingestion_config = IngestionConfig.model_validate(
            _json.loads(plan_path.read_text(encoding="utf-8"))
        )
        segment_batch = SegmentSetBatch.model_validate(
            _json.loads(decompose_path.read_text(encoding="utf-8"))
        )
    except Exception:  # noqa: BLE001
        return None

    # Resolve real providers from the ingestion config (Ruling 2: honest cost gate)
    costing_providers = resolve_costing_providers(ingestion_config)

    if costing_providers.embedding_unavailable:
        # Non-local provider declared but cannot be constructed (e.g. no API key).
        # Return unavailable sentinel — the CLI will print an explicit message.
        return CostEstimateUnavailable(
            provider_name=ingestion_config.embedding.provider,
            reason=costing_providers.embedding_unavailable_reason or "unknown reason",
        )

    return estimate_ingestion_cost(
        segment_set_batch=segment_batch,
        ingestion_config=ingestion_config,
        embedding_provider=costing_providers.embedding_provider,
        llm_provider_or_none=costing_providers.llm_provider,
    )


def _print_cost_estimate(estimate: Any) -> None:
    """Print the cost estimate in a human-readable format.

    Handles both ``IngestionCostEstimate`` and ``CostEstimateUnavailable``
    sentinels (Ruling 2: honest cost gate).
    """
    from finecorpus.pipeline.costing import CostEstimateUnavailable

    print()
    print("=" * 60)
    print("  INGESTION COST ESTIMATE (pre-Build)")
    print("=" * 60)

    if isinstance(estimate, CostEstimateUnavailable):
        # Honest unavailable message — never fabricate $0.00 (Ruling 2)
        print(f"  Cost estimate unavailable for declared provider '{estimate.provider_name}'")
        print(f"  Reason: {estimate.reason}")
        print()
        print("  You must still confirm before Build proceeds.")
        print("=" * 60)
        print()
        return

    print(f"  Tokenizer         : {estimate.tokenizer_name}")
    print()
    print("  Embedding:")
    print(f"    Provider/model  : {estimate.embedding_provider_id}/{estimate.embedding_model_id}")
    print(f"    Token count     : {estimate.embedding_token_count:,}")
    if estimate.embedding_zero_marginal_cost:
        print("    Cost            : $0.00 (local provider — zero marginal cost)")
    else:
        print(f"    Cost per 1k     : ${estimate.embedding_cost_per_1k}")
        print(f"    Estimated cost  : ${estimate.embedding_cost_usd:.4f}")
    if estimate.embedding_pricing_as_of:
        print(f"    Pricing as of   : {estimate.embedding_pricing_as_of}")
    print()
    if estimate.llm_provider_id:
        print("  LLM augmentation:")
        print(f"    Provider/model  : {estimate.llm_provider_id}/{estimate.llm_model_id}")
        print(f"    Table calls     : {estimate.llm_table_call_count}")
        print(f"    Other calls     : {estimate.llm_other_call_count}")
        print(f"    Total calls     : {estimate.llm_total_call_count}")
        if estimate.llm_zero_marginal_cost:
            print("    Cost            : $0.00 (local provider — zero marginal cost)")
        else:
            print(f"    Estimated cost  : ${estimate.llm_cost_usd:.4f}")
    else:
        print("  LLM augmentation  : none configured")
    print()
    print(f"  TOTAL ESTIMATED   : ${estimate.total_cost_usd:.4f}")
    print("=" * 60)
    print(f"  Basis: {estimate.basis}")
    print("=" * 60)
    print()


def _cmd_preview(args: argparse.Namespace) -> int:
    """Wire corpus preview → BuildStage(dry_run=True) over plan artifact.

    Loads the plan artifact (IngestionConfig + SegmentSetBatch), runs Build
    in dry_run mode over a sample of documents / a specific class, and prints
    the resulting chunks with text, augmentation fields, and provenance.
    """
    import json as _json
    import pathlib

    artifacts_root = pathlib.Path(args.artifacts)
    run_id = args.run_id
    plan_path = artifacts_root / run_id / "plan.json"
    decompose_path = artifacts_root / run_id / "decompose.json"

    if not plan_path.exists():
        print(f"ERROR: Plan artifact not found at {plan_path}", file=sys.stderr)
        print("Run 'corpus pipeline run' first to produce the plan artifact.", file=sys.stderr)
        return 1
    if not decompose_path.exists():
        print(f"ERROR: Decompose artifact not found at {decompose_path}", file=sys.stderr)
        return 1

    try:
        from finecorpus.pipeline.build.stage import BuildStage
    except ImportError as exc:
        print(f"ERROR: Import failure — {exc}", file=sys.stderr)
        return 1

    try:
        ingestion_config_dict = _json.loads(plan_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: Could not load plan artifact — {exc}", file=sys.stderr)
        return 1

    # Run Build in dry_run mode — no embedding/upsert, chunks inline
    stage = BuildStage(
        artifacts_root=artifacts_root,
        run_id=run_id,
        dry_run=True,
    )

    try:
        result_dict = stage._produce(ingestion_config_dict)  # noqa: SLF001
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: Preview build failed — {exc}", file=sys.stderr)
        return 1

    chunks = result_dict.get("chunks", [])
    n_samples = getattr(args, "samples", 5)
    filter_class = getattr(args, "class_filter", None)

    # Filter by class if requested
    if filter_class:
        chunks = [c for c in chunks if c.get("provenance", {}).get("segment_type") == filter_class]

    # Sample
    sample_chunks = chunks[:n_samples]

    print(f"Preview: {len(chunks)} chunks total (showing {len(sample_chunks)}")
    if filter_class:
        print(f"  class filter: {filter_class}")
    print()

    for i, chunk in enumerate(sample_chunks):
        print(f"--- Chunk {i + 1} ---")
        print(f"  document_id   : {chunk.get('document_id', '?')}")
        print(f"  segment_path  : {chunk.get('segment_path', '?')}")
        print(f"  chunk_index   : {chunk.get('chunk_index', '?')}")
        print(f"  token_count   : {chunk.get('token_count', '?')}")
        print()
        text = chunk.get("text", "")
        print(f"  text ({len(text)} chars):")
        print("    " + text[:200].replace("\n", "\n    "))
        if len(text) > 200:
            print("    [... truncated]")
        print()

        aug = chunk.get("augmentation", {})
        if any(v is not None for v in aug.values()):
            print("  augmentation:")
            for key, val in aug.items():
                if val is not None:
                    short_val = str(val)[:80]
                    print(f"    {key}: {short_val}")
            print()

        prov = chunk.get("provenance", {})
        transforms = prov.get("transformations", [])
        if transforms:
            print(f"  transformations ({len(transforms)}):")
            for tr in transforms:
                changed = tr.get("changed_text", False)
                print(f"    [{tr.get('tier')}] {tr.get('operation')} changed={changed}")
            print()

    print(f"Build summary: {result_dict.get('report', '')}")
    return 0


def _cmd_config_export(args: argparse.Namespace) -> int:
    """Wire corpus config export → finecorpus.pipeline.plan.config_io.export_config."""
    from finecorpus.pipeline.plan.config_io import ConfigExportError, export_config
    from finecorpus.pipeline.plan.config_io import import_config as _load

    try:
        # Load the existing config artifact from an artifact store path or direct JSON file.
        config = _load(Path(args.input))
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: Could not load config — {exc}", file=sys.stderr)
        return 1

    try:
        export_config(config, Path(args.output))
    except ConfigExportError as exc:
        print(f"ERROR: Export refused — {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"ERROR: Could not write — {exc}", file=sys.stderr)
        return 1

    print(f"Config exported to {args.output}")
    return 0


def _cmd_config_import(args: argparse.Namespace) -> int:
    """Wire corpus config import → finecorpus.pipeline.plan.config_io.import_config."""
    from pydantic import ValidationError

    from finecorpus.pipeline.plan.config_io import ConfigImportError, import_config

    try:
        config = import_config(Path(args.input))
    except ConfigImportError as exc:
        print(f"ERROR: Import rejected — {exc}", file=sys.stderr)
        return 2
    except ValidationError as exc:
        print(f"ERROR: Schema validation failed — {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print("Config imported successfully.")
    print(f"  schema_version : {config.schema_version}")
    print(f"  config_version : {config.config_version}")
    print(f"  created_at     : {config.created_at}")
    return 0


def _cmd_config_diff(args: argparse.Namespace) -> int:
    """Wire corpus config diff → finecorpus.pipeline.plan.config_io.diff_configs.

    D-23: Detects confidence-floor and default_salience_filter weakenings.
    A weakening means the incoming config (B) is LESS restrictive than the
    current config (A) — lower confidence_floor or more salience tiers allowed.
    Printed as a WARNING so operators can decide whether to proceed.
    """
    import json as _json

    from finecorpus.pipeline.plan.config_io import (
        ConfigImportError,
        detect_confidence_floor_lowering,
        diff_configs,
        import_config,
    )

    try:
        config_a = import_config(Path(args.a))
        config_b = import_config(Path(args.b))
    except ConfigImportError as exc:
        print(f"ERROR: Import rejected — {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    result = diff_configs(config_a, config_b)

    if result["same"]:
        print("Configs are identical.")
        return 0

    print("Configs differ:")
    if result["config_version_changed"]:
        print("  config_version changed (build-affecting difference)")
    if result["a_only"]:
        print(f"  Only in A: {result['a_only']}")
    if result["b_only"]:
        print(f"  Only in B: {result['b_only']}")
    if result["changed"]:
        print("  Changed fields:")
        for field, diff in result["changed"].items():
            print(f"    {field}:")
            print(f"      A: {_json.dumps(diff['a'], separators=(',', ':'))[:120]}")
            print(f"      B: {_json.dumps(diff['b'], separators=(',', ':'))[:120]}")

    # D-23: Confidence-floor lowering detection
    weakenings = detect_confidence_floor_lowering(config_a, config_b)
    if weakenings:
        print(
            "\nWARNING (D-23): Incoming config weakens retrieval filters "
            "relative to the current config:",
            file=sys.stderr,
        )
        for w in weakenings:
            print(
                f"  [{w['severity'].upper()}] class={w['class']} field={w['field']}: "
                f"{w['old']!r} → {w['new']!r}  "
                "(lower confidence_floor or broader salience tiers reduces filter strength)",
                file=sys.stderr,
            )
        print(
            "  Audit: recorded when config activation flows through the control plane\n"
            "  (Phase 6); in direct file mode this warning is the D-23 surface.\n"
            "  Review the weakening before triggering a reindex.",
            file=sys.stderr,
        )

    return 1  # non-zero = configs differ (useful in scripts)


def _cmd_plan(args: argparse.Namespace) -> int:
    """Wire corpus plan → PlanStage over existing artifacts.

    Prints: per-class routing table with basis labels, language warnings,
    exclusion summary.  Zero business logic — C-5 thin wrapper.
    """
    from finecorpus.contracts.ingestion_config import LanguageDecision
    from finecorpus.pipeline.artifact_store import ArtifactStore, ArtifactStoreError
    from finecorpus.pipeline.plan import PlanStage
    from finecorpus.pipeline.stage import StageError

    artifacts_root = args.artifacts
    run_id = args.run_id
    class_descriptions = args.class_descriptions

    store = ArtifactStore(artifacts_root=artifacts_root, run_id=run_id)

    try:
        segment_set_batch = store.load("decompose")
    except ArtifactStoreError as exc:
        print(f"ERROR: Could not load decompose artifact — {exc}", file=sys.stderr)
        print(
            "Run 'corpus pipeline run' first to produce the decompose artifact.",
            file=sys.stderr,
        )
        return 4

    try:
        plan = PlanStage(
            class_descriptions_path=class_descriptions,
        )
        ingestion_config_dict = plan.run(input_data=segment_set_batch, store=store)
    except StageError as exc:
        print(f"ERROR: Plan stage failure — {exc}", file=sys.stderr)
        return 3
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    # Print per-class routing table with basis labels
    class_rules = ingestion_config_dict.get("class_rules", [])
    provenance = ingestion_config_dict.get("provenance", [])

    # Build a provenance index: target prefix → list of (basis, rationale)
    prov_by_class: dict[str, list[dict]] = {}
    for p in provenance:
        target: str = p.get("target", "")
        if target.startswith("/class_rules/"):
            parts = target.split("/")
            if len(parts) >= 3:
                seg_class = parts[2]
                if seg_class not in prov_by_class:
                    prov_by_class[seg_class] = []
                prov_by_class[seg_class].append(p)

    print(f"\nPlan Stage — run '{run_id}'")
    print("=" * 60)

    print("\nPer-Class Routing Table:")
    print("-" * 60)
    print(f"{'Class':<20} {'Strategy':<16} {'MaxTok':<8} {'Basis'}")
    print(f"{'-----':<20} {'--------':<16} {'------':<8} {'-----'}")
    for rule in class_rules:
        seg_class = rule.get("segment_class", "?")
        chunking = rule.get("chunking", {})
        strategy = chunking.get("strategy", "?")
        max_tokens = chunking.get("max_tokens", "?")
        # Determine dominant basis for this class
        prov_entries = prov_by_class.get(seg_class, [])
        bases = {p.get("basis", "heuristic") for p in prov_entries}
        if "class_description" in bases:
            basis_label = "heuristic+class_desc"
        elif "heuristic" in bases:
            basis_label = "heuristic"
        else:
            basis_label = "heuristic"
        print(f"{seg_class:<20} {strategy:<16} {str(max_tokens):<8} {basis_label}")

    # Language warning (M-041)
    lang_support = ingestion_config_dict.get("language_support", {})
    decision = lang_support.get("decision", "proceed")
    if decision == LanguageDecision.warned_proceed:
        unsupported = lang_support.get("unsupported_languages", [])
        print("\nWARNING (M-041): Language support gap detected")
        print(f"  Unsupported languages: {', '.join(unsupported)}")
        print("  The configured embedding model does not declare support for these languages.")
        print("  Embeddings for these languages may be quietly meaningless.")
        print("  Consider switching to a multilingual embedding model.")
    else:
        print("\nLanguage support: all detected languages supported (or no language data).")

    # Exclusion summary
    exclusions = ingestion_config_dict.get("exclusions_confirmed", [])
    if exclusions:
        from collections import Counter

        reason_counts = Counter(e.get("reason", "other") for e in exclusions)
        print(f"\nExclusions ({len(exclusions)} total):")
        for reason, count in sorted(reason_counts.items()):
            print(f"  {reason}: {count}")
    else:
        print("\nExclusions: none.")

    print(f"\nConfig version: {ingestion_config_dict.get('config_version', '?')[:16]}...")
    print(f"Class rules: {len(class_rules)} classes")
    return 0


def _cmd_kb_delete_doc(args: argparse.Namespace) -> int:
    """Wire corpus kb delete-doc → pipeline.deletion.delete_document (purge=False).

    Thin wrapper: deletes the document from the live and N-1 index collections and
    appends a tombstone + audit record.  Cold snapshots are NOT destroyed; they will
    age out per snapshot_retention_period_days (M-089: deleted from service).
    """
    from finecorpus.pipeline.deletion import DeletionError, delete_document

    # Session and adapter construction is beyond the CLI's scope in Phase 4
    # (the full wiring lives in the ingest_worker service).  The CLI wrapper
    # prints an honest error if the required dependencies are absent.
    try:
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from finecorpus.config.loader import load_config
        from finecorpus.index.qdrant.backend import QdrantAdapter

        config_path = getattr(args, "config", None)
        config = load_config(config_path)
        engine = create_engine(config.storage.postgres.url or "sqlite:///:memory:")
        SessionLocal = sessionmaker(bind=engine)
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: Could not initialize control-plane connection — {exc}", file=sys.stderr)
        print("Ensure POSTGRES_URL is set and Qdrant is reachable.", file=sys.stderr)
        return 1

    try:
        adapter = QdrantAdapter(url=config.storage.qdrant.url)
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: Could not connect to Qdrant — {exc}", file=sys.stderr)
        return 1

    with SessionLocal() as session:
        try:
            report = delete_document(
                kb_id=args.kb_id,
                document_id=args.doc_id,
                purge=False,
                session=session,
                adapter=adapter,
                artifacts_root=getattr(args, "artifacts", None),
                deleted_by=getattr(args, "actor", "cli"),
            )
        except DeletionError as exc:
            print(f"ERROR: Deletion failed — {exc}", file=sys.stderr)
            return 1
        except Exception as exc:  # noqa: BLE001
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1

    print(report.summary)
    print(f"  tombstone entry_id : {report.tombstone_entry_id}")
    print(f"  live chunks removed: {report.live_chunks_removed}")
    print(f"  N-1 chunks removed : {report.n1_chunks_removed}")
    print(f"  artifacts removed  : {len(report.artifacts_removed)}")
    return 0


def _cmd_kb_purge_doc(args: argparse.Namespace) -> int:
    """Wire corpus kb purge-doc → pipeline.deletion.delete_document (purge=True).

    Requires --confirm to prevent accidental use.  Destroys all snapshots
    immediately (D-05).  M-089: purged from all copies.
    """
    if not getattr(args, "confirm", False):
        print(
            "ERROR: purge requires --confirm. "
            "M-089: purge destroys ALL copies including cold snapshots IMMEDIATELY.",
            file=sys.stderr,
        )
        print(
            "Re-run with --confirm to proceed. "
            "Use 'corpus kb delete-doc' if you only want to remove from service "
            "(cold snapshots will age out per snapshot_retention_period_days).",
            file=sys.stderr,
        )
        return 2

    from finecorpus.pipeline.deletion import DeletionError, delete_document

    try:
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from finecorpus.config.loader import load_config
        from finecorpus.index.qdrant.backend import QdrantAdapter

        config_path = getattr(args, "config", None)
        config = load_config(config_path)
        engine = create_engine(config.storage.postgres.url or "sqlite:///:memory:")
        SessionLocal = sessionmaker(bind=engine)
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: Could not initialize control-plane connection — {exc}", file=sys.stderr)
        return 1

    try:
        adapter = QdrantAdapter(url=config.storage.qdrant.url)
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: Could not connect to Qdrant — {exc}", file=sys.stderr)
        return 1

    with SessionLocal() as session:
        try:
            report = delete_document(
                kb_id=args.kb_id,
                document_id=args.doc_id,
                purge=True,
                session=session,
                adapter=adapter,
                artifacts_root=getattr(args, "artifacts", None),
                deleted_by=getattr(args, "actor", "cli"),
            )
        except DeletionError as exc:
            print(f"ERROR: Purge failed — {exc}", file=sys.stderr)
            return 1
        except Exception as exc:  # noqa: BLE001
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1

    print(report.summary)
    print(f"  tombstone entry_id     : {report.tombstone_entry_id}")
    print(f"  live chunks removed    : {report.live_chunks_removed}")
    print(f"  N-1 chunks removed     : {report.n1_chunks_removed}")
    print(f"  artifacts removed      : {len(report.artifacts_removed)}")
    print(f"  snapshots destroyed    : {len(report.snapshots_destroyed)}")
    if report.snapshots_destroyed:
        for sid in report.snapshots_destroyed:
            print(f"    - {sid}")
    return 0


def _cmd_kb_snapshots(args: argparse.Namespace) -> int:
    """Wire corpus kb snapshots → adapter.list_snapshots for KB collections.

    Lists all snapshots for the KB's live and N-1 collections.
    """
    try:
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from finecorpus.config.loader import load_config
        from finecorpus.control.metadata import AliasRepository
        from finecorpus.index.adapter import alias_name
        from finecorpus.index.qdrant.backend import QdrantAdapter

        config_path = getattr(args, "config", None)
        config = load_config(config_path)
        engine = create_engine(config.storage.postgres.url or "sqlite:///:memory:")
        SessionLocal = sessionmaker(bind=engine)
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: Could not initialize control-plane connection — {exc}", file=sys.stderr)
        return 1

    try:
        adapter = QdrantAdapter(url=config.storage.qdrant.url)
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: Could not connect to Qdrant — {exc}", file=sys.stderr)
        return 1

    als = alias_name(args.kb_id)
    with SessionLocal() as session:
        alias_repo = AliasRepository(session)
        record = alias_repo.get(als)

    if record is None:
        print(f"No alias record found for kb '{args.kb_id}'.", file=sys.stderr)
        return 1

    collections: list[str] = []
    if record.collection_name:
        collections.append(record.collection_name)
    if record.previous_collection:
        collections.append(record.previous_collection)

    if not collections:
        print(f"KB '{args.kb_id}' has no promoted collections yet.")
        return 0

    total = 0
    for coll in collections:
        try:
            snaps = adapter.list_snapshots(coll)
        except Exception as exc:  # noqa: BLE001
            print(f"  [{coll}] ERROR listing snapshots: {exc}", file=sys.stderr)
            continue
        print(f"[{coll}] — {len(snaps)} snapshot(s)")
        for snap in snaps:
            created = snap.created_at.isoformat() if snap.created_at else "unknown"
            print(f"  snapshot_id: {snap.snapshot_id}")
            print(f"    created_at : {created}")
            print(f"    location   : {snap.location}")
        total += len(snaps)

    print(f"\nTotal: {total} snapshot(s) across {len(collections)} collection(s)")
    return 0


def _cmd_kb_status(args: argparse.Namespace) -> int:
    """Wire corpus kb status → hot/cold inventory + memory footprint (M-096).

    Displays:
    - Live collection and N-1 collection names
    - Vector count and estimated memory footprint per hot copy
    - §10.2 advisory about raising hot_retention_count
    """
    try:
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from finecorpus.config.loader import load_config
        from finecorpus.control.metadata import AliasRepository
        from finecorpus.index.adapter import alias_name
        from finecorpus.index.qdrant.backend import QdrantAdapter

        config_path = getattr(args, "config", None)
        config = load_config(config_path)
        engine = create_engine(config.storage.postgres.url or "sqlite:///:memory:")
        SessionLocal = sessionmaker(bind=engine)
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: Could not initialize control-plane connection — {exc}", file=sys.stderr)
        return 1

    try:
        adapter = QdrantAdapter(url=config.storage.qdrant.url)
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: Could not connect to Qdrant — {exc}", file=sys.stderr)
        return 1

    kb_id = args.kb_id
    als = alias_name(kb_id)
    with SessionLocal() as session:
        alias_repo = AliasRepository(session)
        record = alias_repo.get(als)

    if record is None:
        print(f"No alias record found for kb '{kb_id}'.", file=sys.stderr)
        return 1

    print(f"KB status: {kb_id}")
    print(f"  alias            : {record.alias}")
    print(f"  live collection  : {record.collection_name or '(none)'}")
    print(f"  N-1 collection   : {record.previous_collection or '(none)'}")
    promoted = record.promoted_at.isoformat() if record.promoted_at else "(never)"
    print(f"  promoted_at      : {promoted}")
    print()

    # Hot copy inventory
    hot_collections: list[str] = []
    if record.collection_name:
        hot_collections.append(record.collection_name)
    if record.previous_collection:
        hot_collections.append(record.previous_collection)

    hot_retention = getattr(getattr(config, "index_lifecycle", None), "hot_retention_count", 1)
    print(f"  hot_retention_count (config): {hot_retention}")
    print(f"  hot collections found       : {len(hot_collections)}")
    print()

    for coll in hot_collections:
        label = "(live)" if coll == record.collection_name else "(N-1)"
        try:
            info = adapter.get_collection_info(coll)
            dims = info.vector_size
            count = info.point_count
            # §10.2 memory estimate: vector_count × dims × 4 bytes + payload estimate
            # Payload estimate: ~500 bytes per point (heuristic; label as estimate)
            vector_bytes = count * dims * 4
            payload_estimate = count * 500
            total_bytes = vector_bytes + payload_estimate
            total_mb = total_bytes / (1024 * 1024)
            print(f"  [{coll}] {label}")
            print(f"    vector count     : {count:,}")
            print(f"    dimensions       : {dims}")
            mem_note = f"ESTIMATE: {count:,} × {dims}d × 4B + ~500B payload/point"
            print(f"    memory estimate  : {total_mb:.1f} MB  ({mem_note})")
        except Exception as exc:  # noqa: BLE001
            print(f"  [{coll}] {label}  ERROR: {exc}", file=sys.stderr)

    print()
    print(
        "  NOTE (§10.2): Raising hot_retention_count adds one full hot collection to"
        " memory cost per additional copy. Each hot copy costs approximately"
        " vector_count × dimensions × 4 bytes plus payload overhead."
    )
    return 0


def _cmd_kb_export(args: argparse.Namespace) -> int:
    """Wire corpus kb export → finecorpus.pipeline.export.export_kb (M-090)."""
    try:
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from finecorpus.config.loader import load_config
        from finecorpus.index.qdrant.backend import QdrantAdapter

        config_path = getattr(args, "config", None)
        config = load_config(config_path)
        engine = create_engine(config.storage.postgres.url or "sqlite:///:memory:")
        SessionLocal = sessionmaker(bind=engine)
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: Could not initialize control-plane connection — {exc}", file=sys.stderr)
        return 1

    try:
        adapter = QdrantAdapter(url=config.storage.qdrant.url)
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: Could not connect to Qdrant — {exc}", file=sys.stderr)
        return 1

    from finecorpus.pipeline.export import export_kb

    try:
        with SessionLocal() as session:
            manifest = export_kb(
                kb_id=args.kb_id,
                out_dir=args.out,
                session=session,
                adapter=adapter,
                artifacts_root=getattr(args, "artifacts", None),
                run_id=getattr(args, "run_id", None),
            )
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"ERROR: File system error — {exc}", file=sys.stderr)
        return 1

    print(f"Export complete: {manifest.out_dir}")
    print(f"  KB          : {manifest.kb_id}")
    print(f"  exported_at : {manifest.exported_at.isoformat()}")
    print(f"  chunk_count : {manifest.chunk_count:,}")
    print(f"  files       : {len(manifest.file_hashes)}")
    print(f"  manifest    : {manifest.out_dir / 'manifest.json'}")
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    """Wire corpus report → finecorpus.pipeline.report.generate_report."""
    from pathlib import Path

    from finecorpus.pipeline.artifact_store import ArtifactStoreError
    from finecorpus.pipeline.report import ReportError, ReportFormat, generate_report

    try:
        result = generate_report(
            artifacts_root=args.artifacts,
            run_id=args.run_id,
        )
    except ArtifactStoreError as exc:
        print(f"ERROR: Artifact store — {exc}", file=sys.stderr)
        return 4
    except ReportError as exc:
        print(f"ERROR: Report generation — {exc}", file=sys.stderr)
        return 3
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    fmt = ReportFormat(args.format)

    if args.out:
        out_dir = Path(args.out)
        written = result.write(out_dir=out_dir, fmt=fmt, run_id=args.run_id)
        print(f"Reports written to {out_dir}:")
        for label, path in sorted(written.items()):
            print(f"  {label:20s}: {path}")
    else:
        # Print to stdout; respect format selection
        if fmt in (ReportFormat.md, ReportFormat.both):
            print(result.findings_md)
            print()
            print(result.exclusions_md)
        if fmt in (ReportFormat.json, ReportFormat.both):
            import json

            print(json.dumps(result.findings_json, indent=2, ensure_ascii=False))
            print()
            print(json.dumps(result.exclusions_json, indent=2, ensure_ascii=False))

    return 0


def _cmd_preflight(args: argparse.Namespace) -> int:
    """Wire corpus preflight → finecorpus.config.preflight.run_preflight.

    The embedding_provider check is injected from finecorpus.embedding (F-04):
    finecorpus.config must not import finecorpus.embedding — both sit at the
    same import-linter layer tier (C-5 layers contract in pyproject.toml).
    """
    from finecorpus.config.loader import load_config
    from finecorpus.config.preflight import run_preflight
    from finecorpus.embedding.preflight_check import make_embedding_check

    try:
        config = load_config(args.config)
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: Could not load config — {exc}", file=sys.stderr)
        return 1

    report = run_preflight(config, extra_checks=[make_embedding_check()])
    print(str(report))

    return 1 if report.has_failures else 0


def _get_control_session() -> Any:
    """Return a (engine, session) pair for the control-plane DB.

    Raises SystemExit(1) when no DSN is configured or the DB is unreachable.
    Caller is responsible for closing the session and disposing the engine.
    """
    try:
        from finecorpus.config.loader import load_config

        config = load_config("corpus.yaml")
        dsn = config.storage.postgres.url
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: Could not load config — {exc}", file=sys.stderr)
        sys.exit(1)

    if dsn is None:
        print(
            "ERROR: No control-plane DSN configured (storage.postgres.url). "
            "Set FINECORPUS__STORAGE__POSTGRES__URL or configure corpus.yaml.",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        from sqlalchemy.orm import Session

        from finecorpus.control.metadata import create_engine, create_tables

        engine = create_engine(dsn)
        create_tables(engine)
        session = Session(engine)
        return engine, session
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: Could not connect to control-plane DB — {exc}", file=sys.stderr)
        sys.exit(1)


def _cmd_jobs_enqueue(args: argparse.Namespace) -> int:
    """Wire corpus jobs enqueue → pipeline.jobs.enqueue_job."""
    import json as _json

    from finecorpus.pipeline.jobs import enqueue_job

    payload: dict[str, Any] = {}
    if getattr(args, "source", None):
        payload["source_dir"] = args.source
    if getattr(args, "artifacts", None):
        payload["artifacts_root"] = args.artifacts
    if getattr(args, "run_id", None):
        payload["run_id"] = args.run_id
    if getattr(args, "payload_json", None):
        try:
            payload.update(_json.loads(args.payload_json))
        except Exception as exc:  # noqa: BLE001
            print(f"ERROR: Could not parse --payload JSON — {exc}", file=sys.stderr)
            return 2

    engine, session = _get_control_session()
    try:
        job = enqueue_job(
            session=session,
            kb_id=args.kb,
            workspace_id=args.workspace,
            job_type=args.job_type,
            payload=payload,
            dedupe_key=getattr(args, "dedupe_key", None),
            priority=getattr(args, "priority", 0),
        )
        print(f"Enqueued job: {job.job_id}")
        print(f"  type     : {job.job_type}")
        print(f"  state    : {job.state}")
        print(f"  kb_id    : {job.kb_id}")
        print(f"  priority : {job.priority}")
        return 0
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        session.close()
        engine.dispose()


def _cmd_jobs_list(args: argparse.Namespace) -> int:
    """Wire corpus jobs list → pipeline.jobs.list_jobs."""
    from finecorpus.pipeline.jobs import list_jobs

    engine, session = _get_control_session()
    try:
        jobs = list_jobs(
            session=session,
            kb_id=getattr(args, "kb", None),
            state=getattr(args, "state", None),
            limit=getattr(args, "limit", 50),
        )
        if not jobs:
            print("No jobs found.")
            return 0
        print(f"{'JOB_ID':<34} {'TYPE':<22} {'STATE':<16} {'KB_ID':<20} CREATED")
        print("-" * 110)
        for job in jobs:
            created = job.created_at.strftime("%Y-%m-%d %H:%M:%S") if job.created_at else "?"
            print(f"{job.job_id:<34} {job.job_type:<22} {job.state:<16} {job.kb_id:<20} {created}")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        session.close()
        engine.dispose()


def _cmd_jobs_status(args: argparse.Namespace) -> int:
    """Wire corpus jobs status → pipeline.jobs.get_job."""
    import json as _json

    from finecorpus.pipeline.jobs import get_job

    engine, session = _get_control_session()
    try:
        job = get_job(session=session, job_id=args.job_id)
        if job is None:
            print(f"ERROR: job {args.job_id!r} not found.", file=sys.stderr)
            return 1
        print(f"Job: {job.job_id}")
        print(f"  type          : {job.job_type}")
        print(f"  state         : {job.state}")
        print(f"  kb_id         : {job.kb_id}")
        print(f"  workspace_id  : {job.workspace_id}")
        print(f"  priority      : {job.priority}")
        print(f"  attempt       : {job.attempt}")
        print(f"  cost_accrued  : ${job.cost_accrued_usd or 0:.6f}")
        print(f"  claimed_by    : {job.claimed_by}")
        print(f"  created_at    : {job.created_at}")
        print(f"  started_at    : {job.started_at}")
        print(f"  finished_at   : {job.finished_at}")
        if job.error_msg:
            print(f"  error         : {job.error_msg}")
        if job.checkpoint:
            print(f"  checkpoint    : {_json.dumps(job.checkpoint, indent=4)}")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        session.close()
        engine.dispose()


def _cmd_jobs_resume(args: argparse.Namespace) -> int:
    """Wire corpus jobs resume → pipeline.jobs.resume_job."""
    from finecorpus.pipeline.jobs import resume_job

    engine, session = _get_control_session()
    try:
        job = resume_job(session=session, job_id=args.job_id)
        print(f"Resumed job {job.job_id}: state → {job.state}")
        return 0
    except KeyError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        session.close()
        engine.dispose()


def _cmd_jobs_cancel(args: argparse.Namespace) -> int:
    """Wire corpus jobs cancel → pipeline.jobs.cancel_job."""
    from finecorpus.pipeline.jobs import cancel_job

    engine, session = _get_control_session()
    try:
        job = cancel_job(session=session, job_id=args.job_id)
        print(f"Cancelled job {job.job_id}: state → {job.state}")
        return 0
    except KeyError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        session.close()
        engine.dispose()


def _cmd_reindex(args: argparse.Namespace) -> int:
    """Wire corpus reindex <kb_id> [--full] → pipeline.reindex.trigger_manual_reindex."""
    from finecorpus.pipeline.reindex import trigger_manual_reindex

    engine, session = _get_control_session()
    try:
        # Infer workspace_id from the most recent job for this KB
        from finecorpus.pipeline.jobs import list_jobs

        jobs = list_jobs(session=session, kb_id=args.kb_id, limit=1)
        workspace_id = jobs[0].workspace_id if jobs else args.kb_id

        info = trigger_manual_reindex(
            session=session,
            kb_id=args.kb_id,
            workspace_id=workspace_id,
            full=getattr(args, "full", False),
        )
        print(f"Reindex enqueued: {info.job_id}")
        print(f"  type       : {info.job_type}")
        print(f"  trigger    : {info.trigger_type}")
        print(f"  kb_id      : {info.kb_id}")
        print(f"  coalesced  : {info.coalesced}")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        session.close()
        engine.dispose()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="corpus",
        description="Read The Fine Corpus — corpus management CLI",
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    # --- init subcommand (M-103, §4.6 first-run provider prompt) ---
    init_parser = sub.add_parser(
        "init",
        help="First-run provider setup — writes corpus.yaml and runs preflight (§4.6)",
    )
    init_parser.add_argument(
        "--config",
        metavar="FILE",
        default=None,
        help="Destination path for corpus.yaml (default: ./corpus.yaml)",
    )
    init_parser.add_argument(
        "--example",
        metavar="FILE",
        default=None,
        help="Path to corpus.example.yaml template (auto-located if not set)",
    )
    init_parser.add_argument(
        "--provider",
        metavar="PROVIDER",
        choices=["openai", "ollama", "both"],
        default=None,
        help="Embedding provider: openai | ollama | both",
    )
    init_parser.add_argument(
        "--ollama-base-url",
        dest="ollama_base_url",
        metavar="URL",
        default=None,
        help="Ollama HTTP endpoint (default: http://localhost:11434)",
    )
    init_parser.add_argument(
        "--ollama-model",
        dest="ollama_model",
        metavar="MODEL",
        default=None,
        help="Ollama model name (default: nomic-embed-text)",
    )
    init_parser.add_argument(
        "--openai-model",
        dest="openai_model",
        metavar="MODEL",
        default=None,
        help=(
            "OpenAI model identifier "
            "(default: text-embedding-3-small; "
            "valid: text-embedding-3-small, text-embedding-3-large, text-embedding-ada-002)"
        ),
    )
    init_parser.add_argument(
        "--non-interactive",
        dest="non_interactive",
        action="store_true",
        default=False,
        help=(
            "Require all settings from flags; raise an error if any mandatory "
            "flag is missing rather than prompting.  --provider is required."
        ),
    )

    # --- pipeline subcommand ---
    pipeline_parser = sub.add_parser("pipeline", help="Pipeline operations")
    pipeline_sub = pipeline_parser.add_subparsers(dest="pipeline_command", metavar="<subcommand>")

    # pipeline sweep (Phase 5 §9.3, M-046..051)
    sweep_parser = pipeline_sub.add_parser(
        "sweep",
        help=(
            "Run a configuration sweep for a knowledge base (§9.3). "
            "Estimates cost (M-047), gates on threshold (M-048), samples corpus (M-046), "
            "evaluates candidates, and prints the ranked table (§19 crit 1)."
        ),
    )
    sweep_parser.add_argument(
        "kb_id",
        metavar="KB_ID",
        help="Knowledge-base identifier to sweep.",
    )
    sweep_parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        default=False,
        dest="yes",
        help=(
            "Skip the sweep cost confirmation prompt (M-048). "
            "Useful for non-interactive or scripted invocations."
        ),
    )
    sweep_parser.add_argument(
        "--config",
        metavar="FILE",
        default=None,
        help="Path to corpus.yaml (default: ./corpus.yaml)",
    )

    run_parser = pipeline_sub.add_parser("run", help="Run the ingestion pipeline end-to-end")
    run_parser.add_argument(
        "--source",
        required=True,
        metavar="DIR",
        help="Source directory to collect documents from",
    )
    run_parser.add_argument(
        "--artifacts",
        required=True,
        metavar="DIR",
        help="Root directory for pipeline artifacts",
    )
    run_parser.add_argument(
        "--run-id",
        required=True,
        dest="run_id",
        metavar="ID",
        help="Stable run identifier (no defaults generated — determinism discipline)",
    )
    run_parser.add_argument(
        "--workspace",
        required=True,
        metavar="WORKSPACE_ID",
        help="Workspace identity (ULID or human-readable id)",
    )
    run_parser.add_argument(
        "--kb",
        required=True,
        metavar="KB_ID",
        help="Knowledge base identity (ULID or human-readable id)",
    )
    run_parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        default=False,
        dest="yes",
        help=(
            "Skip the pre-Build cost gate confirmation prompt. Useful in non-interactive scripts."
        ),
    )

    # --- preview subcommand (M-038) ---
    preview_parser = sub.add_parser(
        "preview",
        help=(
            "Preview chunks from the plan artifact without indexing (M-038 dry-run). "
            "Runs Tier 1 + chunking + Tier 2 augmentation but skips embedding and Qdrant upsert."
        ),
    )
    preview_parser.add_argument(
        "--artifacts",
        required=True,
        metavar="DIR",
        help="Root directory for pipeline artifacts (same as used with 'corpus pipeline run')",
    )
    preview_parser.add_argument(
        "--run-id",
        required=True,
        dest="run_id",
        metavar="ID",
        help="Pipeline run ID containing plan + decompose artifacts",
    )
    preview_parser.add_argument(
        "--class",
        dest="class_filter",
        metavar="CLASS",
        default=None,
        help=(
            "Filter preview to a specific segment class "
            "(e.g. prose, table, code). Default: all classes."
        ),
    )
    preview_parser.add_argument(
        "--samples",
        dest="samples",
        type=int,
        default=5,
        metavar="N",
        help="Number of sample chunks to display (default: 5)",
    )

    # --- preflight subcommand ---
    preflight_parser = sub.add_parser("preflight", help="Run configuration preflight checks")
    preflight_parser.add_argument(
        "--config",
        required=True,
        metavar="FILE",
        help="Path to corpus.yaml configuration file",
    )

    # --- config subcommand (Phase 3: export/import/diff) ---
    config_parser = sub.add_parser(
        "config",
        help="Ingestion config management — export, import, diff (M-026, M-071)",
    )
    config_sub = config_parser.add_subparsers(dest="config_command", metavar="<subcommand>")

    export_parser = config_sub.add_parser(
        "export",
        help="Export an IngestionConfig artifact as canonical JSON (secret-scanned, M-071)",
    )
    export_parser.add_argument(
        "--input",
        required=True,
        metavar="FILE",
        help="Path to the source IngestionConfig JSON (e.g. artifact from pipeline run)",
    )
    export_parser.add_argument(
        "--output",
        required=True,
        metavar="FILE",
        help="Destination path for the exported canonical JSON",
    )

    import_parser = config_sub.add_parser(
        "import",
        help=(
            "Import and validate an IngestionConfig JSON — "
            "version-checks and re-derives config_version for tamper detection (M-015)"
        ),
    )
    import_parser.add_argument(
        "--input",
        required=True,
        metavar="FILE",
        help="Path to the IngestionConfig JSON to import",
    )

    diff_parser = config_sub.add_parser(
        "diff",
        help="Show structural differences between two IngestionConfig JSON files",
    )
    diff_parser.add_argument(
        "a",
        metavar="FILE_A",
        help="First config file",
    )
    diff_parser.add_argument(
        "b",
        metavar="FILE_B",
        help="Second config file",
    )

    # --- plan subcommand (Phase 3: run Plan stage over existing artifacts) ---
    plan_parser = sub.add_parser(
        "plan",
        help=(
            "Run the Plan stage over existing pipeline artifacts — "
            "produces per-class routing table, language warnings, and exclusion summary"
        ),
    )
    plan_parser.add_argument(
        "--artifacts",
        required=True,
        metavar="DIR",
        help="Root directory for pipeline artifacts (same as used with 'corpus pipeline run')",
    )
    plan_parser.add_argument(
        "--run-id",
        required=True,
        dest="run_id",
        metavar="ID",
        help="Pipeline run ID whose decompose artifact to plan from",
    )
    plan_parser.add_argument(
        "--class-descriptions",
        dest="class_descriptions",
        metavar="FILE",
        default=None,
        help=(
            "Optional YAML file with per-class descriptions "
            "(see finecorpus.pipeline.plan.class_descriptions for schema)"
        ),
    )

    # --- jobs subcommand (Phase 4: queue management) ---
    jobs_parser = sub.add_parser(
        "jobs",
        help="Job queue management — enqueue, list, status, resume, cancel (§6.6)",
    )
    jobs_sub = jobs_parser.add_subparsers(dest="jobs_command", metavar="<subcommand>")

    # jobs enqueue
    enqueue_parser = jobs_sub.add_parser("enqueue", help="Enqueue a new job")
    enqueue_parser.add_argument("--kb", required=True, metavar="KB_ID", help="Knowledge base ID")
    enqueue_parser.add_argument(
        "--workspace", required=True, metavar="WORKSPACE_ID", help="Workspace ID"
    )
    enqueue_parser.add_argument(
        "--type",
        required=True,
        dest="job_type",
        metavar="TYPE",
        choices=["ingest", "reindex_full", "reindex_incremental", "restore", "purge"],
        help="Job type",
    )
    enqueue_parser.add_argument(
        "--source",
        default=None,
        metavar="DIR",
        help="Source directory (for ingest jobs)",
    )
    enqueue_parser.add_argument(
        "--artifacts",
        default=None,
        metavar="DIR",
        help="Artifacts root directory",
    )
    enqueue_parser.add_argument(
        "--run-id",
        dest="run_id",
        default=None,
        metavar="ID",
        help="Pipeline run ID",
    )
    enqueue_parser.add_argument(
        "--dedupe-key",
        dest="dedupe_key",
        default=None,
        metavar="KEY",
        help="Deduplication key (one active job per KB+key)",
    )
    enqueue_parser.add_argument(
        "--priority",
        type=int,
        default=0,
        metavar="N",
        help="Job priority (higher = higher priority; default 0)",
    )
    enqueue_parser.add_argument(
        "--payload",
        dest="payload_json",
        default=None,
        metavar="JSON",
        help="Additional payload as a JSON object string",
    )

    # jobs list
    list_parser = jobs_sub.add_parser("list", help="List jobs")
    list_parser.add_argument("--kb", default=None, metavar="KB_ID", help="Filter by KB ID")
    list_parser.add_argument("--state", default=None, metavar="STATE", help="Filter by state")
    list_parser.add_argument(
        "--limit", type=int, default=50, metavar="N", help="Maximum rows (default 50)"
    )

    # jobs status
    status_parser = jobs_sub.add_parser("status", help="Show job details")
    status_parser.add_argument("job_id", metavar="JOB_ID", help="Job ID")

    # jobs resume
    resume_parser = jobs_sub.add_parser("resume", help="Resume a paused_budget job")
    resume_parser.add_argument("job_id", metavar="JOB_ID", help="Job ID")

    # jobs cancel
    cancel_parser = jobs_sub.add_parser("cancel", help="Cancel a queued or paused job")
    cancel_parser.add_argument("job_id", metavar="JOB_ID", help="Job ID")

    # --- kb subcommand (Phase 4: deletion, purge, snapshots) ---
    # Self-contained additive block to make rebase with jobs sibling trivial.
    kb_parser = sub.add_parser(
        "kb",
        help="Knowledge-base document lifecycle — delete, purge, snapshot list (Phase 4)",
    )
    kb_sub = kb_parser.add_subparsers(dest="kb_command", metavar="<subcommand>")

    # corpus kb delete-doc <kb_id> <doc_id>
    del_doc_parser = kb_sub.add_parser(
        "delete-doc",
        help=(
            "Remove a document from service (live + N-1 collections). "
            "M-089: 'deleted from service' — cold snapshots age out per "
            "snapshot_retention_period_days."
        ),
    )
    del_doc_parser.add_argument("kb_id", metavar="KB_ID", help="Knowledge-base identifier")
    del_doc_parser.add_argument("doc_id", metavar="DOC_ID", help="Document identifier to delete")
    del_doc_parser.add_argument(
        "--config",
        metavar="FILE",
        default=None,
        help="Path to corpus.yaml (default: ./corpus.yaml)",
    )
    del_doc_parser.add_argument(
        "--actor",
        metavar="ACTOR",
        default="cli",
        help="Actor performing the deletion (logged in audit trail)",
    )
    del_doc_parser.add_argument(
        "--artifacts",
        metavar="DIR",
        default=None,
        help="Artifacts root for derived-artifact removal (optional)",
    )

    # corpus kb purge-doc <kb_id> <doc_id> --confirm
    purge_doc_parser = kb_sub.add_parser(
        "purge-doc",
        help=(
            "Purge a document from ALL copies including cold snapshots (D-05: immediate). "
            "Requires --confirm. M-089: 'purged from all copies'."
        ),
    )
    purge_doc_parser.add_argument("kb_id", metavar="KB_ID", help="Knowledge-base identifier")
    purge_doc_parser.add_argument("doc_id", metavar="DOC_ID", help="Document identifier to purge")
    purge_doc_parser.add_argument(
        "--confirm",
        action="store_true",
        default=False,
        help=(
            "REQUIRED. Confirms that you understand this destroys ALL copies "
            "including cold snapshots immediately (right-to-erasure semantics)."
        ),
    )
    purge_doc_parser.add_argument(
        "--config",
        metavar="FILE",
        default=None,
        help="Path to corpus.yaml (default: ./corpus.yaml)",
    )
    purge_doc_parser.add_argument(
        "--actor",
        metavar="ACTOR",
        default="cli",
        help="Actor performing the purge (logged in audit trail)",
    )
    purge_doc_parser.add_argument(
        "--artifacts",
        metavar="DIR",
        default=None,
        help="Artifacts root for derived-artifact removal (optional)",
    )

    # corpus kb snapshots <kb_id>
    snaps_parser = kb_sub.add_parser(
        "snapshots",
        help="List cold snapshots for a knowledge base's collections.",
    )
    snaps_parser.add_argument("kb_id", metavar="KB_ID", help="Knowledge-base identifier")
    snaps_parser.add_argument(
        "--config",
        metavar="FILE",
        default=None,
        help="Path to corpus.yaml (default: ./corpus.yaml)",
    )

    # corpus kb status <kb_id>  (M-096)
    status_parser = kb_sub.add_parser(
        "status",
        help=(
            "Show hot/cold version inventory + estimated memory footprint per hot copy (M-096). "
            "Includes the §10.2 advisory about hot_retention_count."
        ),
    )
    status_parser.add_argument("kb_id", metavar="KB_ID", help="Knowledge-base identifier")
    status_parser.add_argument(
        "--config",
        metavar="FILE",
        default=None,
        help="Path to corpus.yaml (default: ./corpus.yaml)",
    )

    # corpus kb export <kb_id> --out DIR  (M-090)
    export_parser = kb_sub.add_parser(
        "export",
        help=(
            "Export a knowledge base to a portable bundle directory (M-090). "
            "Includes config, class descriptions, findings/exclusion reports, "
            "chunks (scrolled from live collection), eval-set stub, and manifest."
        ),
    )
    export_parser.add_argument("kb_id", metavar="KB_ID", help="Knowledge-base identifier")
    export_parser.add_argument(
        "--out",
        required=True,
        metavar="DIR",
        help="Output directory for the export bundle (created if it does not exist).",
    )
    export_parser.add_argument(
        "--config",
        metavar="FILE",
        default=None,
        help="Path to corpus.yaml (default: ./corpus.yaml)",
    )
    export_parser.add_argument(
        "--artifacts",
        metavar="DIR",
        default=None,
        help="Artifacts root for config/report files (optional).",
    )
    export_parser.add_argument(
        "--run-id",
        dest="run_id",
        metavar="ID",
        default=None,
        help="Pipeline run ID for artifact lookup (required when --artifacts is set).",
    )

    # --- report subcommand (Phase 2: findings + exclusion reports) ---
    report_parser = sub.add_parser(
        "report",
        help="Generate findings and exclusion reports from pipeline artifacts (§6.2, §7.5)",
    )
    report_parser.add_argument(
        "--artifacts",
        required=True,
        metavar="DIR",
        help="Root directory for pipeline artifacts (same as used with 'corpus pipeline run')",
    )
    report_parser.add_argument(
        "--run-id",
        required=True,
        dest="run_id",
        metavar="ID",
        help="Pipeline run ID to report on",
    )
    report_parser.add_argument(
        "--format",
        dest="format",
        choices=["md", "json", "both"],
        default="both",
        help="Output format: md (human-readable), json (machine-readable), both (default)",
    )
    report_parser.add_argument(
        "--out",
        dest="out",
        metavar="DIR",
        default=None,
        help=("Directory to write report files to. If omitted, reports are printed to stdout."),
    )

    # --- reindex subcommand (Phase 4: manual reindex trigger) ---
    reindex_parser = sub.add_parser(
        "reindex",
        help="Enqueue a manual reindex job for a knowledge base (M-052/M-053)",
    )
    reindex_parser.add_argument(
        "kb_id",
        metavar="KB_ID",
        help="Knowledge-base identifier to reindex",
    )
    reindex_parser.add_argument(
        "--full",
        action="store_true",
        default=False,
        help=(
            "Force a full reindex (reindex_full) instead of incremental (reindex_incremental). "
            "Required when config_version has changed (M-053)."
        ),
    )

    return parser


def main() -> None:
    """Entry point for the corpus CLI."""
    parser = _build_parser()
    args = parser.parse_args()

    if args.command == "init":
        sys.exit(_cmd_init(args))
    elif args.command == "preview":
        sys.exit(_cmd_preview(args))
    elif args.command == "pipeline":
        if args.pipeline_command == "run":
            sys.exit(_cmd_pipeline_run(args))
        elif args.pipeline_command == "sweep":
            sys.exit(_cmd_pipeline_sweep(args))
        else:
            parser.print_help()
            sys.exit(1)
    elif args.command == "preflight":
        sys.exit(_cmd_preflight(args))
    elif args.command == "config":
        if args.config_command == "export":
            sys.exit(_cmd_config_export(args))
        elif args.config_command == "import":
            sys.exit(_cmd_config_import(args))
        elif args.config_command == "diff":
            sys.exit(_cmd_config_diff(args))
        else:
            parser.print_help()
            sys.exit(1)
    elif args.command == "plan":
        sys.exit(_cmd_plan(args))
    elif args.command == "kb":
        if args.kb_command == "delete-doc":
            sys.exit(_cmd_kb_delete_doc(args))
        elif args.kb_command == "purge-doc":
            sys.exit(_cmd_kb_purge_doc(args))
        elif args.kb_command == "snapshots":
            sys.exit(_cmd_kb_snapshots(args))
        elif args.kb_command == "status":
            sys.exit(_cmd_kb_status(args))
        elif args.kb_command == "export":
            sys.exit(_cmd_kb_export(args))
        else:
            parser.print_help()
            sys.exit(1)
    elif args.command == "report":
        sys.exit(_cmd_report(args))
    elif args.command == "reindex":
        sys.exit(_cmd_reindex(args))
    elif args.command == "jobs":
        if args.jobs_command == "enqueue":
            sys.exit(_cmd_jobs_enqueue(args))
        elif args.jobs_command == "list":
            sys.exit(_cmd_jobs_list(args))
        elif args.jobs_command == "status":
            sys.exit(_cmd_jobs_status(args))
        elif args.jobs_command == "resume":
            sys.exit(_cmd_jobs_resume(args))
        elif args.jobs_command == "cancel":
            sys.exit(_cmd_jobs_cancel(args))
        else:
            parser.print_help()
            sys.exit(1)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
