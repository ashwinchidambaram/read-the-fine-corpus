"""Stdlib-only latency statistics for the benchmark harness.

No numpy.  Percentiles are computed from a sorted list using the
nearest-rank method (the same method a p99 SLA is normally read against):
the p-th percentile is the value at rank ceil(p/100 * N) in the sorted sample.

This is deliberately simple and interpolation-free so the numbers are easy to
reason about and reproduce.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


def percentile(sorted_samples: list[float], p: float) -> float:
    """Return the p-th percentile of an already-sorted, non-empty sample.

    Uses the nearest-rank method: rank = ceil(p/100 * N), clamped to [1, N].

    Args:
        sorted_samples: Ascending-sorted, non-empty list of samples.
        p: Percentile in [0, 100].

    Returns:
        The sample at the nearest rank.

    Raises:
        ValueError: If ``sorted_samples`` is empty.
    """
    if not sorted_samples:
        raise ValueError("percentile() requires a non-empty sample")
    if p <= 0:
        return sorted_samples[0]
    if p >= 100:
        return sorted_samples[-1]
    n = len(sorted_samples)
    rank = math.ceil((p / 100.0) * n)
    rank = max(1, min(rank, n))
    return sorted_samples[rank - 1]


@dataclass(frozen=True)
class LatencySummary:
    """Summary statistics for a batch of latency measurements (milliseconds)."""

    count: int
    p50_ms: float
    p95_ms: float
    p99_ms: float
    min_ms: float
    max_ms: float
    mean_ms: float

    def as_dict(self) -> dict[str, float | int]:
        return {
            "count": self.count,
            "p50_ms": round(self.p50_ms, 4),
            "p95_ms": round(self.p95_ms, 4),
            "p99_ms": round(self.p99_ms, 4),
            "min_ms": round(self.min_ms, 4),
            "max_ms": round(self.max_ms, 4),
            "mean_ms": round(self.mean_ms, 4),
        }


def summarize_latencies(samples_ms: list[float]) -> LatencySummary:
    """Summarize a list of latency samples (in milliseconds).

    Args:
        samples_ms: Non-empty list of per-request latencies in ms.

    Returns:
        LatencySummary with p50/p95/p99, min/max, and mean.

    Raises:
        ValueError: If ``samples_ms`` is empty.
    """
    if not samples_ms:
        raise ValueError("summarize_latencies() requires at least one sample")
    ordered = sorted(samples_ms)
    return LatencySummary(
        count=len(ordered),
        p50_ms=percentile(ordered, 50),
        p95_ms=percentile(ordered, 95),
        p99_ms=percentile(ordered, 99),
        min_ms=ordered[0],
        max_ms=ordered[-1],
        mean_ms=sum(ordered) / len(ordered),
    )


__all__ = ["LatencySummary", "percentile", "summarize_latencies"]
