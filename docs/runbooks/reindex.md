# Runbook: Reindex

**Status: stub — procedure ships with Phase 1 (reindex behaviour is first reachable in Phase 1).**

This file closes the dead link from `docs/architecture/index-lifecycle.md` §7 and §10.4.

## Triggering events

1. **Manual** — KB Editor or Platform Admin via `corpus reindex --kb <id>` or the UI.
2. **Change-detected** — source connector detects a content hash change; platform schedules incremental upsert or full rebuild per config-change status.
3. **Scheduled** — cron-style reindex fires per KB schedule (`index_lifecycle.scheduled_reindex_cron`).
4. **Config-change** — any change to chunking strategy, transformation tier, embedding model, or segment taxonomy version triggers a full rebuild automatically.

## Designed behaviour

See `docs/architecture/index-lifecycle.md`: §7 (triggers), §8 (incremental vs. full rebuild), §5 (validation gates), §3 (state machine).
