"""Target thresholds and verdict vocabulary for the §4.5 benchmark harness.

Verdicts are deliberately honest.  Only a subset of the §4.5 targets can be
*met in-harness* here; the rest require the phase-close compose overlay or real
reference hardware.  The verdict vocabulary encodes that distinction:

  MET               — measured in this environment and within the target.
  MET-IN-HARNESS    — within a LOOSE/proxy threshold, but the environment is not
                      the reference deployment, so this does NOT satisfy the
                      §19 release gate on its own.
  UNMET             — measured and outside the target.
  NEEDS-OVERLAY     — requires the docker-compose integration overlay (LIVE mode).
  NEEDS-REFERENCE-HW — requires the reference deployment / commodity reference
                      hardware the orchestrator/owner must run.
  ENV-LIMITED       — could not be measured meaningfully in this environment.
"""

from __future__ import annotations

# §4.5 targets (source of truth: spec §4.5).
RETRIEVAL_P50_TARGET_MS = 150.0
RETRIEVAL_P99_TARGET_MS = 800.0
COLD_START_TARGET_SECONDS = 30 * 60

# LOOSE regression floors for FAKE-mode CI smoke.  These are deliberately far
# from the real targets: FAKE mode has no network, so it is normally sub-1 ms.
# The smoke test only needs to catch a GROSS regression (e.g. an accidental
# O(n^2) blow-up or a per-query DB hit), not to certify the SLA.
FAKE_SMOKE_P50_CEILING_MS = 50.0
FAKE_SMOKE_P99_CEILING_MS = 200.0
# Loose throughput floor for FAKE-mode ingestion (docs/sec). Real path does far
# better; this only trips if parse/chunk/build regresses catastrophically.
FAKE_SMOKE_MIN_DOCS_PER_SEC = 1.0

__all__ = [
    "RETRIEVAL_P50_TARGET_MS",
    "RETRIEVAL_P99_TARGET_MS",
    "COLD_START_TARGET_SECONDS",
    "FAKE_SMOKE_P50_CEILING_MS",
    "FAKE_SMOKE_P99_CEILING_MS",
    "FAKE_SMOKE_MIN_DOCS_PER_SEC",
]
