"""Control API service.

Tenancy, RBAC, break-glass, config, job orchestration, audit log, cost accounting.
Business logic lives in finecorpus.control.*; this module is wiring only (router includes).

Phase 4: break-glass routes added.
"""

from fastapi import FastAPI

import finecorpus
from finecorpus.services.control_routes.break_glass import router as break_glass_router

app = FastAPI(title="control-api", version=finecorpus.__version__)

_SERVICE_NAME = "control-api"

# ---------------------------------------------------------------------------
# Router includes — one line per Phase unit.  Sibling units (MCP, etc.) add
# their own include lines here without touching the break-glass router.
# ---------------------------------------------------------------------------

app.include_router(break_glass_router)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"service": _SERVICE_NAME, "status": "ok", "version": finecorpus.__version__}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("finecorpus.services.control_api:app", host="0.0.0.0", port=8000, reload=False)
