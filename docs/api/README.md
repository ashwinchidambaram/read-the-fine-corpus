# Read The Fine Corpus — Phase 1 REST API Reference

**Scope**: Phase 1 implements **dense-only retrieval** via a REST API. Hybrid
search, reranking, and per-request metadata filters are not part of Phase 1 and
do not appear as dead parameters. The Phase 1 surface is:

- `POST /v1/kb/{kb_id}/query` — dense vector query
- `GET /v1/kb/{kb_id}/status` — alias record summary
- `GET /healthz` — liveness check

**Trust statement (§14.1)**: All results from ingested content carry
`trust_level: untrusted_ingested`. Callers must treat retrieved chunks as
untrusted external content.

**Base URL**: Configured per deployment (default: `http://localhost:8001`).

---

## POST /v1/kb/{kb_id}/query

Execute a dense vector query against a promoted knowledge base.

### Path parameters

| Parameter | Type   | Description                        |
|-----------|--------|------------------------------------|
| `kb_id`   | string | Knowledge-base identifier (URL-safe) |

### Request body (JSON)

```json
{
  "query": "What is the refund policy?",
  "top_k": 10,
  "score_threshold": 0.75
}
```

| Field             | Type    | Required | Default | Constraints              | Description                                                       |
|-------------------|---------|----------|---------|--------------------------|-------------------------------------------------------------------|
| `query`           | string  | yes      | —       | 1–8192 characters        | The query text to embed and search.                               |
| `top_k`           | integer | no       | 10      | 1–100 (clamped server-side) | Maximum number of results to return.                           |
| `score_threshold` | float   | no       | null    | 0.0–1.0                  | If set, results below this cosine similarity score are removed.   |

### Response body (JSON)

The following is a real response captured from the TestClient with a seeded collection and
`score_threshold: 0.7`:

```json
{
  "schema_version": "1.1.0",
  "request_echo": {
    "query": "What is the refund policy?",
    "filters_applied": [
      {
        "expression": "tenancy.kb_id == 'kb-prod-01'",
        "origin": "tenancy"
      },
      {
        "expression": "score >= 0.7",
        "origin": "request"
      }
    ]
  },
  "result_status": "matches",
  "results": [
    {
      "chunk_id": "chk_01jadocumentchunkidexample",
      "text": "Refunds are processed within 5 business days...",
      "provenance": {
        "source_document_id": "doc_01jexampledocumentidhere",
        "source_document_version": "v1",
        "source_location": {
          "locator_kind": "char_range",
          "char_start": 0,
          "char_end": 100,
          "page_start": null,
          "page_end": null,
          "byte_start": null,
          "byte_end": null,
          "cell_range": null,
          "dom_path": null,
          "bbox": null,
          "coordinate_note": null
        },
        "structural_path": ["Section 1"],
        "transformations": [],
        "confidence": 0.95,
        "ocr_confidence": null,
        "segment_type": "prose",
        "salience_tier": "primary",
        "salience_basis": "default",
        "salience_signals": [
          {
            "kind": "default",
            "implied_tier": "supporting",
            "won": true,
            "detail": "no signal fired"
          }
        ],
        "language": "en",
        "injection_suspicion": 0.0,
        "invisible_content_flags": [],
        "sensitivity_flags": [],
        "trust_level": "untrusted_ingested"
      },
      "score": 0.91,
      "scores": {
        "raw": 0.91,
        "reranked": null
      },
      "trust_level": "untrusted_ingested"
    }
  ],
  "error": null,
  "explain": null
}
```

### RequestEcho fields

| Field             | Type                    | Description                                                                        |
|-------------------|-------------------------|------------------------------------------------------------------------------------|
| `query`           | string                  | The query text exactly as received.                                                |
| `filters_applied` | list[AppliedFilter]     | Every filter in effect with its origin — tenancy (always present), request (optional score_threshold). |

Each `AppliedFilter` has:

| Field        | Type   | Description                                                                                             |
|--------------|--------|---------------------------------------------------------------------------------------------------------|
| `expression` | string | Human-readable filter predicate (e.g. `"tenancy.kb_id == 'kb-prod-01'"`, `"score >= 0.7"`).            |
| `origin`     | enum   | `"tenancy"` — mandatory server-side clause; `"request"` — caller-supplied (score_threshold); `"kb_config"` — KB default. |

### result_status values

| Value              | Meaning                                                              | HTTP status |
|--------------------|----------------------------------------------------------------------|-------------|
| `matches`          | At least one result survived the (optional) score threshold.         | 200         |
| `no_matches`       | Vector search returned zero candidates before threshold application. | 200         |
| `filtered_to_zero` | Search returned candidates but `score_threshold` eliminated all.     | 200         |
| `error`            | Fail-closed condition — see `error.code`.                            | 4xx / 5xx   |

### Error codes

| `error.code`                | HTTP | Meaning                                                                 | Retriable |
|-----------------------------|------|-------------------------------------------------------------------------|-----------|
| `EMBEDDING_MODEL_MISMATCH`  | 409  | The configured provider model does not match the indexed model. Provider was NOT called. Correct the config and restart. | No |
| `VECTOR_DB_UNAVAILABLE`     | 503  | Qdrant is unreachable or the alias is not found.                        | Yes       |
| `PROVIDER_UNAVAILABLE`      | 503  | The embedding provider is unreachable or returned an error.             | Yes       |
| `KB_NOT_READY`              | 404  | The KB has no alias record, or the alias has no promoted collection.    | No (wait for ingest) |
| `CONTROL_PLANE_UNAVAILABLE` | 503  | The control-plane database (Postgres) is unreachable at query time.    | Yes       |
| `PAYLOAD_CORRUPT`           | 500  | A retrieved point payload is missing required provenance fields. Re-ingest the affected document. | No |
| `PERMISSION_DENIED`         | 403  | (Phase 4) Resolved principal has no access to this KB.                 | No        |
| `INVALID_QUERY`             | 422  | The request body failed schema validation (empty query, top_k out of range, etc.). | No |

### Error response shape

When `result_status == "error"`, the response body still has the standard envelope.
The following is a real error response (KB not yet registered):

```json
{
  "detail": {
    "schema_version": "1.1.0",
    "request_echo": {
      "query": "test",
      "filters_applied": [
        {
          "expression": "tenancy.kb_id == 'kb-prod-01'",
          "origin": "tenancy"
        }
      ]
    },
    "result_status": "error",
    "results": [],
    "error": {
      "code": "KB_NOT_READY",
      "message": "Knowledge base 'kb-prod-01' has no promoted collection yet. Run ingestion and promote before querying.",
      "retriable": false
    },
    "explain": null
  }
}
```

The HTTP status code for error responses is also set (409 / 503 / 404 / 422 / 500 as per
the table above). The `detail` field of the HTTP error body is the full response
envelope (deserializable as `RetrievalResponse`).

### Tenancy

The `tenancy.kb_id` filter is injected server-side on every query. Clients cannot
override or widen it. The applied filter is visible in `request_echo.filters_applied` with
`origin: "tenancy"`.

---

## GET /v1/kb/{kb_id}/status

Return a summary of the alias record for a knowledge base. Useful for checking
whether a KB is ready to receive queries.

**C-3 hygiene**: The internal `collection_name` is NOT exposed. Consumers should
use `alias`, `ready`, and model identity to determine queryability.

### Path parameters

| Parameter | Type   | Description                        |
|-----------|--------|------------------------------------|
| `kb_id`   | string | Knowledge-base identifier          |

### Response body (JSON, 200)

The following is a real response captured from the TestClient:

```json
{
  "kb_id": "kb-prod-01",
  "alias": "rtfc_kbprod01",
  "embedding_provider": "openai",
  "embedding_model": "text-embedding-3-small",
  "embedding_dimensions": 1536,
  "config_version": "cfgv-2025-01-15",
  "promoted_at": "2025-01-15T10:30:00",
  "ready": true
}
```

| Field                 | Type          | Description                                                                                           |
|-----------------------|---------------|-------------------------------------------------------------------------------------------------------|
| `kb_id`               | string        | The knowledge-base ID.                                                                                |
| `alias`               | string        | Stable alias used for all queries (e.g. `rtfc_{kb_id}`).                                             |
| `embedding_provider`  | string / null | Provider identifier (e.g. `"openai"`, `"ollama"`, `"fake"`).                                         |
| `embedding_model`     | string / null | Model identifier (e.g. `"text-embedding-3-small"`).                                                  |
| `embedding_dimensions`| int / null    | Vector dimensionality.                                                                                |
| `config_version`      | string / null | Ingestion config version hash.                                                                        |
| `promoted_at`         | string / null | ISO-8601 UTC timestamp of last promotion. Null if never promoted.                                     |
| `ready`               | bool          | `true` when a promoted collection exists. `false` if the KB exists but has never been promoted.       |

### Error responses

| Condition                  | HTTP | Body                                          |
|----------------------------|------|-----------------------------------------------|
| KB not registered          | 404  | `{"detail": "Knowledge base 'kb-prod-01' is not registered."}` |

---

## GET /healthz

Liveness probe. Returns 200 when the service process is running.

### Response body (JSON, 200)

```json
{
  "service": "retrieval-api",
  "status": "ok",
  "version": "0.0.1"
}
```

This endpoint does not check connectivity to Qdrant or Postgres. Use
`GET /v1/kb/{kb_id}/status` to verify end-to-end readiness.

---

## Capability declarations (Phase 1 scope)

| Capability          | Phase 1 status | Notes                                               |
|---------------------|---------------|-----------------------------------------------------|
| Dense retrieval     | Supported      | Single-stage cosine similarity via Qdrant alias.    |
| Hybrid search       | Not supported  | No sparse/BM25 component. Not a silent omission — this parameter does not exist. |
| Reranking           | Not supported  | No cross-encoder pass. Phase 2 planned.             |
| Explain mode        | Not supported  | Phase 3 planned.                                    |
| Per-request filters | Not supported  | Phase 4 will add workspace-scoped ACL filters.      |
| Rate limits         | Not enforced   | Phase 4 (tenant billing + quotas).                  |
| MCP server          | Not exposed    | In-process; Phase 3/4.                              |

---

## Client example

```python
import httpx

client = httpx.Client(base_url="http://localhost:8001")

response = client.post(
    "/v1/kb/kb-prod-01/query",
    json={"query": "refund policy", "top_k": 5, "score_threshold": 0.7},
)
response.raise_for_status()
data = response.json()

print(data["result_status"])  # "matches" | "no_matches" | "filtered_to_zero" | "error"
for result in data["results"]:
    print(result["score"], result["text"][:80])
    print("  trust:", result["trust_level"])  # always "untrusted_ingested" in Phase 1
    print("  doc:  ", result["provenance"]["source_document_id"])
    print("  loc:  ", result["provenance"]["source_location"]["locator_kind"])
```
