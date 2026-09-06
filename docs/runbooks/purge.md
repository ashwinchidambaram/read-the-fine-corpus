# Runbook: Purge (Right-to-Erasure)

**Triggering event:** A right-to-erasure request requiring that a document's derived content be removed from all copies — live index, N-1 index, and all cold snapshots — immediately.

**Delete vs. purge (M-089):** This is a critical distinction with legal implications.

| Operation | What is removed | Cold snapshots | Summary string |
|---|---|---|---|
| `delete-doc` | Live + N-1 index chunks | Age out per `snapshot_retention_period_days` | `"deleted from service"` |
| `purge-doc` | Live + N-1 index chunks + ALL cold snapshots | Destroyed immediately (D-05) | `"purged from all copies"` |

Use `purge-doc` only when there is a legal obligation to ensure no copy of the content persists anywhere the platform controls. Use `delete-doc` for operational cleanup where snapshot retention is acceptable.

---

## Step 1 — Confirm the request requires a full purge

Before proceeding with purge:

1. Review the erasure request to confirm it applies to snapshot copies, not just the live service.
2. Identify the `document_id` of the content to purge. The document ID can be found in:
   - The ingestion job's artifact output (`collect.json` → `items[].document_id`)
   - The audit log for any previous access to the document
   - The findings report from the ingestion run

3. Note: purge removes chunks derived from the document. The source file is not stored by the platform (it is not ingested into object storage by default). Verify whether the source file itself needs to be deleted from the originating system.

---

## Step 2 — Execute the purge

**Via CLI (requires Qdrant + Postgres connectivity):**
```
corpus kb purge-doc <kb_id> <doc_id> --confirm --actor <requester_id>
```

The `--confirm` flag is required (prevents accidental use). The `--actor` is written to the audit trail and should identify who authorized the purge.

**What the purge does:**
1. Looks up the live collection and N-1 collection for the KB from the control-plane alias record.
2. Calls `delete_by_document(collection, doc_id)` on the live collection — removes all chunks whose `provenance.source_document_id` matches.
3. Calls `delete_by_document(collection, doc_id)` on the N-1 collection (if one exists).
4. Enumerates all snapshots for all collections associated with the KB and destroys each one immediately (D-05).
5. Removes any pipeline artifact files under the artifacts root associated with the document.
6. Writes a tombstone record to the control-plane DB with `kind="purge"` and lists the destroyed snapshot IDs.
7. Writes an audit log entry with `entry_type=AuditAction.purge`.

**CLI output on success:**
```
purged from all copies — doc <doc_id> from kb <kb_id>
  tombstone entry_id     : <entry_id>
  live chunks removed    : <N>
  N-1 chunks removed     : <M>
  artifacts removed      : <K>
  snapshots destroyed    : <S>
    - <snapshot_id_1>
    - <snapshot_id_2>
```

**Via control API:**
```
DELETE /v1/kb/<kb_id>/documents/<doc_id>?purge=true
X-Actor: <requester_id>
```

---

## Step 3 — Verify completeness

After the purge command returns:

1. Confirm the document is absent from the live index:
   ```
   POST /v1/kb/<kb_id>/query
   {"query": "content unique to the purged document", "filters": {"provenance.source_document_id": "<doc_id>"}}
   ```
   Expected: `result_status: "no_matches"` and `results: []`.

2. Confirm the tombstone record exists:
   ```
   GET /admin/tombstones?kb_id=<kb_id>
   ```
   Expect an entry with `kind="purge"` and `document_id="<doc_id>"`.

3. Confirm the audit entry:
   ```
   GET /admin/audit?kb_id=<kb_id>&limit=20
   ```
   Expect an entry with `entry_type="purge"`.

4. Confirm snapshots are gone:
   ```
   corpus kb snapshots <kb_id>
   ```
   The output should show no snapshots for the KB's collections (or only snapshots that were created after the purge).

---

## LLM cache purge path

If the KB had LLM augmentation operations (Tier 2 or Tier 3) and those operations cached their outputs, the derived cache entries for the document must also be invalidated.

The purge path (`delete_document(purge=True)`) calls `_purge_llm_cache()` if an artifacts root is provided. This removes cached files under `<artifacts_root>/<run_id>/cache/<doc_hash>/`. Pass `--artifacts <dir>` to enable this path:
```
corpus kb purge-doc <kb_id> <doc_id> --confirm --actor <requester_id> --artifacts <artifacts_dir>
```

If the artifacts root is not provided, LLM cache entries are not automatically swept. In this case, manually remove files under the artifacts directory for the document's content hash before responding to the erasure requester.

---

## Snapshot destruction (D-05)

D-05 resolution: purge destroys all snapshots immediately. There is no deferred sweep — snapshot destruction is synchronous within the `delete_document(purge=True)` call.

The `snapshots_destroyed` list in the DeletionReport and in the tombstone record contains the snapshot IDs that were destroyed. Use this list when reporting to the erasure requester as evidence of completeness.

**Destruction is permanent.** Once destroyed, snapshots cannot be recovered. If the wrong document ID was purged, there is no undo path via snapshot restore (the snapshots are gone). A full reindex from the original source documents is the only recovery path.

---

## Future restores and the tombstone log

After a purge, if a cold snapshot is later restored from object storage (by a third-party backup system that holds copies the platform cannot control), the tombstone log entry with `kind="purge"` will cause the restore procedure (`restore_from_snapshot()`) to replay the deletion and remove the document from the restored collection before it is promoted.

This is the durable correctness guarantee: the tombstone log is the source of truth for what has been purged, and restore always replays it. This is why the tombstone log itself must be included in every database backup (see `runbooks/backup.md`).

---

**See also:**
- `docs/architecture/index-lifecycle.md` §12.1 (deletion), §12.2 (purge vs. delete)
- `runbooks/tombstone-replay.md` — tombstone replay and gap recovery
- `runbooks/restore-cold.md` — cold snapshot restore and M-087 marker
- `runbooks/backup.md` — tombstone log backup requirements
