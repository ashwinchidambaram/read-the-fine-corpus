"""RTFC performance benchmark harness (Phase 7, Wave 2, WU-D).

Measures the §4.5 performance & scale targets and records the numbers honestly:

  - Retrieval p50 latency (cache warm)  < 150 ms
  - Retrieval p99 latency                < 800 ms
  - Ingestion throughput                 MUST NOT regress the Phase-1 baseline
  - Cold start to first query (1k docs)  < 30 minutes

This package lives OUTSIDE ``src/finecorpus`` on purpose: it is a measurement
tool, not part of the shipped library, and it must not add an import edge into
the core layering contract (C-5).  It measures the REAL service/pipeline
functions — it never reimplements them.

Two measurement modes:

  FAKE  — FakeProvider + FakeAdapter test doubles.  Deterministic, seeded,
          always runnable in CI.  Measures service-path / pipeline-stage
          overhead only.  The absolute numbers are NOT the reference-deployment
          numbers the §19 release gate needs — see benchmarks/RESULTS.md.

  LIVE  — Real Qdrant (+ Postgres) via the docker-compose integration overlay.
          Measures real end-to-end latency/throughput.  Gated: skipped when the
          infrastructure is unreachable.

CLI:

    uv run python -m benchmarks retrieval --mode fake
    uv run python -m benchmarks ingestion --mode fake
    uv run python -m benchmarks coldstart --mode fake --docs 1000
    uv run python -m benchmarks all --mode fake --write-results
"""

from __future__ import annotations

from benchmarks.stats import LatencySummary, percentile, summarize_latencies

__all__ = [
    "LatencySummary",
    "percentile",
    "summarize_latencies",
]
