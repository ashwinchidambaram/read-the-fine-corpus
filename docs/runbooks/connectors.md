# Connectors Runbook — supplying real credentials and running a live smoke test

**Triggering event:** Operator configuring a source connector (Google Drive, SharePoint,
Atlassian/Confluence, Atlassian/Jira) with real credentials for the first time, or verifying a
connector against the live provider after a credential rotation.
**Phase:** 6 (this runbook ships with the three concrete connectors).
**Related:** [ingest runbook](ingest.md),
[ADR-0009 — connector framework](../adr/ADR-0009-connector-framework.md),
spec §14.2 (credential handling), §14.3 (source permission fidelity), §4.5 / §6.1 (connectors).

---

## Why CI cannot do this

Connector tests in this repo run entirely against **recorded HTTP fixtures**
(`tests/connectors/fixtures/*.json`, replayed by `RecordedHTTPClient`). They make **no network
calls** and use **no real credentials** — the replay client raises `UnmatchedRequestError` on
any request without a recorded match, so an accidental live call fails the test loudly. That is
the correct posture for CI, but it means the *live* auth handshake against a real provider can
only be exercised by an operator. This runbook is that procedure.

---

## Credential handling rules (§14.2) — read first

- Credentials are supplied **only through environment variables**, never in `corpus.yaml` or any
  config file. `ConnectorConfig` uses `extra="forbid"`, so a literal `client_secret` or
  `access_token` key in config is rejected at load time — config exports stay secret-free by
  construction (§6.4).
- `corpus.yaml` carries only **references**: `token_ref` (a control-plane token handle) and
  `secret_env_vars` (a map of logical name → environment-variable *name*, never a value).
- Token *values* are resolved at run time through the `TokenStore` seam (encrypted at rest in the
  control plane). They are never logged, never put in exceptions, and are redacted in `__repr__`.
- Do not paste tokens into shell history. Use a secret manager or `read -s`, and unset the
  variables when done.

---

## Step 1 — Register an OAuth app / API token with the provider

Each connector needs an OAuth2 client (or, for Atlassian, an API token). Scopes should be the
**minimum read-only set** that also lets the connector read per-item permissions — otherwise the
permissions call returns 403 and the connector reports `permission_fidelity=unavailable`
(fail-closed, §14.3).

| Connector | Provider app | Minimum scopes |
|---|---|---|
| Google Drive (`gdrive`) | Google Cloud OAuth 2.0 client (Drive API enabled) | `drive.readonly` **and** the permissions read implied by it (Drive returns per-file `permissions` under the same scope). |
| SharePoint (`sharepoint`) | Microsoft Entra (Azure AD) app registration, Microsoft Graph | `Files.Read.All`, `Sites.Read.All` (delegated or application). Per-item `permissions` require the same read grant. |
| Atlassian Confluence (`confluence`) | Atlassian OAuth 2.0 (3LO) app, or a scoped API token | `read:confluence-content.all`, `read:confluence-space.summary`, and permission to read content restrictions. |
| Atlassian Jira (`jira`) | Atlassian OAuth 2.0 (3LO) app, or a scoped API token | `read:jira-work`. **Note:** Jira issue-level security is not exposed as a filterable ACL, so the Jira connector reports `permission_fidelity=unavailable` for every issue by design — see Step 5. |

---

## Step 2 — Complete the OAuth handshake and store the token

The connectors resolve an **already-obtained access token** by `token_ref` at run time; they do
not run the interactive browser consent themselves. Complete the authorization-code exchange out
of band (the platform's `OAuth2Flow` performs the `exchange_code` / `refresh` calls), then store
the resulting token in the control-plane `TokenStore` under a handle you will reference from
config.

The token exchange itself uses `finecorpus.connectors.oauth.OAuth2Flow`. In production it is
driven by `HttpxClient`; the client secret is used transiently and never persisted by that
module.

---

## Step 3 — Provide credentials via environment variables

Export the environment variables named by your `secret_env_vars` map. Example:

```bash
# Google Drive
export FINECORPUS_GDRIVE_ACCESS_TOKEN="ya29...."      # resolved via TokenStore/token_ref

# SharePoint (Microsoft Graph)
export FINECORPUS_SHAREPOINT_ACCESS_TOKEN="eyJ0...."

# Atlassian (Confluence and/or Jira)
export FINECORPUS_ATLASSIAN_API_TOKEN="ATATT...."
```

The corresponding (secret-free) `corpus.yaml` connector entries reference these by name only:

```yaml
connectors:
  - connector_id: drive-corp
    source_kind: gdrive
    endpoint: https://www.googleapis.com
    token_ref: drive-corp-token           # control-plane handle, NOT the token
    options:
      folder_id: "0A...."                  # optional: restrict to a folder subtree

  - connector_id: sp-policies
    source_kind: sharepoint
    endpoint: https://graph.microsoft.com
    token_ref: sp-policies-token
    options:
      drive_id: "b!...."                   # required: the target drive

  - connector_id: wiki-eng
    source_kind: confluence
    endpoint: https://acme.atlassian.net/wiki
    token_ref: atlassian-token
    options:
      space_key: ENG

  - connector_id: jira-eng
    source_kind: jira
    endpoint: https://acme.atlassian.net
    token_ref: atlassian-token
    options:
      jql: "project = ENG order by created"
```

---

## Step 4 — Run a live smoke test

The smoke test exercises the real handshake and a single page of `list_documents` /
`fetch_permissions` / `fetch_document`, using the production `HttpxClient`. Run it against a
**non-production, low-sensitivity** source first.

A minimal operator smoke script (run with `uv run python <script>.py`) looks like this — it
builds the connector from the registry, authenticates via the control-plane `TokenStore`, and
drives one gated collect item through the framework's `collect()` loop (the sole ingestion
producer, which enforces §14.3):

```python
from finecorpus.connectors import build_connector
from finecorpus.connectors.models import ConnectorConfig
from finecorpus.contracts.inventory import SourceKind, SourceRun

# TokenStore is provided by the control plane; it reads the env var named in
# secret_env_vars / resolves token_ref. Never inline the token here.
from your_control_plane import ProductionTokenStore  # noqa: not shipped in this package

config = ConnectorConfig(
    connector_id="drive-corp",
    source_kind=SourceKind.gdrive,
    endpoint="https://www.googleapis.com",
    token_ref="drive-corp-token",
    options={"folder_id": "0A...."},
)

conn = build_connector(config)  # httpx-backed in production
conn.authenticate(config, ProductionTokenStore())  # resolves token by reference

run = SourceRun(source_kind=SourceKind.gdrive, connector_id="drive-corp")
for item in conn.collect(run):  # framework-owned, gated loop
    print(item.document.ref.display_name, item.permissions.fidelity)
    break  # one item is enough to smoke-test
```

**Expected result:** the first document's display name prints alongside a `fidelity` of
`authoritative` (Google Drive, SharePoint, Confluence when restrictions are readable). If you see
a `PermissionFidelityError` instead, that is the fail-closed gate working — go to Step 5.

---

## Step 5 — Interpreting `permission_fidelity` and the fail-closed gate (§14.3)

The `collect()` loop is the **only** path from a connector to an ingestable item, and it routes
every document through the §14.3 gate. Outcomes:

- **`authoritative`** — the connector read reliable per-item ACLs. Ingestion proceeds; the
  principals are carried into `SourcePermissions` for later resolution into filterable fields.
- **`unavailable`** — the connector could **not** obtain reliable ACLs and refuses to fabricate
  open access. This happens when:
  - the OAuth scope is missing the permissions read (provider returns 403 on the ACL call), or
  - the source has no filterable ACL model the connector trusts — **all Jira issues** fall here
    by design (issue-level security is not exposed as a filterable ACL).
- When fidelity is `unavailable`, `collect()` raises `PermissionFidelityError` and the run
  **fails closed** — no restricted content is laundered in as public.

**To proceed with an `unavailable`-fidelity source anyway**, an operator must explicitly
acknowledge the permission gap by setting `acknowledged_permission_gap=True` on the `SourceRun`.
The run is then recorded as an acknowledged gap. Do this only when the team accepts that the
ingested content will not carry source-side permission filtering. For Jira this is the normal
path; for Drive/SharePoint/Confluence a 403 usually means the OAuth scope is wrong — fix the
scope (Step 1) rather than acknowledging the gap.

---

## Step 6 — Incremental (delta) sync

- **SharePoint** and **Google Drive** are incremental-capable. After a full listing completes,
  persist the `incremental_cursor` (Graph `@odata.deltaLink` / Drive start page token) onto
  `SourceRun.incremental_cursor`; pass it back on the next run so only changed items are fetched.
- **Atlassian** (Confluence/Jira) listing is offset-paginated and not delta-incremental in this
  release (`supports_incremental=False`); each run re-lists from the configured space/JQL.

---

## Failure modes

| Symptom | Likely cause | Action |
|---|---|---|
| `PermissionFidelityError` on `collect()` | ACL call 403 (missing scope) or Jira issue | Fix OAuth scope (Step 1); for Jira, acknowledge the gap if acceptable (Step 5). |
| `ValueError: requires config.token_ref` | Connector configured without a `token_ref` | Add the control-plane token handle to the connector config. |
| `ValueError: requires options.drive_id` (SharePoint) / `options.endpoint` (Atlassian) | Missing required non-secret option | Add `drive_id` / `endpoint` to the connector `options`. |
| OAuth `OAuth2Error: Token endpoint returned HTTP 4xx` | Expired/invalid client secret or refresh token | Re-run the authorization-code exchange; rotate the stored token. The error body is deliberately not logged (§14.2). |
| Token appears in a log line | A caller logged a `ConnectorConfig`/token by mistake | This must never happen: models redact secrets in `__repr__`. Treat as a security incident, rotate the credential, and file a bug. |

---

## Cleanup

Unset credential environment variables when the smoke test is complete:

```bash
unset FINECORPUS_GDRIVE_ACCESS_TOKEN FINECORPUS_SHAREPOINT_ACCESS_TOKEN FINECORPUS_ATLASSIAN_API_TOKEN
```
