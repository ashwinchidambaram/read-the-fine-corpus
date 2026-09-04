# Data Contracts

Status: **accepted** (design review complete; D-11 closed 2026-09-03). Governing spec: §12 (terms of reference), §8
(provenance invariant), §5 (pipeline), §10.5 (chunk identity).

The inter-stage data contracts are the load-bearing artifacts of Read The Fine Corpus. Each
pipeline stage (§5) produces exactly one contract and consumes the previous one; a seventh
contract (the retrieval response) is the Serve→caller envelope. These pages are the implementation
checklist for the pydantic v2 models under `finecorpus/contracts/`.

The core invariant (§8) — every chunk carries complete provenance — is upstream of every one of
these schemas. A contract that cannot carry provenance forward to the chunk is wrong.

## The contracts

| # | Contract | Stage boundary | Page |
|---|---|---|---|
| 1 | Inventory | Collect → Assess | [inventory.md](inventory.md) |
| 2 | Parse result | Assess → Decompose | [parse-result.md](parse-result.md) |
| 3 | Segment set | Decompose → Plan | [segment-set.md](segment-set.md) |
| 4 | Ingestion config | Plan → Build | [ingestion-config.md](ingestion-config.md) |
| 5 | Chunk | Build → Serve (the spine) | [chunk.md](chunk.md) |
| 6 | Eval set | ⟷ Plan, Serve | [eval-set.md](eval-set.md) |
| 7 | Retrieval response | Serve → caller | [retrieval-response.md](retrieval-response.md) |

Contracts 1–6 are the inter-stage pipeline contracts (each stage produces one and consumes the
previous). Contract 7 is the Serve→caller response envelope (§11, §15); it is versioned like the
others. Chunk identity derivation is decided in
[ADR-0006](../adr/0006-chunk-identity-scheme.md).

## Design stance

Design for **representability, not minimalism**. The golden corpus fixtures (§18.1) — a scanned
PDF with degraded regions, three kinds of spreadsheet, an adversarial injection document, a
near-duplicate family, a bloated cross-referenced manual — must every one be expressible in these
schemas. A schema that cannot represent a fixture is a defect, not a simplification. Where a field
is rarely populated, it is optional, not absent.

Every page carries a field table (name, type, required, semantics), an invariants list, and an
**Open questions** section. Open questions propose a default and flag it; per build rule 11,
nothing ambiguous is resolved silently.

---

## Batch envelope contracts (D-26, CLOSED 2026-09-03)

The batch envelope contracts wrap per-document contracts for inter-stage handoffs. Both are
official §12 contracts with `schema_version: "1.0.0"`, dedicated modules, and SpecRange version
checks on every consuming stage. D-26 (CLOSED) promoted these from pipeline-internal models.

### §12a — ParseResultBatch (`Assess → Decompose`)

Source: `src/finecorpus/contracts/parse_result_batch.py`. Wraps a list of `ParseResult` objects.

| Field | Type | Required | Semantics |
|---|---|---|---|
| `schema_version` | `str` (semver) | yes | Must be `"1.0.0"`. Checked by `DecomposeStage` against `SUPPORTED_PARSE_RESULT_BATCH`. |
| `run_id` | `str` | yes | Identifies the pipeline run that produced this batch. |
| `produced_at` | `datetime` (UTC, ISO 8601) | yes | When AssessStage produced this batch. |
| `results` | `list[ParseResult]` | yes | One entry per inventory item. Nothing dropped: len(results) == len(inventory.items). |
| `skeleton` | `bool \| None` | no | `True` = Phase 0 pass-through (no real parsing). `None` in real Phase 1+ runs. |

Invariants:
- `len(results)` equals the inventory item count. Every inventory item produces exactly one `ParseResult` regardless of parse outcome (`parsed`, `partial`, `failed`, `excluded_pre_parse`).
- Version checked by `DecomposeStage` via `SUPPORTED_PARSE_RESULT_BATCH = SpecRange(major=1, min_minor=0)`.

### §12b — SegmentSetBatch (`Decompose → Plan`)

Source: `src/finecorpus/contracts/segment_set_batch.py`. Wraps a list of `SegmentSet` objects.

| Field | Type | Required | Semantics |
|---|---|---|---|
| `schema_version` | `str` (semver) | yes | Must be `"1.0.0"`. Checked by `PlanStage` against `SUPPORTED_SEGMENT_SET_BATCH`. |
| `run_id` | `str` | yes | Identifies the pipeline run that produced this batch. |
| `produced_at` | `datetime` (UTC, ISO 8601) | yes | When DecomposeStage produced this batch. |
| `segment_sets` | `list[SegmentSet]` | yes | One entry per ParseResult. Nothing dropped. |
| `skeleton` | `bool \| None` | no | `True` = Phase 0 pass-through (empty segments). `None` in real Phase 1+ runs. |

Invariants:
- `len(segment_sets)` equals `len(ParseResultBatch.results)`. Every ParseResult produces exactly one SegmentSet (excluded/failed documents produce an empty SegmentSet with an ExclusionRecord).
- Version checked by `PlanStage` via `SUPPORTED_SEGMENT_SET_BATCH = SpecRange(major=1, min_minor=0)`.

### Phase status table

| Stage boundary | Artifact model | Official §12 contract? |
|---|---|---|
| Collect → Assess | `Inventory` | Yes — `schema_version: 1.0.0`, all fields validated |
| Assess → Decompose | `ParseResultBatch` | Yes (Phase 1, D-26) — `schema_version: 1.0.0`, version-checked by DecomposeStage |
| Decompose → Plan | `SegmentSetBatch` | Yes (Phase 1, D-26) — `schema_version: 1.0.0`, version-checked by PlanStage |
| Plan → Build | `IngestionConfig` | Yes — `schema_version: 1.0.0`, all fields validated |
| Build → (Serve) | `BuildResult` | No — pipeline-internal envelope; 0 chunks in Phase 0 skeleton |

---

## Contract versioning

Contracts evolve. A stage must never silently consume a shape it was not built for, because a
subtly-changed field read by an old consumer degrades quietly — exactly the failure mode §6
build rule 6 forbids.

### The scheme

- **Every contract instance declares its own `schema_version`**, a string field, present on the
  root model of each of the six contracts. The value is **semver** (`MAJOR.MINOR.PATCH`).
- Each contract's producer stamps the version it emits. Each consumer declares, in code, the set
  of versions it supports as an explicit compatibility range.
- **Compatibility rule (semver-with-explicit-declaration):**
  - **MAJOR** bump = breaking change (field removed, renamed, type changed, semantics changed,
    or a previously-optional field becomes required). A consumer supports a MAJOR line only if it
    declares that exact major.
  - **MINOR** bump = backward-compatible addition (new optional field, new enum member that old
    consumers may treat as unknown). A consumer that supports `X.Y` accepts `X.Y'` for `Y' >= Y`
    within the same major.
  - **PATCH** bump = documentation or constraint tightening that does not change the wire shape.
    Always compatible within the major.
- **A consumer MUST reject a version it does not declare support for**, rather than parse it
  best-effort. Rejection is a hard error (`ContractVersionError`) that fails the stage and is
  reported, per §18.2 contract-test layer ("a stage MUST reject malformed input rather than
  degrading"). It is never a warning-and-continue.

### How support is declared

Each consumer module declares a constant, e.g.:

```python
SUPPORTED_INVENTORY = SpecRange(major=1, min_minor=2)  # accepts 1.2.x .. 1.<latest>.x
```

On receipt the consumer validates `instance.schema_version` against its `SpecRange`. Mismatch
raises `ContractVersionError(contract="inventory", got="2.0.1", supported="1.>=2")`. The six
version constants live together in `finecorpus/contracts/versions.py` so the compatibility matrix
is inspectable in one place.

### Enum forward-compatibility

Enums (segment type, salience tier, transformation tier, review status, fetch policy, etc.) are
closed pydantic enums. **Adding an enum member is a MINOR bump.** A consumer encountering an
unknown enum value under a supported MINOR range treats it as a version error, not a silent
coerce-to-default — because coercing an unknown salience tier to `supporting` would silently
mis-route content. New members therefore require the consumer to be upgraded and its `min_minor`
raised.

### Invariants (versioning)

- Every contract root model has a required `schema_version: str` (semver).
- No consumer parses a contract without first checking `schema_version` against its declared range.
- A rejected version fails loud and is reported; it is never downgraded to best-effort parse.
- The version of the **ingestion config** additionally participates in chunk identity
  (`config_version`, see [chunk.md](chunk.md) and §10.5) — this is a distinct concept from
  `schema_version` and both are recorded. `schema_version` is the shape of the contract;
  `config_version` is the content-hash of the config's build-affecting fields.

---

## Shared blocks

Four blocks are reused across contracts. They are defined once in
`finecorpus/contracts/shared/` and embedded by reference. Defining them once guarantees a field
means the same thing at every stage.

### Tenancy block (`TenancyBlock`)

Present in **all six contracts** from Phase 0 (§19 Phase 0 MUST). Enforcement lands in Phase 4,
but the fields exist from day zero — retrofitting tenant identity into an existing index means
reindexing everything (§19). Document-level permission fields are designed against §11.4
(payload-filter access control) and §14.3 (source permission fidelity).

| Field | Type | Required | Semantics |
|---|---|---|---|
| `workspace_id` | `str` (ULID) | yes | The owning workspace (§2.1). Stable for the life of the workspace. |
| `kb_id` | `str` (ULID) | yes | The owning knowledge base. A document, segment, chunk belongs to exactly one KB. |
| `permission_mode` | `enum{public_to_kb, restricted, source_mirrored}` | yes | How `permission_principals` is interpreted. `public_to_kb`: any KB member may retrieve. `restricted`: only listed principals. `source_mirrored`: mirrored from a connector's source ACLs (§14.3). |
| `permission_principals` | `list[str]` | yes (may be empty) | Resolved principal identifiers (user IDs, group IDs, role tags) permitted to retrieve. Empty + `public_to_kb` = all KB members. Empty + `restricted` = nobody but admins. Resolved at ingestion into a filterable field (§11.4); enforced server-side, never widened by a client param. |
| `permission_source` | `enum{platform, connector, manual}` | yes | Where the permission facts came from. `connector` means mirrored from a source system (§14.3). |
| `permission_fidelity` | `enum{authoritative, best_effort, unavailable}` | yes | Reliability of the permission data. `unavailable` from a connector that cannot supply reliable ACLs — requires the explicit ingestion acknowledgement of §14.3. Surfaced so a `best_effort` KB is not mistaken for `authoritative`. |
| `permission_resolved_at` | `datetime` (UTC, ISO 8601) | no | When permissions were last resolved from source. Null for `manual`/`platform`. |

Invariants:
- `TenancyBlock` appears on the root of every contract and is inherited by every sub-record
  (segments inherit the document's block, §7.1).
- `permission_mode = source_mirrored` requires `permission_source = connector`.
- `permission_fidelity = unavailable` MUST NOT reach an index without a recorded acknowledgement
  (§14.3); this is checked at Plan/Build, not silently allowed.

### Source location block (`SourceLocation`)

Points into the *original* source. Must address native PDFs, scans, HTML, and spreadsheets, so
it carries several coordinate systems and marks which are authoritative for this instance.

| Field | Type | Required | Semantics |
|---|---|---|---|
| `locator_kind` | `enum{page, byte_range, char_range, cell_range, dom_path, time_range}` | yes | Which coordinate system is authoritative for this location. `time_range` reserved for future A/V; not served in v1. |
| `page_start` | `int` | no | 1-based first page. Required when `locator_kind=page` or for any paged source (PDF, scan). |
| `page_end` | `int` | no | 1-based last page (inclusive). Equals `page_start` for single-page. |
| `byte_start` | `int` | no | 0-based byte offset into the decoded source stream. |
| `byte_end` | `int` | no | Exclusive end byte offset. |
| `char_start` | `int` | no | 0-based Unicode codepoint offset into extracted text. Used for HTML/native text where byte offsets are unstable across encodings. |
| `char_end` | `int` | no | Exclusive end codepoint offset. |
| `cell_range` | `str` (A1 notation, e.g. `Sheet1!B2:D40`) | no | Spreadsheet cell/region address, sheet-qualified. Required for spreadsheet-report locations. |
| `dom_path` | `str` (CSS/XPath-like) | no | Stable structural path into an HTML export (e.g. Confluence). |
| `bbox` | `list[float]` len 4 `[x0,y0,x1,y1]` | no | Bounding box in PDF user-space units, for scanned regions / injection-position evidence (§14.1 off-page detection). |
| `coordinate_note` | `str` | no | Free text where a coordinate is approximate (e.g. OCR region estimated). |

Invariants:
- At least the coordinate set named by `locator_kind` is fully populated (both ends).
- Every extraction, segment, and chunk traces to a `SourceLocation` (§8, §12 parse-result
  invariant). A location that resolves to nothing is a defect.
- Offsets address the **original** source stream, so a location remains valid independent of any
  transformation applied downstream.

### Transformation record (`TransformationRecord`)

One entry per transformation applied, in application order. The chunk carries the full ordered
list (§8). Shared so the tier vocabulary is identical everywhere.

| Field | Type | Required | Semantics |
|---|---|---|---|
| `tier` | `enum{1,2,3}` | yes | 1 = structure normalization; 2 = contextual augmentation; 3 = full rewriting (§7.2). |
| `operation` | `str` (stable id) | yes | e.g. `ocr_cleanup`, `table_to_markdown`, `whitespace_repair`, `breadcrumb_augment`, `table_description`, `class_context`, `rewrite`. |
| `applied_by` | `enum{deterministic, model}` | yes | Whether a model was involved (all Tier 2 augmentation and Tier 3 are `model`; most Tier 1 is `deterministic`). |
| `model_ref` | `str` | no | Provider/model identity when `applied_by=model`, for audit and reproducibility. Never a secret (§14.2). |
| `changed_text` | `bool` | yes | Whether **this** operation altered the chunk `text` relative to what it received. Recorded **per op**. A **Tier 1** op that alters text (OCR character correction, `table_to_markdown`, whitespace/encoding repair) sets `true`; a Tier 1 op that only reshapes provenance or is a no-op sets `false`. A **Tier 2** op MUST set `false` — Tier 2 never touches `text` (this is what §18.3 test 4 asserts). A **Tier 3** rewrite sets `true` (per §7.2). See the **canonical source text** definition below and in [chunk.md](chunk.md). |
| `note` | `str` | no | Human-readable detail (e.g. "OCR corrected 3 substitutions; changed_text"). |

Invariants:
- The list is ordered by application. Order is significant and preserved to the chunk.
- **Canonical source text.** The chunk `text` is byte-identical to the span of the
  **Tier-1-normalized** document representation — *not* to the raw original. This is the only
  coherent reading: §7.2's Tier 1 (e.g. `table_to_markdown`, OCR cleanup) necessarily changes
  bytes, so "byte-identical to the raw source" is unsatisfiable. Instead:
  - Every Tier 1 op is recorded with its own `changed_text` flag; the RAW original is **always
    retained** and addressable via `source_location` (nothing is destroyed).
  - **Tier 2 never touches `text`** (`changed_text=false` for all Tier 2 ops); it writes only the
    separate augmentation fields.
  - **Tier 3** may differ from the Tier-1-normalized text only when its own
    `tier=3, changed_text=true` record is present; the pre-rewrite text is retained.
  - Byte-identity is therefore checked against the Tier-1-normalized canonical source, with the
    ordered `transformations` list (and its per-op `changed_text` flags) as the audit trail from
    raw original to served `text`.
  - This definition is the accepted interpretation of spec §12 (owner ruling 2026-09-03,
    D-11 CLOSED): the literal "byte-identical to source" means "byte-identical to the
    Tier-1-normalized canonical source." Every Tier 1 op is recorded with its own `changed_text`
    flag in provenance; the raw original is always retained and addressable via `source_location`.

### Provenance block (`Provenance`)

The §8 core invariant, verbatim, plus the security and language fields the spec attaches to it
(§14.1, §14.4, §7.6). Carried in full on every chunk (§12). Assembled incrementally across
stages and finalized at Build.

| Field | Type | Required | Semantics |
|---|---|---|---|
| `source_document_id` | `str` (ULID) | yes | Stable document identity (§8 "source document identity"). Matches Inventory `document_id`. |
| `source_document_version` | `str` (content hash, sha256 hex) | yes | Which version of the document this came from (§8 "and version"). Same value as Inventory `content_hash`; makes replace-by-document correct (§10.5). |
| `source_location` | `SourceLocation` | yes | Position within source (§8). |
| `structural_path` | `list[str]` | yes (may be empty) | Ordered heading breadcrumb from document root (§8, §6.3). Empty list = document had no heading structure (recorded, not omitted). |
| `transformations` | `list[TransformationRecord]` | yes (may be empty) | Ordered list of transformations with tier of each (§8). |
| `confidence` | `float` [0,1] | yes | Composite confidence for this chunk, incorporating OCR confidence where applicable (§8). See `ocr_confidence`. |
| `ocr_confidence` | `float` [0,1] | no | Per-region OCR confidence, retained not thresholded (§6.2, §6.4). Null for native-text sources. Filterable at retrieval (§6.2). |
| `segment_type` | `enum` (segment taxonomy, §6.3) | yes | The segment's type (prose, table, list, code, figure_caption, form_field, boilerplate, front_matter, scanned_region, …). |
| `salience_tier` | `enum{primary, supporting, boilerplate, excluded}` | yes | Default retrieval weight class (§6.3). `excluded` is indexed but default-filtered, not dropped. |
| `salience_basis` | `SalienceSignalKind` (enum) | yes | The **winning** salience signal that set `salience_tier` (taxonomy §4.3). Carried through from the segment (see [segment-set.md](segment-set.md) `SalienceSignal`), so explain mode (§11.5) can state why this chunk got its tier. |
| `salience_signals` | `list[SalienceSignal]` | yes (may be empty) | **All** firing signals (winning and contributing), carried through from the segment (taxonomy §4.3, C-R3). Enables the §11.5 "why this tier" trace at retrieval. |
| `language` | `str` (BCP-47, e.g. `en`, `es`) | yes | Detected language of the segment (§7.6). `und` for undetermined; recorded, never omitted. |
| `injection_suspicion` | `float` [0,1] | yes | Injection-pattern suspicion score (§14.1). Retrievable and filterable; never used to silently exclude. `0.0` when no pattern detected (recorded, not omitted). |
| `invisible_content_flags` | `list[enum{white_on_white, zero_size_font, off_page, metadata_only, render_hidden}]` | yes (may be empty) | Invisible-content detections from parse (§14.1). Empty list = none detected. |
| `sensitivity_flags` | `list[enum{pii, phi, financial, credential, legal_privileged, other}]` | yes (may be empty) | PII/sensitive-category detections (§14.4). Advisory; never triggers silent redaction. |
| `trust_level` | `enum{untrusted_ingested}` | yes | Content trust label (§14.1). Single value in v1: all ingested content is untrusted material. Surfaced at retrieval and in the MCP tool description. |

Invariants:
- Every chunk carries a complete, non-null `Provenance` (§8). This is the first thing checked on
  every PR and the subject of non-negotiable test §18.3.3.
- `confidence` reflects `ocr_confidence` when the latter is present (a low-OCR region cannot have
  high composite confidence).
- `injection_suspicion`, `invisible_content_flags`, and `sensitivity_flags` are always present
  (possibly empty/zero); their absence would make "no detection" indistinguishable from "not
  checked".
- Provenance fields are filled progressively (Inventory → Parse → Segment → Chunk) but the block
  is only *complete* at the chunk. Earlier stages carry partial provenance in their own fields;
  the chunk assembles the finalized block.

---

## Audit & break-glass seeds (Phase 0 fields, Phase 4 enforcement)

The §2.3 break-glass requirements (a stated reason, a time-bound grant, and an immutable audit
record of every content read) are enforced in Phase 4, but their **record shapes are sketched now**
so Phase 4 is not a reindex-requiring retrofit (S-R12; MUST traceability M-001–M-004). These are
control-plane / audit-log records, **not** stored in chunk payloads.

**`BreakGlassGrant`** (control-plane record):

| Field | Type | Required | Semantics |
|---|---|---|---|
| `grant_id` | `str` (ULID) | yes | Identity of the grant; referenced by every read performed under it. |
| `grantor` | `str` | yes | The admin/principal who issued the grant. |
| `reason` | `str` | **yes** | Stated reason — required, never blank (§2.3). |
| `scope_kb_id` | `str` | yes | The KB the grant authorizes access to. |
| `granted_at` | `datetime` (UTC) | yes | When the grant took effect. |
| `expires_at` | `datetime` (UTC) | **yes** | Time bound — required and **finite** (a grant cannot be open-ended; §2.3). |
| `revoked_at` | `datetime` (UTC) | no | Set if revoked before expiry. |

**`AuditRecord`** (append-only audit-log record):

| Field | Type | Required | Semantics |
|---|---|---|---|
| `actor` | `str` | yes | Principal who performed the action. |
| `action` | `str` | yes | What was done (e.g. `retrieve`, `explain`, `config_change`). |
| `resource` | `str` | yes | What was acted on (KB, chunk set, config). |
| `tenancy` | `TenancyBlock` | yes | Workspace/KB scope of the action. |
| `timestamp` | `datetime` (UTC) | yes | When it happened. |
| `grant_id` | `str` | no | The `BreakGlassGrant.grant_id` when the action was performed under break-glass; null otherwise. |

Because chunk payloads and retrieval responses already carry the tenancy fields audit needs
(`TenancyBlock` on every contract; see [retrieval-response.md](retrieval-response.md)), **no
reindex-requiring retrofit is expected** to wire break-glass audit in Phase 4 — the grant/read
linkage lives in the audit log and (for a served response) in the retrieval-response envelope, not
in the stored chunk. The default grant window is tracked as decision **D-04**.

---

## Open questions

1. **Composite `confidence` formula.** §8 says confidence incorporates OCR confidence but does
   not specify the function. **Proposed default:** `confidence = ocr_confidence` when OCR applies,
   else `1.0` for clean native extraction, reduced by parse-quality penalties; the exact
   weighting is an Assess-stage concern documented in parse-result. **Flagged** — needs a single
   canonical formula before Phase 2 so the field is comparable across KBs.
2. **`permission_principals` identifier namespace.** Whether principal IDs are opaque platform
   IDs, source-system IDs, or a mapped union. **Proposed default:** platform-resolved IDs with a
   `permission_source` marker; connector IDs mapped at ingestion. **Flagged** — ties to Open
   Decision §20.8 (auth model) and §20.6 (cross-KB retrieval).
3. **ULID vs UUID for identities.** Overview does not fix an id scheme. **Proposed default:**
   ULID (sortable, timestamped, URL-safe). **Flagged** — must be decided in Phase 0 as it appears
   in every contract.
4. **`trust_level` extensibility.** v1 has a single value. **Proposed default:** keep it an enum
   (not a bool) so future values (e.g. `verified_internal`) are a MINOR bump. **Flagged.**
