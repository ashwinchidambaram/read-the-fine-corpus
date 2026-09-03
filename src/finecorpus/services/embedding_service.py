"""Embedding service stub.

Provider abstraction, request batching, query-embedding cache.
Shared by ingestion and retrieval paths.
Business logic will live in finecorpus.embedding; this module is wiring only.
"""

from fastapi import FastAPI

import finecorpus

app = FastAPI(title="embedding-service", version=finecorpus.__version__)

_SERVICE_NAME = "embedding-service"


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"service": _SERVICE_NAME, "status": "ok", "version": finecorpus.__version__}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "finecorpus.services.embedding_service:app", host="0.0.0.0", port=8000, reload=False
    )
