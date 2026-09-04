"""Preflight check framework for Read The Fine Corpus.

Implements the named-check infrastructure described in
``docs/configuration/reference.md §3`` and spec §4.6.

Each check returns a :class:`CheckResult` with status ``OK``, ``WARN``,
``FAIL``, or ``SKIPPED``.  A ``FAIL`` result blocks ingestion.  A ``WARN``
allows ingestion to proceed but is surfaced in the dashboard.  ``SKIPPED``
results are always *reported* — never silently passed — so operators know
which checks were deferred to a later phase.

Real implementations
--------------------
- **config_parse**        — validates that the config loaded without error.
- **postgres**            — TCP + handshake reachability using ``psycopg`` if
                           available, or a raw socket check otherwise.
- **qdrant**              — HTTP HEAD / GET on ``storage.qdrant.url/healthz``.
- **object_store**        — TCP socket check against the object-store endpoint.

SKIPPED stubs (Phase 1 wires them)
-----------------------------------
- **embedding_provider**  — probe embedding model availability / dimensions.
- **resource_headroom**   — Qdrant memory vs configured hot-retention count.

Public API
----------
:func:`run_preflight` — executes all checks and returns a
:class:`PreflightReport`.
"""

from __future__ import annotations

import socket
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .models import Config

# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Individual check implementations
# ---------------------------------------------------------------------------


def _check_config_parse(config: Config) -> CheckResult:
    """Check config_parse — verifies the config object loaded without errors.

    If this function is called with a valid :class:`Config` instance, the
    parse already succeeded.  This check exists so the report always contains
    an explicit config-parse entry.
    """
    return CheckResult(
        name="config_parse",
        status=CheckStatus.OK,
        message="corpus.yaml parsed and validated successfully.",
    )


def _tcp_check(host: str, port: int, timeout: float = 5.0) -> tuple[bool, str, float]:
    """Attempt a TCP connection to *host*:*port*.

    Returns ``(success, error_message, latency_ms)``.
    """
    start = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            latency = (time.monotonic() - start) * 1000
            return True, "", latency
    except OSError as exc:
        latency = (time.monotonic() - start) * 1000
        return False, str(exc), latency


def _check_postgres(config: Config) -> CheckResult:
    """Check postgres — TCP reachability of the configured PostgreSQL endpoint.

    Parses the URL for host/port and performs a raw TCP connection.
    A full SQL handshake (via psycopg) would require an optional heavy
    dependency; the TCP check is sufficient to confirm the host is reachable.
    """
    url = config.storage.postgres.url
    if not url:
        return CheckResult(
            name="postgres",
            status=CheckStatus.FAIL,
            message=(
                "storage.postgres.url is not set. "
                "Set FINECORPUS_STORAGE__POSTGRES__URL to a valid PostgreSQL "
                "connection URL (e.g. postgresql://user:pass@host:5432/db)."
            ),
        )

    try:
        parsed = urllib.parse.urlparse(url)
        host = parsed.hostname or "localhost"
        port = parsed.port or 5432
    except Exception as exc:
        return CheckResult(
            name="postgres",
            status=CheckStatus.FAIL,
            message=(
                f"Could not parse storage.postgres.url: {exc}. "
                f"Expected format: postgresql://user:pass@host:port/db"
            ),
        )

    ok, err, latency = _tcp_check(host, port)
    if ok:
        return CheckResult(
            name="postgres",
            status=CheckStatus.OK,
            message=f"Connected to {host}:{port}.",
            latency_ms=latency,
        )
    return CheckResult(
        name="postgres",
        status=CheckStatus.FAIL,
        message=(
            f"Cannot connect to PostgreSQL at {host}:{port} — {err}. "
            f"Verify the host/port in storage.postgres.url and that the database "
            f"server is running (e.g. ``docker compose ps``)."
        ),
        latency_ms=latency,
    )


def _check_qdrant(config: Config) -> CheckResult:
    """Check qdrant — HTTP health-check against ``storage.qdrant.url/healthz``."""
    base_url = config.storage.qdrant.url.rstrip("/")
    health_url = f"{base_url}/healthz"

    headers: dict[str, str] = {}
    if config.storage.qdrant.api_key:
        headers["api-key"] = config.storage.qdrant.api_key

    start = time.monotonic()
    try:
        req = Request(health_url, headers=headers, method="GET")
        with urlopen(req, timeout=5) as resp:
            latency = (time.monotonic() - start) * 1000
            if resp.status == 200:
                return CheckResult(
                    name="qdrant",
                    status=CheckStatus.OK,
                    message=f"Qdrant at {base_url} is healthy.",
                    latency_ms=latency,
                )
            return CheckResult(
                name="qdrant",
                status=CheckStatus.FAIL,
                message=(
                    f"Qdrant at {base_url} returned HTTP {resp.status}. "
                    f"Check that the Qdrant service is running and that "
                    f"storage.qdrant.url is correct."
                ),
                latency_ms=latency,
            )
    except HTTPError as exc:
        latency = (time.monotonic() - start) * 1000
        return CheckResult(
            name="qdrant",
            status=CheckStatus.FAIL,
            message=(
                f"Qdrant at {base_url} returned HTTP {exc.code}: {exc.reason}. "
                f"If authentication is required, set "
                f"FINECORPUS_STORAGE__QDRANT__API_KEY."
            ),
            latency_ms=latency,
        )
    except URLError as exc:
        latency = (time.monotonic() - start) * 1000
        return CheckResult(
            name="qdrant",
            status=CheckStatus.FAIL,
            message=(
                f"Cannot connect to Qdrant at {base_url} — {exc.reason}. "
                f"Check that the Qdrant service is running "
                f"(e.g. ``docker compose ps``) and that storage.qdrant.url is correct."
            ),
            latency_ms=latency,
        )
    except Exception as exc:
        latency = (time.monotonic() - start) * 1000
        return CheckResult(
            name="qdrant",
            status=CheckStatus.FAIL,
            message=(f"Unexpected error connecting to Qdrant at {base_url}: {exc}."),
            latency_ms=latency,
        )


def _check_object_store(config: Config) -> CheckResult:
    """Check object_store — TCP reachability of the configured object-store endpoint.

    For MinIO/S3-compatible backends, parses the ``endpoint_url``; for AWS S3
    without an override, checks ``s3.amazonaws.com:443``.
    """
    backend = config.storage.object_store.backend.value
    endpoint_url = config.storage.object_store.endpoint_url

    if endpoint_url:
        try:
            parsed = urllib.parse.urlparse(endpoint_url)
            host = parsed.hostname or "localhost"
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
        except Exception as exc:
            return CheckResult(
                name="object_store",
                status=CheckStatus.FAIL,
                message=(
                    f"Could not parse storage.object_store.endpoint_url: {exc}. "
                    f"Expected a full URL, e.g. http://minio:9000"
                ),
            )
    elif backend == "s3":
        host = "s3.amazonaws.com"
        port = 443
    elif backend == "gcs":
        host = "storage.googleapis.com"
        port = 443
    elif backend == "azure_blob":
        host = "blob.core.windows.net"
        port = 443
    else:
        # minio without endpoint_url set
        return CheckResult(
            name="object_store",
            status=CheckStatus.FAIL,
            message=(
                f"Backend is '{backend}' but storage.object_store.endpoint_url is not "
                f"set. Set FINECORPUS_STORAGE__OBJECT_STORE__ENDPOINT_URL to the "
                f"MinIO endpoint (e.g. http://minio:9000)."
            ),
        )

    ok, err, latency = _tcp_check(host, port)
    bucket = config.storage.object_store.bucket
    if ok:
        return CheckResult(
            name="object_store",
            status=CheckStatus.OK,
            message=f"Object store ({backend}) reachable at {host}:{port}, bucket='{bucket}'.",
            latency_ms=latency,
        )
    return CheckResult(
        name="object_store",
        status=CheckStatus.FAIL,
        message=(
            f"Cannot connect to object store ({backend}) at {host}:{port} — {err}. "
            f"Verify the endpoint URL and that the storage service is running. "
            f"Bucket: '{bucket}'."
        ),
        latency_ms=latency,
    )


# ---------------------------------------------------------------------------
# SKIPPED stubs — Phase 1 wires real implementations
# ---------------------------------------------------------------------------


def _check_embedding_provider(config: Config) -> CheckResult:
    """SKIPPED — Embedding provider availability check.

    Phase 1 wires this to a real provider health_check() that:
    - Sends a probe string and confirms the endpoint is reachable.
    - Confirms the declared model ID is available.
    - Confirms probe embedding dimensions match ``providers.embedding.*.dimensions``.
    - Reports latency.
    - In airgap mode: skips cloud providers and confirms all providers are local.
    - Checks pricing staleness (§16, reference.md §3 check 6).
    """
    return CheckResult(
        name="embedding_provider",
        status=CheckStatus.SKIPPED,
        message=(
            "Embedding provider availability check is not yet implemented "
            "(Phase 1). Configure providers.embedding and re-run after Phase 1."
        ),
    )


def _check_resource_headroom(config: Config) -> CheckResult:
    """SKIPPED — Qdrant resource headroom check.

    Phase 1 wires this to query Qdrant for current collection memory usage
    and compare against ``index_lifecycle.hot_retention_count`` × estimated
    index size.  Warns (does not block) if available RAM appears insufficient.
    """
    return CheckResult(
        name="resource_headroom",
        status=CheckStatus.SKIPPED,
        message=(
            "Resource headroom check is not yet implemented (Phase 1). "
            "Ensure Qdrant has sufficient RAM for "
            f"{config.index_lifecycle.hot_retention_count} hot collection(s)."
        ),
    )


# ---------------------------------------------------------------------------
# Check registry
# ---------------------------------------------------------------------------

# Ordered list of (name, callable).  The name is informational; the
# callable receives the Config and returns a CheckResult.
_CHECKS: list[tuple[str, Callable[[Config], CheckResult]]] = [
    ("config_parse", _check_config_parse),
    ("postgres", _check_postgres),
    ("qdrant", _check_qdrant),
    ("object_store", _check_object_store),
    ("embedding_provider", _check_embedding_provider),
    ("resource_headroom", _check_resource_headroom),
]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run_preflight(config: Config) -> PreflightReport:
    """Execute all registered preflight checks against *config*.

    Returns a :class:`PreflightReport` whose :attr:`~PreflightReport.passed`
    property is ``True`` when no check has status ``FAIL``.

    ``SKIPPED`` checks are always included in the report — they are never
    silently omitted.

    Parameters
    ----------
    config:
        A fully validated :class:`~finecorpus.config.models.Config` instance.

    Returns
    -------
    PreflightReport
    """
    report = PreflightReport()
    for _name, check_fn in _CHECKS:
        result = check_fn(config)
        report.results.append(result)
    return report
