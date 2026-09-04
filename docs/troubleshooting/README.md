# Troubleshooting

Phase 1 error codes and operational guidance. Every error code the platform can emit
gets an entry here in the same PR that introduces it (spec §4.6 — living wiki discipline).

Error codes are returned in `error.code` of the retrieval-response envelope (see
[API reference](../api/README.md#error-codes)).

---

## Error codes

### `EMBEDDING_MODEL_MISMATCH`

**Symptom:** A query against a knowledge base returns HTTP 409 with
`result_status: error` and `error.code: EMBEDDING_MODEL_MISMATCH`. No results are
returned. The embedding provider's `embed_batch` is not called (the service detects
the mismatch before calling the provider).

**Meaning:** The embedding provider configured in the running retrieval service uses
a different model identity than the one used to build the promoted collection. The
alias record carries the model ID, provider, and dimensions that were used at ingest
time; the retrieval service checks these against the current provider's declared
capabilities before calling embed. A mismatch means the query vector would be in a
different vector space than the indexed vectors — results would be meaningless. The
service fails closed rather than returning silent garbage.

**Operator action:**

1. Check the alias record to see what model was used to build the collection:
   `GET /v1/kb/{kb_id}/status` — the `embedding_model`, `embedding_provider`, and
   `embedding_dimensions` fields show what was indexed.
2. Check the current provider configuration in `corpus.yaml`:
   `providers.embedding.default`, `providers.embedding.cloud.model` (OpenAI), or
   `providers.embedding.local.model` (Ollama).
3. Either:
   - Restore the provider config to match the indexed model (so queries work
     against the existing collection), **or**
   - Re-run ingestion with the new model (which produces a new shadow collection
     with the new model identity, then promotes it — the alias record is updated
     and the mismatch is resolved).
4. After correcting the config, re-run `corpus preflight --config corpus.yaml` to
   confirm the embedding provider check passes before re-starting the retrieval
   service.

---

### `PROVIDER_UNAVAILABLE`

**Symptom:** Query returns HTTP 503 with `error.code: PROVIDER_UNAVAILABLE`. The
embedding provider did not respond or returned an error when the retrieval service
tried to embed the query text.

**Meaning:** The configured embedding provider (OpenAI or Ollama) is unreachable or
returned an error at query time. The retrieval service fails closed: no results are
returned, no degraded fallback occurs (§15 row 2 — silent fallback to a different
model is explicitly prohibited).

**Operator action:**

1. Check provider health directly:
   - **Ollama:** `curl http://localhost:11434/api/tags` — should return 200.
   - **OpenAI:** verify `OPENAI_API_KEY` is set correctly and the OpenAI API is
     reachable (`curl https://api.openai.com/v1/models`).
2. Run `corpus preflight --config corpus.yaml` to run the full provider health check
   with latency measurement and dimension verification.
3. Once the provider is healthy, queries will succeed automatically — the retrieval
   service does not cache the error state. No restart required.
4. If ingestion was in progress, it will have entered `PAUSED` state. After the
   provider recovers, resume ingestion — the job is resumable from the last
   checkpoint without data loss or duplication.

See also: [provider-outage runbook](../runbooks/provider-outage.md).

---

### `VECTOR_DB_UNAVAILABLE`

**Symptom:** Query returns HTTP 503 with `error.code: VECTOR_DB_UNAVAILABLE`. Both
retrieval (`GET /v1/kb/{kb_id}/status` returning 503) and ongoing ingestion jobs
pause. The alias lookup or Qdrant query failed.

**Meaning:** Qdrant is unreachable, or the alias referenced in the query is not
found (which is indistinguishable from Qdrant being down at the adapter level). All
reads and writes through the index adapter fail closed.

**Operator action:**

1. Check Qdrant health: `curl http://localhost:6333/healthz` — should return 200.
2. Check `docker compose ps` to confirm the `qdrant` container is running and
   healthy.
3. If Qdrant restarted, in-memory collections may need to be reloaded. Qdrant
   persists to disk by default; check `docker compose logs qdrant` for
   reload errors.
4. Once Qdrant is healthy, retrieval requests will succeed automatically.
   In-progress ingestion jobs will resume from their last checkpoint.
5. Run `corpus preflight --config corpus.yaml` to confirm the qdrant check passes
   before declaring recovery.

---

### `CONTROL_PLANE_UNAVAILABLE`

**Symptom:** Query returns HTTP 503 with `error.code: CONTROL_PLANE_UNAVAILABLE`.
The retrieval service cannot read the alias record from the PostgreSQL control-plane
database. Ingestion may also report failures at the control-plane boundary.

**Meaning:** PostgreSQL is unreachable at query time. The retrieval service reads
the alias record (which stores model identity, config version, and promotion
timestamp) from the control plane on every query. Without this record, the
EMBEDDING_MODEL_MISMATCH check cannot be performed safely, so the service fails
closed.

**Operator action:**

1. Check PostgreSQL health: `curl -s $(docker inspect --format '{{.NetworkSettings.IPAddress}}' <postgres-container>):5432` or check `docker compose ps`.
2. Run `corpus preflight --config corpus.yaml` — the `postgres` check reports
   connectivity and latency.
3. Once PostgreSQL recovers, retrieval will work again automatically (no restart
   required).
4. If the database was restored from backup, verify the tombstone log is intact
   before resuming ingestion — a database backup that predates deletion events
   can produce a tombstone-replay gap. See
   [backup runbook](../runbooks/backup.md) for the tombstone log backup
   discipline.

---

### `PAYLOAD_CORRUPT`

**Symptom:** A query returns HTTP 500 with `error.code: PAYLOAD_CORRUPT`. This error
is per-point, not per-query — it fires when a retrieved Qdrant point is missing one
or more required provenance fields from its payload.

**Meaning:** The payload stored in Qdrant for a specific chunk is missing required
§8 provenance fields (e.g., `source_document_id`, `trust_level`, `segment_type`).
This indicates a data integrity issue — either the chunk was written by an older
version of the ingest worker that did not populate the full provenance block, or the
payload was corrupted in storage.

**Operator action:**

1. Identify the affected document(s): the error message will include the `chunk_id`
   of the corrupt point. Use `chunk_id` to trace back to `source_document_id`
   via the control-plane metadata.
2. Re-ingest the affected document: remove it from the source directory, run
   ingestion to remove its chunks (replace-by-document semantics), then re-add
   the document and re-ingest. The new chunks will have complete provenance.
3. If multiple documents are affected, check whether the issue occurred during a
   specific build ID — the ingestion log will show which build produced the
   corrupt points.
4. After re-ingesting, re-run the query to confirm the error is resolved.

---

### `KB_NOT_READY`

**Symptom:** Query returns HTTP 404 with `error.code: KB_NOT_READY`. The knowledge
base ID is known but has no promoted collection.

**Meaning:** The knowledge base has been registered in the control plane but no
ingestion run has successfully completed and promoted a collection. Queries cannot
be served until at least one collection is promoted (the alias has no target).

**Operator action:**

1. Check whether ingestion has been run: `GET /v1/kb/{kb_id}/status` — if
   `ready: false`, no collection has been promoted yet.
2. Run the ingestion pipeline:
   ```
   corpus pipeline run \
     --source <source-dir> \
     --artifacts <artifacts-dir> \
     --run-id <run-id> \
     --workspace <workspace-id> \
     --kb <kb-id>
   ```
3. Wait for the pipeline to complete and promote. Once `ready: true`, queries
   will be served.
4. If ingestion failed before promotion, check the artifact store for the
   pipeline run — the stage artifacts show where it stopped.

See also: [ingest runbook](../runbooks/ingest.md).

---

## QdrantClient thread-safety note

**Context:** The `QdrantAdapter` singleton wraps the `qdrant-client` library. In
async/multi-threaded deployments, multiple retrieval requests may share a single
`QdrantClient` instance.

**Behaviour:** The `qdrant-client` uses `httpcore` connection pooling internally.
`httpcore` pools are protected by a threading lock, so concurrent requests through
a single `QdrantClient` are thread-safe at the HTTP layer. The client's internal
connection pool serializes concurrent access correctly.

**Supported topology:** A shared singleton `QdrantAdapter` (one per process) is the
supported production topology. Do NOT create a new `QdrantAdapter` per request — the
connection pool would not be shared, defeating the pool's purpose and causing
connection exhaustion under load.

**Not supported:** Using the same `QdrantClient` instance from multiple _processes_
without a connection proxy (e.g., a PgBouncer equivalent) is not supported. Each
process should construct its own `QdrantAdapter` singleton.

This note was added from the PR #13 review (concurrency review finding F-03).
