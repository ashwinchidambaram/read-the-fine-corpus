# Runbook: Budget Cap Hit

**Triggering event:** An ingestion or reindex job enters `paused_budget` state because the estimated cost would exceed a per-KB or per-workspace budget cap configured in `corpus.yaml`.

---

## What the operator sees

1. **Job state:** The job's `state` field transitions from `running` to `paused_budget`. This is visible via:
   ```
   corpus jobs status <job_id>
   ```
   or
   ```
   corpus jobs list --kb <kb_id>
   ```
   The `paused_budget` state is a permanent pause — the job does not automatically resume.

2. **CLI output (direct-run mode):** When `corpus pipeline run` is used directly (not via the job queue), the CLI prints:
   ```
   ERROR: Per-KB budget cap hit (estimated $X.XXXX would exceed cap).
   ```
   or
   ```
   ERROR: Per-workspace budget cap hit (...)
   ```
   and exits with code 1.

3. **Scheduled reindex cap-hits:** A scheduled reindex trigger increments `consecutive_cap_hits` on the trigger record each time it fires and hits the cap. When `consecutive_cap_hits >= budgets.scheduled_reindex_cap_hit_alert_count` (default: 3), the reindex engine logs an `ERROR` with `ALERT` in the message, indicating the scheduled reindex has been blocked repeatedly.

---

## Diagnosing which cap was hit

Check the job details for cost attribution:
```
corpus jobs status <job_id>
```

The `payload` and `checkpoint_data` fields in the job record contain cost tracking information including:
- Current spend so far
- Projected total cost
- Which cap (per-KB or per-workspace) triggered the pause

Also check the configuration caps:
```
# corpus.yaml
budgets:
  per_kb_cap_usd: <value>
  per_workspace_cap_usd: <value>
  scheduled_reindex_cap_hit_alert_count: 3
```

---

## Resuming a paused job

**Option 1: Resume the existing job** (the cost incurred so far counts against the cap; you are authorizing continuation within the current cap):
```
corpus jobs resume <job_id>
```

Or via the control API:
```
POST /v1/jobs/<job_id>/resume
```

The `resume` command transitions the job from `paused_budget` back to `queued`, where it will be claimed by the next available ingest worker. The worker will continue from the last checkpoint (M-082: no duplication or loss).

**Note:** If the job hits the cap again after resuming (because the remaining work still exceeds the budget), it will pause again.

**Option 2: Raise the budget cap** and then resume. Edit `corpus.yaml`:
```yaml
budgets:
  per_kb_cap_usd: <new_higher_value>
```
Then resume the job:
```
corpus jobs resume <job_id>
```

**Option 3: Cancel the job** if the work should not proceed:
```
corpus jobs cancel <job_id>
```
Cancellation leaves the shadow collection in a partial state (not promoted). The alias is unchanged. The shadow collection may be cleaned up by a maintenance sweep.

---

## Diagnosing repeated cap-hits from scheduled reindexes

When a scheduled reindex trigger repeatedly hits the budget cap:

1. Check the `consecutive_cap_hits` count on the trigger record by reviewing the job history:
   ```
   corpus jobs list --kb <kb_id> --limit 10
   ```
   The job records show `consecutive_cap_hits` in the job payload. Reindex trigger configuration (including the cron expression) is managed via `corpus.yaml` — there is no REST endpoint for reading or updating trigger records.

2. Review recent reindex jobs to understand how much they cost and why:
   ```
   corpus jobs list --kb <kb_id> --limit 10
   ```

3. Common causes of repeated cap-hits:
   - The corpus has grown significantly since the cap was set.
   - The embedding model was changed (triggering `reindex_full` via `config_change` trigger, which is more expensive than `reindex_incremental`).
   - The `scheduled_reindex_cap_hit_alert_count` threshold has been crossed — an `ERROR ALERT` log is emitted.

4. Resolution options:
   - **Raise the cap** as described above.
   - **Reduce reindex frequency** — change the cron expression for the scheduled trigger in `corpus.yaml`:
     ```yaml
     reindex_triggers:
       - kind: scheduled
         cron_expr: "0 3 * * 0"  # weekly instead of daily
     ```
     Then reload the config and trigger a new evaluation.
   - **Switch to change-detected** — if the corpus is largely static, replace the scheduled trigger with a `change_detected` trigger so reindexes only fire when content actually changes.

---

## Alert threshold for consecutive cap-hits

The `budgets.scheduled_reindex_cap_hit_alert_count` configuration key (default: 3) controls when an `ERROR ALERT` log entry is emitted. This is not a hard block — the trigger continues to fire on schedule, hit the cap, and pause. The alert signals that the situation requires human attention.

When the alert fires:
1. The reindex job is still being paused; content may be stale if the scheduled reindex is the primary update mechanism.
2. Manual action is required to either raise the cap or adjust the schedule.
3. Reset `consecutive_cap_hits` to 0 (automatically happens on a successful completion) by raising the cap and allowing one successful run.

---

**See also:**
- `docs/architecture/index-lifecycle.md` §7.3 (scheduled reindex budget cap), §16 (budget caps)
- `docs/configuration/reference.md §2.9` (`budgets.*` configuration keys)
- `runbooks/reindex.md` — reindex trigger monitoring
