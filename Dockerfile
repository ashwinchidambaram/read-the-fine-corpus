# Multi-stage uv-based build for all finecorpus services.
#
# Build arg SERVICE selects the entrypoint:
#   retrieval-api     -> finecorpus.services.retrieval_api:app  (uvicorn, port 8000)
#   control-api       -> finecorpus.services.control_api:app    (uvicorn, port 8000)
#   embedding-service -> finecorpus.services.embedding_service:app (uvicorn, port 8000)
#   ingest-worker     -> finecorpus.services.ingest_worker      (plain Python loop)
#
# Usage:
#   docker build --build-arg SERVICE=retrieval-api -t rtfc/retrieval-api .

ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.7.21-python3.12-bookworm-slim

# ---------------------------------------------------------------------------
# Stage 1: build — install dependencies with uv
# ---------------------------------------------------------------------------
FROM ${UV_IMAGE} AS builder

WORKDIR /app

# Copy lockfile, project metadata, and README (required by uv_build) first for layer caching
COPY pyproject.toml uv.lock README.md ./
COPY src/ ./src/

# Install all dependencies (including the editable package) into /app/.venv
# --frozen: respect lockfile; --no-cache: keep image lean
RUN uv sync --frozen --no-cache

# ---------------------------------------------------------------------------
# Stage 2: runtime — lean image, non-root user
# ---------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS runtime

WORKDIR /app

# Non-root user for security
RUN groupadd --gid 1001 finecorpus && \
    useradd --uid 1001 --gid finecorpus --shell /bin/bash --create-home finecorpus

# Copy the built venv and project source from the builder stage
COPY --from=builder --chown=finecorpus:finecorpus /app/.venv /app/.venv
COPY --from=builder --chown=finecorpus:finecorpus /app/src /app/src
COPY --from=builder --chown=finecorpus:finecorpus /app/pyproject.toml /app/pyproject.toml

# Put the venv on PATH
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# SERVICE selects the entrypoint at container start time (can also be set via
# docker-compose environment:)
ARG SERVICE=retrieval-api
ENV SERVICE=${SERVICE}

USER finecorpus

# Default healthcheck for HTTP services (port 8000/healthz).
# The ingest-worker overrides this in docker-compose.yml with a pgrep check.
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import sys,urllib.request; urllib.request.urlopen('http://localhost:8000/healthz',timeout=4); sys.exit(0)"

# Entrypoint script selects the right process based on $SERVICE
COPY --chown=finecorpus:finecorpus docker-entrypoint.sh /app/docker-entrypoint.sh
RUN chmod +x /app/docker-entrypoint.sh

EXPOSE 8000

ENTRYPOINT ["/app/docker-entrypoint.sh"]
