# Runbook: Restore from Cold Snapshot

**Triggering event:** Operator needs to roll back beyond N-1 to a cold snapshot — for example, both the live and N-1 collections are corrupted, or a deletion event that should have been blocked occurred multiple builds ago.

**What cold restore does:** Restores a snapshot from object storage into a new shadow collection, replays all tombstone entries since the snapshot was taken (so deleted documents remain deleted), and leaves the restored collection ready for validation and promotion.

Cold restore is substantially slower than an N-1 rollback (alias swap). It requires tombstone replay (which removes any content deleted after the snapshot was taken) and full validation before promotion.

---

## Step 1 — Identify the target snapshot

1. List all cold snapshots for the KB's collections:
   ```
   corpus kb snapshots <kb_id>
   ```
   Output shows snapshot IDs, creation timestamps, and the collection they belong to.

2. Identify the snapshot that predates the problem. Use the `created_at` timestamp and the control-plane audit log to correlate:
   ```
   GET /admin/audit?kb_id=<kb_id>&limit=50
   ```

3. Note the `snapshot_id` and `new_build_id` you will assign to the restored collection. The `new_build_id` must be higher than any existing build ID for the KB.

---

## Step 2 — Initiate restore

Restore is performed via the lifecycle API. `restore_from_snapshot()` automatically:
1. Copies the snapshot data into a new shadow collection (`rtfc_<kb>_<new_build_id>`).
2. Sets the `_restored_unreplayed_marker` on the collection metadata to `"true"`.
3. Queries the tombstone log for all deletion events since the snapshot's creation.
4. Replays each tombstone entry by calling `delete_by_document` on the restored collection.
5. Clears the unreplayed marker to `"false"`.

**Via control API:**
```
POST /v1/kb/<kb_id>/restore
{
  "snapshot_id": "<snapshot_id>",
  "new_build_id": <N>
}
```

The operation returns the restored collection name.

---

## Step 3 — Monitor tombstone replay

Tombstone replay runs synchronously during `restore_from_snapshot()`. For large KBs with many deletions, this may take several seconds.

After restore completes, verify the unreplayed marker is cleared:
```
GET /v1/kb/<kb_id>/status
```
The status shows the restored shadow collection and its metadata. If `restored_unreplayed_marker` is absent or `"false"`, replay succeeded.

**M-087 invariant:** The restored collection cannot be promoted until the unreplayed marker is cleared. Any attempt to call `promote()` with the marker still `"true"` raises `RestoredUnreplayedError`. This prevents serving a restored collection that still contains content deleted after the snapshot was taken.

---

## Step 4 — Verify the restored collection

Before promotion, verify the restored collection is correct:

1. Run a test query against the restored collection directly (before alias retarget):
   ```
   GET /v1/kb/<kb_id>/shadow/<restored_collection>/query
   {"query": "sentinel content"}
   ```

2. Confirm that any documents deleted after the snapshot creation are absent:
   - Find their doc IDs from the audit log or tombstone log.
   - Query with `payload_filter={"provenance.source_document_id": "<doc_id>"}` and expect 0 results.

3. Confirm the chunk count is within expected bounds (pre-promotion validation gates run automatically during `promote()`).

---

## Step 5 — Promote the restored collection

```
POST /v1/kb/<kb_id>/promote
{"build_id": <N>}
```

The promotion runs all four pre-promotion validation gates (Gate 1–4 from `index-lifecycle.md §5`). If validation fails, the alias is unchanged and the restored shadow is retained for inspection.

---

## Tombstone log gap recovery (OQ-L-8)

**Critical data integrity condition.** If the PostgreSQL database was restored from a backup that predates one or more deletion events, those tombstone entries are missing. `restore_from_snapshot()` will replay only the tombstones it can find — it cannot know about entries that no longer exist in the database.

**Symptoms of a tombstone log gap:**
- The restore completes without error, but documents that should have been deleted (per your erasure records) are present in the restored collection.
- The unreplayed marker is cleared (replay ran) but the results are wrong.

**The M-087 marker and gap detection:**
The unreplayed marker (`_restored_unreplayed_marker`) is set to `"true"` at restore start and cleared only after replay runs. If the database tombstone log has been truncated or partially restored from backup, replay will complete successfully (clearing the marker) but will have missed the deleted documents. The marker alone does not detect this case.

**Resolution when a gap is detected:**

1. **Identify which deletions are missing.** Compare the tombstone log in the restored database against:
   - External audit log or SIEM (if configured)
   - Right-to-erasure request records or deletion SLA documentation
   - The `break_glass_read` audit entries that may have been made for the deleted documents

2. **Apply missing deletions manually.** For each missing document:
   ```
   corpus kb delete-doc <kb_id> <doc_id> --actor gap-recovery
   ```
   This removes the document from the live+N-1 index and appends a new tombstone entry.

3. **Re-initiate the restore** from the same or a newer snapshot after the manual deletions have been applied. The new tombstone entries will be replayed.

4. **Escalation path:** If the gap cannot be reconstructed (the erasure records were themselves lost), treat this as a data breach event: notify the Data Protection Officer, consult the organization's right-to-erasure SLA, and document the gap in the incident record. A restore that cannot guarantee deletion completeness must not be promoted to serve traffic for affected KBs.

---

## Tombstone log backup requirement

The tombstone log is the critical durability dependency for this runbook. See `runbooks/README.md §Tombstone log backup discipline` for the required backup configuration.

**In brief:** Use continuous WAL archiving so the database can be restored to any point in time. A database restore that predates a deletion event creates an unresolvable gap. Verify tombstone log completeness before relying on a database backup for restore operations.

---

**See also:**
- `docs/architecture/index-lifecycle.md` §10.4 (cold restore), §12.1 (deletion completeness)
- `runbooks/tombstone-replay.md` — tombstone replay failure handling
- `runbooks/rollback.md` — instant N-1 rollback (faster; use this first)
- `runbooks/backup.md` — backup requirements including tombstone log discipline
