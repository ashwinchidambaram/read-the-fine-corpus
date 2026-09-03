# ADR 0003: Technology Stack and Dependencies

**Status:** Accepted  
**Date:** 2026-09-03  
**Deciders:** Ashwin Chidambaram (product owner), build orchestrator

## Context

The specification (Section 4.2, build rule 10) mandates "boring, inspectable implementations." Read The Fine Corpus is infrastructure software intended for self-hosting and debugging by teams with varying levels of expertise. Every dependency choice must optimize for:

1. Debuggability — a self-hoster should be able to read the code and understand what is happening
2. Operational simplicity — minimal failure domains and infrastructure to operate
3. Community maturity — large, active communities with good documentation
4. Standard practices — mainstream patterns that teams recognize

## Decision

| Component | Choice | Rationale |
|---|---|---|
| **Language & runtime** | Python >= 3.12, managed by `uv` | Python is accessible to most infrastructure teams; `uv` is fast, reproducible, and handles lockfiles well. No system Python version concerns. |
| **Web framework** | FastAPI + Pydantic v2 | FastAPI's async-native design fits the I/O-heavy retrieval workload; Pydantic v2 provides contract validation directly in the framework layer. Both are actively maintained. |
| **Vector database** | Qdrant (reference backend) | Spec §4.4 designates Qdrant as the reference backend. Qdrant's support for atomic alias swaps, payload filtering, and hybrid search is mature and well-documented. |
| **Control plane DB** | PostgreSQL via SQLAlchemy + Alembic | Standard choice for application state; SQLAlchemy abstracts database details while remaining inspectable; Alembic provides versioned, reviewable migrations. No vendor lock-in. |
| **Job queue** | PostgreSQL with `SELECT ... FOR UPDATE SKIP LOCKED` | Eliminates a separate broker (Redis, RabbitMQ, etc.). Single-node self-hosted deployments are simpler and inspectable via SQL. The `SKIP LOCKED` pattern handles concurrent workers without external coordination. |
| **Object storage** | S3-compatible (MinIO in reference compose) | S3 API is standard and widely understood. MinIO provides the reference implementation for self-hosted deployments. Cloud deployments use native S3, Azure Blob, or compatible services. |
| **Testing** | pytest + hypothesis | pytest is the industry standard for Python; hypothesis enables property-based testing to catch edge cases in transformation logic. |
| **Linting & formatting** | ruff | Single tool for both linting and formatting; fast; opinionated defaults reduce configuration overhead. |
| **Type checking** | mypy | Static type checking catches contract violations and refactoring errors early; well-integrated with Python's type hints. |
| **Observability** | OpenTelemetry + prometheus-client | OpenTelemetry enables observability without lock-in; prometheus-client exports metrics that every monitoring stack understands. Standard for infrastructure products. |

## Rationale

- **Pydantic v2 for contracts:** Pydantic v2 gives the contract validation layer directly in the API and library boundaries, replacing the need for separate validation schemes. Every payload is checked against the spec's data contracts at the framework layer.
- **PostgreSQL as job queue:** Removing a separate broker (Redis, Celery, RabbitMQ) eliminates an entire failure domain for single-node deployments. Teams running a database already have operational tooling for it. The queue state is inspectable and debuggable with SQL. Concurrent ingestion workers coordinate via standard database lock semantics.
- **Boring choices throughout:** Every choice is mainstream with large, active communities. There are no bespoke systems, no young frameworks, no single-author dependencies. A team self-hosting this system will find familiar tools in each layer.

## Consequences

- **Single database:** The control plane, configuration, job queue, and audit log all live in one PostgreSQL database. This simplifies deployment (no separate infrastructure for different concerns) but requires careful schema design to avoid bottlenecks under load. Indexes and partitioning strategies are important.
- **No Celery/Redis:** Teams expecting those tools will need to understand the polling-based job model instead. The tradeoff is worth it for single-node simplicity.
- **Qdrant-specific features:** The system uses Qdrant's atomic alias swap, collection management, and payload indexing as first-class operations. Adapters for other backends must support these primitives, or the platform must declare the limitation explicitly.

## Alternatives Considered

- **Celery + Redis:** Traditional choice for async task queues. Adds operational complexity and a separate failure domain. Requires Redis expertise and monitoring. For single-node deployments, overkill. For multi-node deployments, PostgreSQL-based queues are inspectable via the same database already in use.
- **SQLite for control plane:** Simpler than PostgreSQL and adequate for single-node deployments. However, SQLite's concurrent-write limitations and lack of row-level locking make it unsuitable for a shared queue under multiple concurrent workers. PostgreSQL is required for the job queue to work well, so the choice is made.
- **Django ORM + Django admin:** Heavier than needed for an API-only set of services. FastAPI + Pydantic + SQLAlchemy is more modular and scales to a larger codebase without the overhead of Django's full feature set. Django's admin is not useful when the UI is a separate frontend service.
- **Other vector databases (pgvector, Weaviate, Milvus):** Qdrant is the spec's reference backend because its alias and payload primitives are mature and reliable. Other backends can be added via the adapter interface (§4.4) once the reference backend is solid.
