"""Vector-backend adapter interface and lifecycle (Phase 1).

Public re-exports for consumers outside this package.
"""

from finecorpus.index.adapter import (
    AliasRecord,
    BackendCapabilities,
    CollectionInfo,
    IndexAdapter,
    IndexError,
    SearchResult,
    UnsupportedCapabilityError,
)

__all__ = [
    "AliasRecord",
    "BackendCapabilities",
    "CollectionInfo",
    "IndexAdapter",
    "IndexError",
    "SearchResult",
    "UnsupportedCapabilityError",
]
