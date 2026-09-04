"""Configuration loader for Read The Fine Corpus.

Implements the three-layer precedence stack defined in
``docs/configuration/reference.md §1``:

    env var  >  corpus.yaml  >  built-in default

Usage::

    from finecorpus.config import load_config

    cfg = load_config()                 # reads corpus.yaml from cwd
    cfg = load_config("/path/corpus.yaml")

Environment variable convention (§1 — Environment variable overrides):
  - Prefix:                ``FINECORPUS_``
  - Key-path separator:    ``__``  (double underscore)
  - Array index separator: ``__0__``, ``__1__``, etc.

Examples::

    FINECORPUS_PROVIDERS__EMBEDDING__CLOUD__MODEL=text-embedding-3-large
    FINECORPUS_STORAGE__POSTGRES__URL=postgresql://...
    FINECORPUS_BUDGETS__PER_KB_CAP_USD=50.00

Unknown keys in ``corpus.yaml`` are a hard error (naming the key) — spec §4.6
"nothing is hidden in code" requires every tunable to have a row in the
reference table; by the same logic, an unknown key in the file is almost
certainly a typo or a removed setting that should be caught loudly.

Secret validation (§14.2): the ``Config`` model validator fires during YAML
loading and rejects any value that matches a plaintext-credential pattern.
Env-var overrides bypass the YAML validator; they are trusted at the OS level.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ValidationError

from .models import _SKIP_MODEL_SECRET_CHECK, Config

ENV_PREFIX = "FINECORPUS_"
PATH_SEP = "__"

# ---------------------------------------------------------------------------
# Allowed top-level and nested keys — derived from the Config model schema.
# ---------------------------------------------------------------------------

# We validate unknown keys ourselves (before Pydantic sees the dict) so we can
# name the offending key in the error message rather than producing a generic
# Pydantic error.


def _allowed_keys_for_model(model_cls: type[BaseModel]) -> set[str]:
    """Return the set of field names accepted by *model_cls* (aliases included)."""
    names: set[str] = set()
    for field_name, field_info in model_cls.model_fields.items():
        names.add(field_name)
        if field_info.alias:
            names.add(str(field_info.alias))
    return names


def _validate_unknown_keys(
    data: dict[str, Any], model_cls: type[BaseModel], path: str = ""
) -> None:
    """Recursively check that every key in *data* is known to *model_cls*.

    Raises ``ValueError`` naming the first offending key path.
    """
    allowed = _allowed_keys_for_model(model_cls)
    for key in data:
        current_path = f"{path}.{key}" if path else key
        if key not in allowed:
            raise ValueError(
                f"Unknown configuration key: '{current_path}'. "
                f"Check docs/configuration/reference.md for valid keys. "
                f"Known top-level sections: "
                f"{sorted(_allowed_keys_for_model(Config))}."
            )
        # Recurse into nested dicts where we know the sub-model type
        if isinstance(data[key], dict):
            field_info = model_cls.model_fields.get(key)
            if field_info is not None:
                sub_annotation = field_info.annotation
                # Unwrap Optional[X] -> X
                sub_annotation = _unwrap_optional(sub_annotation)
                if sub_annotation is not None and _is_pydantic_model(sub_annotation):
                    _validate_unknown_keys(data[key], sub_annotation, current_path)


def _unwrap_optional(annotation: Any) -> Any:
    """Return the inner type of ``Optional[T]`` / ``T | None``, else *annotation*."""
    import types as _types

    # Python 3.10+ union: X | None
    if isinstance(annotation, _types.UnionType):
        args = annotation.__args__
        non_none = [a for a in args if a is not type(None)]
        return non_none[0] if len(non_none) == 1 else None
    # typing.Optional / typing.Union
    origin = getattr(annotation, "__origin__", None)
    if origin is _types.UnionType or str(origin) in ("<class 'typing.Union'>", "typing.Union"):
        args = getattr(annotation, "__args__", ())
        non_none = [a for a in args if a is not type(None)]
        return non_none[0] if len(non_none) == 1 else None
    return annotation


def _is_pydantic_model(cls: Any) -> bool:
    """Return ``True`` if *cls* is a Pydantic BaseModel subclass."""
    try:
        from pydantic import BaseModel

        return isinstance(cls, type) and issubclass(cls, BaseModel)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# YAML loading
# ---------------------------------------------------------------------------


def _load_yaml(path: Path) -> dict[str, Any]:
    """Read and parse *path* as YAML.  Returns an empty dict if the file is empty."""
    text = path.read_text(encoding="utf-8")
    result = yaml.safe_load(text)
    if result is None:
        return {}
    if not isinstance(result, dict):
        raise ValueError(
            f"corpus.yaml at '{path}' must be a YAML mapping at the top level, "
            f"got {type(result).__name__}."
        )
    return result


# ---------------------------------------------------------------------------
# Environment variable merging
# ---------------------------------------------------------------------------


def _env_overrides() -> dict[str, str]:
    """Collect all ``FINECORPUS_*`` environment variables."""
    return {k[len(ENV_PREFIX) :]: v for k, v in os.environ.items() if k.startswith(ENV_PREFIX)}


def _apply_env_to_dict(target: dict[str, Any], env_vars: dict[str, str]) -> None:
    """Merge *env_vars* into *target* using ``__``-separated path nesting.

    Example: key ``PROVIDERS__EMBEDDING__CLOUD__MODEL`` (after prefix strip)
    sets ``target["providers"]["embedding"]["cloud"]["model"]``.

    All keys are lowercased before lookup/insertion, matching YAML convention.
    """
    for raw_key, value in env_vars.items():
        parts = [p.lower() for p in raw_key.split(PATH_SEP)]
        _set_nested(target, parts, value)


def _set_nested(d: dict[str, Any], parts: list[str], value: str) -> None:
    """Set ``d[parts[0]][parts[1]]...[parts[-1]] = value``, creating dicts as needed."""
    for part in parts[:-1]:
        if part not in d or not isinstance(d[part], dict):
            d[part] = {}
        d = d[part]
    d[parts[-1]] = value


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_config(path: str | Path | None = None) -> Config:
    """Load configuration from *path* and apply ``FINECORPUS_*`` env overrides.

    Parameters
    ----------
    path:
        Path to the YAML config file.  Defaults to ``corpus.yaml`` in the
        current working directory.  Can also be set via the
        ``FINECORPUS_CONFIG_PATH`` environment variable (env var wins if both
        are supplied).

    Returns
    -------
    Config
        Fully validated configuration object.

    Raises
    ------
    FileNotFoundError
        If the config file does not exist.
    ValueError
        If the YAML contains unknown keys or plaintext credentials (§14.2).
    pydantic.ValidationError
        If any value fails type/range validation.
    """
    # Resolve file path: env var > explicit arg > default
    env_path = os.environ.get("FINECORPUS_CONFIG_PATH")
    if env_path:
        resolved = Path(env_path)
    elif path is not None:
        resolved = Path(path)
    else:
        resolved = Path("corpus.yaml")

    # Load YAML (or start from empty dict if file absent and not required)
    if resolved.exists():
        file_data: dict[str, Any] = _load_yaml(resolved)
    elif path is not None or env_path:
        # An explicit path was given — require it to exist
        raise FileNotFoundError(f"Config file not found: '{resolved}'")
    else:
        # Default corpus.yaml absent → pure-defaults mode (no error)
        file_data = {}

    # Validate unknown keys against the model schema
    _validate_unknown_keys(file_data, Config)

    # Check for plaintext secrets in the YAML data only (§14.2).
    # Env-var overrides are trusted at the OS level and bypass this check.
    from .models import _check_secrets_in_dict

    _check_secrets_in_dict(file_data, path="")

    # Apply env overrides AFTER the YAML secret check (env vars bypass schema check
    # intentionally — they are controlled by the operator, not the file author)
    env_vars = _env_overrides()
    # Remove CONFIG_PATH — it is meta, not a model field
    env_vars.pop("CONFIG_PATH", None)
    _apply_env_to_dict(file_data, env_vars)

    # Suppress the model-validator secret sweep: load_config already ran the
    # file-level scan before env-var merging, so the validator would fire on
    # env-sourced secrets (which are trusted at the OS level).
    token = _SKIP_MODEL_SECRET_CHECK.set(True)
    try:
        config = Config.model_validate(file_data)
    except ValidationError:
        raise
    finally:
        _SKIP_MODEL_SECRET_CHECK.reset(token)
    return config
