# Runbook: Break-Glass Content Access

**Triggering event:** A Platform Admin needs to read document content from a KB for a legitimate operational reason (e.g., investigating a support ticket, responding to a legal hold, debugging a retrieval anomaly) without being in the KB's `permission_principals` list.

**What break-glass is:** A time-bound, admin-only, audited and notified content-read access mechanism. Every access under a grant produces an immutable audit record. The KB's team (Workspace Owner and KB Editors) is notified at grant time — not after.

---

## Prerequisites

- The requesting principal must have `role=admin` and `scope_kind=global_` in the platform's key store.
- A stated reason is required (M-001). Empty or whitespace-only reasons are rejected.
- The grant applies to a single KB — grants do not cross KB boundaries (§2.3).

---

## Step 1 — Issue the grant

**Via control API:**
```
POST /admin/break-glass/grant
Authorization: Bearer <admin_api_key>
Content-Type: application/json

{
  "target_kb_id": "<kb_id>",
  "reason": "<non-empty reason text — this appears in the audit record>",
  "window_hours": 4,
  "notified_principals": ["<workspace_owner_id>", "<kb_editor_id>"]
}
```

Admin identity is derived from the API key used to authenticate (the `Authorization` or `X-API-Key` header). There is no separate `X-Admin-Id` header — the key must be scoped `role=admin, scope_kind=global_` in the platform's key store.

The `window_hours` field defaults to 4 hours (D-04 resolution). Any finite positive value is accepted; the default 4-hour window is enforced by `issue_grant()` with a `_validate_window()` call that rejects `None` (infinite) windows.

**Response:**
```json
{
  "grant_id": "<grant_id>",
  "target_kb_id": "<kb_id>",
  "granting_admin_id": "<admin_id>",
  "reason": "<reason>",
  "granted_at": "<ISO8601>",
  "expires_at": "<ISO8601>",
  "audit_entry_id": "<audit_entry_id>"
}
```

Record the `grant_id` — it is required for every subsequent read under the grant.

**Note on notification (M-004):** The `notified_principals` list in the request captures the notification intent. In Phase 4, notification is recorded in the audit log and grant record (the `audit_entry_id` is written before the grant, per the M-004 audit-ordering invariant). Delivery of real-time notifications to workspace owners awaits the Phase 6 UI. The audit record is the ground truth of who was notified.

---

## Step 2 — Read content under the grant

Include the `grant_id` in every query that requires break-glass access:

**Via retrieval API:**
```
POST /v1/kb/<kb_id>/query
X-API-Key: <admin_api_key>
X-Break-Glass-Grant-Id: <grant_id>
Content-Type: application/json

{"query": "your query text"}
```

**What happens on each read (T-05):**
- The service validates the grant is active and not expired or revoked.
- The grant's `target_kb_id` is checked against the query `kb_id` — a grant for KB-A cannot be used to read KB-B.
- A `break_glass_read` audit row is written with: `grant_id`, `chunk_ids_returned`, and timestamp. This audit write is fail-closed (M-003): if the audit row cannot be persisted, the content is NOT served.
- The response includes a `break_glass_read_ref` field containing the audit entry ID for traceability.

---

## Step 3 — Monitor and audit visibility

The grant and all reads under it are visible in the KB's audit log:

**List active grants:**
```
GET /admin/break-glass/active
Authorization: Bearer <admin_api_key>
```

**View audit entries for a KB:**
```
GET /v1/audit?kb_id=<kb_id>&limit=50
```
Filter for `entry_type="break_glass_grant"` and `entry_type="break_glass_read"`.

The audit log is visible to the KB's team (Workspace Owner and KB Editors) — break-glass access is not hidden from the KB operators.

---

## Grant expiry (D-04)

Grants expire automatically at `expires_at` (default: 4 hours from grant time, per D-04 resolution). After expiry:
- `active_grant_for()` returns `None`.
- Any query referencing the expired `grant_id` returns `PERMISSION_DENIED`.
- No further audit rows are written for the expired grant.

The default 4-hour window is intentionally short to limit the exposure window. The window can be configured up to the `break_glass_grant_window_hours` config key (see `configuration/reference.md §2.6`), but the spec requires it to be **finite** — indefinite grants are not permitted.

---

## Revoking a grant early

If break-glass access needs to be terminated before the grant expires:

```
DELETE /admin/break-glass/<grant_id>
Authorization: Bearer <admin_api_key>
```

After revocation, any query referencing the revoked `grant_id` returns `PERMISSION_DENIED`. Revocation is permanent — a revoked grant cannot be reinstated.

---

## Audit fail-closed behavior (M-003)

If the break-glass audit record cannot be written (database connectivity failure, transaction conflict), the content retrieval fails with `PERMISSION_DENIED`. The service does **not** serve content without a durable audit record.

This means:
- An unaudited break-glass read is impossible in normal operation.
- Database failure during a break-glass session causes the session to fail, not to produce silent unaudited reads.
- After the database recovers, a new query (with the same or a new grant) will produce an audit record.

---

**See also:**
- `docs/architecture/overview.md §Tenant isolation mechanism` (break-glass design)
- `docs/contracts/README.md §Audit & break-glass seeds` (BreakGlassGrant + AuditRecord contracts)
- `docs/architecture/index-lifecycle.md §2.3` (break-glass policy)
- Decision ledger D-04 (4h window default, finite enforced)
