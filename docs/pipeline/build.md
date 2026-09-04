# Build stage (Phase 1)

Stage 5 of the pipeline. Consumes `IngestionConfig` (from Plan); produces `BuildResult`.
Real implementation since Phase 1.

Governing spec: §6.6 (resumability), §7.1 (segment routing unit), §8 (provenance),
§9.3 (naive reference config), §10.5 (chunk identity), §12 (chunk invariants).

---

## Overview

Build iterates every included segment from the `SegmentSetBatch` artifact produced by
Decompose (loaded directly from disk as `decompose.json`), splits each segment into
overlapping chunks via the recursive-character chunker, embeds the chunks via the injected
`EmbeddingProvider`, and writes the result to a shadow Qdrant collection via `IndexAdapter`.
After all documents are processed, lifecycle validation runs; the shadow collection is
marked eligible for promotion.

The actual alias-swap promotion is a separate orchestrator call — Build only makes the
shadow *eligible*.

---

## Chunker (`finecorpus.pipeline.build.chunker`)

### Parameters

| Parameter | Default | Spec ref | Description |
|---|---|---|---|
| `max_tokens` | 512 | §9.3 naive baseline | Target chunk size. One "token" = one whitespace-delimited word (see Token counting below). |
| `overlap_tokens` | 50 | §9.3 naive baseline (range 50–100) | Number of tokens shared between consecutive chunks. 50 is the lower bound of the §9.3 range. |

These defaults are the **§9.3 fixed reference configuration** — the Phase 1 naive baseline
against which all future improvements are measured.

### Token counting

The chunker uses a **whitespace-word proxy**: `token_count = len(text.split())`.

This is an intentional honest approximation, not a BPE tokenizer:

- GPT-4 BPE typically produces 1.2–1.4 tokens per whitespace word for English prose.
- The whitespace-word count therefore *underestimates* BPE token count by roughly 20–40%.
- In practice this means chunks may be slightly larger in BPE terms than the `max_tokens`
  target, not smaller — the approximation is conservative.

This trade-off is accepted for Phase 1. Phase 3 may introduce `tiktoken` or an equivalent
real tokenizer if the evaluation sweep shows material retrieval impact. `tiktoken` is NOT
a project dependency and is NOT used here.

### Splitting algorithm

Recursive character splitting with a greedy forward scan:

1. If the full segment text fits within `max_tokens`, return it as a single chunk.
2. Otherwise, estimate the character window for `max_tokens` tokens using the density
   (chars/token) of the remaining text.
3. Find the best split point at or before that window by trying separators in order:
   `"\n\n"`, `"\n"`, `". "`, `"! "`, `"? "`, `"; "`, `", "`, `" "`.
4. If no separator is found, hard-truncate at the character boundary.
5. Record the chunk span, then step back `overlap_tokens` worth of characters to start
   the next chunk (ensuring forward progress — the cursor always advances).
6. Repeat from the new cursor position.

### Invariants

All invariants are enforced and tested:

- **Substring** (`T-04`): every chunk's text is an exact contiguous substring of its segment.
- **No span across segments**: chunker is called once per segment; output never crosses the
  segment boundary (`§7.1`).
- **Dense `chunk_index`**: values are 0, 1, 2, … with no gaps, per-segment.
- **Single chunk for tiny segments**: segments shorter than `max_tokens` always produce
  exactly one chunk (the full segment text).
- **Deterministic**: same (text, max_tokens, overlap_tokens) always produces identical
  splits; no randomness, no global state.
- **Full coverage**: every character of the segment appears in at least one chunk.

---

## Provenance mapping (§8)

Every chunk payload carries a complete provenance block inherited from its parent segment.
No field is nullable in Phase 1 (trust_level is always `untrusted_ingested`).

| Chunk provenance field | Source |
|---|---|
| `source_document_id` | `SegmentSet.document_id` |
| `source_document_version` | `SegmentSet.content_hash` |
| `source_location` | `Segment.location` (segment-level; chunk char offsets stored separately in `chunk_char_start`/`chunk_char_end` payload fields for explain mode) |
| `structural_path` | `Segment.structural_path` |
| `transformations` | `[]` — empty in Phase 1 (no Tier 1/2/3 applied; Tier 2 augmentation arrives in Phase 3) |
| `confidence` | `Segment.ocr_confidence` if present, else `1.0` (native text) |
| `ocr_confidence` | `Segment.ocr_confidence` |
| `segment_type` | `Segment.segment_type.value` |
| `salience_tier` | `Segment.salience_tier.value` |
| `salience_basis` | `Segment.salience_basis.value` |
| `salience_signals` | `Segment.salience_signals` (kind, implied_tier, won, detail per signal) |
| `language` | `Segment.language` |
| `injection_suspicion` | `Segment.injection_suspicion` |
| `invisible_content_flags` | `Segment.invisible_content_flags` |
| `sensitivity_flags` | `Segment.sensitivity_flags` |
| `trust_level` | Always `untrusted_ingested` (§14.1) |

Tenancy (`workspace_id`, `kb_id`, `permission_mode`, etc.) is inherited from
`SegmentSet.tenancy`.

---

## Chunk identity (§10.5)

Every chunk has a deterministic `chunk_id` and `point_id` derived from:

```
derive_chunk_id(
    document_id=...,      # from SegmentSet
    content_hash=...,     # from SegmentSet (document content hash)
    config_version=...,   # from IngestionConfig
    segment_path=...,     # from Segment
    chunk_index=...,      # 0-based position within segment
)
```

Same inputs → same IDs on every run. This makes re-upserts idempotent: if a document
is re-processed (e.g. because the checkpoint was lost), the Qdrant upsert overwrites
the same point ID with identical content.

---

## Shadow collection (C-4)

Build NEVER writes to a live (alias-backed) collection. It creates a dedicated shadow
collection via `lifecycle.create_shadow(...)`, writes all chunks there, then runs
`lifecycle.validate_shadow(...)` to check chunk count bounds. The shadow is then eligible
for promotion.

Promotion — the alias swap from shadow to live — is a separate call to `lifecycle.promote()`
triggered by `run_pipeline(..., promote=True)`. This separation means a failed promotion
never corrupts the live collection.

---

## Resumability and checkpointing (§6.6)

Build checkpoints progress per document in `<artifacts_root>/<run_id>/build_checkpoint.json`:

```json
{
  "documents_completed": ["doc-id-1", "doc-id-2", ...]
}
```

On each run:
1. The checkpoint is loaded at the start of `_produce_real()`.
2. Documents already in `documents_completed` are skipped without re-embedding.
3. After each document is processed successfully, its ID is appended and the checkpoint
   is saved immediately (atomic on POSIX via `write_text`).

On re-run with the same `run_id`:
- Completed documents are skipped entirely (no embedding API calls).
- Because `chunk_id` / `point_id` are deterministic, a document that was partially written
  on the previous run can also be safely re-processed — the Qdrant upsert is idempotent.
  The checkpoint skips even this redundant work when possible.

The resumability test (`TestResumability.test_kill_and_resume`) verifies this end-to-end:
it runs Build over 5 documents with a provider that raises `ProviderUnavailableError` on
document 4, confirms the checkpoint records docs 1–3, then re-runs without the failure
and asserts all 5 documents complete with no duplicate Qdrant points.

---

## Skipped / excluded segments and documents

- **Excluded-tier segments**: segments with `salience_tier = excluded` are never chunked.
  They are recorded in a `skipped_segments` list inside `_process_segment_set()` for
  observability but never silently dropped (§1.4 principle: nothing destroyed).
- **Excluded documents**: if ALL segments in a document are excluded (or the document
  has no text segments), the document is recorded in `BuildResult.skipped_documents`
  with an explanatory `reason` field (`all_segments_excluded` or `no_text_segments`).

---

## Promote flag

```python
run_pipeline(
    ...,
    embedding_provider=provider,
    index_adapter=adapter,
    build_id=1,
    promote=True,
    db_session=session,
)
```

When `promote=True`:
1. After Build succeeds, the orchestrator reads `BuildResult.shadow_collection`.
2. It calls `lifecycle.promote(adapter, session, ctx, ...)` to atomically swap the alias.
3. The alias is created (if new) or updated in the control-plane metadata store.
4. `db_session` must be supplied; `RuntimeError` is raised if it is None.

When `promote=False` (default), the shadow collection is left in place and the alias is
not updated. A subsequent call with `promote=True` and the same artifacts can promote later.

---

## BuildResult schema

```json
{
  "schema_version": "1.0.0",
  "contract": "build_result",
  "skeleton": null,
  "chunk_count": 1234,
  "chunks_by_document": {"doc-id-1": 42, "doc-id-2": 17},
  "skipped_documents": [{"document_id": "doc-id-3", "reason": "all_segments_excluded"}],
  "token_accounting": {
    "total_input_tokens": 56789,
    "total_embed_calls": 13
  },
  "shadow_collection": "kb-xyz__build-1__sha1abc",
  "validation_passed": true,
  "promoted": false,
  "report": "Build complete: 1234 chunks from 2 documents. ...",
  "built_at": "2026-09-03T12:34:56.789012+00:00",
  "chunks": []
}
```

`skeleton: null` means a real Phase 1 run. `chunks` is always empty — chunks live in
Qdrant, not inline in the artifact. `token_accounting` is the §16 cost-accrual seed
(whitespace-word proxy token counts, not billable BPE tokens).

---

## Cost accrual seed (§16)

`BuildResult.token_accounting` accumulates:

- `total_input_tokens`: sum of `EmbedBatchResult.input_tokens_used` across all embed calls
  (returned by the provider; FakeProvider returns `len(texts)` as a proxy).
- `total_embed_calls`: number of `embed_batch()` invocations.

This is a seed only — cost attribution to a workspace/KB is a Phase 4+ concern. The
whitespace-word count used here is not a billable token count for any current model.

---

## Layer contract

`finecorpus.pipeline` sits **above** `finecorpus.index` in the import layer graph
(C-5 layers contract in `pyproject.toml`). This reflects the actual dependency direction:
the pipeline orchestrates index writes; the index layer is a pure infrastructure adapter.

```
finecorpus.cli | finecorpus.services
       ↓
finecorpus.pipeline          ← imports from index (allowed)
       ↓
finecorpus.config | finecorpus.index
       ↓
finecorpus.control | finecorpus.contracts
```

---

## Related pages

- [chunker source](../../src/finecorpus/pipeline/build/chunker.py)
- [stage source](../../src/finecorpus/pipeline/build/stage.py)
- [unit tests — chunker](../../tests/pipeline/test_build_chunker.py)
- [unit + integration tests — stage](../../tests/pipeline/test_build_stage.py)
- [Index lifecycle](../architecture/index-lifecycle.md)
- [Contracts](../contracts/)
