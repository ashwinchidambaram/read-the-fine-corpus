"""Performance smoke tests — §4.5 harness regression guard (Phase 7, WU-D).

These run the FAKE-mode benchmark harness with LOOSE thresholds so CI catches a
GROSS performance regression (e.g. an accidental per-query DB hit or an O(n^2)
blow-up in the service/pipeline path) without being flaky about absolute wall
times.  They are fast and deterministic.

They deliberately do NOT assert the §4.5 targets (p50<150ms, p99<800ms, no
throughput regression, cold-start<30min): those are reference-deployment
targets and CANNOT be certified from FAKE-mode numbers in a dev environment.
See ``benchmarks/RESULTS.md`` for the honest verdict table and what must be run
under the compose overlay / on reference hardware.

The LIVE/overlay benchmarks are exercised via ``python -m benchmarks ...
--mode live`` and are marked so they skip when infra is absent.
"""

from __future__ import annotations

import pytest

from benchmarks import bench_coldstart, bench_ingestion, bench_retrieval
from benchmarks.stats import percentile, summarize_latencies
from benchmarks.verdict import (
    FAKE_SMOKE_MIN_DOCS_PER_SEC,
    FAKE_SMOKE_P50_CEILING_MS,
    FAKE_SMOKE_P99_CEILING_MS,
)

pytestmark = pytest.mark.perf


# ---------------------------------------------------------------------------
# Pure stdlib percentile math
# ---------------------------------------------------------------------------


def test_percentile_nearest_rank() -> None:
    data = [float(i) for i in range(1, 101)]  # 1..100
    assert percentile(data, 50) == 50.0
    assert percentile(data, 99) == 99.0
    assert percentile(data, 100) == 100.0
    assert percentile(data, 0) == 1.0


def test_summarize_latencies_shape() -> None:
    s = summarize_latencies([5.0, 1.0, 3.0, 2.0, 4.0])
    assert s.count == 5
    assert s.min_ms == 1.0
    assert s.max_ms == 5.0
    assert s.p50_ms == 3.0
    assert 0 < s.mean_ms <= s.max_ms


def test_summarize_empty_raises() -> None:
    with pytest.raises(ValueError):
        summarize_latencies([])


# ---------------------------------------------------------------------------
# FAKE-mode harness regression smoke (loose thresholds)
# ---------------------------------------------------------------------------


def test_retrieval_fake_determinism_and_loose_latency() -> None:
    """Two identical FAKE runs return the same structural outcome + sane latency."""
    r1 = bench_retrieval.run_fake(queries=400, warmup=40, seeded_points=200)
    r2 = bench_retrieval.run_fake(queries=400, warmup=40, seeded_points=200)

    # Determinism: identical structural outcome across runs (not wall-time).
    assert r1.measured_queries == r2.measured_queries == 400
    assert r1.seeded_points == r2.seeded_points == 200
    assert r1.summary.count == r2.summary.count == 400

    # Loose regression ceilings — FAKE mode is normally sub-millisecond.
    assert r1.summary.p50_ms < FAKE_SMOKE_P50_CEILING_MS, (
        f"FAKE p50 {r1.summary.p50_ms:.3f}ms exceeded loose ceiling "
        f"{FAKE_SMOKE_P50_CEILING_MS}ms — possible gross regression"
    )
    assert r1.summary.p99_ms < FAKE_SMOKE_P99_CEILING_MS, (
        f"FAKE p99 {r1.summary.p99_ms:.3f}ms exceeded loose ceiling "
        f"{FAKE_SMOKE_P99_CEILING_MS}ms — possible gross regression"
    )


def test_ingestion_fake_produces_chunks_and_positive_throughput() -> None:
    res = bench_ingestion.run_fake()
    assert res.docs_processed > 0, "FAKE ingestion produced 0 docs — pipeline broken"
    assert res.chunks_produced > 0, "FAKE ingestion produced 0 chunks — chunker/build broken"
    assert res.docs_per_sec >= FAKE_SMOKE_MIN_DOCS_PER_SEC, (
        f"FAKE docs/s {res.docs_per_sec:.2f} below loose floor "
        f"{FAKE_SMOKE_MIN_DOCS_PER_SEC} — possible gross throughput regression"
    )


def test_ingestion_baseline_doc_is_parseable() -> None:
    """The Phase-1 baseline doc must parse (rows may be empty in this env)."""
    rows = bench_ingestion.parse_baseline_rows()
    # No assertion on row count: the integration test may not have populated it
    # in this environment.  We only assert the parser does not raise and returns
    # a list, so the LIVE regression gate has a well-formed input.
    assert isinstance(rows, list)


def test_coldstart_fake_scales_to_distinct_docs() -> None:
    """FAKE cold-start proxy: N distinct docs → N processed docs + a first query."""
    res = bench_coldstart.run_fake(docs=60)
    assert res.docs_processed == 60, (
        f"expected 60 distinct docs processed, got {res.docs_processed} — "
        "synthetic corpus is being deduped/skipped"
    )
    assert res.chunks_produced >= 60
    assert res.first_query_status == "matches"
    assert res.total_seconds > 0


# ---------------------------------------------------------------------------
# LIVE/overlay benchmarks — skipped when infra is absent
# ---------------------------------------------------------------------------


@pytest.mark.qdrant_integration
def test_ingestion_live_against_baseline() -> None:
    """LIVE ingestion vs the Phase-1 baseline (skips without the overlay)."""
    res = bench_ingestion.run_live()
    if res is None:
        pytest.skip("Qdrant unreachable — start the docker-compose integration overlay")
    assert res.docs_processed > 0
    cmp = bench_ingestion.compare_against_baseline(res)
    # If there is a comparable baseline row, it MUST NOT have regressed >20%.
    if cmp["verdict"] in ("pass", "regressed"):
        assert cmp["verdict"] == "pass", f"ingestion throughput regressed: {cmp}"


@pytest.mark.qdrant_integration
def test_coldstart_live_smoke() -> None:
    """LIVE cold-start over a small corpus (skips without the overlay).

    Uses a small doc count so the integration smoke stays fast; the full 1k-doc
    §4.5 cold-start is run by the orchestrator via
    ``python -m benchmarks coldstart --mode live --docs 1000``.
    """
    res = bench_coldstart.run_live(docs=20)
    if res is None:
        pytest.skip("Qdrant unreachable — start the docker-compose integration overlay")
    assert res.docs_processed > 0
    assert res.first_query_status in ("matches", "no_matches")
    assert res.total_seconds > 0
