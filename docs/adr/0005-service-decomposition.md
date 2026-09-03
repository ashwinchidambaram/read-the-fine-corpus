# ADR 0005: Service Decomposition and Library Boundary

**Status:** Proposed  
**Date:** 2026-09-03  
**Deciders:** Ashwin Chidambaram (product owner), build orchestrator

## Context

The specification (Section 4.1) requires that ingestion, retrieval, control, and UI concerns be independently scalable and deployable. Each responsibility has different scaling characteristics:

- **Retrieval** is a read-heavy, stateless workload that scales horizontally with query volume.
- **Ingestion** is a batch workload that scales with corpus size and complexity.
- **Embedding** is a compute-bound workload best scaled independently based on demand from both ingestion and retrieval.
- **Control plane** handles metadata, configuration, job orchestration, and audit.
- **Web UI** is a stateless client of the control plane.

The constraint C-5 states: "Business logic MUST live in the core library, not in the CLI or the UI. Both are consumers."

This ADR specifies how to achieve that separation and the interfaces between services.

## Decision

Five independently deployable services plus one shared library:

| Service | Responsibility | Deployment |
|---|---|---|
| **retrieval-api** | Stateless query serving; sole owner of vector DB connection pool | Horizontally scalable HTTP service |
| **ingest-worker** | Long-running ingestion jobs, resumable after failure; driven by job queue | Worker pool; one or more instances |
| **embedding-service** | Embedding generation with provider abstraction, batching, query-cache; shared by retrieval and ingestion | Separate process from day one; independently scalable |
| **control-api** | Tenancy, RBAC, config, job orchestration, audit, metrics | Stateless HTTP service |
| **web-ui** | Browser client; holds no business logic | Static/SPA deployment or served by control-api |
| **finecorpus library** | All business logic, data contracts, segment processing, validation | Imported by all services and the CLI |
| **corpus CLI** | Client for control-api and retrieval-api; never a second implementation of business logic | End-user tool; imports finecorpus library |

**MCP Server placement:** The MCP server that exposes retrieval to Claude and other agents runs in-process with the retrieval-api, sharing its authentication and tenancy paths. It does not introduce a separate service.

**Embedding service independence:** The embedding-service is a separate process from day one, not an internal library call within retrieval or ingestion. This allows independent scaling, provider swaps, and fault isolation.

**CLI as client:** The `corpus` command-line tool is a client of the control and retrieval APIs, never a second implementation. If a feature is needed, it is added to the library and exposed through the API, which both the CLI and UI then consume.

## Rationale

- **Separation of concerns:** Each service owns one failure domain and one scaling axis. Retrieval latency does not degrade when ingestion is CPU-bound.
- **Library enforcement:** All business logic lives in one place (`finecorpus`), preventing divergence between the CLI, UI, and services.
- **No duplicate logic:** The CLI is not a standalone tool with its own implementation. It is a thin client over the APIs provided by the system.
- **Independent embedding:** Embedding is a shared, expensive resource. Making it a separate service allows dedicated tuning and capacity planning, and enables swapping providers or models without redeploying retrieval and ingestion services.
- **Stateless HTTP services:** Retrieval and control are both stateless, enabling standard Kubernetes patterns (deployments, autoscaling, rolling updates) and simple horizontal scaling.

## Consequences

- **Job queue is the coordination mechanism:** Ingestion workers pull jobs from the PostgreSQL queue. The control plane enqueues jobs and monitors progress. No inter-service RPC beyond the APIs.
- **Vector DB access:** Only the retrieval-api accesses the vector database directly. Ingest workers write to a shadow collection via an internal task system, but do not own the connection pool.
- **Configuration distribution:** All services read the same configuration from the control plane database at startup and periodically refresh. Coordinating a configuration change requires a brief service restart or dynamic reloading.
- **Import-linter enforcement:** A CI check enforces that code in `services/` and `cli/` imports only from the public API of `finecorpus.library`. This prevents internal dependencies from leaking into services.

## Alternatives Considered

- **Monolithic service:** All concerns in one process. Simpler to deploy initially, but scaling is all-or-nothing. Ingestion CPU pressure slows retrieval. Not suitable for production use cases.
- **Embedding as a library within services:** Each service imports an embedding library. Simple at first, but makes it impossible to implement batching across services, difficult to swap providers without redeploying all services, and wastes compute on duplicate batches from different services calling separately.
- **Separate CLI implementation:** The `corpus` tool could implement its own business logic. Avoids the latency of calling APIs for local operations. Creates immediate divergence — CLI and API versions of a feature quickly differ, and bugs must be fixed in two places. Against constraint C-5.
