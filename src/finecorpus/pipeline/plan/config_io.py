"""Config export, import, and diff for IngestionConfig (M-026, M-071).

This module implements:
- export_config: write canonical JSON; D-21 denylist secret scan (hard-fail).
- import_config: version check + re-derive config_version for tamper detection.
- diff_configs: structural diff over canonical JSON.

Secret scan (D-21 ruling):
  The structural guarantee (extra="forbid", no secret-typed fields) is the primary
  defense. The denylist scan is a belt-and-braces runtime check (D-21 ruling) that
  catches secrets injected via free-text fields (e.g. a description containing an
  API key). Patterns:
    - "sk-"           (OpenAI-style secret keys)
    - "AKIA"          (AWS access key prefix)
    - "-----BEGIN"    (PEM private key / certificate)
    - "Bearer "       (authorization header value)
    High-entropy heuristic: any word-boundary token ≥ 20 chars with entropy ≥ 4.5 bits/char.

Import tamper detection:
  After parsing, re-derive config_version from the loaded config's build-affecting fields
  and compare against the stored config_version. Any mismatch (hand-edit, truncation,
  field corruption) is a hard error — the file is rejected as untrustworthy (M-015).
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from pathlib import Path
from typing import Any

from finecorpus.contracts.ingestion_config import IngestionConfig, to_canonical_json
from finecorpus.contracts.versions import SUPPORTED_INGESTION_CONFIG
from finecorpus.pipeline.plan.config_version import derive_config_version

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ConfigExportError(Exception):
    """Raised when export_config refuses to write a file (e.g. secret detected)."""


class ConfigImportError(Exception):
    """Raised when import_config rejects a file (version mismatch or tamper detected)."""


# ---------------------------------------------------------------------------
# Secret denylist patterns (D-21)
# ---------------------------------------------------------------------------

_DENYLIST_PATTERNS: list[re.Pattern[str]] = [
    # OpenAI-style secret keys: legacy sk-<48 alnum> AND sk-proj-...-... multi-segment.
    # Pattern: sk- followed by one or more hyphen-separated alnum segments, last ≥10 chars.
    re.compile(r"sk-(?:[A-Za-z0-9]+-)*[A-Za-z0-9]{10,}", re.ASCII),
    re.compile(r"AKIA[0-9A-Z]{16}", re.ASCII),  # AWS access key ID prefix
    re.compile(r"-----BEGIN\s+\w", re.ASCII),  # PEM private key / cert
    re.compile(r"Bearer\s+[A-Za-z0-9+/=._-]{8,}", re.ASCII),  # Authorization Bearer
]

# High-entropy heuristic: tokens this long or longer with Shannon entropy ≥ this threshold
# are flagged as potential secrets (D-21 belt-and-braces).
_ENTROPY_MIN_LEN = 20
_ENTROPY_THRESHOLD = 4.5


def _shannon_entropy(s: str) -> float:
    """Compute Shannon entropy (bits per character) of string s."""
    if not s:
        return 0.0
    counts = Counter(s)
    length = len(s)
    return -sum((c / length) * math.log2(c / length) for c in counts.values())


def _scan_for_secrets(text: str) -> list[str]:
    """Return a list of findings (descriptions) for any secret-like content found in text.

    Empty list means no secrets detected.
    """
    findings: list[str] = []

    # Denylist pattern scan.
    for pattern in _DENYLIST_PATTERNS:
        m = pattern.search(text)
        if m:
            # Redact the match in the finding message — don't echo the secret.
            findings.append(
                f"Denylist pattern {pattern.pattern!r} matched at position {m.start()}."
            )

    # High-entropy token scan: split on whitespace and common delimiters.
    tokens = re.split(r'[\s,;:"\'\[\]{}()\n\r\t]+', text)
    for token in tokens:
        if len(token) >= _ENTROPY_MIN_LEN:
            entropy = _shannon_entropy(token)
            if entropy >= _ENTROPY_THRESHOLD:
                findings.append(
                    f"High-entropy token (len={len(token)}, entropy={entropy:.2f} bits/char) "
                    f"at content (first 6 chars): {token[:6]}... — possible secret."
                )

    return findings


def _scan_config_for_secrets(config: IngestionConfig) -> list[str]:
    """Scan all string fields in the config for secret-like content.

    Walks the canonical JSON representation and checks every string value.
    Returns a list of findings (empty = clean).
    """
    canonical = to_canonical_json(config)
    return _scan_for_secrets(canonical)


# ---------------------------------------------------------------------------
# export_config
# ---------------------------------------------------------------------------


def export_config(config: IngestionConfig, path: Path) -> None:
    """Write the IngestionConfig as canonical JSON to path.

    Performs a D-21 denylist secret scan before writing. Any hit is a hard error —
    the file is NOT written.

    The canonical JSON format (sorted keys, no insignificant whitespace, UTF-8,
    trailing newline) is defined by to_canonical_json() in ingestion_config.py
    and is the same format used for config_version hashing.

    Args:
        config: The IngestionConfig to export.
        path: Destination file path. Parent directory must exist.

    Raises:
        ConfigExportError: If a secret-like value is detected in the config.
        OSError: If writing fails (disk full, permissions, etc.).
    """
    # D-21: denylist secret scan — belt-and-braces on top of structural guarantee.
    findings = _scan_config_for_secrets(config)
    if findings:
        raise ConfigExportError(
            "Export refused: secret-like content detected in config (D-21 denylist scan). "
            "The config must be secret-free by construction (§14.2, M-071). "
            f"Findings ({len(findings)}):\n" + "\n".join(f"  - {f}" for f in findings)
        )

    canonical = to_canonical_json(config)
    path.write_text(canonical, encoding="utf-8")


# ---------------------------------------------------------------------------
# import_config
# ---------------------------------------------------------------------------


def import_config(path: Path) -> IngestionConfig:
    """Load an IngestionConfig from a canonical JSON file.

    Performs:
    1. Version check via SUPPORTED_INGESTION_CONFIG range.
    2. Parse with extra="forbid" (unknown keys → hard error, M-071).
    3. Re-derive config_version from build-affecting fields and compare against
       the stored value. Any mismatch → hard error (tamper/hand-edit detection, M-015).

    Args:
        path: Path to the canonical JSON file.

    Returns:
        Validated IngestionConfig with a trusted config_version.

    Raises:
        ConfigImportError: On version mismatch, unsupported schema_version, or
            config_version tamper detection.
        pydantic.ValidationError: On schema validation failure (unknown keys, type errors).
        OSError: If reading fails.
    """
    from finecorpus.contracts.versions import ContractVersionError

    text = path.read_text(encoding="utf-8")
    raw: dict[str, Any] = json.loads(text)

    # 1. Version check.
    schema_version = raw.get("schema_version", "")
    try:
        SUPPORTED_INGESTION_CONFIG.check("ingestion_config", schema_version)
    except ContractVersionError as exc:
        raise ConfigImportError(
            f"Import rejected: {exc}. Upgrade the consumer or use a compatible config version."
        ) from exc

    # 2. Parse (extra="forbid" enforced by IngestionConfig.model_config).
    config = IngestionConfig.model_validate(raw)

    # 3. Re-derive config_version and compare (tamper detection).
    expected_version = derive_config_version(config)
    if config.config_version != expected_version:
        raise ConfigImportError(
            f"Import rejected: config_version mismatch. "
            f"Stored: {config.config_version!r}, "
            f"Re-derived: {expected_version!r}. "
            "This indicates the file was hand-edited or corrupted after export. "
            "A config with a mismatched config_version cannot be trusted for "
            "reindex decisions (M-015). Re-export from the original IngestionConfig."
        )

    return config


# ---------------------------------------------------------------------------
# diff_configs
# ---------------------------------------------------------------------------


def diff_configs(a: IngestionConfig, b: IngestionConfig) -> dict[str, Any]:
    """Compute a structural diff between two IngestionConfig objects.

    Diffs over the canonical JSON representation (sorted keys, compact) so the
    result is stable regardless of object construction order.

    Returns a dict with:
        "same": bool — True if configs are byte-identical in canonical form.
        "a_only": list[str] — keys present in a's canonical JSON but not b (top-level).
        "b_only": list[str] — keys present in b's canonical JSON but not a (top-level).
        "changed": dict[str, {"a": ..., "b": ...}] — top-level keys whose values differ.
        "config_version_changed": bool — convenience flag.

    Note: This is a shallow top-level diff. Deep structural diffing is deferred to
    a future phase when the UI diff preview (M-033) is implemented.

    Args:
        a: First IngestionConfig.
        b: Second IngestionConfig.

    Returns:
        Diff result dict.
    """
    a_data: dict[str, Any] = json.loads(to_canonical_json(a))
    b_data: dict[str, Any] = json.loads(to_canonical_json(b))

    a_keys = set(a_data)
    b_keys = set(b_data)
    common_keys = a_keys & b_keys

    a_only = sorted(a_keys - b_keys)
    b_only = sorted(b_keys - a_keys)
    changed: dict[str, dict[str, Any]] = {}

    for key in sorted(common_keys):
        if a_data[key] != b_data[key]:
            changed[key] = {"a": a_data[key], "b": b_data[key]}

    canonical_a = to_canonical_json(a)
    canonical_b = to_canonical_json(b)

    return {
        "same": canonical_a == canonical_b,
        "a_only": a_only,
        "b_only": b_only,
        "changed": changed,
        "config_version_changed": a.config_version != b.config_version,
    }


__all__ = [
    "ConfigExportError",
    "ConfigImportError",
    "export_config",
    "import_config",
    "diff_configs",
]
