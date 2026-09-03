# Read The Fine Corpus

Turns a heterogeneous document corpus into a production retrieval endpoint, and explains every decision it made along the way.

![Status: pre-alpha — Phase 0 wiring stubs](https://img.shields.io/badge/Status-pre--alpha_%E2%80%94_Phase_0_wiring_stubs-orange)

## Quickstart

```bash
cp .env.example .env          # fill in POSTGRES_PASSWORD and MINIO_ROOT_PASSWORD
docker compose up             # pulls images, builds service images, starts everything
curl localhost:8080/healthz   # {"service":"retrieval-api","status":"ok","version":"0.0.1"}
curl localhost:8081/healthz   # {"service":"control-api","status":"ok","version":"0.0.1"}
```

**What you get in Phase 0:** wiring proof only. All four services boot and answer health
checks; no retrieval pipeline exists yet. The ingest worker runs a heartbeat loop. Phase 1
delivers the thin end-to-end slice (documents in, chunks out, alias-backed retrieval endpoint).

Host-exposed ports: `8080` retrieval-api, `8081` control-api, `9001` MinIO console.
Qdrant is internal-only (constraint C-2 — no host port mapping).

## Overview

Read The Fine Corpus is a self-hostable retrieval platform. The system owns everything up to and including the retrieval endpoint and nothing above it — it is the foundation layer, not the application.

## Documentation

- **[Specification](spec-read-the-fine-corpus.md)** — full specification and design document
- **[Documentation](docs/)** — versioned wiki including architecture, contracts, configuration, and runbooks

## Development

This project uses `uv` for Python tooling. See the docs/ directory for architecture and design decisions.
