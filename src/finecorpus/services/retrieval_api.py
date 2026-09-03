"""Retrieval API service stub.

Sole owner of the vector-DB connection pool (C-1, C-2).
Business logic will live in finecorpus.retrieval; this module is wiring only.
"""

from fastapi import FastAPI

import finecorpus

app = FastAPI(title="retrieval-api", version=finecorpus.__version__)

_SERVICE_NAME = "retrieval-api"


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"service": _SERVICE_NAME, "status": "ok", "version": finecorpus.__version__}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("finecorpus.services.retrieval_api:app", host="0.0.0.0", port=8000, reload=False)
