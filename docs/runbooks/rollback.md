# Runbook: Rollback

**Triggering events:**
- A promotion produces unexpected retrieval regression; operator needs to revert to N-1 without waiting for a new build.
- An automated quality gate detects a post-promotion regression below the configured threshold.

**What rollback does:** Rollback is an atomic alias swap to N-1 (the previous collection). It is instantaneous — no data is moved or rebuilt. Blocked if the N-1 embedding model provider is no longer configured (OQ-L-7 guard; see [Blocked rollback](#blocked-rollback-oq-l-7) below).

---

## Step 1 — Confirm a rollback is the right action

Before rolling back, verify:

1. Check the current alias status to confirm a live N-1 exists:
   ```
   corpus kb status <kb_id>
   ```
   The output lists the current live collection and the hot standby (N-1). If no N-1 is shown, this is the first build and rollback is not available.

2. Check what content the N-1 collection has by reviewing the most recent completed job before the current promotion:
   ```
   corpus jobs list --kb <kb_id> --limit 5
   ```

3. If the regression is retrieval-quality related (wrong results), confirm the issue is in the index content — not in a retrieval service bug — before rolling back.

---

## Step 2 — Initiate rollback via the control API

Rollback is performed via the control-plane API. There is no dedicated `corpus rollback` CLI subcommand in Phase 4; the lifecycle function is called through the control API or directly.

**Via control API (preferred):**
```
POST /v1/kb/<kb_id>/rollback
```
The control API calls `finecorpus.index.lifecycle.rollback()` which:
1. Reads the alias record to find the N-1 collection and its embedding provider.
2. Validates that the N-1 provider is available in the current configuration (OQ-L-7).
3. Phase 1: retargets the Qdrant alias to the N-1 collection (atomic swap).
4. Phase 2: updates the control-plane alias record (swaps current and previous).

The operation returns the N-1 collection name the alias now serves.

---

## Step 3 — Verify the alias was reverted

After rollback completes:

1. Confirm the alias now points at the previous collection:
   ```
   corpus kb status <kb_id>
   ```
   The current live collection shown should be the N-1 collection from before.

2. Send a test query to confirm content matches the pre-promotion state:
   ```
   POST /v1/kb/<kb_id>/query
   {"query": "sentinel content from previous build"}
   ```

3. Review the control-plane audit log for the rollback event:
   ```
   GET /admin/audit?kb_id=<kb_id>&limit=10
   ```

---

## Blocked rollback (OQ-L-7)

Rollback is **blocked** (`RollbackError`) if the N-1 collection was built with an embedding provider that is not in the current platform configuration. This prevents serving a collection where every query would fail due to model-identity mismatch.

**Resolution options:**
1. **Re-configure the provider** — add the N-1 provider back to `corpus.yaml` under `providers.embedding`. Re-run `corpus preflight` to confirm. Then retry rollback.
2. **Accept a full rebuild** — if the N-1 provider is genuinely unavailable (deprecated model, removed integration), there is no safe rollback path. Trigger a fresh reindex:
   ```
   corpus reindex <kb_id> --full
   ```
   This builds a new shadow collection with the current provider, validates it, and promotes it. N-1 will then be the collection the regression was introduced in — which may be intentional if the regression was in a prior build.

**How to check the N-1 provider:**
```
GET /v1/kb/<kb_id>/status
```
The `previous_collection` fields show the N-1 collection name and its `embedding_provider`.

---

## If rollback fails mid-operation (Phase 2 failure)

If Phase 1 (Qdrant retarget) succeeded but Phase 2 (control-plane update) failed:
- The Qdrant alias already points at the N-1 collection.
- The control-plane record still shows the old live collection.
- This is an inconsistency that `startup_reconcile()` detects and repairs at service restart.

**Immediate operator action:**
1. Do not restart the rollback — it may double-swap.
2. Check which collection Qdrant's alias actually resolves to via the Qdrant admin UI or API.
3. Restart the control-plane service. On startup, `startup_reconcile()` reads the live Qdrant alias target and updates the control-plane record to match.
4. Verify with `corpus kb status <kb_id>` after restart.

---

## Cold snapshot restore (beyond N-1)

If you need to roll back further than N-1, see [`runbooks/restore-cold.md`](restore-cold.md). Cold restore is substantially slower (requires tombstone replay) and should only be used when the N-1 collection is also compromised.

---

**See also:**
- `docs/architecture/index-lifecycle.md` §10.3 (instant rollback), §4 (alias swap protocol), OQ-L-7 (blocked rollback path)
- `runbooks/restore-cold.md` — cold snapshot restore
