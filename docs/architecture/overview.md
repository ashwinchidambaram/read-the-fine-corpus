# Architecture Overview

Status: **proposed** (design review pending). Governing spec: §4, §5, §11.
Decisions here are recorded as ADRs under [`docs/adr/`](../adr/).

Read The Fine Corpus is one core library (`finecorpus`) consumed by five deployable
services, a CLI, and a web UI. Business logic lives in the library; every other artifact is a
thin consumer (constraint C-5).

## Services

| Service | Process | Responsibility | Scaling |
|---|---|---|---|
| `retrieval-api` | FastAPI | All query traffic: REST, **MCP server** (in-process, same auth and tenancy path), explain mode. Sole owner of the vector-DB connection pool. Stateless (C-1). | Horizontal replicas behind LB |
| `ingest-worker` | worker loop | Pipeline stages Collect→Build. Queue-driven, checkpointed, resumable. Never in the request path. | Worker count |
| `embedding-service` | FastAPI | Provider abstraction, request batching, query-embedding cache. Shared by ingestion and retrieval. | Horizontal; batch tuning |
| `control-api` | FastAPI | Tenancy, RBAC, break-glass, config, job orchestration, audit log, cost accounting. | Horizontal |
| `web-ui` | static + SPA | Client of `control-api` and `retrieval-api`. Holds no business logic. | CDN/static |

The CLI (`corpus`) is a client of `control-api` (and local library for offline preflight),
never a second implementation path.

## Backing stores

| Store | Reference implementation | Holds |
|---|---|---|
| Vector DB | Qdrant (adapter interface for others, §4.4) | Chunk vectors + payloads. Accessed **only** by `retrieval-api` and `ingest-worker` via the adapter (C-2). |
| Control-plane DB | PostgreSQL | Tenancy, RBAC, KB config, job state + checkpoints, audit log, tombstone log, inventory metadata, eval sets, baselines, cost ledger. |
| Object storage | S3-compatible (MinIO in compose) | Original documents, stage artifacts, cold index snapshots, exports. All storage locations declared in config (§17.2). |
| Job queue | PostgreSQL (`SELECT … FOR UPDATE SKIP LOCKED`) | Ingestion and maintenance jobs. Boring and inspectable by design; no extra broker. |

## Data flow

```
                    ┌────────────────────── control-api ──────────────────────┐
                    │  tenancy · config · jobs · audit · budgets              │
                    └──────────────┬──────────────────────────────────────────┘
                                   │ enqueue job
 sources ──► ingest-worker: Collect ─► Assess ─► Decompose ─► Plan ─► Build ──► shadow collection
             (inventory)   (findings)  (segments)  (config)   │                    │ validate (§10.4)
                                   artifacts to object store ◄┘                    ▼ atomic alias swap
                                                                              live collection
 callers (REST / MCP / Python client)                                              ▲
        └────► retrieval-api ──► embedding-service (query embedding, cached)       │
                    └────────────── vector search via alias only (C-3) ────────────┘
```

Each stage emits a durable, inspectable artifact (§5) and consumes the previous stage's
contract (§12); a stage rejects a contract version it does not declare support for.

## Core library decomposition

```
src/finecorpus/
  contracts/        # §12: pydantic v2 models — the six contracts + shared blocks
                    #   (tenancy block, provenance block, source location, versioning)
  pipeline/
    collect/  assess/  decompose/  plan/  build/     # one package per stage
  embedding/        # provider interface; openai/, ollama/ reference impls
  llm/              # internal LLM ops (§7.3): classify, describe, questions, rewrite
  index/            # vector-backend adapter interface; qdrant/ impl;
                    # lifecycle: alias, shadow, promotion, retention, tombstone replay
  retrieval/        # query planning, tenancy filter injection, rerank, explain
  evalx/            # eval generation, sweep, drift ("eval" avoided: stdlib shadowing)
  tenancy/          # roles, grants, break-glass
  config/           # single-file config model, env overrides, preflight
  jobs/             # job model, checkpointing, queue claim/heartbeat
  audit/            # append-only audit records
  observability/    # structured logs, OTel traces, Prometheus metrics
  cli/              # `corpus` — consumer of the public API only
  services/         # thin FastAPI/worker entrypoints wiring the library
```

## How the hard constraints are enforced (not just stated)

| Constraint | Enforcement |
|---|---|
| C-1 retrieval stateless | No local persistence in `retrieval-api`; all state in Qdrant/Postgres. Kill-a-replica test in the concurrency layer (§18.2). |
| C-2 no direct vector-DB clients | Vector-DB credentials are provisioned only to `retrieval-api` and `ingest-worker`; compose/Helm never exposes the Qdrant port beyond the internal network; docs never publish a direct-access path. |
| C-3 queries target alias | The adapter's search operations accept an alias handle type, not a collection name; collection names are private to `index/`. |
| C-4 ingestion writes shadow | `index/` exposes `create_shadow`/`promote` only; there is no write path targeting the alias target. |
| C-5 logic in library | `cli/` and `services/` import only the public `finecorpus` API; an import-linter contract in CI fails on violation. |

## Tenancy from day zero

Every contract carries a tenancy block (`workspace_id`, `kb_id`, permission fields) from
Phase 0, enforced from Phase 4 (§19 Phase 0 MUST). The retrieval service injects tenant
filters server-side; no client-supplied parameter can widen them (§11.4).

### Tenant isolation mechanism

The non-overridability of the tenant filter is a **mechanism**, not an assertion (S-R1). The
retrieval service constructs the vector-DB filter as follows, on every query and on every path
including explain mode:

- **Tenancy conditions are mandatory `must` clauses.** The service builds `must` clauses on
  `tenancy.kb_id` (and, where relevant, `tenancy.workspace_id`) plus the permission predicate on
  `permission_principals`/`permission_mode`. These are **derived exclusively from the authenticated
  principal's resolved scope** — the identity and KB/workspace membership established by
  `control-api`, never from anything in the request body.
- **Client-supplied filters are AND-merged underneath.** Any caller-supplied filter expression is
  attached as an **additional** `must` clause conjoined with the tenancy clauses. It can only
  *narrow* the result set; it can never replace, `should`-away, or `OR` around the tenancy `must`.
- **The request schema contains no scope-widening field.** There is no request parameter for
  `kb_id`, `workspace_id`, or permission principals that the caller can set to reach another
  tenant's data. Scope is a property of the authenticated session, resolved server-side.
- **Explain mode is not a bypass.** The same mandatory-`must` injection applies to explain queries;
  §11.5 explicitly requires explain to respect tenancy and permission rules. Explain output never
  contains cross-tenant candidates, exclusions, or scores.

**Test (§18.3 test 2).** A forged-filter attempt — a request that tries to widen or replace the
tenancy clause (e.g. supplying a filter that `OR`s in another `kb_id`, or a crafted payload query)
— MUST **fail closed**: the injected tenancy `must` clauses still bind, so the forged predicate
cannot broaden scope and the query returns no cross-tenant content. A raw vector-DB query that
bypasses the retrieval service is the only way to see cross-tenant content, confirming enforcement
lives in the service (C-2 keeps the vector DB private to `index/` and `retrieval-api`).

## Proposed stack (ADR-0003)

Python ≥3.12 managed by uv · FastAPI + pydantic v2 · qdrant-client · SQLAlchemy + Alembic
(Postgres) · S3-compatible client for object storage · pytest + hypothesis · ruff · mypy ·
OpenTelemetry + prometheus-client. Docker Compose is the reference deployment (§4.3); Helm in
Phase 7.

## Related pages

- [Index lifecycle](index-lifecycle.md) — alias indirection, shadow build, promotion, retention
- [Provider abstraction](provider-abstraction.md) — embedding + internal LLM providers
- [Segment taxonomy](segment-taxonomy.md) — segment types and salience tiers
- [Contracts](../contracts/) — the six inter-stage data contracts
- [MUST traceability](../process/must-traceability.md) — every spec requirement mapped

## Open questions

1. Whether `embedding-service` is a separate process from day one or a library component
   promoted to a service at Phase 4 scale-out — compose ships it separate (spec §4.1 requires
   independent scalability; separate from the start avoids a later split).
2. MCP transport: in-process with `retrieval-api` (proposed, same auth path) vs sidecar.
3. Whether the web UI ships as its own container or is served by `control-api` in the
   single-node compose (proposed: own container, static).
