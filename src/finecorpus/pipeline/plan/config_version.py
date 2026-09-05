"""config_version derivation for IngestionConfig (§10.5).

Extracted from plan/stage.py so Wave 2 can rewrite stage.py without
touching this logic. This module is the single source of truth for the
build-affecting field set.

See docs/contracts/ingestion-config.md §config_version derivation for the
authoritative field list. Summary:

INCLUDED (build-affecting — change → different text or embedding_input):
  - class_rules[*].transformation  (Tier 1/2/3 settings)
  - class_rules[*].chunking        (chunk boundaries + tokenizer)
  - class_rules[*].embedding_override (embedding model identity per-class)
  - class_rules[*].metadata_schema
  - default_rule (same subfields as above)
  - embedding  (KB-wide embedding model identity)
  - class_descriptions[*].class_id / .description (attack 8, S-R14)
  - spreadsheet_triage[*].kind / .disposition

EXCLUDED (retrieval-time only or provenance/rationale text):
  - created_at
  - provenance / rationale strings
  - retrieval_defaults / retrieval_treatment fields
  - exclusions_confirmed remediation text
  - schema_version, config_version itself, tenancy, naive_baseline, secret_free_attestation
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from finecorpus.contracts.ingestion_config import IngestionConfig


def _extract_class_rule_build_fields(rule_dict: dict[str, Any]) -> dict[str, Any]:
    """Extract only the build-affecting fields from a serialized ClassRule dict."""
    return {
        "transformation": rule_dict["transformation"],
        "chunking": rule_dict["chunking"],
        "embedding_override": rule_dict.get("embedding_override"),
        "metadata_schema": rule_dict.get("metadata_schema", []),
        # segment_class identifies which rule this is — include it so that
        # swapping two class rules with identical settings still changes the hash.
        "segment_class": rule_dict["segment_class"],
    }


def derive_config_version(config: IngestionConfig) -> str:
    """Derive a deterministic config_version from all build-affecting fields (§10.5).

    The config_version is the sha256 (hex) of the canonical JSON (sorted keys,
    no insignificant whitespace, UTF-8) of exactly the build-affecting field set
    described in docs/contracts/ingestion-config.md.

    This function is the SINGLE SOURCE OF TRUTH for what is hashed.  It is used by:
    - PlanStage when producing a new IngestionConfig.
    - import_config() in config_io when re-deriving the version for tamper detection.

    Args:
        config: The IngestionConfig to derive the version for.

    Returns:
        sha256 hex digest (64 hex chars) of the build-affecting field set.
    """
    raw = config.model_dump(mode="json")

    # Build the build-affecting field set (exact order and keys are canonical).
    build_affecting: dict[str, Any] = {
        # Per-class rules (build-affecting subfields only).
        "class_rules": [_extract_class_rule_build_fields(r) for r in raw.get("class_rules", [])],
        # Default rule (same subfield treatment).
        "default_rule": _extract_class_rule_build_fields(raw["default_rule"]),
        # KB-wide embedding model identity.
        "embedding": raw["embedding"],
        # Class descriptions: class_id + description text (attack 8, S-R14).
        # Sort by class_id for stability (list order must not affect the hash).
        "class_descriptions": sorted(
            [
                {"class_id": cd["class_id"], "description": cd["description"]}
                for cd in raw.get("class_descriptions", [])
            ],
            key=lambda x: x["class_id"],
        ),
        # Spreadsheet triage disposition (affects which content is ingested).
        "spreadsheet_triage": [
            {"kind": t["kind"], "disposition": t["disposition"]}
            for t in raw.get("spreadsheet_triage", [])
        ],
    }

    canonical = json.dumps(
        build_affecting, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


__all__ = ["derive_config_version"]
