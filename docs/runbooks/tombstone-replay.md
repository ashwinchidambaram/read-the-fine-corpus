# Runbook: Tombstone Replay Failure and Gap Recovery

**Triggering event:** A cold snapshot restore triggers tombstone replay (`restore_from_snapshot()`), but the replay detects a gap in the tombstone log, or content that should have been deleted is found in the restored collection.

**What this runbook covers:** The OQ-L-8 scenario — the tombstone log has gaps because the PostgreSQL database was restored from a backup that predates one or more deletion events.

---

## Background: how tombstone replay works

When `restore_from_snapshot()` runs, it:

1. Sets `_restored_unreplayed_marker = "true"` on the restored collection.
2. Queries the tombstone log for all entries for the KB since time immemorial (`tomb_repo.list_for_kb(kb_id)`).
3. For each tombstone entry not yet replayed on this collection (`tomb_repo.unreplayed_for(kb_id, restored_coll)`), calls `delete_by_document(restored_coll, tombstone.document_id)`.
4. Marks each tombstone as replayed for this collection.
5. Sets `_restored_unreplayed_marker = "false"`.

The `_restored_unreplayed_marker` prevents the collection from being promoted until step 5 completes successfully. If the process crashes between steps 1 and 5, the marker remains `"true"` and promotion is blocked — this is the M-087 invariant.

**The gap scenario:** If the PostgreSQL database was restored from a backup taken before a deletion event, the tombstone entry for that deletion does not exist. Steps 2–4 run successfully (they process all tombstones they can find), step 5 clears the marker — but the restored collection still contains the deleted document. The platform cannot distinguish this from a correct restore without external evidence.

---

## Detecting a gap

A gap is **not automatically detected** by the platform. The `_restored_unreplayed_marker` only tracks whether replay ran, not whether the tombstone log is complete.

**Manual gap detection procedure:**

1. After restore completes (marker cleared), check whether the restored collection contains content that should have been deleted:
   - Query the restored collection directly with the document IDs of all known erasure events.
   - Cross-reference the tombstone log in the restored database against:
     - External audit logs or SIEM events for `entry_type=deletion` or `entry_type=purge`
     - Right-to-erasure request records and their document IDs
     - The `break_glass_read` audit entries that may have been made for sensitive documents

2. If the external audit log shows a deletion that the tombstone log does not contain, you have a confirmed gap.

3. Record the missing `document_id` values and when they were deleted (from external sources).

---

## Resolving a gap

**Do NOT promote the restored collection until the gap is resolved.**

If the restored collection has the `_restored_unreplayed_marker = "false"` (replay completed) but gaps exist:

1. **Apply the missing deletions manually** using the deletion CLI:
   ```
   corpus kb delete-doc <kb_id> <doc_id> --actor gap-recovery
   ```
   Repeat for each document ID missing from the tombstone log. Each call:
   - Removes the document from the restored shadow collection.
   - Appends a new tombstone entry to the tombstone log (ensuring future restores replay it).
   - Appends an audit entry with `entry_type=deletion`.

2. **Verify the restored collection** is now correct by querying for each document that was manually deleted. Shadow-collection queries are a **library/admin-tooling operation** — there is no REST shadow-query endpoint. Use the adapter directly:
   ```python
   results = adapter.search(
       alias=restored_collection,  # collection name, not alias
       query_vector=embed("..."),
       top_k=5,
       payload_filter={"provenance.source_document_id": "<doc_id>"},
   )
   # Expected: len(results) == 0
   ```

3. **Promote the restored collection** only after verification. Promotion is also a **library/admin-tooling operation** — call `promote()` via the core library (see `runbooks/restore-cold.md §Step 5`).

---

## If the gap cannot be reconstructed

If the erasure records themselves were lost (e.g., both the database and the external audit log are compromised), the gap is unresolvable with certainty.

**Actions to take:**

1. **Do not promote** the restored collection to serve traffic for affected KBs.
2. **Notify the Data Protection Officer** and follow the organization's data breach protocol.
3. **Document the incident** with:
   - Which KBs are affected
   - The time window during which the tombstone log was not backed up
   - Any erasure requests received during that window (even if the document IDs are unknown)
   - The restore operation details (snapshot ID, creation timestamp)
4. **Consider a full reindex** from the originating source system (which should have had the documents removed by the erasure request). A fresh index from a verified-clean source avoids the need for tombstone replay.

---

## Tombstone log backup discipline (required)

The tombstone log is stored in the PostgreSQL control-plane database (table: `tombstone_log`). It is append-only and never truncated.

**Required backup configuration (OQ-L-8):**

1. Use continuous WAL archiving for PostgreSQL (e.g., pgBackRest, Barman) so the database can be restored to any point in time — specifically to any moment after any deletion event.
2. Never rely on daily snapshot backups alone. A daily snapshot backup means any deletions in the previous 24 hours are not replayed during a restore from that snapshot.
3. **Before restoring the PostgreSQL database from backup**, verify tombstone log completeness:
   - Get the latest tombstone sequence ID from the backup.
   - Compare against the most recent deletion event in the external audit log.
   - If the backup predates any deletion event, restore to a more recent backup point (WAL archiving is required for this).

---

## Resuming a restore after a crash

If `restore_from_snapshot()` crashed mid-replay (between setting the marker to `"true"` and clearing it to `"false"`):

- The `_restored_unreplayed_marker` remains `"true"`.
- `promote()` will raise `RestoredUnreplayedError` if attempted.

**Recovery:**
1. Do not re-initiate the full restore from scratch unless the collection data is corrupt.
2. Call `restore_from_snapshot()` again with the same `new_build_id`. The function re-checks the unreplayed tombstones and replays only those not yet recorded as replayed for this collection.
3. After completion (marker cleared), proceed with promotion.

---

**See also:**
- `docs/architecture/index-lifecycle.md` §10.4 (cold restore), §12.1 (deletion completeness), OQ-L-8
- `runbooks/restore-cold.md` — full cold restore procedure
- `runbooks/backup.md` — tombstone log backup requirements
- `runbooks/purge.md` — purge (right-to-erasure) procedure
