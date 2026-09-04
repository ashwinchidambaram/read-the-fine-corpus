"""Preflight check result types shared between finecorpus.config and finecorpus.embedding.

This module is intentionally thin — no imports from other finecorpus packages.
It exists so that ``finecorpus.config.preflight`` (config layer) and
``finecorpus.embedding.preflight_check`` (embedding layer) can share the same
``CheckResult`` / ``CheckStatus`` types without creating a same-layer import
cycle (F-04).

The module is NOT listed in the C-5 layers contract (``exhaustive = false``),
so it is importable from any layer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class CheckStatus(StrEnum):
    """Outcome of a single preflight check."""

    OK = "OK"
    WARN = "WARN"
    FAIL = "FAIL"
    SKIPPED = "SKIPPED"


@dataclass
class CheckResult:
    """Result of a single named preflight check.

    Attributes
    ----------
    name:
        Short identifier for the check (e.g. ``"postgres"``, ``"qdrant"``).
    status:
        :class:`CheckStatus` value.
    message:
        Human-readable detail.  ``FAIL`` messages are always actionable —
        they name the failing component and state what to fix.
    latency_ms:
        Optional round-trip latency in milliseconds (set for connectivity
        checks that successfully established a connection).
    """

    name: str
    status: CheckStatus
    message: str
    latency_ms: float | None = None

    def __str__(self) -> str:
        latency = f" (latency={self.latency_ms:.0f}ms)" if self.latency_ms is not None else ""
        return f"{self.status.value:<8} {self.name:<20} {self.message}{latency}"


@dataclass
class PreflightReport:
    """Aggregated results from all preflight checks.

    Attributes
    ----------
    results:
        Ordered list of :class:`CheckResult` objects.
    passed:
        ``True`` only when every check is ``OK``, ``WARN``, or ``SKIPPED``
        (i.e. no ``FAIL``).  Ingestion is only allowed when ``passed`` is
        ``True``.
    """

    results: list[CheckResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        """``True`` when no check has status ``FAIL``."""
        return all(r.status != CheckStatus.FAIL for r in self.results)

    @property
    def has_failures(self) -> bool:
        """``True`` when at least one check has status ``FAIL``."""
        return any(r.status == CheckStatus.FAIL for r in self.results)

    def __str__(self) -> str:
        lines = [str(r) for r in self.results]
        lines.append("")
        lines.append("PASS" if self.passed else "FAIL — fix the issues above before ingesting.")
        return "\n".join(lines)


__all__ = ["CheckStatus", "CheckResult", "PreflightReport"]
