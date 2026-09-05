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

See also: `provider-outage runbook`.

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
   `backup runbook` for the tombstone log backup
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

---

## Phase 2 failure modes

### `missing_dependency` (finding code on scanned PDFs)

**Symptom:** A scanned PDF document's `ParseResult` has `parse_status: failed` and a
finding with `code: missing_dependency`. The document is recorded as failed in the
Assess artifact; it will appear in the exclusion report with reason `parse_failed`.

**Meaning:** The `tesseract` binary was not found on `PATH` at the time the Assess
stage ran. The scanned-PDF parser checks for `tesseract` using `shutil.which` before
performing any OCR work. If the binary is absent the parser returns an honest failed
result rather than crashing; no pages are processed.

**Operator action:**

1. Install the `tesseract-ocr` OS package:
   - **Debian/Ubuntu:** `sudo apt-get install tesseract-ocr`
   - **macOS (Homebrew):** `brew install tesseract`
   - **RHEL/CentOS:** `sudo yum install tesseract`
2. Confirm the binary is on PATH: `which tesseract` should return a path.
3. If running in Docker, rebuild the image — `tesseract-ocr` is included in the
   provided Dockerfile. Confirm with `docker compose build ingest-worker`.
4. Re-run the pipeline from the Collect stage. The scanned-PDF parser will retry
   the previously failed documents automatically (they remain in the inventory).
5. Confirm by checking that the Assess artifact no longer contains `missing_dependency`
   findings for scanned-PDF documents.

Note: tests that depend on tesseract are skipped with `pytest.skip()` when the binary
is absent. This is the expected CI behaviour in environments without tesseract.

---

### `spreadsheet_open_failed` (finding code on spreadsheet documents)

**Symptom:** A spreadsheet document's `ParseResult` has `parse_status: failed` and a
finding with `code: spreadsheet_open_failed`. The document appears in the exclusion
report.

**Meaning:** The spreadsheet parser could not open the file with `openpyxl`. This
typically indicates the file is corrupt, is not a valid `.xlsx` file (e.g., a `.csv`
renamed to `.xlsx`), or uses a format not supported by `openpyxl` (e.g., legacy `.xls`
binary format).

**Operator action:**

1. Try opening the file in Excel or LibreOffice to confirm it is valid.
2. If the file is a legacy `.xls` format, convert it to `.xlsx` using
   `libreoffice --headless --convert-to xlsx <file>` before ingesting.
3. If the file is a `.csv`, add it as a plaintext document instead — CSV files are
   handled by the plaintext parser, not the spreadsheet parser.
4. If the file is corrupt, obtain a fresh copy from the source system.

---

### `html_read_failed` / `html_parse_failed` (finding codes on HTML documents)

**Symptom:** An HTML document's `ParseResult` has `parse_status: failed` with finding
code `html_read_failed` (file could not be opened) or `html_parse_failed` (file opened
but `html.parser` raised an error).

**Meaning:** `html_read_failed` indicates the file could not be read (wrong encoding,
permission issue, or the file was removed after the Collect stage). `html_parse_failed`
indicates a severe structural defect in the HTML that caused the stdlib `html.parser`
to fail; this is rare since `html.parser` is permissive.

**Operator action:**

1. For `html_read_failed`: confirm the file exists at the path recorded in the
   Collect artifact and is readable. Check file encoding — the parser uses
   UTF-8 with `errors="replace"` so pure encoding issues should not normally
   cause this error.
2. For `html_parse_failed`: inspect the file for embedded null bytes or binary
   data masquerading as HTML. Run `file <path>` to confirm the MIME type.
3. Re-run the pipeline after fixing the file. If the file cannot be fixed,
   exclude it from the source directory.

---

---

## Phase 3 failure modes

### pytest silently skips tests in `tests/**/build/` directories

**Symptom:** Adding test files under a `tests/phase3/build/` or `tests/pipeline/build/`
subdirectory does not increase the reported test count. `pytest --collect-only` does not
list any tests from those directories. No error is reported.

**Meaning:** `pytest` ships with a default `norecursedirs` setting that includes `build`.
Any test directory named `build` (at any depth under the test root) is silently skipped
by the collector. This affects not only top-level `build/` but also nested paths like
`tests/phase3/build/` — the directory name alone triggers the skip, regardless of nesting.

**How discovered:** During Phase 3 build, tests written under
`tests/phase3/build/test_chunkers.py` produced a mysteriously stable test count.
`pytest --collect-only` confirmed zero items collected from that path. Renaming the
directory to `tests/phase3/chunkers/` immediately fixed collection.

**Fix:** Either rename the directory away from `build`, or add an explicit `norecursedirs`
override in `pyproject.toml` to un-exclude it:

```toml
[tool.pytest.ini_options]
norecursedirs = [".git", ".venv", "__pycache__", "node_modules", "dist", ".eggs"]
# Note: "build" is intentionally absent — we have test directories under that name.
```

The full default `norecursedirs` list that pytest applies (as of pytest 7+) is:
`*.egg`, `.svn`, `CVS`, `.bzr`, `.hg`, `.git`, `__pycache__`, `{arch}`, `.tox`,
`venv`, `.venv`, `_darcs`, `buck-out`, `build`, `dist`, `node_modules`.

**Recommendation:** Always add an explicit `norecursedirs` in `pyproject.toml` so the
setting is version-controlled and does not depend on pytest's default behaviour.

---

### Absolute checkout paths in subprocess cwd in tests

**Symptom:** A test that uses `subprocess.run(..., cwd="/home/runner/work/repo")` or
similar hard-coded paths passes locally but fails in CI with `FileNotFoundError` or
`subprocess.CalledProcessError`. The failure message references a path that does not
exist in the CI runner's filesystem.

**Meaning:** CI runners (GitHub Actions, GitLab CI, etc.) check out repositories to
runner-specific paths (`/home/runner/work/<repo>/<repo>` on GitHub) that differ from
developer machines. Hard-coded absolute paths are never portable across environments.

**How discovered:** During Phase 3 test development, a subprocess-based integration test
hard-coded `cwd=pathlib.Path("/Users/dev/projects/rtfc/")` in the test helper. The test
passed locally and failed immediately in CI with `No such file or directory`.

**Fix:** Derive the working directory from `__file__` (the test module's own path) and
navigate relative to that:

```python
# WRONG — hard-coded absolute path
subprocess.run(["uv", "run", "pytest"], cwd="/Users/dev/projects/rtfc")

# CORRECT — derived from __file__
REPO_ROOT = pathlib.Path(__file__).parent.parent.parent  # adjust depth as needed
subprocess.run(["uv", "run", "pytest"], cwd=REPO_ROOT)
```

For golden corpus paths specifically, use:

```python
CORPUS_DIR = pathlib.Path(__file__).parent.parent / "fixtures" / "golden" / "corpus"
```

This pattern is used consistently throughout the Phase 3 test suite.

**Rule:** Never hard-code absolute paths as `cwd`, `source_dir`, or any filesystem
argument in tests. Always derive from `__file__` or from `tmp_path` / `tmp_path_factory`
pytest fixtures.

---

### Missing decompose artifact in report

**Symptom:** Running `corpus report` produces an exclusion report that opens with the
warning: *"WARNING: The decompose artifact is missing for this run."* Segment-level
exclusions are absent from the report.

**Meaning:** The `generate_report()` function found the Assess artifact but not the
Decompose artifact for the requested run ID. This happens when the pipeline was
interrupted after the Assess stage completed but before the Decompose stage wrote its
artifact, or when the Decompose artifact was deleted.

**Operator action:**

1. Check whether the Decompose artifact file exists:
   `ls <artifacts-dir>/<run-id>/decompose.json`.
2. If the file is missing, re-run the pipeline starting from the Decompose stage.
   The pipeline is resumable — pass the same `--run-id` to pick up from the
   last completed stage.
3. Once the Decompose artifact exists, re-run `corpus report` to produce the
   complete exclusion report.
4. If resuming is not possible (e.g., source files were deleted after Collect),
   the partial exclusion report is still valid for Assess-level exclusions.
   Document the gap in your audit trail.
