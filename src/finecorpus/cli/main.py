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
    """Wire corpus pipeline run → finecorpus.pipeline.run_pipeline."""
    from finecorpus.contracts.versions import ContractVersionError
    from finecorpus.pipeline import run_pipeline
    from finecorpus.pipeline.artifact_store import ArtifactStoreError
    from finecorpus.pipeline.stage import StageError

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
    elif args.command == "report":
        sys.exit(_cmd_report(args))
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
