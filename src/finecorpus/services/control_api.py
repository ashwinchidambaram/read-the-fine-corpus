"""Control API service (Phase 4).

Tenancy, RBAC, break-glass, job orchestration, audit log, cost accounting,
key management, tombstones.  Business logic lives in ``finecorpus.control``;
this module is wiring only (C-5, router includes below /healthz and /metrics).

/metrics endpoint: prometheus-client exposition for all five telemetry
domains (M-102).
"""

from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.responses import Response

import finecorpus
from finecorpus.services.control_routes.break_glass import router as break_glass_router

logger = logging.getLogger(__name__)

app = FastAPI(title="control-api", version=finecorpus.__version__)

_SERVICE_NAME = "control-api"

# ---------------------------------------------------------------------------
# Router includes — one line per Phase unit.  Sibling units (MCP, etc.) add
# their own include lines here without touching the break-glass router.
# ---------------------------------------------------------------------------

app.include_router(break_glass_router)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


@app.get("/healthz", tags=["ops"])
def healthz() -> dict[str, str]:
    return {"service": _SERVICE_NAME, "status": "ok", "version": finecorpus.__version__}


# ---------------------------------------------------------------------------
# Metrics (prometheus-client exposition)
# ---------------------------------------------------------------------------


@app.get("/metrics", tags=["ops"], response_class=Response)
def metrics() -> Response:
    """Prometheus /metrics endpoint (M-102).

    Exposes all five telemetry domains:
    ingestion, index, retrieval, quality (stubs), governance.
    """
    import prometheus_client

    data = prometheus_client.generate_latest()
    return Response(
        content=data,
        media_type=prometheus_client.CONTENT_TYPE_LATEST,
    )


# ---------------------------------------------------------------------------
# Control routes — additive include_router block
# A sibling PR adds a break_glass router here; keep this block additive.
# ---------------------------------------------------------------------------

from finecorpus.services.control_routes import audit as _audit_routes  # noqa: E402
from finecorpus.services.control_routes import budgets as _budgets_routes  # noqa: E402
from finecorpus.services.control_routes import jobs as _jobs_routes  # noqa: E402
from finecorpus.services.control_routes import keys as _keys_routes  # noqa: E402
from finecorpus.services.control_routes import tombstones as _tombstones_routes  # noqa: E402

app.include_router(_jobs_routes.router)
app.include_router(_audit_routes.router)
app.include_router(_budgets_routes.router)
app.include_router(_keys_routes.router)
app.include_router(_tombstones_routes.router)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("finecorpus.services.control_api:app", host="0.0.0.0", port=8001, reload=False)
