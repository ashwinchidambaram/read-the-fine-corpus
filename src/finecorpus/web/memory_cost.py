"""Hot-copy memory-cost figure (M-096, §10.2).

When a user raises the hot-copy retention count, the UI MUST display the memory
cost of the additional copy.  §10.2 / ``corpus kb status`` use the same estimate:
each hot copy costs approximately ``vector_count × dimensions × 4 bytes`` plus a
payload overhead (~500 bytes/point).  This module is the single source of that
figure for the web layer so the CLI and UI agree.
"""

from __future__ import annotations

_BYTES_PER_FLOAT = 4
_PAYLOAD_BYTES_PER_POINT = 500


def hot_copy_bytes(vector_count: int, dimensions: int) -> int:
    """Estimated bytes for one hot collection copy (§10.2)."""
    vectors = vector_count * dimensions * _BYTES_PER_FLOAT
    payload = vector_count * _PAYLOAD_BYTES_PER_POINT
    return vectors + payload


def hot_copy_mb(vector_count: int, dimensions: int) -> float:
    """Estimated megabytes for one hot collection copy (§10.2)."""
    return hot_copy_bytes(vector_count, dimensions) / (1024 * 1024)


def retention_delta_summary(
    *, vector_count: int, dimensions: int, current_count: int, new_count: int
) -> str:
    """Plain-language M-096 memory figure for raising hot_retention_count.

    Returns a sentence that names the memory cost of each ADDITIONAL hot copy
    and the total added memory for the requested increase.
    """
    per_copy_mb = hot_copy_mb(vector_count, dimensions)
    added = max(0, new_count - current_count)
    total_added_mb = per_copy_mb * added
    return (
        f"Raising hot_retention_count from {current_count} to {new_count} keeps "
        f"{added} additional hot copy(ies) in memory. Each hot copy costs about "
        f"{per_copy_mb:.1f} MB (ESTIMATE: {vector_count:,} vectors × {dimensions}d × 4 B "
        f"+ ~500 B payload/point), so this adds about {total_added_mb:.1f} MB of memory."
    )


__all__ = ["hot_copy_bytes", "hot_copy_mb", "retention_delta_summary"]
