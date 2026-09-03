# Contract 7 — Retrieval response

Stage boundary: **Serve → caller.** Governing spec: §11 (retrieval), §11.5 (explain mode), §14.1
(trust label), §15 (failure semantics / zero-result distinction), §8/§12 (provenance).

This is the **Serve→caller envelope**: the shape the retrieval API (REST, MCP, Python client)
returns for a query. It exists because §15 requires the response to distinguish "no matches" from
"filtered to nothing" from "error", and no such schema existed in the contract package (C-R8). It
is versioned like the other six contracts (`schema_version`, semver; see
[README.md](README.md#contract-versioning)).

Root model: `RetrievalResponse`.

---

## `RetrievalResponse` (root)

| Field | Type | Required | Semantics |
|---|---|---|---|
| `schema_version` | `str` (semver) | yes | Contract version. |
| `request_echo` | `RequestEcho` | yes | The query as received and the filters actually applied, with each filter's origin (§11.5). |
| `result_status` | `enum{matches, no_matches, filtered_to_zero, error}` | yes | **Required.** The §15 distinction (see below). Never inferred by the caller from an empty list — it is explicit. |
| `results` | `list[RetrievalResult]` | yes (may be empty) | The returned chunks with scores and full provenance. Empty when `result_status` is `no_matches`, `filtered_to_zero`, or `error`. |
| `error` | `ErrorEnvelope` | no | Present iff `result_status = error`. Carries the error code taxonomy (§15). |
| `explain` | `ExplainBlock` | no | Present only when the query requested explain mode (§11.5). |

## `RequestEcho`

| Field | Type | Required | Semantics |
|---|---|---|---|
| `query` | `str` | yes | The query text as received. |
| `filters_applied` | `list[AppliedFilter]` | yes (may be empty) | Every filter in effect, with its origin — so the caller can see what narrowed the search (§11.5). |

## `AppliedFilter`

| Field | Type | Required | Semantics |
|---|---|---|---|
| `expression` | `str` | yes | Human-readable rendering of the filter predicate. |
| `origin` | `enum{request, kb_config, tenancy}` | yes | Where the filter came from (§11.5): a caller-supplied `request` filter, a `kb_config` default, or a mandatory `tenancy` clause. Tenancy-origin filters are the mandatory `must` clauses of the tenant-isolation mechanism (overview.md) and are always present. |

## `RetrievalResult`

| Field | Type | Required | Semantics |
|---|---|---|---|
| `chunk_id` | `str` | yes | The chunk's deterministic ID ([chunk.md](chunk.md)). |
| `text` | `str` | yes | The served chunk text (the Tier-1-normalized canonical text; [chunk.md](chunk.md)). |
| `provenance` | `Provenance` | yes | The **full** §8 provenance block ([README.md](README.md#provenance-block-provenance)), including transformation history, confidence, salience, and the salience signal trace. |
| `score` | `float` | yes | Final relevance score for this result. |
| `scores` | `Scores` | no | Component scores (raw / reranked) when available; always populated in explain mode. |
| `trust_level` | `enum{untrusted_ingested}` | yes | The §14.1 trust label — carried through from the chunk; all ingested content is `untrusted_ingested`. Surfaced so a caller/agent knows the material is untrusted. |

## `Scores`

| Field | Type | Required | Semantics |
|---|---|---|---|
| `raw` | `float` | yes | Pre-rerank retrieval score. |
| `reranked` | `float` | no | Post-rerank score when a reranker ran. |

## `ErrorEnvelope`

Present only when `result_status = error`. Errors **fail closed** (§15): the platform returns an
error rather than degrading to a different model or an unscoped search.

| Field | Type | Required | Semantics |
|---|---|---|---|
| `code` | `ErrorCode` (enum) | yes | The error taxonomy (below). |
| `message` | `str` | yes | Human-readable detail; never leaks a secret (§14.2). |
| `retriable` | `bool` | yes | Whether the caller may retry (e.g. transient vector-DB unavailability). |

`ErrorCode` includes at least:

```
enum ErrorCode {
    EMBEDDING_MODEL_MISMATCH,   # query embedded with a model != the alias's model identity — fail closed (§15, provider-abstraction §3.3)
    VECTOR_DB_UNAVAILABLE,      # Qdrant unreachable — fail closed, do not serve stale/unscoped (§15)
    PROVIDER_UNAVAILABLE,       # embedding provider down at query time — fail closed, no model fallback (§15)
    KB_NOT_READY,               # alias points at nothing yet (index-lifecycle §2.2)
    PERMISSION_DENIED,          # authenticated principal has no access to the requested scope
    INVALID_QUERY,              # malformed request
}
```

`EMBEDDING_MODEL_MISMATCH` and `VECTOR_DB_UNAVAILABLE` are called out explicitly because they are
the two §15 fail-closed paths most likely to be mishandled as "empty result"; they MUST surface as
`result_status = error` with the corresponding code, never as `no_matches`.

## `ExplainBlock` (§11.5)

Present only for explain-mode queries. Extends the response with the debugging detail §11.5
requires. It respects the **same tenancy and permission rules** as ordinary retrieval — explain is
not a bypass (§11.5); it never contains cross-tenant candidates, exclusions, or scores.

| Field | Type | Required | Semantics |
|---|---|---|---|
| `parsed_query` | `str` | yes | The parsed query and any expansion applied. |
| `candidates` | `list[ExplainCandidate]` | yes | The candidate set considered, each with raw and reranked scores. |
| `exclusions` | `list[ExplainExclusion]` | yes (may be empty) | Chunks that were excluded, each paired with the filter that removed it. |
| `strategy` | `str` | yes | The retrieval strategy used (dense/sparse/hybrid, rerank, §11.5). |

### `ExplainCandidate`

| Field | Type | Required | Semantics |
|---|---|---|---|
| `chunk_id` | `str` | yes | Candidate chunk. |
| `scores` | `Scores` | yes | Raw and reranked scores. |
| `provenance` | `Provenance` | yes | Full provenance for the candidate (§11.5 requires provenance for every candidate). |

### `ExplainExclusion`

| Field | Type | Required | Semantics |
|---|---|---|---|
| `chunk_id` | `str` | yes | The excluded chunk. |
| `removed_by` | `AppliedFilter` | yes | The specific filter (with origin) that removed this chunk — so a KB editor can tell a filter problem from a chunking problem (§11.5). |

---

## The `result_status` distinction (§15)

§15 requires the response to distinguish these outcomes; this contract makes the distinction
**explicit and required**, so the §18.2 failure-injection layer can assert against it:

| `result_status` | Meaning | `results` | `error` |
|---|---|---|---|
| `matches` | At least one chunk matched and survived all filters. | non-empty | null |
| `no_matches` | The vector search itself returned nothing before any KB/request filtering removed results — the corpus has no relevant content. | empty | null |
| `filtered_to_zero` | The search found candidates but **every** candidate was removed by a filter (salience, confidence floor, request filter, or a tenancy/permission `must`). The `request_echo.filters_applied` (and, in explain mode, `explain.exclusions`) explain *why* nothing survived. | empty | null |
| `error` | A fail-closed condition occurred (embedding-model mismatch, vector-DB or provider unavailable, etc.). | empty | present |

The difference between `no_matches` and `filtered_to_zero` is what §15 exists to make visible: the
former is "your corpus doesn't have it", the latter is "it exists but your filters/permissions hid
it". A caller can act on each differently.

---

## Invariants

- **`result_status` is required and authoritative.** The caller never infers status from an empty
  `results` list; the four-way enum is always set (§15).
- **Provenance complete on every returned result and every explain candidate** — the same §8 block
  the chunk carries ([README.md](README.md#provenance-block-provenance), §12).
- **Trust label always surfaced.** Every `RetrievalResult` carries `trust_level` (§14.1); the MCP
  tool description also states the trust level.
- **Fail closed.** `EMBEDDING_MODEL_MISMATCH` and `VECTOR_DB_UNAVAILABLE` (and the other fail-closed
  codes) surface as `result_status=error`, never as an empty success (§15).
- **No cross-tenant data, ever.** The response — including explain candidates, exclusions, and
  scores — contains only content within the authenticated principal's scope; the tenant-isolation
  `must` clauses (overview.md) bind on every path, and explain respects the same rules (§11.5).
- `filters_applied` records each filter's origin (`request`/`kb_config`/`tenancy`), so the caller
  can distinguish a self-imposed narrowing from a mandatory tenancy clause (§11.5).

## Open questions

1. **Pagination / cursor.** Whether large result sets paginate and how a cursor interacts with the
   `result_status` distinction. **Proposed default:** top-k with a `k` request parameter, no cursor
   in v1; `result_status` reflects the filtered top-k. **Flagged.**
2. **Score comparability across models.** Raw scores are not comparable across embedding models.
   **Proposed default:** scores are within-query-comparable only; do not present them as absolute
   quality. **Flagged.**
