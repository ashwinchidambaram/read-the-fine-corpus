# ADR-0009 — Connector framework with framework-enforced permission fidelity

Status: **accepted**.
Governing spec: §14.2 (credential handling), §14.3 (source permission fidelity), §6.1 (collect / incremental sync).
Related:
[connectors/base.py](../../src/finecorpus/connectors/base.py) (`Connector`, `enforce_permission_fidelity`),
[connectors/oauth.py](../../src/finecorpus/connectors/oauth.py) (OAuth2 flow),
[connectors/fixtures/](../../src/finecorpus/connectors/fixtures/__init__.py) (`RecordedHTTPClient`),
[contracts/inventory.py](../../src/finecorpus/contracts/inventory.py) (`SourceRun`, `SourcePermissions`),
[contracts/shared/blocks.py](../../src/finecorpus/contracts/shared/blocks.py) (`PermissionFidelity`).

## Context

Phase 6 adds *connectors*: sources that ingest from external systems (SharePoint,
Confluence/Jira, Google Drive) which carry their own access controls. This ADR records the
framework abstraction only; the three concrete connectors are a later work unit.

Two properties drive the design:

1. **§14.3 is a platform guarantee, not a per-connector courtesy.** The spec states the
   platform MUST record source-side permissions and either mirror them into filterable fields
   or *refuse the connection*. The failure to prevent is a "permission-laundering machine": a
   SharePoint site with restricted folders ingested wholesale and served to users who were
   never entitled to it. If each connector had to remember to enforce this, a new connector
   that simply never checked would silently launder permissions — the guarantee would be
   forgeable by omission.

2. **CI must run connector tests with no live credentials and no network.** OAuth flows and
   paginated API listings are exactly the code most likely to rot untested. The concrete
   connectors need a deterministic test path from day one.

## Decision

### Framework shape mirrors the existing provider abstraction

`Connector` is an ABC with a small fixed surface (`authenticate`, `list_documents`,
`fetch_document`, `fetch_permissions`, `incremental_cursor`) plus a static
`ConnectorCapabilities` dataclass — the same shape as `EmbeddingProvider` /
`ProviderCapabilities`. A name→factory `registry` mirrors the embedding/llm registries so
concrete connectors register by name and the Collect stage builds one from a
`ConnectorConfig`.

### Permission fidelity is enforced in the framework

`enforce_permission_fidelity(connector_id, fidelity, source_run)` is a single pure function
that implements §14.3: `unavailable` fidelity is BLOCKED unless
`SourceRun.acknowledged_permission_gap is True`; `authoritative` and `best_effort` proceed.
`Connector.guard_permissions` routes every `fetch_permissions` result through it. The check is a
pure function so it is trivially unit-tested.

**Honesty about the guarantee's current strength (do not overstate).** Today the gate is
*enforced-if-called*, not structurally unbypassable: a caller that invokes
`PermissionRecord.to_source_permissions()` directly, or a Collect loop that omits
`guard_permissions`, can still launder permissions — exactly the failure §14.3 warns about.
Making it truly structural requires wiring the gate into the single ingestion seam so the only
path from a connector's permission data to a contract `SourcePermissions` runs through the
check. That wiring lands with the concrete connectors and is tracked as decision-ledger **D-42**.

Crucially, this **reuses the existing contracts machinery** rather than inventing a parallel
one. `PermissionFidelity` (the enum), `SourcePermissions` (the raw-ACL model on
`InventoryItem`), and `SourceRun.acknowledged_permission_gap` already exist from Phase 0.
`PermissionRecord.to_source_permissions()` is the single seam that converts connector
permission data into the contract `SourcePermissions` the Inventory carries. There is exactly
one permission model in the system.

### Secrets by reference only (§14.2)

`ConnectorConfig` (pydantic, `extra="forbid"`) carries secrets only by reference:
`token_ref` (a control-plane token handle) and `secret_env_vars` (env-var *names*). A literal
`client_secret` cannot be smuggled in because unknown keys are rejected — so config exports
stay secret-free by construction (§6.4). Token *values* are resolved through the abstract
`TokenStore` seam, whose concrete implementation lives in the control plane (encrypted at
rest). `OAuth2Config`/`OAuth2Token` override `__repr__` to redact credential material, and the
OAuth error path never echoes response bodies.

### Recorded-fixture testing strategy (no live creds in CI)

Connectors and the OAuth helper depend on a minimal `HTTPClient` protocol (one `request`
method returning a plain `HTTPResponse`), never on `httpx` directly. Production injects
`HttpxClient` (a thin adapter over the `httpx` already vendored via FastAPI — no new
dependency). Tests inject `RecordedHTTPClient`, which replays `{match, response}` JSON
interactions in recorded order and raises `UnmatchedRequestError` (an `AssertionError`
subclass) for any unrecorded request. That last property lets a test *prove* no unexpected
network call was attempted — the key enabler for the concrete-connector work unit.

### Import-linter placement

`finecorpus.connectors` joins the provider tier alongside `embedding | llm`:
`"finecorpus.embedding | finecorpus.llm | finecorpus.connectors"`. Connectors may import
`control | contracts` (below them) for the token-storage seam and inventory/permission
contracts, and are consumed by `pipeline`/`services` (above). They receive `ConnectorConfig`
by parameter and do not import `finecorpus.config`, avoiding an upward back-edge — the same
discipline the embedding/llm registries use.

## Consequences

- The §14.3 fail-closed check is a single tested gate every connector routes through; it is
  enforced-if-called today and becomes structurally unbypassable once wired into the single
  ingestion seam with the concrete connectors (D-42).
- CI runs connector tests deterministically with no credentials and no network.
- No new runtime dependency (httpx already present).
- Concrete connectors have a clear, minimal contract to implement and a ready fixture harness.
- The framework does not itself resolve permissions into `TenancyBlock.permission_principals`;
  it supplies the raw `SourcePermissions` and enforces the gate. Resolution into filterable
  fields remains Collect/Assess-stage work, wired in the concrete-connector work unit.
