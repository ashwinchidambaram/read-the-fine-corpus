"""First-run provider prompt for Read The Fine Corpus (M-103, spec §4.6).

Implements ``corpus init``: an interactive-capable but fully scriptable flow
that asks which embedding provider(s) to use, collects base settings, writes
``corpus.yaml`` from ``corpus.example.yaml``, then runs preflight to validate
the chosen provider before anything else.

Design rules (§4.6)
--------------------
- API keys MUST NOT be written to ``corpus.yaml``; they are printed as an
  ``export`` instruction so the operator adds them to the environment.
- ``--non-interactive`` mode requires that all mandatory flags are supplied;
  missing flags raise ``InitError`` immediately rather than prompting.
- Logic lives here; the CLI wires it thin (C-5 constraint).
- ``first_run_complete: true`` is stamped in the written config only after
  preflight passes; a failed preflight leaves the config on disk but does
  NOT set the flag (so the operator can re-run after fixing the issue).

Provider choices
----------------
- ``openai``  — cloud (OpenAI text-embedding-3-* family)
- ``ollama``  — local (Ollama with nomic-embed-text or bge-m3)
- ``both``    — write both sections; default is ``openai``

Public API
----------
:func:`run_init_flow` — executes the flow and returns an
:class:`InitResult` describing what happened.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO

# ---------------------------------------------------------------------------
# Result and error types
# ---------------------------------------------------------------------------


class InitError(Exception):
    """Raised when the init flow cannot proceed (missing flags, invalid input)."""


@dataclass
class InitResult:
    """Outcome of a completed init flow.

    Attributes
    ----------
    config_path:
        Absolute path of the ``corpus.yaml`` that was written.
    provider_choice:
        The provider(s) configured: ``"openai"``, ``"ollama"``, or ``"both"``.
    preflight_passed:
        ``True`` when every preflight check returned ``OK``, ``WARN``, or ``SKIPPED``.
    first_run_stamped:
        ``True`` when ``first_run_complete: true`` was written to the config.
    preflight_report:
        The string representation of the :class:`PreflightReport`.
    env_instructions:
        Lines to print to the operator instructing which env vars to export.
    """

    config_path: Path
    provider_choice: str
    preflight_passed: bool
    first_run_stamped: bool
    preflight_report: str
    env_instructions: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Known defaults matching corpus.example.yaml values
# ---------------------------------------------------------------------------

_DEFAULT_OLLAMA_BASE_URL = "http://localhost:11434"
_DEFAULT_OLLAMA_MODEL = "nomic-embed-text"
_DEFAULT_OPENAI_MODEL = "text-embedding-3-small"

_VALID_PROVIDERS = frozenset({"openai", "ollama", "both"})
_VALID_OPENAI_MODELS = frozenset(
    {
        "text-embedding-3-small",
        "text-embedding-3-large",
        "text-embedding-ada-002",
    }
)
_OPENAI_MODEL_DIMENSIONS: dict[str, int] = {
    "text-embedding-3-small": 1536,
    "text-embedding-3-large": 3072,
    "text-embedding-ada-002": 1536,
}


# ---------------------------------------------------------------------------
# YAML patch helpers
# ---------------------------------------------------------------------------


def _set_yaml_scalar(yaml_text: str, key: str, value: str | bool) -> str:
    """Replace the value of a scalar key in the YAML text.

    Only replaces simple ``key: value`` lines (no nesting handling needed
    because we target specific leaf keys in a controlled example file).

    For boolean ``True``, writes ``true``; for ``False``, writes ``false``.
    For strings, writes without quotes (valid YAML for simple strings).
    For None / empty string, writes an empty value (``key:``).
    """
    if isinstance(value, bool):
        str_value = "true" if value else "false"
    elif value is None or value == "":
        str_value = ""
    else:
        str_value = str(value)

    # Match: optional indent + key: <rest of same line only>
    # Use [^\S\n]* (space/tab but NOT newline) so the pattern never crosses
    # line boundaries when the key has an empty value followed by blank lines.
    pattern = re.compile(r"^(\s*" + re.escape(key) + r":)[^\S\n]*[^\n]*$", re.MULTILINE)
    if str_value == "":
        replacement = r"\1"
    else:
        replacement = r"\g<1> " + str_value
    return pattern.sub(replacement, yaml_text, count=1)


def _patch_config_for_provider(
    yaml_text: str,
    *,
    provider_choice: str,
    ollama_base_url: str,
    ollama_model: str,
    openai_model: str,
) -> str:
    """Patch the YAML template with chosen provider settings.

    Sets:
    - ``providers.embedding.default``
    - ``providers.local.endpoint`` (if ollama)
    - ``providers.local.model`` (if ollama)
    - ``providers.cloud.model`` (if openai)
    - ``platform.first_run_complete`` → false (set to true after preflight)
    """
    # Set the default provider
    if provider_choice == "both":
        default_val = "openai"
    else:
        default_val = provider_choice

    yaml_text = _set_yaml_scalar(yaml_text, "default", default_val)
    yaml_text = _set_yaml_scalar(yaml_text, "first_run_complete", False)

    # Patch local (ollama) settings
    if provider_choice in {"ollama", "both"}:
        yaml_text = _set_yaml_scalar(yaml_text, "endpoint", ollama_base_url)
        yaml_text = _patch_section_model(yaml_text, "local", ollama_model)

    # Patch cloud (openai) model
    if provider_choice in {"openai", "both"}:
        # We set the model key under the cloud section; since there are two
        # ``model:`` lines in the file (cloud and local), we do a targeted
        # replacement by locating the cloud section first.
        yaml_text = _patch_cloud_model(yaml_text, openai_model)

    return yaml_text


def _patch_section_model(yaml_text: str, section: str, model: str) -> str:
    """Replace the model value inside a named section (``cloud:`` or ``local:``).

    Locates the *section* marker and replaces only the first ``model:`` line
    after it, stopping at the next same-level section to avoid touching the
    wrong provider block.
    """
    section_start = re.compile(r"^\s*" + re.escape(section) + r"\s*:", re.MULTILINE)
    other_sections = {"cloud", "local"} - {section}
    other_pattern = re.compile(
        r"^\s*(?:" + "|".join(re.escape(s) for s in other_sections) + r")\s*:",
        re.MULTILINE,
    )

    lines = yaml_text.splitlines(keepends=True)
    in_section = False
    model_replaced = False
    result: list[str] = []
    for line in lines:
        if not model_replaced:
            if section_start.match(line):
                in_section = True
            elif in_section and other_pattern.match(line):
                # Exited our section without finding model
                in_section = False
            elif in_section and re.match(r"\s*model\s*:", line):
                indent = len(line) - len(line.lstrip())
                result.append(" " * indent + "model: " + model + "\n")
                model_replaced = True
                continue
        result.append(line)
    return "".join(result)


def _patch_cloud_model(yaml_text: str, model: str) -> str:
    """Replace the cloud provider model value."""
    return _patch_section_model(yaml_text, "cloud", model)


def _stamp_first_run_complete(yaml_text: str) -> str:
    """Set ``first_run_complete: true`` in the YAML text."""
    return _set_yaml_scalar(yaml_text, "first_run_complete", True)


# ---------------------------------------------------------------------------
# Prompt helpers
# ---------------------------------------------------------------------------


def _prompt(
    message: str,
    default: str | None = None,
    *,
    stdin: IO[str],
    stdout: IO[str],
) -> str:
    """Print *message* and return stripped input.  Uses *default* on empty input."""
    if default is not None:
        stdout.write(f"{message} [{default}]: ")
    else:
        stdout.write(f"{message}: ")
    stdout.flush()
    try:
        raw = stdin.readline()
    except (EOFError, OSError):
        raw = ""
    value = raw.strip()
    return value if value else (default or "")


def _prompt_provider(*, stdin: IO[str], stdout: IO[str]) -> str:
    """Prompt for provider choice; returns 'openai', 'ollama', or 'both'."""
    stdout.write(
        "\nWhich embedding provider(s) do you want to configure?\n"
        "  openai  — cloud provider (OpenAI text-embedding-3-*)\n"
        "  ollama  — local provider (Ollama, no internet required)\n"
        "  both    — configure both; default will be openai\n"
    )
    while True:
        choice = _prompt("Provider", "openai", stdin=stdin, stdout=stdout).lower().strip()
        if choice in _VALID_PROVIDERS:
            return choice
        stdout.write(f"  Invalid choice '{choice}'. Enter 'openai', 'ollama', or 'both'.\n")


def _prompt_ollama_url(*, stdin: IO[str], stdout: IO[str]) -> str:
    """Prompt for Ollama base URL."""
    return _prompt("Ollama base URL", _DEFAULT_OLLAMA_BASE_URL, stdin=stdin, stdout=stdout)


def _prompt_ollama_model(*, stdin: IO[str], stdout: IO[str]) -> str:
    """Prompt for Ollama model name."""
    stdout.write("  Available models: nomic-embed-text (768-dim), bge-m3 (1024-dim)\n")
    return _prompt("Ollama model", _DEFAULT_OLLAMA_MODEL, stdin=stdin, stdout=stdout)


def _prompt_openai_model(*, stdin: IO[str], stdout: IO[str]) -> str:
    """Prompt for OpenAI model; must be a known model."""
    stdout.write(
        "  Available models: text-embedding-3-small (1536-dim, default), "
        "text-embedding-3-large (3072-dim), text-embedding-ada-002 (1536-dim)\n"
    )
    while True:
        model = _prompt("OpenAI model", _DEFAULT_OPENAI_MODEL, stdin=stdin, stdout=stdout)
        if model in _VALID_OPENAI_MODELS:
            return model
        stdout.write(
            f"  Unknown model '{model}'. "
            "Choose text-embedding-3-small, text-embedding-3-large, or text-embedding-ada-002.\n"
        )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_init_flow(
    *,
    config_path: Path | None = None,
    example_path: Path | None = None,
    provider: str | None = None,
    ollama_base_url: str | None = None,
    ollama_model: str | None = None,
    openai_model: str | None = None,
    non_interactive: bool = False,
    run_preflight: bool = True,
    stdin: IO[str] | None = None,
    stdout: IO[str] | None = None,
) -> InitResult:
    """Execute the first-run provider prompt and write corpus.yaml.

    Parameters
    ----------
    config_path:
        Destination path for corpus.yaml.  Defaults to ``./corpus.yaml``.
    example_path:
        Path to the corpus.example.yaml template.  Defaults to the file
        located adjacent to config_path named ``corpus.example.yaml``.
        If not found there, falls back to the repo root.
    provider:
        Override for provider choice (``openai``, ``ollama``, or ``both``).
        Required when ``non_interactive=True``.
    ollama_base_url:
        Ollama HTTP endpoint override.  Prompted when provider is ollama/both
        and this is not supplied (unless non-interactive).
    ollama_model:
        Ollama model name override.
    openai_model:
        OpenAI model identifier override.
    non_interactive:
        When ``True``, all required settings must come from flags; any missing
        mandatory flag raises :exc:`InitError` rather than prompting.
    run_preflight:
        When ``True`` (default), runs preflight after writing the config and
        stamps ``first_run_complete: true`` only on success.  Set to ``False``
        in tests that want to verify config content only.
    stdin / stdout:
        Override streams for prompting (defaults to sys.stdin / sys.stdout).

    Returns
    -------
    InitResult

    Raises
    ------
    InitError
        In non-interactive mode if a required flag is missing, or if the
        provider value is invalid.
    FileNotFoundError
        If ``example_path`` cannot be located.
    """
    _stdin = stdin if stdin is not None else sys.stdin
    _stdout = stdout if stdout is not None else sys.stdout

    # Resolve paths
    cfg_path = Path(config_path) if config_path else Path("corpus.yaml")
    cfg_path = cfg_path.resolve()

    if example_path is not None:
        example = Path(example_path).resolve()
    else:
        # Look adjacent to config_path, then fall back to repo root
        candidate = cfg_path.parent / "corpus.example.yaml"
        if candidate.exists():
            example = candidate
        else:
            # Try the module's own repo root (two parents of src/finecorpus/)
            module_root = Path(__file__).resolve().parent.parent.parent.parent
            fallback = module_root / "corpus.example.yaml"
            if fallback.exists():
                example = fallback
            else:
                raise FileNotFoundError(
                    f"Cannot find corpus.example.yaml. Tried: {candidate}, {fallback}. "
                    "Supply --example or place corpus.example.yaml adjacent to corpus.yaml."
                )

    if not example.exists():
        raise FileNotFoundError(f"corpus.example.yaml not found at '{example}'.")

    # ---------------------------------------------------------------------------
    # Collect settings — interactive or from flags
    # ---------------------------------------------------------------------------

    if non_interactive:
        # Validate that all required settings are present
        if not provider:
            raise InitError(
                "--provider is required in --non-interactive mode. "
                "Choose 'openai', 'ollama', or 'both'."
            )
        if provider not in _VALID_PROVIDERS:
            raise InitError(
                f"Invalid --provider value '{provider}'. Choose 'openai', 'ollama', or 'both'."
            )
        chosen_provider = provider

        if chosen_provider in {"openai", "both"}:
            chosen_openai_model = openai_model or _DEFAULT_OPENAI_MODEL
            if chosen_openai_model not in _VALID_OPENAI_MODELS:
                raise InitError(
                    f"Invalid --openai-model '{chosen_openai_model}'. "
                    f"Valid choices: {sorted(_VALID_OPENAI_MODELS)}"
                )
        else:
            chosen_openai_model = _DEFAULT_OPENAI_MODEL

        if chosen_provider in {"ollama", "both"}:
            chosen_ollama_url = ollama_base_url or _DEFAULT_OLLAMA_BASE_URL
            chosen_ollama_model = ollama_model or _DEFAULT_OLLAMA_MODEL
        else:
            chosen_ollama_url = _DEFAULT_OLLAMA_BASE_URL
            chosen_ollama_model = _DEFAULT_OLLAMA_MODEL

    else:
        # Interactive prompts — flags override prompts when supplied
        _stdout.write(
            "\n"
            "=== corpus init — first-run provider setup (§4.6) ===\n"
            "\n"
            "This wizard writes corpus.yaml from corpus.example.yaml, then runs\n"
            "preflight to confirm the chosen provider is reachable.\n"
            "\n"
            "API keys are NEVER written to corpus.yaml.  You will be shown\n"
            "the export command to add the key to your environment.\n"
        )

        chosen_provider = provider or _prompt_provider(stdin=_stdin, stdout=_stdout)
        if chosen_provider not in _VALID_PROVIDERS:
            raise InitError(
                f"Invalid provider choice '{chosen_provider}'. "
                "Choose 'openai', 'ollama', or 'both'."
            )

        # Ollama settings
        if chosen_provider in {"ollama", "both"}:
            if ollama_base_url is None:
                chosen_ollama_url = _prompt_ollama_url(stdin=_stdin, stdout=_stdout)
            else:
                chosen_ollama_url = ollama_base_url
            if ollama_model is None:
                chosen_ollama_model = _prompt_ollama_model(stdin=_stdin, stdout=_stdout)
            else:
                chosen_ollama_model = ollama_model
        else:
            chosen_ollama_url = _DEFAULT_OLLAMA_BASE_URL
            chosen_ollama_model = _DEFAULT_OLLAMA_MODEL

        # OpenAI settings
        if chosen_provider in {"openai", "both"}:
            if openai_model is None:
                chosen_openai_model = _prompt_openai_model(stdin=_stdin, stdout=_stdout)
            else:
                chosen_openai_model = openai_model
                if chosen_openai_model not in _VALID_OPENAI_MODELS:
                    raise InitError(
                        f"Invalid --openai-model '{chosen_openai_model}'. "
                        f"Valid choices: {sorted(_VALID_OPENAI_MODELS)}"
                    )
        else:
            chosen_openai_model = _DEFAULT_OPENAI_MODEL

    # ---------------------------------------------------------------------------
    # API key instructions — NEVER written to the file
    # ---------------------------------------------------------------------------

    env_instructions: list[str] = []
    if chosen_provider in {"openai", "both"}:
        env_instructions.append(
            "  export OPENAI_API_KEY=<your-key>  "
            "# Required for OpenAI provider — NEVER written to corpus.yaml"
        )

    # ---------------------------------------------------------------------------
    # Write corpus.yaml
    # ---------------------------------------------------------------------------

    yaml_text = example.read_text(encoding="utf-8")
    yaml_text = _patch_config_for_provider(
        yaml_text,
        provider_choice=chosen_provider,
        ollama_base_url=chosen_ollama_url,
        ollama_model=chosen_ollama_model,
        openai_model=chosen_openai_model,
    )

    cfg_path.write_text(yaml_text, encoding="utf-8")
    _stdout.write(f"\nWritten: {cfg_path}\n")

    if env_instructions:
        _stdout.write("\nAdd your API key to the environment (never write it to corpus.yaml):\n")
        for line in env_instructions:
            _stdout.write(line + "\n")

    # ---------------------------------------------------------------------------
    # Run preflight
    # ---------------------------------------------------------------------------

    preflight_passed = False
    preflight_report_str = ""
    first_run_stamped = False

    if run_preflight:
        _stdout.write("\nRunning preflight checks...\n")
        try:
            from finecorpus.config.loader import load_config
            from finecorpus.config.preflight import run_preflight as _run_preflight
            from finecorpus.embedding.preflight_check import make_embedding_check

            config = load_config(cfg_path)
            report = _run_preflight(config, extra_checks=[make_embedding_check()])
            preflight_report_str = str(report)
            _stdout.write(preflight_report_str + "\n")
            preflight_passed = report.passed

            if preflight_passed:
                # Stamp first_run_complete: true in the file
                current = cfg_path.read_text(encoding="utf-8")
                stamped = _stamp_first_run_complete(current)
                cfg_path.write_text(stamped, encoding="utf-8")
                first_run_stamped = True
                _stdout.write(
                    "\nPreflight passed — corpus.yaml updated with first_run_complete: true.\n"
                )
            else:
                _stdout.write(
                    "\nPreflight FAILED — fix the issues above, then re-run "
                    "`corpus init` or `corpus preflight --config corpus.yaml`.\n"
                    "corpus.yaml has been written but first_run_complete is NOT set.\n"
                )

        except Exception as exc:
            preflight_report_str = f"Preflight error: {exc}"
            _stdout.write(f"\n{preflight_report_str}\n")

    return InitResult(
        config_path=cfg_path,
        provider_choice=chosen_provider,
        preflight_passed=preflight_passed,
        first_run_stamped=first_run_stamped,
        preflight_report=preflight_report_str,
        env_instructions=env_instructions,
    )
