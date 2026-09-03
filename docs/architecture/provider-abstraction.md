# Provider Abstraction

**Scope:** embedding provider interface, internal LLM operations interface, model identity pinning, query embedding cache, secrets handling, and the OpenAI/Ollama reference pair.

**Spec references:** §4.4, §4.5, §4.6, §7.3, §7.6, §9.4, §10.5, §11.3, §14.1, §14.2, §15, §16, §18.2 (provider parity), §18.3 tests 7 and 10.

---

## 1. Design principles

The platform never silently degrades. Where a provider cannot satisfy a declared capability, the platform surfaces the limitation before ingestion begins. §4.4 states this explicitly: "a backend that cannot support a capability MUST declare that explicitly and the platform MUST surface the limitation to the user rather than silently degrading." This principle applies equally to embedding providers and internal LLM providers.

Provider selection is a configuration concern, not a code-path concern. Cloud and local providers are first-class peers. Air-gapped operation (no egress, Ollama only) is a fully supported configuration and receives the same test coverage as cloud-hosted operation.

---

## 2. Embedding provider interface

### 2.1 Capability declaration

Every embedding provider declares its capabilities at registration time. The platform reads these at startup and enforces them before any ingestion job begins. Capabilities are not runtime-discovered — they are declared by the provider adapter and pinned in the provider registry.

| Field | Type | Description |
|---|---|---|
| `provider_id` | string | Stable identifier for the provider (`openai`, `ollama`, etc.) |
| `model_id` | string | Exact model identifier as the provider accepts it (`text-embedding-3-small`, `nomic-embed-text`, etc.) |
| `vector_dimensions` | int | Output embedding dimension. Pinned; must not vary across calls. |
| `max_input_tokens` | int | Maximum tokens per single input string. The platform truncates to this boundary rather than silently losing tail content — see §6, open question OQ-P-1. |
| `max_batch_size` | int | Maximum number of texts in one `embed_batch` call. |
| `supported_languages` | list[str] \| `"*"` | BCP-47 language codes the model covers adequately, or `"*"` if the provider claims universal coverage. Used in §7.6 language-capability checks. |
| `cross_lingual` | bool | Whether the model supports cross-lingual retrieval (query in language A, retrieve language B content). Reported to the user; not added by the platform. |
| `is_local` | bool | True if the model runs locally with no egress. Used to enforce air-gapped operation mode. |
| `cost_per_1k_tokens` | decimal \| null | Used by cost-estimation hooks (§16). Null for local providers. Provider adapter MUST keep this current; stale values are a defect. |
| `api_version` | string | Opaque version string recorded in index metadata for drift diagnosis. |

The capability declaration is a plain data structure, not an interface method that executes. It is populated when the adapter is registered and read by the platform; it does not make network calls.

### 2.2 Operations

#### `embed_batch(texts: list[str], model_id: str) -> EmbedBatchResult`

The only embedding operation the platform calls. All embedding, whether during ingestion or query time, goes through this method.

**Inputs:**

| Field | Constraint |
|---|---|
| `texts` | Non-empty list. Each element is a UTF-8 string. Length of each element must not exceed `max_input_tokens` as verified by the caller before dispatch. |
| `model_id` | Must match the provider's declared `model_id`. The adapter MUST reject a call with a mismatched model ID rather than silently using a default. |

**Output — `EmbedBatchResult`:**

| Field | Description |
|---|---|
| `embeddings` | List of float vectors, one per input text, in the same order. Each vector has exactly `vector_dimensions` elements. |
| `model_id` | The model ID as confirmed by the provider response. Compared against the declared model ID; mismatch is logged as a warning and surfaced in observability. |
| `input_tokens_used` | Total tokens consumed. Accumulated by the cost-tracking subsystem (§16). |
| `provider_id` | Echoed from the provider. Used to confirm routing correctness. |

**Semantics:**

- The call is synchronous from the caller's perspective. Internal batching and concurrency management are the adapter's concern.
- If the provider returns fewer embeddings than inputs, the adapter MUST raise a `ProviderError` rather than returning a partial result. Partial embedding batches cannot be safely associated with their source texts.
- If the provider is unavailable, the adapter MUST raise a `ProviderUnavailableError`. The ingestion layer catches this and follows the §15 pause-and-retry-with-backoff behaviour. The retrieval layer catches this and fails closed with a distinguishable error code.

#### `health_check() -> HealthCheckResult`

Called at startup (preflight) and periodically by the embedding service. Does not embed any document content — uses a fixed, constant probe string that carries no information.

**Output — `HealthCheckResult`:**

| Field | Description |
|---|---|
| `reachable` | bool — whether the provider endpoint responded. |
| `model_available` | bool — whether the declared model ID is available at the provider. |
| `latency_ms` | Round-trip latency of the probe call. Surfaced in the preflight report. |
| `declared_dimensions_confirmed` | bool — whether a probe embedding's dimension matches the declared `vector_dimensions`. A mismatch here is a fatal configuration error and MUST block startup. |
| `error` | Optional human-readable error string. MUST NOT include any credential material. |

The preflight check (§4.6) runs this for every configured provider and reports results before ingestion begins. Air-gapped mode: if `is_local` is false and `RTFC_AIRGAP=true` is set, `health_check` fails before making any network call.

#### `estimate_cost(texts: list[str]) -> CostEstimate`

Called before ingestion, reindex, or configuration sweep. No network call is made. The adapter uses the declared `cost_per_1k_tokens` and counts tokens using the model's declared tokenizer (or a conservative approximation where the tokenizer is not public).

**Output — `CostEstimate`:**

| Field | Description |
|---|---|
| `estimated_tokens` | Total tokens if the input list were embedded. |
| `estimated_cost_usd` | Estimated cost in USD. `0.0` for local providers. |
| `basis` | Human-readable statement of the estimation basis and its uncertainty (e.g., "tiktoken approximation, ±5%"). |
| `is_exact` | bool — false whenever the estimate uses an approximation. |

This is a pure function. It MUST NOT use network calls and MUST NOT embed any text.

### 2.3 Provider surface area

The platform's embedding service wraps the provider interface and adds:

- **Batching.** Concurrent `embed_batch` calls from ingestion workers are coalesced into provider-optimal batch sizes before dispatch.
- **Rate-limit handling.** `HTTP 429` or equivalent provider signals trigger backoff using the §15 behaviour: surface remaining budget, do not fail the job.
- **Cost accumulation.** Every `embed_batch` response's `input_tokens_used` is posted to the cost tracker attributed to the active knowledge base and operation type.
- **Health monitoring.** The health check runs on a configurable interval. Provider unhealthy → ingestion pauses and retrieval fails closed per §15.

---

## 3. Model identity pinning

This section governs §18.3 test 7: a query against an index built with a different embedding model MUST fail closed.

### 3.1 What is pinned

At the moment a shadow collection is created, the following fields are written into the collection's metadata record in the control-plane database and into Qdrant's collection payload schema comment. They are immutable for the life of the collection.

| Field | Source |
|---|---|
| `embedding_provider_id` | Provider's declared `provider_id` |
| `embedding_model_id` | Provider's declared `model_id` |
| `embedding_vector_dimensions` | Provider's declared `vector_dimensions` |
| `embedding_api_version` | Provider's declared `api_version` |
| `config_version` | Hash of the ingestion config (see §10.5 and docs/contracts/chunk.md) |

These five fields together constitute the **model identity** of the collection. They are also written into the alias metadata record so that the retrieval service can read them without querying collection internals.

### 3.2 Where model identity lives

Two authoritative locations, kept in sync:

1. **Control-plane database** — the `collections` table has a `model_identity` JSONB column. Written at shadow-collection creation, read by the retrieval service at startup and on alias change events.
2. **Alias metadata** — the alias record in the control-plane database carries a denormalized copy of the model identity of its current target collection. Updated atomically as part of every alias swap.

Qdrant's own collection metadata is not used as the authority because it is not readable through the platform's control path and does not survive snapshot/restore without a replay step.

### 3.3 The mismatch check

At query time, the retrieval service:

1. Resolves the alias to its current collection and reads the collection's `embedding_model_id` and `embedding_vector_dimensions` from the alias metadata (cached in memory; invalidated on alias-change events).
2. Reads the query-embedding request's `model_id` — which is always the model declared by the currently active embedding provider for this knowledge base.
3. Compares. If either `model_id` or `vector_dimensions` differs, the retrieval service **fails closed**: returns `HTTP 409 Conflict` with error code `EMBEDDING_MODEL_MISMATCH` and a payload that names the index model and the query model. No vector search is performed. No fallback to a different model is attempted.

The check happens before the query embedding is dispatched to the embedding service, so no provider call is wasted.

This invariant is why §15 specifies "MUST NOT silently fall back to a different model — mismatched vector spaces return plausible garbage."

### 3.4 When mismatch is expected

During an alias swap (new model → new collection), queries in flight against the current alias still see the old model identity. The swap is atomic from the alias-resolution perspective: before the swap, model identity is the old collection's; after, it is the new collection's. There is no window where the alias points at a collection whose model identity does not match. This is a consequence of the shadow-build protocol (§10, C-4).

After a swap, the retrieval service's cached model identity for the alias is invalidated by the alias-change event before the swap confirmation is returned to the caller.

---

## 4. Internal LLM operations interface

### 4.1 Purpose

The platform makes its own LLM calls for four operations: classification, augmentation (including table description and parent-breadcrumb blurb generation), question generation (for eval sets), and Tier 3 rewriting. These are internal infrastructure calls, not product features exposed to callers.

Per §7.3: "provider, model, temperature, and per-operation overrides" are all configurable. Local models MUST be supported for all four operations.

Per §14.1: document content is data, never instruction. Outputs are schema-validated, not free-form trusted.

### 4.2 Configuration

Internal LLM configuration lives in the platform config file under the `internal_llm` key. It has a default provider/model/temperature and per-operation overrides. Temperature appears nowhere else in the product (§7.3).

```
internal_llm:
  default:
    provider: ollama          # or openai, or any registered internal-llm provider
    model: llama3.1           # exact model ID
    temperature: 0.2          # 0.0–1.0; appears here only
  operations:
    classification:
      provider: ollama
      model: llama3.1
      temperature: 0.0        # classification benefits from determinism
    augmentation:
      provider: openai
      model: gpt-4o-mini
      temperature: 0.3
    question_generation:
      provider: openai
      model: gpt-4o-mini
      temperature: 0.7
    rewriting:
      provider: ollama
      model: llama3.1
      temperature: 0.2
```

Any operation not listed in `operations` inherits the `default` block. Local providers MUST be selectable for every operation, including rewriting, because a team may accept cloud embedding but not cloud rewriting of sensitive content (§7.3).

### 4.3 Operations

Each operation has a fixed input schema and a fixed output schema. Outputs are validated against the schema before use. A response that does not conform to the output schema is treated as a provider error; the operation is retried up to the configured limit and then the pipeline records the failure per §15 (single-document failure = record and continue; class-wide failure = halt and alert).

#### Classification

**Purpose:** assign a segment type (prose, table, code, figure caption, etc.) and salience tier to a segment.

**Input schema:**
- `segment_text`: string — the raw text of the segment. Passed as data in a structured prompt that frames it unambiguously as content to be described, not as an instruction.
- `document_context`: object — structural path, surrounding heading context, document type. No credential material; no platform configuration.
- `class_description`: string | null — user-supplied class description for this content class (§6.5), if available.

**Output schema:**
- `segment_type`: enum — one of the declared segment types (see docs/architecture/segment-taxonomy.md for the definitive taxonomy).
- `salience_tier`: enum — `primary` | `supporting` | `boilerplate` | `excluded`.
- `confidence`: float 0–1.
- `reasoning`: string — one sentence, retained in provenance and surfaced in explain mode.

**Prompt discipline:** the prompt wraps `segment_text` in a delimiter that is declared in the prompt structure (e.g., `<document_content>...</document_content>`). The system message explicitly states that content between the delimiters is untrusted document text and MUST be described, not executed.

**Output validation:** the output is parsed as JSON against the schema above. If any enum value is outside the declared set, or any field is missing, it is a schema validation failure.

#### Augmentation

**Purpose:** generate contextual fields stored alongside chunks: table natural-language descriptions, parent-breadcrumb summary blurbs, and class context summaries. These go into separate fields; the chunk text itself is not modified (Tier 2 behaviour, §7.2).

**Input schema:**
- `content`: string — the verbatim segment text (for tables: the structured table text).
- `structural_path`: list[string] — ordered heading breadcrumb.
- `class_description`: string | null.
- `content_type`: enum — `table` | `prose_paragraph` | `list` | `code` | other segment types.

**Output schema:**
- `natural_language_description`: string | null — for tables: a description of the table's subject, columns, and key findings. For other types: null.
- `breadcrumb_blurb`: string | null — a sentence contextualising this chunk within the heading structure, for use in augmented embedding.
- `class_context_blurb`: string | null — derived from the class description; placed in the embedding input but not returned to callers.

**Key constraint:** none of the output fields replace or modify `content`. The chunk contract (docs/contracts/chunk.md) stores them in separate named fields.

#### Question generation

**Purpose:** produce candidate retrieval questions for eval sets (§9.1). Output is labelled `provisional` and carries `generation_method: "llm_generated"` so it cannot lose its provisional status.

**Input schema:**
- `segments`: list of objects, each with `segment_text`, `segment_type`, `structural_path`, `source_document_id`.
- `class_description`: string | null.
- `question_types`: list[enum] — `factual_lookup` | `interpretive` | `multi_document_synthesis`.
- `count_per_type`: int.

**Output schema:**
- `questions`: list of objects, each with `question_text` (string), `question_type` (enum), `source_segment_ids` (list[string]), `generation_method` (always `"llm_generated"`), `review_status` (always `"provisional"` on generation).

The `review_status` field MUST NOT be settable to `"reviewed"` by the LLM output. It can only be promoted to `"reviewed"` by an authenticated human action via the review interface.

#### Rewriting (Tier 3)

**Purpose:** rewrite a segment into a more retrievable form when the original is genuinely unusable as-is. Requires explicit per-class opt-in; never global (§7.2).

**Input schema:**
- `original_text`: string — verbatim original. Always retained, never overwritten.
- `rewrite_instructions`: string — describes the target form. This is platform-generated text, not user-supplied free text that could steer the model.
- `class_description`: string | null.

**Output schema:**
- `rewritten_text`: string — the rewritten form.
- `diff_summary`: string — a one-paragraph human-readable description of what changed.

The original text is retained unconditionally. The rewritten text is stored in a separate field flagged as `transformation_tier: 3` in the chunk's transformation list. The diff preview (§7.4) is derived from comparing `original_text` and `rewritten_text` before the chunk is committed.

### 4.4 Content-as-data enforcement

All four operations follow the same prompt structure:

1. A system message that defines the task, the output schema, and explicitly states that document content is untrusted input that MUST be described/analysed/paraphrased but MUST NOT be executed.
2. A user message that places document content inside a named XML-style delimiter whose name is declared in the system message.
3. A response-format constraint (JSON schema enforcement where the provider supports it; regex-matched JSON parsing where it does not).

The output is validated against the declared schema before any downstream code touches it. A response that passes schema validation but contains content that looks like a prompt injection (imperative model-directed language, role markers) in the `reasoning` or `diff_summary` fields is logged as a security observation but is not used to steer the pipeline — those fields are stored for provenance and not re-fed to any model.

---

## 5. Query embedding cache

### 5.1 Purpose

Agent workloads repeat identical queries far more than human workloads. The cache sits in the embedding service and is shared across retrieval service replicas (backed by Redis or equivalent shared store). It is query-path only; ingestion embeddings are not cached.

### 5.2 Cache key

The cache key is a tuple serialised to a canonical string:

```
(model_id, vector_dimensions, api_version, normalize(query_text))
```

Where:
- `model_id` is the provider's declared model identifier.
- `vector_dimensions` is the provider's declared output dimension — a redundant safety check; if the provider ever returns a different dimension, the cached entry's key will not match.
- `api_version` is the provider's declared API version. When a provider silently updates a model behind the same model ID (a provider behaviour that has happened in practice), the API version change invalidates cached entries.
- `normalize(query_text)` is the query string after Unicode normalization (NFC), whitespace collapse, and lowercasing. This is the only normalization applied; no semantic transformation.

The full tuple is hashed with SHA-256. The hash is the cache key. The raw tuple components are NOT stored in the cache entry; only the hash and the embedding vector.

### 5.3 Cache entry

| Field | Description |
|---|---|
| `embedding` | The float vector. |
| `model_id` | Stored for observability and cache-warming diagnostics. |
| `created_at` | Timestamp. |
| `hit_count` | Incremented on each cache hit. Used to surface hot queries in the dashboard. |

TTL is configurable (default: 1 hour). Cache entries are not invalidated when the alias swaps to a new collection — the key includes `model_id` and `api_version`, so a model change produces a different key automatically. An alias swap that retargets a collection with the same model identity will correctly reuse cached embeddings.

### 5.4 Invalidation

Cache invalidation is key-space eviction, not explicit invalidation:

- **Model change:** new model ID or API version → new key → natural miss → no explicit action needed.
- **TTL expiry:** entries expire after the configured TTL.
- **Cache flush:** a `corpus cache flush --kb <id>` command flushes all entries for a knowledge base's current model. Used after diagnosing a provider-side model update.

There is no mechanism to invalidate a specific query's cache entry. If a query returns a stale embedding (e.g., after a provider-side silent model update), the operator flushes the cache and updates the `api_version` declaration in the provider config.

---

## 6. Secrets handling

### 6.1 Storage

Provider credentials (OpenAI API keys, Ollama endpoint URLs with auth tokens if applicable) are stored encrypted at rest in the control-plane database using AES-256-GCM. The encryption key is stored separately from the database (environment variable or a secrets manager; never in the config file or version control).

The control-plane config file contains only the reference to the secret by name or environment variable name, never the secret value itself. Config exports (§14.2) are secret-free by construction.

### 6.2 Log and trace exclusion

The platform's logging and tracing infrastructure MUST NOT record:

- API key values or any substring of them.
- Ollama endpoint credentials.
- Any HTTP `Authorization` header value.
- Any string matching the pattern of a known provider key format.

The embedding service adapter MUST strip the `Authorization` header before passing request metadata to the tracing layer. Error messages from provider calls MUST be sanitised: HTTP response bodies that may echo back credentials (e.g., in 401 responses) are logged only as `[REDACTED]` plus the HTTP status code.

§18.3 test 10 asserts that no credential appears in any log, trace, error, or config export. This is the acceptance criterion for the above.

### 6.3 Config exports

The ingestion config export (§6.4, §14.2) is secret-free by construction. The export pipeline MUST:

1. Never include fields that reference encrypted credential records.
2. Replace any provider configuration that includes an endpoint URL with a placeholder comment indicating that a credential must be configured at import time.
3. Include a machine-readable `requires_credentials` field listing the provider IDs that need credentials before the config can be activated.

### 6.4 Service principal keys

Service principal keys (§14.2) are scoped to a single knowledge base. They are:
- Generated with 256 bits of entropy, encoded as URL-safe base64.
- Stored as a salted bcrypt hash; the plaintext is returned once at creation and never again.
- Revocable and rotatable independently of any other key.
- Expiring (operator-configurable; see open question OQ-P-2).
- `last_used_at` timestamp is updated on each use.

---

## 7. Reference pair: OpenAI and Ollama

### 7.1 OpenAI embedding provider

**Reference model family:** `text-embedding-3-small` (1536 dimensions) and `text-embedding-3-large` (3072 dimensions). Both are first-class; the adapter supports both.

**Default for first-run:** `text-embedding-3-small`. Cost-per-1k-tokens is populated from the current OpenAI pricing page at adapter registration time; the adapter maintainer is responsible for keeping it current.

**Capability declaration:**
- `supported_languages`: `"*"` (OpenAI's models cover a broad range; the platform conservatively reports this as `"*"` with the caveat that low-resource languages may produce lower-quality embeddings).
- `cross_lingual`: `true` (text-embedding-3 supports cross-lingual retrieval).
- `is_local`: `false`.

**Batch limit:** 2048 texts per call (OpenAI API limit). The embedding service coalesces ingestion worker requests into batches up to this limit.

**Rate limiting:** the adapter surfaces `retry-after` headers from OpenAI 429 responses. The embedding service pauses ingestion and surfaces the remaining budget per §15 and §16.

### 7.2 Ollama embedding provider

**Reference models:** `nomic-embed-text` (768 dimensions) and `bge-m3` (1024 dimensions). Both adapters are written and tested; the operator selects one per knowledge base.

**Capability declarations:**
- `nomic-embed-text`: `supported_languages` is English-primary (the platform warns on non-English corpora); `cross_lingual`: `false`; `is_local`: `true`; `cost_per_1k_tokens`: `null`.
- `bge-m3`: `supported_languages`: `"*"` (BGE-M3 is multilingual with 100+ languages); `cross_lingual`: `true`; `is_local`: `true`; `cost_per_1k_tokens`: `null`.

**Batch limit:** 1 (Ollama's default embedding endpoint handles one text per request). The embedding service serialises calls to the Ollama endpoint or uses Ollama's batch endpoint where supported. See open question OQ-P-3.

**Air-gapped operation:** if `is_local: true` for all configured providers and `RTFC_AIRGAP=true`, the platform blocks all outbound HTTP calls at the embedding service level. The health check confirms reachability of the Ollama endpoint on the local network.

### 7.3 First-run prompt

On the first `docker compose up` of a clean deployment, before any ingestion job is accepted, the platform presents an interactive first-run configuration step. This satisfies §4.6's requirement that "first run prompts for a model provider and validates it before anything else; neither choice requires editing code or rebuilding an image."

The first-run prompt:

1. Asks whether the operator prefers a cloud provider (OpenAI) or a local provider (Ollama).
2. For OpenAI: asks for the API key, confirms it is stored encrypted (never written to disk in plaintext), and runs `health_check` to validate connectivity and model availability.
3. For Ollama: asks for the Ollama endpoint URL (default: `http://localhost:11434`), asks which model to use (`nomic-embed-text` or `bge-m3`), and runs `health_check`. If Ollama is not running, provides the pull command and waits for confirmation.
4. Displays the provider's capability declaration (dimensions, languages, cross-lingual support) so the operator sees what they are getting.
5. Asks for the internal LLM provider and model (can be the same provider or different). For Ollama: lists available models; for OpenAI: suggests `gpt-4o-mini`.
6. Writes the result to the platform config file and marks `first_run_complete: true`.

Neither choice makes the other unavailable. A deployment that starts with Ollama can add an OpenAI provider later via `corpus provider add`. The first-run prompt configures the default; it does not lock the deployment.

### 7.4 Provider parity obligation

Per §4.6 and §18.2 (provider parity test layer): every feature of the platform MUST work under both OpenAI and Ollama providers. No feature may pass integration tests under one provider and fail under the other.

The full golden-corpus integration test suite runs against both providers in CI. A test failure under one provider but not the other is a defect with the same severity as a functional bug.

Parity testing explicitly covers:
- `embed_batch` correctness (vectors are the correct dimension and non-zero).
- `health_check` results.
- `estimate_cost` (OpenAI: non-zero estimate; Ollama: zero cost, non-zero token count).
- End-to-end ingestion and retrieval quality (retrieval quality targets are provider-relative, not cross-provider comparative, because vector spaces are not comparable).
- §15 failure behaviours: provider unavailable mid-ingestion, provider unavailable at query time.
- §18.3 test 7 (embedding model mismatch) exercised with both provider combinations.

---

## Open questions

| ID | Question | Proposed default | Flagged |
|---|---|---|---|
| OQ-P-1 | How should the platform handle a segment whose token count exceeds `max_input_tokens`? Options: (a) hard truncate at the boundary, recording truncation in provenance; (b) split the over-length segment into sub-segments before embedding and aggregate the embeddings; (c) reject the segment as unservable. Truncation is lossy; aggregation requires a choice of aggregation method. | Truncate at boundary, record in provenance, surface as a warning in the findings report. Sub-segment embedding is complex and the segment should not have been that long in the first place — a chunking strategy bug. | YES |
| OQ-P-2 | What is the default expiry for service principal keys? The spec says "expiring" but leaves the window to the executor. | 90 days, with a 7-day warning before expiry. Rotatable at any time. | YES |
| OQ-P-3 | Ollama's embedding endpoint accepts one text per request in its base form. Some Ollama versions support batch embedding. Does the adapter target the lowest common denominator (single-text) or use batch where available with a version check? Batch throughput is substantially better for large ingestion jobs. | Default to single-text calls to maximise compatibility; add opt-in batch mode with a minimum Ollama version requirement documented in the configuration reference. | YES |
| OQ-P-4 | `api_version` for Ollama models is not natively surfaced by the Ollama API. A model pulled at different times may be a different underlying checkpoint. How is `api_version` derived for Ollama? | Use the model's sha256 digest as returned by `ollama show --json <model>`. Store it as `api_version`. Pull time is also recorded. Operators who update a model must update the provider registration to trigger cache invalidation. | YES |
| OQ-P-5 | The cost estimation hook for OpenAI uses declared `cost_per_1k_tokens`. Who is responsible for keeping this accurate as OpenAI changes pricing? Stale estimates mislead operators. | Provider adapter ships with a `pricing_as_of` date field alongside `cost_per_1k_tokens`. The platform warns if the pricing date is more than 90 days old. A `corpus provider update-pricing` command fetches current pricing from a pinned OpenAI pricing JSON endpoint (or prompts manual update if the endpoint is unavailable). | YES |
| OQ-P-6 | The spec does not address whether the internal LLM provider and the embedding provider must be the same or can mix freely (e.g., Ollama for internal LLM + OpenAI for embedding). This is the most common mixed configuration in practice. | They are fully independent. The config supports any combination. Air-gapped mode only requires that the embedding provider is local; internal LLM can also be local independently. | YES |
