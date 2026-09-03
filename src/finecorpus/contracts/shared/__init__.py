"""Shared blocks re-used across all contracts.

See docs/contracts/README.md#shared-blocks for the authoritative definitions of
TenancyBlock, SourceLocation, TransformationRecord, and Provenance.
"""

from finecorpus.contracts.shared.blocks import (
    InvisibleContentKind,
    PermissionFidelity,
    PermissionMode,
    PermissionSource,
    Provenance,
    SalienceSignal,
    SalienceSignalKind,
    SalienceTier,
    SegmentType,
    SensitivityFlag,
    SourceLocation,
    TenancyBlock,
    TransformationRecord,
    TransformationTier,
    TrustLevel,
)

__all__ = [
    "TenancyBlock",
    "SourceLocation",
    "TransformationRecord",
    "Provenance",
    "SalienceSignal",
    "SalienceSignalKind",
    "SalienceTier",
    "SegmentType",
    "PermissionMode",
    "PermissionSource",
    "PermissionFidelity",
    "InvisibleContentKind",
    "SensitivityFlag",
    "TrustLevel",
    "TransformationTier",
]
