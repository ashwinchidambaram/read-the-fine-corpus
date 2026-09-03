# Runbook: Rollback

**Status: stub — procedure ships with Phase 4 (rollback lifecycle features ship in Phase 4).**

This file closes the dead link from `docs/architecture/index-lifecycle.md` §10.3.

## Triggering events

1. A promotion produces unexpected retrieval regression; operator needs to revert to N-1 without waiting for a new build.
2. An automated quality gate detects a post-promotion regression below the configured threshold and triggers a rollback alert.

## Designed behaviour

Rollback to N-1 is an atomic alias swap (same two-phase protocol as promotion). No data is moved or rebuilt. Blocked if the N-1 embedding model is no longer configured — see OQ-L-7 for the recovery path.

See `docs/architecture/index-lifecycle.md`: §10.3 (instant rollback), §4 (alias swap protocol), OQ-L-7 (blocked rollback). For cold-snapshot restore beyond N-1, see `runbooks/restore-cold.md`.
