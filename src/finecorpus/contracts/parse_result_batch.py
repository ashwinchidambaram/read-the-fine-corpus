"""Contract — ParseResultBatch (promoted from pipeline-internal, D-26 resolution).

Stage boundary: Assess → Decompose.
See docs/contracts/README.md §Phase-0-implementation-notes and decision D-26.

D-26 resolution (2026-09-03): `ParseResultBatch` is promoted to an official versioned
contract with its own module, SpecRange declaration, and MAJOR/MINOR discipline.
DecomposeStage now performs a version check on this envelope instead of opting out.

Artifact structure (one per pipeline run):
  {
    "schema_version": "1.0.0",
    "contract": "parse_result_batch",
    "results": [ <ParseResult>, ... ]
  }

The `skeleton` field is retained for backward-compatibility with Phase 0 artifacts;
it is omitted (None) in real runs.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from finecorpus.contracts.versions import SpecRange

# ---------------------------------------------------------------------------
# Version constants
# ---------------------------------------------------------------------------

BATCH_SCHEMA_VERSION = "1.0.0"
"""Version stamped on every ParseResultBatch produced by AssessStage."""

SUPPORTED_PARSE_RESULT_BATCH = SpecRange(major=1, min_minor=0)
"""Versions that DecomposeStage accepts.  Bumping this range is a MINOR change;
removing a field or changing semantics is a MAJOR change (see contracts/README.md)."""


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


class ParseResultBatch(BaseModel):
    """Official versioned envelope for the Assess → Decompose stage boundary.

    Holds one ParseResult per document.  `schema_version` enables full version
    checking by the consuming stage (DecomposeStage); this is the D-26 fix
    for the "no version check" gap on this boundary.

    Invariants:
    - `results` has exactly one entry per InventoryItem (nothing silently dropped,
      §6 rule 6, §12).
    - `schema_version` is validated by DecomposeStage via SUPPORTED_PARSE_RESULT_BATCH.
    """

    schema_version: str = Field(
        description="Envelope schema version (semver).  Checked by the consuming stage."
    )
    contract: str = Field(
        default="parse_result_batch",
        description="Nominal contract name for this artifact.",
    )
    skeleton: bool | None = Field(
        default=None,
        description=(
            "True = Phase 0 pass-through (no real parsing). None / absent in real Phase 1+ runs."
        ),
    )
    results: list[dict[str, Any]] = Field(
        description="List of ParseResult dicts, one per document."
    )


__all__ = [
    "BATCH_SCHEMA_VERSION",
    "SUPPORTED_PARSE_RESULT_BATCH",
    "ParseResultBatch",
]
