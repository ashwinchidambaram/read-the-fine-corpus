# Pipeline

Status: **implemented** (Phase 0 — Collect stage real; Assess/Decompose/Plan/Build skeletons).
Governing spec: §5, §6, §12, §18.2.

The pipeline is a five-stage linear chain. Each stage consumes the previous stage's artifact,
validates it, produces new output, and persists it to disk before returning. No stage reads
another stage's output directly from memory — all handoffs go through the `ArtifactStore`.

## Stage abstraction

Every stage is a subclass of `finecorpus.pipeline.stage.Stage`. The base class enforces a
fixed run protocol; subclasses implement `_produce()` only.

### Class-level declarations

Each concrete stage declares:

| Attribute | Type | Meaning |
|---|---|---|
| `name` | `str` | Stage identity; used as the artifact filename stem (`<name>.json`). |
| `consumed_contract` | `str \| None` | Human-readable name of the input contract. `None` for Collect (no input). |
| `consumed_version_range` | `SpecRange \| None` | The contract versions this stage accepts. `None` opts out of version checking (used when the input is a pipeline-internal envelope, not an official §12 contract). |
| `produced_contract` | `str` | Human-readable name of the output contract. |
| `output_model` | `type[BaseModel]` | Pydantic model used to validate `_produce()`'s return value before persistence. |

### `run()` protocol

`Stage.run(input_data, store)` executes four steps in order:

1. **Contract version check.** If `consumed_contract` and `consumed_version_range` are both
   non-`None`, the stage calls `check_version()` on `input_data["schema_version"]`. A version
   outside the declared `SpecRange` raises `ContractVersionError` immediately — the stage
   never processes an input contract it was not built for (§18.2).

2. **Produce.** The stage calls `_produce(input_data)` to build the output dict.
   Implementations MUST NOT persist anything inside `_produce()`; persistence is the base
   class's responsibility.

3. **Output validation.** The base class calls `output_model.model_validate(output_dict)`. A
   stage that cannot produce a contract-valid output raises `StageError` here rather than
   writing bad data to disk.

4. **Persist and re-load.** The base class calls `store.save(name, output_dict)` then
   `store.load(name)`, and returns the loaded-back dict. The round-trip through JSON is the
   canonical form: any non-serialisable value is caught at save time, and any structural
   deviation introduced by serialization is detected at re-load.

Stages that override `run()` directly (Collect does, because it has no input contract) must
reproduce steps 3 and 4.

### Version-check opt-out

Stages that consume a pipeline-internal batch envelope (`ParseResultBatch`, `SegmentSetBatch`)
set `consumed_version_range = None`. They still receive a well-formed dict from `ArtifactStore`
— the store's `load()` requires `schema_version` to be present and the JSON to be valid — but
version compatibility checking is skipped. This is documented as an open Phase 1 decision
(see [D-26](../process/decision-ledger.md)).

---

## ArtifactStore

`finecorpus.pipeline.artifact_store.ArtifactStore` persists each stage's output as a JSON
file under:

```
<artifacts_root>/<run_id>/<stage_name>.json
```

The run directory is created on construction (`parents=True, exist_ok=True`).

### Key behaviours

- **`save(stage_name, data)`** — serializes `data` with `json.dumps(indent=2, default=str,
  ensure_ascii=False)` and writes it atomically to `<run_dir>/<stage_name>.json`.
- **`load(stage_name)`** — reads the file, parses JSON, and validates that the top level is a
  dict containing a `schema_version` key. Missing `schema_version` raises `ArtifactStoreError`
  (§18.2: malformed input rejected loudly). Full contract-version checking is the consuming
  stage's responsibility.
- **`exists(stage_name)`** — returns whether a stage's artifact file is present.
- **`load_with_model_validation(stage_name, model_class)`** — combines `load()` with a full
  pydantic model validation, raising `ArtifactStoreError` on failure.

### run_id discipline

`run_id` must be supplied by the caller. `ArtifactStore` raises `ArtifactStoreError` if
`run_id` is an empty string. No wall-clock suffix, no random component, and no default are
generated inside the store. This is a deliberate determinism constraint: the same logical
inputs with the same `run_id` produce the same artifact paths, making runs reproducible and
diffable.

---

## Orchestrator

`finecorpus.pipeline.orchestrator.run_pipeline()` chains all five stages end-to-end:

```
Collect → Assess → Decompose → Plan → Build
```

**Signature:**

```python
run_pipeline(
    source_dir: str | pathlib.Path,
    artifacts_root: str | pathlib.Path,
    run_id: str,
    workspace_id: str,
    kb_id: str,
) -> dict[str, str]   # stage name → absolute artifact path
```

The orchestrator fixes `collected_at = datetime.now(tz=UTC)` once at the start of the run and
passes it to `CollectStage`. This ensures the Inventory's timestamp is deterministic for the
same logical inputs within a run, even if processing spans midnight.

Each stage receives the previous stage's return value as `input_data`. Failures propagate
immediately — the orchestrator does not catch or swallow stage errors.

---

## Phase 0 stage status

| Stage | Status | Output contract | Notes |
|---|---|---|---|
| **Collect** | Real implementation | `Inventory` (§12, schema v1.0.0) | Walks `source_dir` recursively; hashes every file; builds a full `Inventory`. Exact-duplicate detection implemented. See [collect.md](collect.md). |
| **Assess** | Skeleton | `ParseResultBatch` (pipeline-internal envelope) | Emits one `ParseResult` per inventory item, all with `parse_status=excluded_pre_parse`. `skeleton=true` field on the envelope. Phase 1 replaces with real parsing. |
| **Decompose** | Skeleton | `SegmentSetBatch` (pipeline-internal envelope) | Emits one `SegmentSet` per parse result, all with empty `segments[]`. `reassembly_digest` is `sha256("")`. `skeleton=true` on envelope. Phase 1+ replaces with real decomposition. |
| **Plan** | Skeleton | `IngestionConfig` (§12, schema v1.0.0) | Emits a minimal but contract-valid `IngestionConfig`: `default_rule` only (recursive_char chunking, dense retrieval), no per-class rules. All provenance labelled `heuristic`. `config_version` derived deterministically from build-affecting fields (§10.5). Phase 1+ replaces with real planning. |
| **Build** | Skeleton | `BuildResult` (pipeline-internal envelope) | Emits 0 chunks with an explanatory report. `skeleton=true` on envelope. Phase 1+ replaces with real chunking, embedding, and shadow-collection writing. |

The `skeleton: true` field on pipeline-internal envelopes is a machine-readable honesty marker.
Downstream tooling can check this field rather than guessing whether a run produced real output.

---

## Related pages

- [collect.md](collect.md) — Collect stage: document_id derivation, duplicate detection, field inventory
- [Contracts](../contracts/) — the six inter-stage data contracts, including `Inventory`
- [Architecture overview](../architecture/overview.md) — pipeline decomposition in the full system
- [Decision ledger D-26](../process/decision-ledger.md) — open decision on batch envelope contracts
