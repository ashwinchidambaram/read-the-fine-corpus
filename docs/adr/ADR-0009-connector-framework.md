# ADR-0009 — Connector framework with framework-enforced permission fidelity

Status: **accepted**.
Governing spec: §14.2 (credential handling), §14.3 (source permission fidelity), §6.1 (collect / incremental sync).
Related:
[connectors/base.py](../../src/finecorpus/connectors/base.py) (`Connector`, `Connector.collect`),
[connectors/gate.py](../../src/finecorpus/connectors/gate.py) (`enforce_permission_fidelity`),
[connectors/models.py](../../src/finecorpus/connectors/models.py) (`PermissionRecord.to_source_permissions`, `CollectedItem`),
[connectors/oauth.py](../../src/finecorpus/connectors/oauth.py) (OAuth2 flow),
[connectors/gdrive.py](../../src/finecorpus/connectors/gdrive.py), [connectors/sharepoint.py](../../src/finecorpus/connectors/sharepoint.py), [connectors/atlassian.py](../../src/finecorpus/connectors/atlassian.py) (concrete connectors),
[connectors/fixtures/](../../src/finecorpus/connectors/fixtures/__init__.py) (`RecordedHTTPClient`),
[contracts/inventory.py](../../src/finecorpus/contracts/inventory.py) (`SourceRun`, `SourcePermissions`),
[contracts/shared/blocks.py](../../src/finecorpus/contracts/shared/blocks.py) (`PermissionFidelity`),
[runbooks/connectors.md](../runbooks/connectors.md) (operator credential + live smoke-test procedure).

## Context

Phase 6 adds *connectors*: sources that ingest from external systems (SharePoint,
Confluence/Jira, Google Drive) which carry their own access controls. This ADR records the
framework abstraction **and** the three concrete connectors, which are implemented on it:
`GoogleDriveConnector` (Drive v3 REST), `SharePointConnector` (Microsoft Graph, `delta` sync),
and `AtlassianConnector` (one class serving both Confluence and Jira). All three call the
provider REST APIs directly over the `HTTPClient` seam — **no provider SDKs** — to keep the
platform self-hostable / air-gapped (§6.4) and to keep every test replayable from recorded
fixtures. `AtlassianConnector` registers under both the `confluence` and `jira` source kinds;
the product is selected by `source_kind` (or an explicit `options.product`).

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
The framework-owned `Connector.collect` loop routes every `fetch_permissions` result through it
(via the gated `PermissionRecord.to_source_permissions` converter). The check is a pure function
so it is trivially unit-tested. `collect` is the **single** enforcement entry point — there is
no separate `guard_permissions` method to remember to call (removed to keep one narrative).

**D-42 — the gate is structural for the connector ingestion path.**
Previously the gate was *enforced-if-called*: a caller could invoke
`PermissionRecord.to_source_permissions()` directly, or a subclass could override the collect
loop, and restricted content would launder through as public — exactly the failure §14.3 warns
about. Within the connector framework that hole is now closed two independent, defence-in-depth
ways, and neither depends on a connector remembering to guard:

1. **No un-gated converter exists (for connector data).** `PermissionRecord.to_source_permissions`
   *requires* `connector_id` and `source_run` and runs `enforce_permission_fidelity` **before**
   it emits anything. There is no argument-free path from connector permission data to a contract
   `SourcePermissions`.
2. **The framework owns the sole, override-proof producer.** `Connector.collect(source_run)` is a
   framework-owned generator — the only producer of ingestable `(RawDocument, SourcePermissions)`
   pairs, packaged as a `CollectedItem`. It is `@typing.final` **and** guarded by
   `Connector.__init_subclass__`, which raises `TypeError` at class-definition time if a subclass
   tries to define its own `collect`; a subclass therefore cannot override the loop to route
   around the gate. A concrete connector implements only `list_documents` / `fetch_document` /
   `fetch_permissions`; it never constructs a `SourcePermissions` itself. `collect()` fetches
   permissions and routes them through the gated converter **before** downloading bytes, so a
   blocked document is never even fetched, and `CollectedItem.permissions` is typed to the
   contract `SourcePermissions` (not a raw `PermissionRecord`).

**Scope boundary, stated honestly.** This is NOT a global lock on the `SourcePermissions`
contract model. `SourcePermissions` is a plain public pydantic model that non-connector pipeline
stages (e.g. `assess/stage.py`, `plan/stage.py`) may legitimately construct, and the framework
neither can nor should prevent that. The truthful, scoped claim is: *within the connector framework,
`collect()` is the SOLE producer of ingested items and is override-proof; it ALWAYS routes
permission data through `enforce_permission_fidelity` before fetching content.* A hand-built
`SourcePermissions` outside the framework is possible and out of the connector framework's
control — by design, not a hole.

The pure gate function itself lives in the leaf module `connectors/gate.py` so both the data
models and the base can route through the exact same check without an import cycle.
`tests/connectors/test_d42_structural_gate.py` proves it: a deliberately hostile `RogueConnector`
is blocked by both `collect()` and the direct converter and its bytes are never downloaded;
defining a subclass that overrides `collect` raises `TypeError`; constructing a `CollectedItem`
from a raw `PermissionRecord` is a validation error; and — documenting the scope boundary
honestly — a hand-built `SourcePermissions` is shown to be freely constructible, not asserted
impossible.

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

- The §14.3 fail-closed check is a single tested gate on the *only* path from connector
  permission data to the inventory; within the connector framework it is now **structural**
  (override-proof `collect()` + gated converter), not merely enforced-if-called. It is scoped to
  the connector ingestion path, not a global lock on the `SourcePermissions` contract model
  (which non-connector stages legitimately construct).
- CI runs connector tests deterministically with no credentials and no network. The three
  concrete connectors ship with recorded fixtures covering auth, pagination, fetch, the
  permission→`SourcePermissions` mapping, and the fail-closed path; plus the D-42 structural
  test. Live credentials are exercised only by an operator via `runbooks/connectors.md`.
- No new runtime dependency: the connectors call provider REST APIs over the existing `httpx`
  (`HttpxClient`); no `google-api-python-client`, `msgraph`, or `atlassian-python-api` SDK.
- Per-connector fidelity is honest about what each source can supply: Drive/SharePoint/Confluence
  can reach `authoritative` per-item ACLs; a failed ACL call degrades to `unavailable`. A
  **Confluence** page with *no page-level read restriction* inherits its space ACL, which the
  connector does not fetch; rather than assert an `authoritative` empty read-list (which would
  launder a space-scoped ACL as "restricted to nobody"), it degrades that case to `best_effort` —
  honestly signalling the ACL is incomplete. (Fetching space membership to restore
  `authoritative` is a documented follow-up.) **Jira** cannot expose issue-level security as a
  filterable ACL, so its connector reports `unavailable` for every issue by design — a Jira run
  fails closed unless the operator acknowledges the gap.
- Google Drive's incremental watermark is currently an **approximation**, not the real Drive
  `changes.*` delta API (documented in a `list_documents` TODO); implementing genuine
  `changes.getStartPageToken` / `changes.list` delta sync is a follow-up.
- The framework does not itself resolve permissions into `TenancyBlock.permission_principals`;
  it supplies the gated `SourcePermissions` via `CollectedItem` and enforces the §14.3 gate.
  Resolution into filterable fields remains Collect/Assess-stage work.
