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


def _cmd_pipeline_run(args: argparse.Namespace) -> int:
    """Wire corpus pipeline run → finecorpus.pipeline.run_pipeline.

    Phase 3 addition: cost gate.  Before the Build stage executes, loads the
    Plan artifact (IngestionConfig + SegmentSetBatch), calls
    ``estimate_ingestion_cost``, prints the estimate, and requires ``--yes``
    or interactive confirmation to proceed.
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
            # Interactive confirmation (skip if --yes was passed)
            try:
                answer = input("Proceed with ingestion? [y/N] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print("\nAborted.", file=sys.stderr)
                return 1
            if answer not in ("y", "yes"):
                print("Ingestion cancelled by user.", file=sys.stderr)
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
    """Wire corpus config diff → finecorpus.pipeline.plan.config_io.diff_configs."""
    import json as _json

    from finecorpus.pipeline.plan.config_io import ConfigImportError, diff_configs, import_config

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
    elif args.command == "report":
        sys.exit(_cmd_report(args))
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
