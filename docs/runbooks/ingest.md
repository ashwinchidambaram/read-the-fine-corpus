# Ingest Runbook

**Triggering event:** Operator starting a new ingestion job.
**Phase:** 1 (this runbook describes the Phase 1 CLI and artifact layout).
**Related:** [pipeline README](../pipeline/README.md), [API reference](../api/README.md),
[index lifecycle](../architecture/index-lifecycle.md), [reindex runbook](reindex.md).

---

## Overview

A full first-time ingestion run follows this sequence:

```
corpus init           → write corpus.yaml + run preflight
corpus preflight      → validate config, connectivity, provider
corpus pipeline run   → Collect → Assess → Decompose → Plan → Build → (promote)
```

Each stage produces a durable JSON artifact. If any stage fails, the run stops and
the completed artifacts remain on disk — the run is resumable by re-running the
same command with the same `--run-id`.

---

## Step 1 — First-time setup: `corpus init`

Run once per deployment. Writes `corpus.yaml` from `corpus.example.yaml`, prompts
for provider settings, and validates the chosen provider before anything else.

```bash
# Interactive (prompts for provider, model, Ollama URL):
corpus init

# Scripted — Ollama only:
corpus init \
  --provider ollama \
  --ollama-base-url http://localhost:11434 \
  --ollama-model nomic-embed-text \
  --non-interactive

# Scripted — OpenAI only:
# Set the API key in the environment BEFORE running (never write it to corpus.yaml):
export OPENAI_API_KEY=<your-key>
corpus init \
  --provider openai \
  --openai-model text-embedding-3-small \
  --non-interactive

# Scripted — both providers (default = openai):
export OPENAI_API_KEY=<your-key>
corpus init \
  --provider both \
  --ollama-base-url http://localhost:11434 \
  --non-interactive
```

**Output:**

- `corpus.yaml` written from `corpus.example.yaml` with chosen provider settings.
- API key export instruction printed to stdout (never written to the file).
- Preflight report printed.
- `platform.first_run_complete: true` stamped in `corpus.yaml` only if preflight passes.

**If preflight fails:** `corpus.yaml` is written but `first_run_complete` is not set.
Fix the issue (provider down, wrong API key, Qdrant unreachable) and re-run:

```bash
corpus preflight --config corpus.yaml
# or re-run the full init:
corpus init --provider ollama --non-interactive
```

---

## Step 2 — Preflight check: `corpus preflight`

Validates the configuration file, connectivity to all services, and the chosen
embedding provider before starting ingestion.

```bash
corpus preflight --config corpus.yaml
```

**Checks performed:**

| Check | What it validates |
|---|---|
| `config_parse` | corpus.yaml parses and validates against the config model. |
| `postgres` | TCP connection to PostgreSQL. |
| `qdrant` | HTTP health check against `storage.qdrant.url/healthz`. |
| `object_store` | TCP connection to the object-store endpoint. |
| `embedding_provider` | Provider is reachable; model available; declared dimensions match probe. |
| `resource_headroom` | (SKIPPED in Phase 1) Qdrant memory vs hot retention count. |

**Exit codes:**

- `0` — all checks passed (no FAIL results).
- `1` — at least one check failed. Fix the failing check before ingesting.

**SKIPPED checks** are always reported — they are never silently omitted. A SKIPPED
check is not a failure; it is a deferred check with a logged reason.

---

## Step 3 — Pipeline run: `corpus pipeline run`

Runs the full five-stage pipeline over a source directory.

```bash
corpus pipeline run \
  --source ./docs-to-ingest \
  --artifacts ./artifacts \
  --run-id my-kb-run-001 \
  --workspace ws-acme \
  --kb kb-policy-docs
```

**Arguments:**

| Flag | Description |
|---|---|
| `--source DIR` | Directory of documents to ingest (PDF files in Phase 1). |
| `--artifacts DIR` | Root directory for stage artifacts (created if absent). |
| `--run-id ID` | Stable run identifier. No default — must be supplied. Same `run-id` = same artifact paths (determinism). |
| `--workspace WORKSPACE_ID` | Workspace identity (ULID or human-readable ID). |
| `--kb KB_ID` | Knowledge-base identity (ULID or human-readable ID). |

**Artifact layout:**

```
<artifacts>/<run-id>/collect.json    — Inventory (§12 contract, schema 1.0.0)
<artifacts>/<run-id>/assess.json     — ParseResultBatch (§12 contract, schema 1.0.0)
<artifacts>/<run-id>/decompose.json  — SegmentSetBatch (§12 contract, schema 1.0.0)
<artifacts>/<run-id>/plan.json       — IngestionConfig (§12 contract, schema 1.0.0)
<artifacts>/<run-id>/build.json      — BuildResult (pipeline-internal envelope)
```

**Phase 1 supported document types:** Native-text PDF. Other types are collected
into the Inventory but are marked with an honest exclusion reason in the Assess
output and produce no segments or chunks.

**Exit codes:**

- `0` — pipeline completed successfully.
- `1` — unexpected error.
- `2` — contract version rejected (a stage received input in an unsupported version).
- `3` — stage failure (a stage's `_produce()` raised `StageError`).
- `4` — artifact store error (serialization/write failure).

---

## Step 4 — Monitor progress

Stage artifacts appear on disk as each stage completes. Check progress:

```bash
ls -la artifacts/<run-id>/
```

The artifact files appear in order: `collect.json`, `assess.json`, `decompose.json`,
`plan.json`, `build.json`. A missing file indicates that stage has not completed.

For the findings report, inspect the Assess artifact:

```bash
python3 -c "
import json
report = json.load(open('artifacts/<run-id>/assess.json'))
for doc in report['results']:
    print(doc['document_id'], doc.get('quality_score'), doc.get('excluded', False))
"
```

---

## Step 5 — Interpret the findings report

The Assess stage (`assess.json`) contains per-document quality findings. Look for:

- `excluded: true` — document could not be parsed (password-protected, malformed,
  image-only). The reason is in `exclusion_reason`.
- `quality_score` below 0.60 — below the OCR confidence exclude floor; segments
  from this document will be tier `excluded`.
- `quality_score` between 0.60 and 0.80 — below the OCR confidence warn level;
  segments assigned tier `supporting` with a low-confidence flag.

---

## Failure modes and recovery

| Failure | Symptom | Recovery |
|---|---|---|
| **Embedding provider unavailable** | Build stage pauses or exits with `PROVIDER_UNAVAILABLE`. | Fix the provider (see `provider-outage runbook`), then re-run with the same `--run-id` to resume from the last checkpoint. |
| **Qdrant unavailable** | Build stage fails at shadow collection creation or chunk write. | Restore Qdrant (see [troubleshooting](../troubleshooting/README.md#vector_db_unavailable)), then re-run. |
| **Single document fails to parse** | Assess artifact includes the document with `excluded: true`. | Ingestion continues; the document is recorded in the exclusion report. No recovery needed unless the document must be indexed. |
| **Whole content class fails** | Build stage exits with `StageError` referencing a class-wide failure. | Review the artifact store for the affected stage. This is treated as a configuration problem — review chunking config and re-run. |
| **Budget cap hit** | (Phase 4) Ingestion pauses with a budget-cap alert. | See `budget-cap runbook`. |
| **Artifact store write failure** | Exit code 4. Disk full or permissions issue. | Free disk space or fix permissions on `--artifacts` directory, then re-run. |

---

## Promote to live

By default, `corpus pipeline run` writes a shadow collection (C-4: ingestion writes
to shadow, never to live). Promotion requires an explicit flag on the extended
`run_pipeline()` API (not yet exposed via CLI in Phase 1 — use the Python API
directly for integration tests).

To check the alias and promotion state:

```bash
curl http://localhost:8001/v1/kb/<kb-id>/status
```

A promoted KB shows `"ready": true`. Queries can be served once `ready` is true.

---

## Related pages

- [Pipeline README](../pipeline/README.md) — stage contract and orchestrator details.
- [Preflight checks](../configuration/reference.md) — full check description.
- `Provider-outage runbook` — embedding provider down mid-run.
- [Reindex runbook](reindex.md) — re-run ingestion after source changes.
- [Troubleshooting](../troubleshooting/README.md) — error codes with operator actions.
