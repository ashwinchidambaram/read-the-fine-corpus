# Transformations, preview, and costing (Phase 3)

Phase 3 Build stage additions. Covers the tier-flow architecture in Build, dry-run preview, and the cost estimation gate.

Governing spec: §6.6, §7.2, §7.4, §7.5, §18.3 (T-04, §19 criteria 2 and 4).

---

## Tier flow in Build

For each segment, the Build stage applies transformations in tier order:

```
segment.text
  ↓ Tier 1 (if enabled)       — text-level transformations; records in TransformationRecord
  → canonical_text             — the Tier-1-normalized source; chunk.text slices come from here
  ↓ chunker                   — splits canonical_text into overlapping chunks
  → chunks[i].text             — verbatim slice of canonical_text (byte-identity invariant)
  ↓ Tier 2 (if enabled)       — augmentation fields only; chunk.text is NEVER modified
  → chunk.augmentation.*       — parent_breadcrumb, table_description, class_context
  ↓ Tier 3 (if enabled)       — rewriting; chunk.text CAN be modified; changed_text=True
  → chunk.text (rewritten)     — Phase 7 only; not implemented in Phase 3
  ↓ embedding                  — encodes chunk.embedding_input (augmented prefix + chunk.text)
  → chunk.vector               — dense embedding; stored in shadow collection
```

### Tier 1 operations

| Operation | Effect | Records changed_text |
|---|---|---|
| `whitespace_repair` | Normalises whitespace runs, removes trailing spaces | True (bytes may change) |
| `table_to_markdown` | Converts table region to Markdown pipe-table format | True |
| `ocr_cleanup` | Removes common OCR artefacts (ligature errors, stray chars) | True |

All Tier 1 ops record a `TransformationRecord(tier=1, operation=..., changed_text=True/False)` in `chunk.provenance.transformations`. The raw original is always retained via `segment.location` and the Collect/Assess artifacts — it is never overwritten (M-032).

### Tier 2 operations (augmentation only)

| Operation | Augmentation field populated | Requires LLM |
|---|---|---|
| `breadcrumb_augment` | `chunk.augmentation.parent_breadcrumb` | No (from `segment.structural_path`) |
| `table_description` | `chunk.augmentation.table_description` | Yes (LLMProvider) |
| `class_context` | `chunk.augmentation.class_context` | No (from `ClassDescription.description`) |

**T-04 invariant:** Every Tier 2 op records `TransformationRecord(tier=2, changed_text=False)`. `chunk.text` is never modified by Tier 2. The `embedding_input` field is the augmented string (prefix + chunk.text); the vector is computed from `embedding_input`, not from `chunk.text` alone. This is correct — augmentation enriches the retrieval signal without altering the source text.

### Byte-identity (§18.3 test 4 / §19 criterion 2)

For every chunk produced by the Build stage:

```python
assert chunk.text.encode() == chunk.canonical_text[chunk.char_start : chunk.char_end].encode()
```

This is a **position-exact** assertion using `char_start`/`char_end` span offsets, NOT `canonical_text.index(chunk.text)`. The `index()`-based approach masks off-by-position bugs when the same text appears at multiple positions.

Verified corpus-wide in `tests/phase3/test_t04_byte_identity.TestT04CorpusWide.test_corpus_wide_byte_identity_position_exact`.

---

## Cache keying

The Build stage uses `config_version` as part of the chunk ID derivation (M-058). The chunk ID is:

```
chk_ + base32(sha256(
    source_document_id + ":" +
    segment_path + ":" +
    str(chunk_index) + ":" +
    config_version
))
```

Changing any build-affecting field (chunking parameters, Tier 1/2 ops, embedding model) changes `config_version`, which changes all chunk IDs, ensuring the shadow collection is populated with fresh chunks and old chunks are replaced by document (M-056).

---

## Dry-run mode

`BuildStage(dry_run=True)` runs the full transformation + chunking pipeline but does not embed or write to the vector store. Instead, it returns an in-memory result dict containing all chunks with their canonical text, augmentation fields, and provenance.

Dry-run is used for:
- **Preview subcommand** (`corpus pipeline preview`): shows sample chunks before committing to a full build (M-038)
- **T-04 corpus-wide test**: runs the full pipeline without needing a live Qdrant instance
- **Cost estimation**: chunk text is available for token counting without embedding cost

`cost_estimate_basis` fields on each chunk carry the token count and estimated cost contribution for the embedding operation.

---

## Cost estimation gate (§19 criterion 4)

Before the Build stage writes any embedding to the vector store, the CLI gate requires a cost estimate to be displayed and confirmed.

### `resolve_costing_providers(config)`

Returns a `CostingResolution` with:
- `embedding_provider`: the constructed embedding provider, or `None` if unavailable
- `embedding_unavailable`: `True` when the declared provider cannot be constructed (missing API key, unreachable endpoint)
- `embedding_unavailable_reason`: human-readable explanation

### Honesty invariant (D-35 / Ruling 2)

When a declared paid provider (e.g., `openai`) is unavailable, the system **must not** substitute a `FakeProvider` and display `$0.00`. Showing `$0.00` for a declared paid provider is dishonest — the user would proceed expecting no cost and then be billed on the next configured run.

Instead:
- `resolve_costing_providers()` returns `embedding_unavailable=True, embedding_provider=None`
- `_try_load_cost_estimate()` in the CLI detects the flag and returns `CostEstimateUnavailable(unavailable=True)`
- `_print_cost_estimate()` prints: `"Cost estimate unavailable for declared provider 'openai' — set OPENAI_API_KEY to see estimated cost before proceeding."`

For local/zero-cost providers (`fake`, `ollama`), the estimate IS available with `embedding_zero_marginal_cost=True`.

### Gate flow

```
plan.json + decompose.json present?
  ↓ yes
resolve_costing_providers(plan)
  ↓
estimate available?
  ├─ yes → display estimate → prompt confirmation → proceed to Build
  └─ no  → display unavailable message → prompt confirmation (still required)
           → proceed to Build with cost unknown
```

The gate always requires confirmation before proceeding — even when the estimate is unavailable. The user cannot bypass the gate by configuring a provider without credentials.

Tested in `tests/phase3/test_honest_cost_gate.py` and `tests/phase3/test_acceptance.py::TestCostGate`.

### Cost basis fields

Each chunk in the dry-run result carries:

| Field | Description |
|---|---|
| `cost_estimate_basis.token_count` | Estimated tokens for this chunk's `embedding_input` |
| `cost_estimate_basis.provider` | Which provider will embed it |
| `cost_estimate_basis.model` | Model ID |
| `cost_estimate_basis.cost_per_1k_tokens_usd` | Provider-reported cost (0.0 for local) |

Summing `token_count * cost_per_1k_tokens_usd / 1000` across all chunks gives the total embedding cost estimate.
