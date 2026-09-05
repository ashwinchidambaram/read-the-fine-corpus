"""Contract versioning support.

See docs/contracts/README.md#contract-versioning for the scheme.

Every contract instance declares its own schema_version (semver). Consumers
declare the range they support as a SpecRange. A consumer MUST reject a version
it does not declare support for — rejection is a hard error (ContractVersionError),
never a warning-and-continue.

Compatibility rules:
- MAJOR bump = breaking change. Consumer supports a MAJOR line only if it declares
  that exact major.
- MINOR bump = backward-compatible addition. A consumer that supports X.Y accepts
  X.Y' for Y' >= Y within the same major.
- PATCH bump = documentation/constraint tightening; always compatible within the major.

The six (seven with retrieval-response) version constants live here so the
compatibility matrix is inspectable in one place.
"""

from __future__ import annotations

from dataclasses import dataclass

# ---------------------------------------------------------------------------
# ContractVersionError
# ---------------------------------------------------------------------------


class ContractVersionError(Exception):
    """Raised when a consumer encounters an unsupported schema_version.

    See docs/contracts/README.md#contract-versioning invariants.
    A rejected version fails loud; it is never downgraded to best-effort parse.
    """

    def __init__(self, contract: str, got: str, supported: str) -> None:
        self.contract = contract
        self.got = got
        self.supported = supported
        super().__init__(
            f"Contract '{contract}': unsupported schema_version '{got}'; "
            f"this consumer supports {supported}"
        )


# ---------------------------------------------------------------------------
# SpecRange
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SpecRange:
    """Declares which contract versions a consumer accepts.

    A SpecRange(major=1, min_minor=2) accepts 1.2.x, 1.3.x, ... 1.<latest>.x
    but rejects 2.x.x and 1.0.x / 1.1.x.

    See docs/contracts/README.md#how-support-is-declared.
    """

    major: int
    """The exact MAJOR version this consumer is built for."""
    min_minor: int = 0
    """Minimum accepted MINOR within the major (inclusive)."""

    def check(self, contract: str, schema_version: str) -> None:
        """Validate schema_version against this range.

        Raises ContractVersionError if unsupported.

        Args:
            contract: Human-readable contract name for the error message.
            schema_version: The semver string from the contract instance.
        """
        try:
            parts = schema_version.split(".")
            if len(parts) != 3:  # noqa: PLR2004
                raise ValueError("not semver")
            got_major = int(parts[0])
            got_minor = int(parts[1])
        except (ValueError, IndexError):
            raise ContractVersionError(
                contract=contract,
                got=schema_version,
                supported=self._describe(),
            ) from None

        if got_major != self.major or got_minor < self.min_minor:
            raise ContractVersionError(
                contract=contract,
                got=schema_version,
                supported=self._describe(),
            )

    def _describe(self) -> str:
        return f"{self.major}.>={self.min_minor}"


# ---------------------------------------------------------------------------
# Consumer-side version constants — one per contract
# ---------------------------------------------------------------------------
# These are the versions the Phase 0 implementation supports.
# Each constant is the SpecRange a *consumer* of that contract declares.
# Producers stamp "1.0.0" on all contracts in this phase.

SUPPORTED_INVENTORY = SpecRange(major=1, min_minor=0)
SUPPORTED_PARSE_RESULT = SpecRange(major=1, min_minor=0)
SUPPORTED_PARSE_RESULT_BATCH = SpecRange(major=1, min_minor=0)
"""Assess → Decompose batch envelope (D-26: promoted to official contract).

Phase 2: schema_version bumped to 1.1.0 to add version_families and
boilerplate_blocks fields.  SpecRange stays at min_minor=0 so that any
consumer built for 1.0.0+ still accepts 1.1.0 (MINOR is backward-compatible).
"""
SUPPORTED_SEGMENT_SET = SpecRange(major=1, min_minor=1)
"""Segment set consumer range.

History:
- 1.1: added ExclusionReason.too_short.
- 1.2: added SalienceSignalKind.superseded_version (D-25 toggle-on override pass).
  SpecRange stays at min_minor=1 (MINOR: backward-compatible addition).
"""
SUPPORTED_SEGMENT_SET_BATCH = SpecRange(major=1, min_minor=0)
"""Decompose → Plan batch envelope (D-26: promoted to official contract)."""
SUPPORTED_INGESTION_CONFIG = SpecRange(major=1, min_minor=1)
"""Ingestion-config consumer range.

History:
- 1.1: added ChunkingConfig.tokenizer.
- 1.2: added ClassDescription/class_descriptions, M-032/M-033/M-034 flag fields,
  extra="forbid" on all models, Tier-3 structural validators (M-031/M-035),
  to_canonical_json(), and config_version derivation module.
  SpecRange stays at min_minor=1 (MINOR: backward-compatible addition); consumers
  built for 1.1.0+ accept 1.2.0.
"""

INGESTION_CONFIG_SCHEMA_VERSION = "1.2.0"
"""Producer stamp for IngestionConfig. Bump here when the contract MINOR/MAJOR changes."""
SUPPORTED_CHUNK = SpecRange(major=1, min_minor=0)
SUPPORTED_EVAL_SET = SpecRange(major=1, min_minor=0)
SUPPORTED_RETRIEVAL_RESPONSE = SpecRange(major=1, min_minor=1)
"""Retrieval-response consumer range — bumped to 1.1 with addition of
CONTROL_PLANE_UNAVAILABLE and PAYLOAD_CORRUPT error codes."""


def check_version(contract: str, schema_version: str, spec_range: SpecRange) -> None:
    """Consumer-side helper: check schema_version against a declared SpecRange.

    Raises ContractVersionError on unsupported versions.
    See docs/contracts/README.md#contract-versioning.

    Args:
        contract: Human-readable contract name (e.g. "inventory").
        schema_version: The semver string from the received contract instance.
        spec_range: The SpecRange this consumer declares support for.
    """
    spec_range.check(contract=contract, schema_version=schema_version)


__all__ = [
    "ContractVersionError",
    "SpecRange",
    "check_version",
    "SUPPORTED_INVENTORY",
    "SUPPORTED_PARSE_RESULT",
    "SUPPORTED_PARSE_RESULT_BATCH",
    "SUPPORTED_SEGMENT_SET",
    "SUPPORTED_SEGMENT_SET_BATCH",
    "SUPPORTED_INGESTION_CONFIG",
    "INGESTION_CONFIG_SCHEMA_VERSION",
    "SUPPORTED_CHUNK",
    "SUPPORTED_EVAL_SET",
    "SUPPORTED_RETRIEVAL_RESPONSE",
]
