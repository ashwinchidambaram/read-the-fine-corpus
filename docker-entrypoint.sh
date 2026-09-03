#!/bin/bash
# Selects the finecorpus service entrypoint based on $SERVICE.
set -euo pipefail

SERVICE="${SERVICE:-retrieval-api}"

case "$SERVICE" in
    retrieval-api)
        exec uvicorn finecorpus.services.retrieval_api:app \
            --host 0.0.0.0 --port 8000 --no-access-log
        ;;
    control-api)
        exec uvicorn finecorpus.services.control_api:app \
            --host 0.0.0.0 --port 8000 --no-access-log
        ;;
    embedding-service)
        exec uvicorn finecorpus.services.embedding_service:app \
            --host 0.0.0.0 --port 8000 --no-access-log
        ;;
    ingest-worker)
        exec python -m finecorpus.services.ingest_worker
        ;;
    *)
        echo "Unknown SERVICE '${SERVICE}'. Valid values: retrieval-api, control-api, embedding-service, ingest-worker" >&2
        exit 1
        ;;
esac
