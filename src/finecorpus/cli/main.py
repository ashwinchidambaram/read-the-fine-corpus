"""corpus CLI — thin wiring over the finecorpus library.

Entry point registered in pyproject.toml:
  [project.scripts]
  corpus = "finecorpus.cli.main:main"

Commands:
  corpus init           -- first-run provider prompt (M-103, spec §4.6)
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
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
