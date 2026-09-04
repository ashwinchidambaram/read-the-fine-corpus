"""Contract — SegmentSetBatch (promoted from pipeline-internal, D-26 resolution).

Stage boundary: Decompose → Plan.
See docs/contracts/README.md §Phase-0-implementation-notes and decision D-26.

D-26 resolution (2026-09-03): `SegmentSetBatch` is promoted to an official versioned
contract with its own module, SpecRange declaration, and MAJOR/MINOR discipline.
PlanStage now performs a version check on this envelope instead of opting out.

Artifact structure (one per pipeline run):
  {
    "schema_version": "1.0.0",
    "contract": "segment_set_batch",
    "segment_sets": [ <SegmentSet>, ... ]
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
"""Version stamped on every SegmentSetBatch produced by DecomposeStage."""

SUPPORTED_SEGMENT_SET_BATCH = SpecRange(major=1, min_minor=0)
"""Versions that PlanStage accepts.  Bumping this range is a MINOR change;
removing a field or changing semantics is a MAJOR change (see contracts/README.md)."""


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


class SegmentSetBatch(BaseModel):
    """Official versioned envelope for the Decompose → Plan stage boundary.

    Holds one SegmentSet per document.  `schema_version` enables full version
    checking by the consuming stage (PlanStage); this is the D-26 fix for the
    "no version check" gap on this boundary.

    Invariants:
    - `segment_sets` has exactly one entry per ParseResult (nothing silently dropped).
    - `schema_version` is validated by PlanStage via SUPPORTED_SEGMENT_SET_BATCH.
    """

    schema_version: str = Field(
        description="Envelope schema version (semver).  Checked by the consuming stage."
    )
    contract: str = Field(
        default="segment_set_batch",
        description="Nominal contract name for this artifact.",
    )
    skeleton: bool | None = Field(
        default=None,
        description=(
            "True = Phase 0 pass-through (no real decomposition). "
            "None / absent in real Phase 1+ runs."
        ),
    )
    segment_sets: list[dict[str, Any]] = Field(
        description="List of SegmentSet dicts, one per document."
    )


__all__ = [
    "BATCH_SCHEMA_VERSION",
    "SUPPORTED_SEGMENT_SET_BATCH",
    "SegmentSetBatch",
]
