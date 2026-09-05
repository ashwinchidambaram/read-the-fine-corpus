"""Retrieval telemetry hooks (Phase 4 stub, D-24).

Defines a minimal counter interface that the full telemetry module (a sibling
Phase 4 unit) can absorb without API changes.  Until that module lands, counters
are in-process module-level integers that can be read by tests.

The primary counter implemented here is:

  filtered_to_zero_total
    Incremented whenever a suspicion/score filter alone empties the survivor
    set after a vector search returned candidates (D-24 ruling).

Callers should use ``increment_filtered_to_zero()`` rather than touching the
counter directly so the sibling unit can swap in a real metrics backend without
changes to all call sites.

Usage::

    from finecorpus.retrieval.metrics import increment_filtered_to_zero, get_filtered_to_zero_count

    increment_filtered_to_zero()          # call at the filter site
    count = get_filtered_to_zero_count()  # read in tests
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Internal state
# ---------------------------------------------------------------------------

_filtered_to_zero_total: int = 0
"""Module-level counter (D-24).  Replaced by real metrics in production."""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def increment_filtered_to_zero() -> None:
    """Increment the filtered-to-zero counter by one (D-24).

    Called when a score/suspicion filter alone empties the candidate set
    after the vector search returned results.

    The sibling telemetry module may replace this function by patching the
    module-level function or by swapping in a real metrics exporter.
    """
    global _filtered_to_zero_total
    _filtered_to_zero_total += 1


def get_filtered_to_zero_count() -> int:
    """Return the current filtered-to-zero counter value.

    Primarily for testing.  The real telemetry backend will export this via
    Prometheus/OTLP instead.
    """
    return _filtered_to_zero_total


def reset_filtered_to_zero_count() -> None:
    """Reset the counter to zero.

    For test isolation only — do not call in production code.
    """
    global _filtered_to_zero_total
    _filtered_to_zero_total = 0


__all__ = [
    "increment_filtered_to_zero",
    "get_filtered_to_zero_count",
    "reset_filtered_to_zero_count",
]
