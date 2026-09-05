# Configuration Reference

> **Status:** Proposed — design phase; defaults marked **(proposed)** are pending design review;
> this page becomes the single authoritative config schema in Phase 0.

---

## 1. Config model

Read The Fine Corpus is configured from **one declarative file** — `corpus.yaml` — located at:

```
<repo-root>/corpus.yaml          # default; override with FINECORPUS_CONFIG_PATH
```

Every option is present in the file, commented, with its default shown. Nothing is hidden in code
(§4.6 "nothing is hidden in code"). If a tunable exists in the implementation, it MUST have a row
on this page.

### Environment variable overrides

Every key in `corpus.yaml` can be overridden by an environment variable. Convention:

- Prefix: `FINECORPUS_`
- Key path separator: double underscore `__`
- Array indices: `__0__`, `__1__`, etc.

Examples:

| Config key path | Env var |
|---|---|
| `providers.embedding.cloud.model` | `FINECORPUS_PROVIDERS__EMBEDDING__CLOUD__MODEL` |
| `storage.postgres.url` | `FINECORPUS_STORAGE__POSTGRES__URL` |
| `budgets.per_kb_cap_usd` | `FINECORPUS_BUDGETS__PER_KB_CAP_USD` |
| `index_lifecycle.hot_retention_count` | `FINECORPUS_INDEX_LIFECYCLE__HOT_RETENTION_COUNT` |

### Precedence

```
env var  >  corpus.yaml  >  built-in default
```

The built-in default is the value shown in the table below when the key is absent from both the
file and the environment. Built-in defaults are all marked **(proposed)** until design review
closes.

### Secrets

Secrets (API keys, database passwords) MUST be supplied via environment variables or a secrets
manager reference, never written into `corpus.yaml`. Config exports produced by `corpus export`
are secret-free by construction (§14.2): provider names appear, credential values do not.

---

## 2. Full configuration table

### 2.1 Platform

| Key path | Type | Default | Spec § | Source |
|---|---|---|---|---|
| `platform.instance_name` | string | `"rtfc"` | §4.6 | Platform identity; used in collection naming prefix `rtfc_`. |
| `platform.first_run_complete` | bool | `false` | §4.6 | Set to `true` by the first-run prompt after provider validation. Do not set manually. |
| `platform.airgap` | bool | `false` | §4.3, §7.2 | Equivalent to `RTFC_AIRGAP=true`. Blocks all outbound HTTP before any provider call. All providers must have `is_local: true`. In airgap mode, pricing-fetch is unconditionally skipped and cost estimates use declared `cost_per_1k_tokens` (null for local providers). |
| `platform.log_level` | enum `{debug,info,warn,error}` | `"info"` | §13 | Structured log level for all services. |
| `platform.config_distribution_poll_interval_seconds` | int | `30` **(proposed)** | §4.6, ADR-0005 | How often services poll the control-plane database for config changes. A config change takes effect within one poll interval without a restart. Setting to `0` disables polling (restart required for config changes). |

### 2.2 Storage backends

| Key path | Type | Default | Spec § | Source |
|---|---|---|---|---|
| `storage.postgres.url` | string | — | §4.1 | PostgreSQL connection URL. Supply via env var; never write the password here. |
| `storage.postgres.pool_min` | int | `2` **(proposed)** | §4.1 | Minimum connection pool size. |
| `storage.postgres.pool_max` | int | `10` **(proposed)** | §4.1 | Maximum connection pool size. |
| `storage.qdrant.url` | string | `"http://qdrant:6333"` | §4.4 | Qdrant gRPC/HTTP endpoint. |
| `storage.qdrant.api_key` | string | — | §4.4 | Qdrant API key if Qdrant is configured with authentication. Supply via env var. |
| `storage.object_store.backend` | enum `{s3,minio,gcs,azure_blob}` | `"minio"` **(proposed)** | §10.2 | Object storage backend for cold snapshots and source-document retention. |
| `storage.object_store.bucket` | string | `"rtfc-snapshots"` **(proposed)** | §10.2 | Bucket / container name. |
| `storage.object_store.prefix` | string | `""` | §10.2 | Key prefix within the bucket. |
| `storage.object_store.endpoint_url` | string | — | §10.2 | Override endpoint (required for MinIO; leave unset for AWS S3). Supply via env var. |
| `storage.object_store.region` | string | `"us-east-1"` **(proposed)** | §10.2 | Region for S3/GCS/Azure backends. |
| `storage.cache.backend` | enum `{redis,postgres}` | — | §11.3, O-R6 | Backing store for the query-embedding cache. **Open question O-R6:** this decision is unresolved — Redis implies a fourth backing service; Postgres is consistent with ADR-0003 "boring and inspectable." See §Open questions. |
| `storage.cache.url` | string | — | §11.3 | Connection URL for the cache backend. Supply via env var. |

### 2.3 Providers — Embedding

| Key path | Type | Default | Spec § | Source |
|---|---|---|---|---|
| `providers.embedding.default` | string | — | §4.6 | Name of the default embedding provider (`openai` or `ollama`). Set by the first-run prompt. |
| `providers.embedding.cloud.provider_id` | string | `"openai"` | §7.1 | Provider identifier. |
| `providers.embedding.cloud.model` | string | `"text-embedding-3-small"` | §7.1, provider-abstraction.md §7.1 | Model identifier passed to the OpenAI API. |
| `providers.embedding.cloud.dimensions` | int | `1536` | provider-abstraction.md §7.1 | Must match the model's output dimensions. |
| `providers.embedding.cloud.max_batch_size` | int | `2048` | provider-abstraction.md §7.1 | OpenAI API batch limit. |
| `providers.embedding.cloud.cost_per_1k_tokens` | decimal | — | §16, provider-abstraction.md §2.1 | Current price; updated by `corpus provider update-pricing`. |
| `providers.embedding.cloud.pricing_as_of` | date | — | provider-abstraction.md OQ-P-5 | Date the cost figure was last verified. Platform warns if more than 90 days old **(proposed)**. |
| `providers.embedding.local.provider_id` | string | `"ollama"` | §7.2, provider-abstraction.md §7.2 | Local provider identifier. |
| `providers.embedding.local.endpoint` | string | `"http://localhost:11434"` | provider-abstraction.md §7.2 | Ollama HTTP endpoint. |
| `providers.embedding.local.model` | string | `"nomic-embed-text"` **(proposed)** | provider-abstraction.md §7.2 | Ollama model name (`nomic-embed-text` or `bge-m3`). |
| `providers.embedding.local.dimensions` | int | `768` **(proposed)** | provider-abstraction.md §7.2 | Must match the model. `nomic-embed-text`=768; `bge-m3`=1024. |
| `providers.embedding.local.batch_mode` | bool | `false` **(proposed)** | provider-abstraction.md OQ-P-3 | Opt-in batch mode for Ollama (requires minimum Ollama version documented per OQ-P-3). |

### 2.4 Providers — Internal LLM

Phase 3 real implementation. Keys marked **(Phase 3)** are wired to `ResolvedOpConfig` and active.

| Key path | Type | Default | Spec § | Source |
|---|---|---|---|---|
| `internal_llm.default.provider` | string | — | §7.3, M-036/M-037 | Default internal LLM provider (`openai`, `ollama`, or `fake`). **(Phase 3)** |
| `internal_llm.default.model` | string | — | §7.3 | Exact model identifier (e.g. `gpt-4o-mini`, `llama3.2`). **(Phase 3)** |
| `internal_llm.default.endpoint` | string | provider default | §7.3, M-037 | Override endpoint URL. Required for Ollama (`http://localhost:11434`); optional for OpenAI-compatible servers. **(Phase 3)** |
| `internal_llm.default.temperature` | float [0,1] | `0.2` | §7.3 | Temperature for internal model calls. **(Phase 3)** |
| `internal_llm.default.max_output_tokens` | int | `1024` | §7.3, D-21 | Hard ceiling on output tokens per call. Bounds cost against compromised/misconfigured endpoints (D-21 resolution). **(Phase 3)** |
| `internal_llm.operations.classification.provider` | string | inherits default | provider-abstraction.md §4.2 | Provider for segment classification. |
| `internal_llm.operations.classification.model` | string | inherits default | provider-abstraction.md §4.2 | |
| `internal_llm.operations.classification.temperature` | float [0,1] | `0.0` **(proposed)** | provider-abstraction.md §4.2 | Determinism is strongly preferred for classification. |
| `internal_llm.operations.augmentation.provider` | string | inherits default | provider-abstraction.md §4.2 | Provider for table description augmentation (Tier 2). **(Phase 3)** |
| `internal_llm.operations.augmentation.model` | string | inherits default | provider-abstraction.md §4.2 | **(Phase 3)** |
| `internal_llm.operations.augmentation.temperature` | float [0,1] | `0.3` **(proposed)** | provider-abstraction.md §4.2 | |
| `internal_llm.operations.augmentation.max_output_tokens` | int | inherits default | §7.3, D-21 | Per-operation override for augmentation output ceiling. **(Phase 3)** |
| `internal_llm.operations.question_generation.provider` | string | inherits default | provider-abstraction.md §4.2 | Provider for eval question generation. |
| `internal_llm.operations.question_generation.model` | string | inherits default | provider-abstraction.md §4.2 | |
| `internal_llm.operations.question_generation.temperature` | float [0,1] | `0.7` **(proposed)** | provider-abstraction.md §4.2 | |
| `internal_llm.operations.rewriting.provider` | string | inherits default | provider-abstraction.md §4.2 | Provider for Tier 3 rewriting. Local providers strongly preferred for sensitive corpora. |
| `internal_llm.operations.rewriting.model` | string | inherits default | provider-abstraction.md §4.2 | |
| `internal_llm.operations.rewriting.temperature` | float [0,1] | `0.2` **(proposed)** | provider-abstraction.md §4.2 | |
| `internal_llm.max_retries` | int | `3` **(proposed)** | §15, provider-abstraction.md §4.3 | Retry limit before the operation is recorded as a failure (single-doc → record+continue; class-wide → halt+alert). |

### 2.5 Ingestion and assessment thresholds

These govern the Assess (Stage 2) and Decompose (Stage 3) stages. They are platform-level defaults; per-KB overrides are expressed in the KB's ingestion config (a Plan-stage artifact, not this file — see §ingestion-config reference note below).

| Key path | Type | Default | Spec § | Source |
|---|---|---|---|---|
| `assessment.ocr_confidence_exclude_floor` | float [0,1] | `0.60` **(proposed)** | §6.2, §6.4 | `scanned_region` segments below this page-level OCR confidence are assigned tier `excluded`. | segment-taxonomy.md OQ-7 |
| `assessment.ocr_confidence_warn_level` | float [0,1] | `0.80` **(proposed)** | §6.2, §6.4 | Segments between `ocr_confidence_exclude_floor` and this level are assigned tier `supporting` with a low-confidence flag. Above this level: normal tier assignment. | segment-taxonomy.md OQ-7 |
| `assessment.boilerplate_corpus_proportion` | float [0,1] | `0.30` **(proposed)** | §6.2 | Branch (a): a text block appearing in more than this fraction of unique corpus documents is classified as `boilerplate`. | segment-taxonomy.md OQ-8 |
| `assessment.boilerplate_small_corpus_proportion` | float [0,1] | `0.50` **(proposed)** | §6.2 | Branch (a): boilerplate fraction threshold for small corpora (below `assessment.boilerplate_small_corpus_doc_count`). Raises the bar to avoid false positives. | segment-taxonomy.md OQ-8 |
| `assessment.boilerplate_small_corpus_doc_count` | int | `10` **(proposed)** | §6.2 | Corpus size below which the small-corpus boilerplate proportion applies (branch a). | segment-taxonomy.md OQ-8 |
| `assessment.boilerplate_abs_floor_count` | int | `3` **(proposed)** | §6.2 | Branch (b) / D-32: minimum total corpus occurrences (counting within-document repetitions) for the absolute-floor boilerplate branch. A block that appears at least this many times across the corpus triggers branch (b) when `assessment.boilerplate_abs_floor_fraction` is also met. | dedup-boilerplate.md §3 |
| `assessment.boilerplate_abs_floor_fraction` | float [0,1] | `0.05` **(proposed)** | §6.2 | Branch (b) / D-32: minimum unique-document fraction for the absolute-floor boilerplate branch. Prevents blocks appearing 3+ times within a single document in a large corpus (e.g. 1 of 100 docs → fraction 0.01) from being classified as corpus-wide boilerplate. | dedup-boilerplate.md §3 |
| `assessment.inline_split_min_lines` | int | `3` **(proposed)** | §6.3 | Inline minority content (e.g., a code snippet inside a prose paragraph) shorter than this line count is absorbed into the parent segment type. | segment-taxonomy.md OQ-5 |
| `assessment.inline_split_min_chars` | int | `200` **(proposed)** | §6.3 | Inline minority content shorter than this character count is absorbed into the parent segment type. Both `inline_split_min_lines` and `inline_split_min_chars` must be exceeded for a split to occur. | segment-taxonomy.md OQ-5 |
| `assessment.ocr_sub_decompose_confidence_floor` | float [0,1] | `0.85` **(proposed)** | §6.3 | Page-level OCR confidence above which the platform attempts sub-decomposition of a `scanned_region` into typed sub-segments. Below this threshold, the whole region stays as `scanned_region`. | segment-taxonomy.md OQ-4 |
| `assessment.chunk_count_tolerance_pct` | float [0,1] | `0.20` **(proposed)** | §10.4 | Validation gate 1: the shadow collection's chunk count must be within this fraction of the expected count (±20% by default) to pass. | index-lifecycle.md §5 Gate 1 |
| `assessment.eval_regression_threshold` | float [0,1] | `0.05` **(proposed)** | §10.4, §9.4 | Validation gate 4: the shadow collection's eval baseline score (context recall) must not fall more than this amount below the prior live baseline. A regression larger than this blocks promotion. | index-lifecycle.md §5 Gate 4 |
| `assessment.sweep_min_corpus_docs` | int | — | §9.3 | Below this document count, the platform declines to run a configuration sweep and applies the reference configuration. Value is executor-proposed and must be documented here before Phase 5. **(proposed — pending executor design)** | spec §9.3 |
| `ingestion.dedup.near_duplicate_threshold` | float [0,1] | `0.50` | §6.1 | Minimum Jaccard similarity (word 5-gram shingles) for two documents to be considered near-duplicates and grouped into a version family. Empirically determined from the golden corpus: policy_v1/v2/v3 share Jaccard similarities of 0.527–0.651. A value of 0.50 groups documents that share roughly half their 5-gram vocabulary — a reliable "same document, different version" signal for real policy/manual corpora. Raise to 0.70+ if you see false positives (unrelated documents grouped together). **Scaling ceiling:** exact pairwise Jaccard is O(N²); for corpora above ~5,000 documents use MinHash-LSH instead (Phase 5 / D-07). | corpus_passes.py `compute_version_families` |
| `ingestion.dedup.index_superseded_versions` | bool | `false` | §6.1, §6.3 | When `false` (default), superseded near-duplicate documents are inventoried and retained in object storage but produce no segments and no chunks; they appear in the exclusion report with reason "superseded by \<primary document_id\>". When `true`, superseded documents are decomposed and indexed at salience tier `excluded`, making them recoverable via explicit retrieval filter. Source: owner ruling 2026-09-03 (D-25). | segment-taxonomy.md OQ-11 (CLOSED); contracts/inventory.md version-family section |

> **Note on the ingestion config:** per-class settings (chunking strategy, transformation tiers, embedding override, salience filters) live in the **ingestion config** — a KB-level artifact produced by the Plan stage, not in `corpus.yaml`. The ingestion config is described in `docs/contracts/ingestion-config.md`. Do not duplicate per-class values here; reference the ingestion config contract instead. The settings above are platform-level assessment defaults that apply before a KB's ingestion config exists.

> **Note on `artifacts_root`:** the root directory for pipeline stage artifacts is **not** a
> `corpus.yaml` key. It is a direct parameter to `run_pipeline()` and to the `corpus pipeline
> run` CLI via the `--artifacts DIR` flag. Rationale: pipeline artifacts are ephemeral local
> files tied to a specific run (written under `<artifacts_root>/<run_id>/`), not a persistent
> storage backend configured alongside databases and object stores. Exposing it through the
> config would conflate two different concerns — long-lived infrastructure configuration and
> per-invocation working-directory selection. In production, `ingest-worker` will supply an
> appropriate artifacts root (local temp directory or a mounted job scratch volume) at job
> dispatch time, not from `corpus.yaml`.

### 2.6 Index lifecycle and retention

| Key path | Type | Default | Spec § | Source |
|---|---|---|---|---|
| `index_lifecycle.hot_retention_count` | int | `1` **(proposed)** | §10.2 | Number of previous collections to retain hot in Qdrant memory (N-1 count). Default 1 = instant rollback to N-1 available; set to 0 to disable hot standby (saves RAM; loses instant rollback — UI warns). | index-lifecycle.md §10.1, OQ-L-5 |
| `index_lifecycle.cold_retention_count` | int | `2` **(proposed)** | §10.2 | Number of cold snapshots to retain in object storage before purging the oldest. | spec §10.2, index-lifecycle.md OQ-L-5 |
| `index_lifecycle.validation_failed_shadow_retention_days` | int | `7` **(proposed)** | §15 | How long a `VALIDATION_FAILED` shadow collection is retained in Qdrant before being automatically cold-snapshotted and deleted. Operator is notified 24 hours before automatic purge. | index-lifecycle.md OQ-L-2 |
| `index_lifecycle.validation_failed_shadow_notify_hours_before` | int | `24` **(proposed)** | §15 | Hours before automatic purge of a VALIDATION_FAILED shadow at which the operator is notified. | index-lifecycle.md OQ-L-2 |
| `index_lifecycle.worker_heartbeat_timeout_seconds` | int | `60` **(proposed)** | §15 | If an ingestion worker misses a heartbeat for this duration, it is declared dead and the job is eligible for resumption by another worker. | index-lifecycle.md §6 (ingestion worker dies) |
| `index_lifecycle.service_principal_key_expiry_days` | int | `90` **(proposed)** | §14.2 | Default expiry for newly issued service principal keys. Keys expire after this period. A 7-day warning is issued before expiry. | provider-abstraction.md OQ-P-2 |
| `index_lifecycle.service_principal_key_expiry_warn_days` | int | `7` **(proposed)** | §14.2 | Days before service principal key expiry at which a warning is issued. | provider-abstraction.md OQ-P-2 |
| `index_lifecycle.pricing_stale_warn_days` | int | `90` **(proposed)** | §16 | Platform warns if `providers.embedding.cloud.pricing_as_of` is older than this many days. | provider-abstraction.md OQ-P-5 |
| `index_lifecycle.scheduled_reindex_cron` | string | — | §10.3 | Default cron expression for scheduled reindex trigger. Per-KB override available via the UI/API. Empty string disables scheduled reindex at platform level. | index-lifecycle.md §7.3 |
| `index_lifecycle.config_distribution_poll_interval_seconds` | int | `30` **(proposed)** | ADR-0005 | (Aliases `platform.config_distribution_poll_interval_seconds` — same setting.) | ADR-0005 Consequences |
| `index_lifecycle.break_glass_grant_window_hours` | int | — | §2.3 | Default time-bound window for a Platform Admin break-glass content-read grant. Value is executor-proposed (Open Decision #4). Must be finite. **(pending Open Decision #4)** | spec §2.3, Open Decision #4 |
| `index_lifecycle.snapshot_retention_period_days` | int | — | §17.1 | Maximum age of a cold snapshot before it is eligible for automatic purge (bounds deleted-content persistence). Value is executor-proposed (Open Decision #5). **(pending Open Decision #5)** | spec §17.1, Open Decision #5 |

### 2.7 Query embedding cache

| Key path | Type | Default | Spec § | Source |
|---|---|---|---|---|
| `cache.query_embedding.enabled` | bool | `true` | §11.3 | Enable the query-embedding cache. Disable only for debugging — agent workloads benefit greatly from caching. |
| `cache.query_embedding.ttl_seconds` | int | `3600` **(proposed)** | §11.3, provider-abstraction.md §5.3 | Cache entry TTL. After this period, entries are evicted and a fresh embedding is fetched. | provider-abstraction.md §5.3 |
| `cache.query_embedding.max_entries` | int | — | §11.3 | Maximum number of cache entries before LRU eviction. Executor-proposed value pending Phase 4 sizing. **(proposed — pending executor design)** | provider-abstraction.md §5 |

### 2.8 Retrieval

| Key path | Type | Default | Spec § | Source |
|---|---|---|---|---|
| `retrieval.default_top_k` | int | `10` **(proposed)** | §11.2 | Default number of chunks returned per query when not specified by the caller. |
| `retrieval.max_top_k` | int | `100` **(proposed)** | §11.2 | Maximum top-k a caller may request. |
| `retrieval.default_strategy` | enum `{dense,sparse,hybrid}` | `"hybrid"` **(proposed)** | §11.2 | Default retrieval strategy at KB level. Per-class and per-request overrides available. |
| `retrieval.supporting_tier_weight` | float | `0.7` **(proposed)** | §11.2, segment-taxonomy.md §3.1 | Score weighting applied to `supporting`-tier chunks relative to `primary` chunks. Configurable at KB level; caller may override per query. |
| `retrieval.rate_limit.queries_per_second_per_tenant` | int | — | §11.3 | Per-tenant query rate limit. Executor-proposed value pending Phase 4 design. **(proposed — pending executor design)** | spec §11.3 |

### 2.9 Budgets and cost governance

| Key path | Type | Default | Spec § | Source |
|---|---|---|---|---|
| `budgets.per_kb_cap_usd` | decimal | — | §16 | Per-knowledge-base budget cap in USD. Ingestion, reindex, and sweep operations pause when this cap is reached and alert. `null` = no cap. |
| `budgets.per_workspace_cap_usd` | decimal | — | §16 | Per-workspace budget cap in USD. `null` = no cap. |
| `budgets.ingestion_confirmation_threshold_usd` | decimal | `10.00` **(proposed)** | §16 | Operations estimated to cost more than this amount require explicit operator confirmation before starting. Easy-mode users see a currency figure; no token counts. |
| `budgets.sweep_confirmation_threshold_usd` | decimal | `5.00` **(proposed)** | §16, §9.3 | Configuration sweeps estimated to cost more than this amount require explicit confirmation. |
| `budgets.scheduled_reindex_cap_hit_alert_count` | int | `3` **(proposed)** | §16 | After this many consecutive budget-cap hits on scheduled reindexes, the Workspace Owner receives a repeated-cap-hit alert (in addition to the per-hit alert). | spec §16 |

### 2.10 Observability

| Key path | Type | Default | Spec § | Source |
|---|---|---|---|---|
| `observability.metrics_port` | int | `9090` **(proposed)** | §13.3 | Port for the Prometheus-compatible metrics endpoint. |
| `observability.otel_endpoint` | string | — | §13.3 | OpenTelemetry collector endpoint. Leave unset to disable trace export. |
| `observability.otel_service_name` | string | `"rtfc"` | §13.3 | Service name reported to the OTEL collector. |
| `observability.drift_detection_cron` | string | — | §9.4 | Cron expression for the scheduled drift-detection eval run. Complements the per-promotion eval gate; this catches drift between reindexes. |
| `observability.log_injection_suspicion_observations` | bool | `true` | §14.1 | Log security observations when augmentation or rewriting outputs contain injection-shaped content in reasoning/diff_summary fields. Does not affect pipeline behaviour; audit only. |

---

## 3. Preflight

The `corpus preflight` command validates the full configuration before any ingestion job is
accepted. It is required before the first ingestion (§4.6) and should be re-run after any
configuration change.

### What preflight checks

1. **Config syntax.** Parses and validates `corpus.yaml` against the schema. Reports every
   invalid key and type mismatch as a separate actionable error.

2. **PostgreSQL connectivity.** Opens a connection using `storage.postgres.url`, runs a
   lightweight query, reports latency. Failure: named connection string, error message.

3. **Qdrant connectivity.** Calls the Qdrant health endpoint at `storage.qdrant.url`. Checks
   that the API key is accepted (if configured). Failure: URL, HTTP status.

4. **Object store connectivity.** Lists the configured bucket with the declared credentials.
   Failure: bucket name, error.

5. **Cache backend connectivity.** Connects to the query-embedding cache backend and runs a
   ping. Failure: backend name, connection URL (credential redacted), error.

6. **Embedding provider availability.** For each configured embedding provider, runs
   `health_check()`:
   - Sends a fixed probe string to confirm the endpoint is reachable.
   - Confirms the declared model ID is available.
   - Confirms the probe embedding's dimensions match the declared `vector_dimensions`.
   - Reports latency.
   - In airgap mode (`platform.airgap: true`): skips cloud providers entirely and confirms
     all configured providers have `is_local: true`. Fails if any provider is cloud-only.
   - Pricing date check: warns if `pricing_as_of` is older than `index_lifecycle.pricing_stale_warn_days`.

7. **Internal LLM provider availability.** Runs the same reachability check for the configured
   internal LLM provider. Confirms the declared model is available.

8. **Resource headroom.** Queries Qdrant for current collection memory usage. Reports
   estimated headroom against `index_lifecycle.hot_retention_count` × estimated index size.
   Warns (does not block) if available RAM appears insufficient for the configured retention.

### Output format

Preflight reports one line per check, prefixed with `OK`, `WARN`, or `FAIL`. A `FAIL` result
blocks ingestion. A `WARN` result allows ingestion to proceed but is surfaced in the dashboard.

Every `FAIL` line is actionable: it names the failing component and states what to fix. Example:

```
FAIL  qdrant         Cannot connect to http://qdrant:6333 — connection refused. Check that the qdrant service is running (docker compose ps) and that storage.qdrant.url is correct.
WARN  pricing        OpenAI pricing_as_of is 2026-06-01 (94 days ago). Run `corpus provider update-pricing openai` to refresh cost estimates.
OK    postgres       Connected in 3ms.
OK    embedding      openai/text-embedding-3-small reachable; dimensions=1536 confirmed; latency=210ms.
```

Exit code is non-zero if any check fails.

---

## 3a. System dependencies (Phase 2+)

Some parsers require OS-level binaries in addition to Python packages.

### tesseract-ocr (Phase 2 — scanned PDF support)

**Required by:** `ScannedPDFParser` (`src/finecorpus/pipeline/assess/parsers/pdf_scanned.py`)
**Python package:** `pytesseract` (in `pyproject.toml` dependencies)
**Binary:** `tesseract` (from OS package `tesseract-ocr`)

| Environment | Install command |
|---|---|
| Debian/Ubuntu (CI, Docker) | `apt-get install -y tesseract-ocr` |
| macOS (local dev) | `brew install tesseract` |
| Alpine | `apk add tesseract-ocr` |

**If absent:** `ScannedPDFParser` returns an honest `parse_status=failed` result with
a `missing_dependency` finding — no exception, no crash. Native-text PDFs are unaffected.

**Verification:** `which tesseract` or `tesseract --version`.

---

## 4. Open questions

The following ambiguities were encountered during this reference page's compilation. None is
resolved silently.

| ID | Question | Source |
|---|---|---|
| OQ-C-1 | The query-embedding cache backing store (O-R6 in the operability review) is unresolved: Redis (an undeclared fourth service) vs. PostgreSQL (consistent with ADR-0003 "boring and inspectable") vs. in-process per replica. The `storage.cache` key above is a placeholder pending this decision. Resolution needed before Phase 4. | review-operability.md O-R6, provider-abstraction.md §5.1 |
| OQ-C-2 | `index_lifecycle.break_glass_grant_window_hours` is pending Open Decision #4 (spec §20). The default must be finite per §2.3. | spec §2.3, §20 decision #4 |
| OQ-C-3 | `index_lifecycle.snapshot_retention_period_days` is pending Open Decision #5 (spec §20). This value bounds how long deleted content can persist in cold storage, which has regulatory implications. | spec §17.1, §20 decision #5 |
| OQ-C-4 | `assessment.sweep_min_corpus_docs` and `retrieval.rate_limit.queries_per_second_per_tenant` and `cache.query_embedding.max_entries` are executor-proposed values that must be filled with evidence-backed numbers before Phase 4/5. | spec §9.3, §11.3 |
| OQ-C-5 | ADR-0005 notes config distribution is poll-based with periodic refresh or restart. The poll interval (`platform.config_distribution_poll_interval_seconds`) requires evidence from load testing to confirm it does not produce inconsistent behaviour across replicas during rolling config changes. If services require a restart after certain config changes (e.g., changing the embedding model), the list of restart-required fields must be documented here. | review-operability.md O-R16, ADR-0005 |
| OQ-C-6 | The `retrieval.supporting_tier_weight` is listed as a platform-level default, but segment-taxonomy.md §3.1 states it is configurable at KB level. The mechanism for KB-level override (via the UI/API, written back into what artifact) is not specified in the design. This must be resolved so operators know where to change it and what triggers a rebuild (if any). | segment-taxonomy.md §3.1 |
