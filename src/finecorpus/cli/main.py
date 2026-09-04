"""corpus CLI — thin wiring over the finecorpus library.

Entry point registered in pyproject.toml:
  [project.scripts]
  corpus = "finecorpus.cli.main:main"

Commands:
  corpus pipeline run   -- run_pipeline over a source directory
  corpus preflight      -- run_preflight over a config file

Logic stays in the library (constraint C-5).  This module is allowed to:
  - Parse arguments.
  - Call library functions.
  - Print results.
  - sys.exit() on error.

It is NOT allowed to contain business logic.
"""

from __future__ import annotations

import argparse
import json
import sys


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


def _cmd_preflight(args: argparse.Namespace) -> int:
    """Wire corpus preflight → finecorpus.config.preflight.run_preflight."""
    from finecorpus.config.loader import load_config
    from finecorpus.config.preflight import run_preflight

    try:
        config = load_config(args.config)
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: Could not load config — {exc}", file=sys.stderr)
        return 1

    report = run_preflight(config)
    print(json.dumps(report.to_dict(), indent=2))

    failed = any(r.status == "FAIL" for r in report.checks)
    return 1 if failed else 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="corpus",
        description="Read The Fine Corpus — corpus management CLI",
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>")

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

    return parser


def main() -> None:
    """Entry point for the corpus CLI."""
    parser = _build_parser()
    args = parser.parse_args()

    if args.command == "pipeline":
        if args.pipeline_command == "run":
            sys.exit(_cmd_pipeline_run(args))
        else:
            parser.print_help()
            sys.exit(1)
    elif args.command == "preflight":
        sys.exit(_cmd_preflight(args))
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
