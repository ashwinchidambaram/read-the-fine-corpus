"""Contract — ParseResultBatch (promoted from pipeline-internal, D-26 resolution).

Stage boundary: Assess → Decompose.
See docs/contracts/README.md §Phase-0-implementation-notes and decision D-26.

D-26 resolution (2026-09-03): `ParseResultBatch` is promoted to an official versioned
contract with its own module, SpecRange declaration, and MAJOR/MINOR discipline.
DecomposeStage now performs a version check on this envelope instead of opting out.

Schema version history:
  1.0.0 — Initial version (D-26).
  1.1.0 — MINOR: added ``version_families`` and ``boilerplate_blocks`` corpus-level
           fields for near-duplicate clustering and boilerplate detection (Phase 2,
           corpus_passes.py).  Backward-compatible addition (no required-field change).

Artifact structure (one per pipeline run):
  {
    "schema_version": "1.1.0",
    "contract": "parse_result_batch",
    "results": [ <ParseResult>, ... ],
    "version_families": [ <VersionFamily>, ... ],
    "boilerplate_blocks": [ "<normalised block text>", ... ]
  }

The ``version_families`` field carries near-duplicate clusters produced by the
corpus-level pass in ``pipeline/assess/corpus_passes.py``.  Each entry is a
VersionFamily-shaped dict matching the inventory.md schema.

The ``boilerplate_blocks`` field carries the set of normalised paragraph strings
detected as corpus-wide boilerplate.  DecomposeStage passes this set to
``BoilerplatePass`` so it can retype matching segments.

The `skeleton` field is retained for backward-compatibility with Phase 0 artifacts;
it is omitted (None) in real runs.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from finecorpus.contracts.versions import SUPPORTED_PARSE_RESULT_BATCH  # noqa: F401

# ---------------------------------------------------------------------------
# Version constants
# ---------------------------------------------------------------------------

BATCH_SCHEMA_VERSION = "1.1.0"
"""Version stamped on every ParseResultBatch produced by AssessStage.

Bumped from 1.0.0 to 1.1.0 (MINOR) to add ``version_families`` and
``boilerplate_blocks`` corpus-level fields (Phase 2 dedup/boilerplate).
"""

# SUPPORTED_PARSE_RESULT_BATCH is the authoritative SpecRange; it lives in
# finecorpus.contracts.versions (the single inspectable compatibility matrix).
# Re-exported here for backward-compatibility with any direct imports.


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


class ParseResultBatch(BaseModel):
    """Official versioned envelope for the Assess → Decompose stage boundary.

    Holds one ParseResult per document.  `schema_version` enables full version
    checking by the consuming stage (DecomposeStage); this is the D-26 fix
    for the "no version check" gap on this boundary.

    Phase 2 additions (schema_version 1.1.0):
    - ``version_families``: near-duplicate version families detected across the
      corpus (§6.1).  Empty list when no families found.
    - ``boilerplate_blocks``: normalised paragraph strings that appear in more
      than the configured proportion of corpus documents (§6.2).  Empty list
      when no boilerplate detected.  Used by DecomposeStage's BoilerplatePass.

    Invariants:
    - `results` has exactly one entry per InventoryItem (nothing silently dropped,
      §6 rule 6, §12).
    - `schema_version` is validated by DecomposeStage via SUPPORTED_PARSE_RESULT_BATCH.
    - Each ParseResult in `results` carries ``dedup_role`` (one of ``"unique"``,
      ``"primary"``, ``"superseded"``) annotated by corpus_passes.run_corpus_passes().
    """

    schema_version: str = Field(
        description="Envelope schema version (semver).  Checked by the consuming stage."
    )
    contract: str = Field(
        default="parse_result_batch",
        description="Nominal contract name for this artifact.",
    )
    run_id: str = Field(
        description=(
            "Pipeline run identifier (§12 traceability). "
            "Threaded from the orchestrator; matches ArtifactStore.run_id."
        )
    )
    produced_at: datetime = Field(
        description=(
            "When AssessStage produced this batch (UTC). "
            "Uses run_started_at from the orchestrator — not wall-clock — for determinism."
        )
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
    version_families: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "Near-duplicate version families detected by corpus_passes (Phase 2, §6.1). "
            "Each entry is a VersionFamily-shaped dict matching the inventory.md schema. "
            "Empty list when no near-duplicate clusters are found. "
            "Added in schema_version 1.1.0."
        ),
    )
    boilerplate_blocks: list[str] = Field(
        default_factory=list,
        description=(
            "Normalised paragraph strings classified as corpus-wide boilerplate "
            "by corpus_passes (Phase 2, §6.2). "
            "A block here will be retyped to segment_type=boilerplate by BoilerplatePass. "
            "Normalised = lower-cased, whitespace-collapsed. "
            "Added in schema_version 1.1.0."
        ),
    )


__all__ = [
    "BATCH_SCHEMA_VERSION",
    "SUPPORTED_PARSE_RESULT_BATCH",
    "ParseResultBatch",
]
