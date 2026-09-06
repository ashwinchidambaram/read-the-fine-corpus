# Runbook: Backup and Disaster Recovery

**Triggering event:** Scheduled backup procedure, or disaster-recovery scenario requiring restoration of the platform from backup.

---

## What to back up

The platform has three storage components, each with different backup requirements:

### 1. PostgreSQL control-plane database (CRITICAL)

**What it contains:** The control-plane DB is the source of truth for:
- Alias records (which collection each KB's alias points to, including N-1)
- Job queue (state of all ingestion and reindex jobs)
- Tombstone log (deletion and purge history — see below)
- Break-glass grant records and audit log
- Reindex trigger configurations

**Backup method:** Continuous WAL archiving (pgBackRest, Barman, or equivalent). Point-in-time recovery is required — see tombstone log discipline below.

### 2. Object storage (cold snapshots)

**What it contains:** Cold snapshots created by `snapshot_cold()` are stored in the object storage configured under `storage.snapshot_bucket`. These snapshots are already in object storage (not in the primary DB or Qdrant).

**Backup method:** Verify that the snapshot bucket has cross-region replication or a cross-region copy policy enabled at the storage provider level. Cold snapshots are the disaster recovery path for the vector index — they must survive infrastructure failure in the primary region.

### 3. Qdrant (live and hot-standby collections)

**What it contains:** The live (currently serving) and N-1 (hot standby) vector collections. These are derived from the source documents and the ingestion config — they can be rebuilt from a cold snapshot via `restore_from_snapshot()`.

**Backup method:** Qdrant's built-in snapshot API (`snapshot_cold()` in the lifecycle layer). Qdrant also supports native replication for high-availability. For disaster recovery specifically, the cold snapshots in object storage are the primary artifact — Qdrant can be rebuilt from them via `restore_from_snapshot()`.

---

## Tombstone log backup discipline (OQ-L-8)

**This is the most critical backup requirement in this runbook.**

The tombstone log (table `tombstone_log` in PostgreSQL) is an append-only record of every deletion and purge event. It is never truncated. If the PostgreSQL database is restored from a backup that predates one or more deletion events:

- Those tombstone entries do not exist in the restored database.
- A subsequent `restore_from_snapshot()` call will replay only the tombstones it finds.
- Documents that were deleted after the backup was taken will appear in the restored collection.
- For right-to-erasure obligations, this is a data breach.

**Required configuration:**

1. **PostgreSQL MUST use continuous WAL archiving** (point-in-time recovery to any moment after any deletion event).
2. **Backup frequency MUST be at least as frequent as the deletion SLA.** If the SLA requires deleted content to be gone within 24 hours, the backup granularity must allow point-in-time recovery to within 24 hours of any deletion event. WAL archiving is the only configuration that reliably satisfies this.
3. **Before restoring the PostgreSQL database from backup**, verify tombstone log completeness:
   - Get the latest tombstone entry sequence ID from the backup.
   - Compare against the most recent deletion event recorded in the external audit log or SIEM.
   - If the backup predates any deletion event, restore to a more recent point (WAL archiving required).
4. See `runbooks/tombstone-replay.md` for the recovery procedure when a gap is detected.

**Verification check before every restore:**
```
GET /admin/tombstones?kb_id=<kb_id>&limit=1&order=desc
```
Compare the returned `created_at` against the most recent deletion event in your external audit trail. If the backup's latest tombstone is older than the external record, you have a gap.

---

## Restore verification procedure

After restoring PostgreSQL and Qdrant from backup:

1. **Verify alias consistency.** The PostgreSQL alias records and the Qdrant alias targets must agree. On service startup, `startup_reconcile()` detects and repairs inconsistencies. Check for reconciliation events in the service log:
   ```
   grep "startup_reconcile" <service_log>
   ```

2. **Verify tombstone completeness.** As described above — compare the tombstone log against external deletion records.

3. **Run a retrieval health check.** Send a known-good query to the retrieval service and verify the response:
   ```
   POST /v1/kb/<kb_id>/query
   {"query": "known content"}
   ```
   Expect `result_status: "matches"` and provenance fields populated.

4. **Check the control API health.**
   ```
   GET /healthz
   ```
   All services should return `{"status": "ok"}`.

5. **For any KB where tombstone gaps were detected**, follow `runbooks/tombstone-replay.md` before serving traffic.

---

## Backup schedule recommendation

| Component | Minimum backup frequency | Recommended |
|---|---|---|
| PostgreSQL (control-plane) | Continuous WAL archiving | Continuous WAL + daily base backup |
| Object storage (cold snapshots) | Cross-region replication enabled | Real-time replication (storage-provider feature) |
| Qdrant (live collections) | Weekly cold snapshot | Daily cold snapshot |

The Qdrant collection data is derivable from the source documents + cold snapshots, so the backup priority is lower than PostgreSQL. However, rebuilding from scratch is slow for large KBs — cold snapshots reduce the recovery time objective.

---

**See also:**
- `runbooks/tombstone-replay.md` — gap detection and recovery
- `runbooks/restore-cold.md` — cold snapshot restore procedure
- `runbooks/purge.md` — right-to-erasure and snapshot destruction
- `docs/architecture/index-lifecycle.md` §10.4 (cold restore), §12 (deletion)
