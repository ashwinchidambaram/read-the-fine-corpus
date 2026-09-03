"""Control API service stub.

Tenancy, RBAC, break-glass, config, job orchestration, audit log, cost accounting.
Business logic will live in finecorpus.tenancy / finecorpus.jobs; this module is wiring only.
"""

from fastapi import FastAPI

import finecorpus

app = FastAPI(title="control-api", version=finecorpus.__version__)

_SERVICE_NAME = "control-api"


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"service": _SERVICE_NAME, "status": "ok", "version": finecorpus.__version__}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("finecorpus.services.control_api:app", host="0.0.0.0", port=8000, reload=False)
