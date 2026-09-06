# Operations Runbooks

Runbooks are written in the phase that ships the behaviour they describe. This index is the
authoritative list of operational procedures, so no operational event the platform can produce
lacks a documented response path.

**Authoring rule:** a phase is not complete until every runbook whose triggering event is first
reachable in that phase exists and is accurate. A runbook that trails the shipped behaviour is
treated as a documentation defect, not as debt.

---

## Runbook index

| Runbook file | Triggering event | Phase it must ship | Content sketch |
|---|---|---|---|
| [`ingest.md`](ingest.md) | Operator starting a new ingestion job | Phase 1 | First-run `corpus init` → `corpus preflight` → `corpus pipeline run` flow with real CLI commands, artifact layout, findings report interpretation, failure modes per §15 rows that exist in Phase 1, and where artifacts land. |
| [`reindex.md`](reindex.md) | Reindex triggered (manual, change-detected, scheduled, or config-change) | Phase 1 | How to initiate a manual reindex, monitor shadow-build progress, interpret validation gate output (all four gates), and what to do on gate failure (inspect the retained shadow collection, which is held for at least 7 days). Referenced by `docs/architecture/index-lifecycle.md` §7 and §10.4. |
| [`rollback.md`](rollback.md) | Promotion produces unexpected regression; operator needs to revert to N-1 | Phase 4 | How to initiate instant rollback to N-1 via alias swap; what the model-identity check does during rollback; what to do if rollback is blocked because the N-1 embedding model is no longer configured (OQ-L-7 resolution path: re-configure the provider or accept a full rebuild). Referenced by `docs/architecture/index-lifecycle.md` §10.3. |
| `restore-cold.md` | Operator needs to roll back beyond N-1 to a cold snapshot | Phase 4 | How to identify the target snapshot via `corpus kb snapshots <kb_id>`, initiate restore via library/admin-tooling call to `restore_from_snapshot()` (no REST endpoint), monitor tombstone replay, wait for validation, interpret a restore failure if the tombstone log has gaps (OQ-L-8 path — critical data integrity condition; manual verification required before re-initiating). |
| `purge.md` | Right-to-erasure request; operator must confirm deletion from all copies | Phase 4 | Difference between `delete-doc` (live + N-1; cold snapshots handled on next restore via tombstone) and `purge-doc` (live + N-1 + all cold snapshots destroyed immediately). How to invoke `corpus kb purge-doc <kb_id> <doc_id> --confirm`, how to verify completeness, what the audit record looks like, and how to communicate the distinction between "deleted from service" and "purged from all copies" to the requester. |
| `tombstone-replay.md` | Restore from cold snapshot triggers tombstone replay failure | Phase 4 | What to do when the tombstone log has gaps (OQ-L-8): restore is aborted; operator manually verifies which deletions are missing using the control-plane audit log; applies them; re-initiates restore. Escalation path when the gap cannot be reconstructed. Documents tombstone log backup requirements — the tombstone log MUST be included in every database backup; a database backup that predates deletion events produces an unresolvable gap on restore. |
| `provider-outage.md` | Embedding provider becomes unavailable mid-ingestion or at query time | Phase 1 | How ingestion enters `PAUSED` state and what the operator sees in the job progress view; how to monitor provider health via `corpus provider status`; how retrieval fails closed with `PROVIDER_UNAVAILABLE` during an outage; steps to resume ingestion after the provider recovers; how to verify no partial shadow was promoted. Covers both cloud (OpenAI) and local (Ollama) providers. |
| `orphan-scan.md` | Post-incremental-ingestion alert: non-zero orphaned chunks detected | Phase 1 | How to interpret the orphan alert (which document IDs are involved, orphan count); how to run `corpus orphan-scan --kb <id>` for a diagnostic report and `corpus orphan-scan --kb <id> --fix` for self-service resolution; when to escalate (e.g., orphans indicate a bug in the replace-by-document logic). Non-zero orphans is always a problem requiring resolution, not a normal operational condition. |
| `budget-cap-hit.md` | Ingestion or sweep paused due to hitting a per-KB or per-workspace budget cap | Phase 4 | What the operator sees (paused job, alert in dashboard); how to review cost attribution per KB and per operation type; how to raise the cap via `corpus budget set` or approve continuation of a paused job; how to diagnose repeated cap-hits from scheduled reindexes (including the repeated-cap-hit alert threshold at `budgets.scheduled_reindex_cap_hit_alert_count`). |
| `break-glass.md` | Platform Admin needs to read document content under a time-bound grant | Phase 4 | How to initiate a break-glass grant via `POST /admin/break-glass/grant` (admin API key required; identity derived from the key, no separate X-Admin-Id header), including the required stated reason; confirmation that the Workspace Owner and KB Editors are notified at grant time; how the time-bound grant expires automatically; how to view the audit record via `GET /v1/audit` (available in the KB's own audit view, visible to its team); what "purged from all copies" looks like versus "deleted from service." |
| `backup.md` | Scheduled backup or disaster-recovery procedure | Phase 4 | What to back up: PostgreSQL (control-plane database — MUST include the tombstone log; see tombstone log backup discipline below), object storage (cold snapshots are already in object store; verify bucket replication or cross-region copy policy), Qdrant (live and hot-standby collections — Qdrant snapshot API). Tombstone log backup discipline (OQ-L-8): the PostgreSQL backup schedule MUST be at least as frequent as the deletion SLA. A database backup that predates a deletion event creates an unresolvable tombstone gap on restore. Recommended: continuous WAL archiving so point-in-time recovery covers any deletion event. Restore verification procedure. |

---

## Tombstone log backup discipline (summary)

The tombstone log is stored in the PostgreSQL control-plane database (table: `tombstone_log`).
It is an append-only record of every deletion event. It is never truncated.

**Critical constraint (OQ-L-8):** if the PostgreSQL database is restored from a backup that
predates one or more deletion events, those tombstone entries are missing. A subsequent
cold-snapshot restore cannot replay them, leaving deleted content silently present in the
restored collection. This is a data integrity failure for right-to-erasure obligations.

**Required backup discipline:**

1. PostgreSQL backups MUST use continuous WAL archiving (point-in-time recovery), or full
   backups taken at an interval shorter than the organisation's deletion SLA.
2. Before restoring the database from backup, verify the tombstone log completeness by
   comparing the backup's latest tombstone sequence ID against the most recent deletion audit
   record the team has outside the database (e.g., external audit log or SIEM).
3. See `runbooks/tombstone-replay.md` for the recovery procedure when a gap is detected.

---

## Phase completion gate

A phase is not complete until every runbook in this table whose phase column matches or
precedes the completed phase exists with real content (not a stub). The stub files for
`reindex.md` and `rollback.md` exist to close the dead links in `index-lifecycle.md`; they
must be replaced with full procedures before Phase 1 and Phase 4 respectively are marked done.
