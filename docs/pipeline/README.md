# Pipeline

Status: **Phase 1 complete** (Collect real; Assess/Decompose real for native-text PDF; Plan skeleton; Build real — recursive-char chunking, shadow-collection write, full §8 provenance, resumability).
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

### Version-check coverage (Phase 1)

All five stage boundaries are now version-checked. `ParseResultBatch` and `SegmentSetBatch` were
promoted to official §12 contracts in Phase 1 (D-26 CLOSED 2026-09-03). `DecomposeStage` checks
against `SUPPORTED_PARSE_RESULT_BATCH` and `PlanStage` checks against `SUPPORTED_SEGMENT_SET_BATCH`.
`BuildResult` remains pipeline-internal (Phase 0 skeleton). See [D-26 in the decision ledger](../process/decision-ledger.md).

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

**Extended signature (Phase 1):**

```python
run_pipeline(
    source_dir: str | pathlib.Path,
    artifacts_root: str | pathlib.Path,
    run_id: str,
    workspace_id: str,
    kb_id: str,
    run_started_at: datetime | None = None,
    embedding_provider: EmbeddingProvider | None = None,
    index_adapter: IndexAdapter | None = None,
    build_id: int = 1,
    promote: bool = False,
    db_session: Session | None = None,
) -> dict[str, str]   # stage name → absolute artifact path
```

When `embedding_provider` and `index_adapter` are both supplied, the real Build stage runs.
When omitted, Build falls back to the Phase 0 skeleton. Setting `promote=True` triggers
alias promotion after Build completes (requires `db_session`).

---

## Phase 1 stage status

| Stage | Status | Output contract | Notes |
|---|---|---|---|
| **Collect** | Real implementation | `Inventory` (§12, schema v1.0.0) | Walks `source_dir` recursively; hashes every file; builds a full `Inventory`. Exact-duplicate detection implemented. See [collect.md](collect.md). |
| **Assess** | Real — native-text PDF | `ParseResultBatch` (§12, schema v1.0.0) | Per-document `ParseResult` with per-page extraction via pypdf. Quality score heuristic. Honest exclusion for non-PDF, encrypted, image-only, and malformed PDFs. See [assess.md](assess.md). |
| **Decompose** | Real — prose segmentation | `SegmentSetBatch` (§12, schema v1.0.0) | Paragraph segmentation, heading detection, segment types, structural path breadcrumbs, salience via type priors. Frozen-artifact semantics (content-addressed cache). See [decompose.md](decompose.md). |
| **Plan** | Skeleton | `IngestionConfig` (§12, schema v1.0.0) | Emits a minimal but contract-valid `IngestionConfig`: `default_rule` only (recursive_char chunking, dense retrieval), no per-class rules. All provenance labelled `heuristic`. `config_version` derived deterministically from build-affecting fields (§10.5). Phase 1+ replaces with real planning. |
| **Build** | Real — recursive-char chunking | `BuildResult` (pipeline-internal envelope) | Recursive character splitting at 512 tokens / 50-token overlap per §9.3. Full §8 provenance carried from segments. Chunks embedded via injected `EmbeddingProvider` and written to a shadow Qdrant collection (C-4). Lifecycle validation gates promotion. Resumability via per-document checkpoint. `skeleton=None` on real runs. See [build.md](build.md). |

The `skeleton: true` field on pipeline-internal envelopes is a machine-readable honesty marker.
Downstream tooling can check this field rather than guessing whether a run produced real output.
Phase 1 real implementations set `skeleton=None`.

---

## Related pages

- [collect.md](collect.md) — Collect stage: document_id derivation, duplicate detection, field inventory
- [assess.md](assess.md) — Assess stage: pypdf extraction, quality score heuristic, honest failure modes
- [decompose.md](decompose.md) — Decompose stage: paragraph segmentation, heading detection, frozen artifacts
- [build.md](build.md) — Build stage: chunker params, provenance mapping, checkpoint/resume semantics, promote flag
- [Contracts](../contracts/) — the seven inter-stage data contracts
- [Architecture overview](../architecture/overview.md) — pipeline decomposition in the full system
- [Decision ledger D-26](../process/decision-ledger.md) — CLOSED: batch envelope contracts promoted to official §12 contracts
