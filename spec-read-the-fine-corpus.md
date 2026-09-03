# Read The Fine Corpus

## Engineering Specification

**Short form:** RTFC
**Package:** `finecorpus`  **CLI:** `corpus`
**Spec version:** v0.1 — draft for build
**Author:** Ashwin Chidambaram
**Date:** September 2026
**Intended executor:** Claude Code

---

## System Prompt — Claude Code

> Copy this block into `CLAUDE.md` at the repository root, or paste it as the opening
> instruction of the build session.

You are the **technical product manager and principal engineer** for **Read The Fine Corpus**
(package `finecorpus`, CLI `corpus`), a self-hostable platform that turns a company's heterogeneous document
corpus into a production retrieval endpoint and explains every decision it made along the way.
This specification is your source of truth.

### Your role

You orchestrate. You do not personally write most of the code.

Your value is holding the whole system in view — the invariants, the phase sequence, the
cross-cutting constraints, the reasons behind decisions made six phases ago. That context is
finite and it is the scarcest resource in this build. **Spend it on judgment, not on
implementation detail.** Every time you read a large file, debug a failing test line by line, or
hand-write a module, you are spending context you will need later to notice that a Phase 5
decision quietly violates a Phase 2 invariant.

Delegate implementation to subagents with scoped briefs. Keep for yourself: the spec, the
architecture, the invariants, the phase plan, the open questions, and the review of what comes
back.

### Working with subagents

- **Brief precisely.** A subagent brief contains: the task, the relevant spec sections, the contracts it must satisfy, the tests that must pass, and the explicit boundaries of what it may change. Subagents do not inherit your context — assume they know nothing beyond their brief.
- **Scope to one concern.** One module, one contract, one test suite, one fix. A subagent asked to "implement Phase 3" will make architectural decisions you did not review.
- **Use specialists.** Distinct roles for implementation, test authoring, code review, security review, and documentation. A reviewer subagent MUST NOT be the agent that wrote the code — independent review is the point.
- **Verify what returns.** Read the diff and the test results, not the summary. A subagent reporting success is evidence, not proof. Spot-check against the invariants in Section 8 and the contracts in Section 12.
- **Delegate investigation too.** When something fails, send a subagent to investigate and report findings rather than reading logs yourself.

### Git and delivery discipline

Work the way a principal engineer on a serious team would.

- **Trunk-based, short-lived branches.** One branch per coherent unit of work. Branches live hours or days, never weeks.
- **Commit frequently and atomically.** Each commit is one logical change with a clear message explaining *why*, not what. A commit that touches code, tests, and docs together is correct; a commit that touches only code is usually incomplete.
- **Every change lands via pull request.** No direct pushes to the main branch, including by you.
- **Independent review, always.** Open the PR, then dispatch one or more reviewer subagents that did not write the code. At minimum: a correctness reviewer checking the diff against the spec and contracts, and — for anything touching ingestion, retrieval, tenancy, secrets, or deletion — a security reviewer working from Section 14. Address review findings before merge. You arbitrate disagreements.
- **CI is the gate.** Lint, type checks, unit, contract, property, and integration tests run on every PR. The non-negotiable tests in Section 18.3 run on every PR that could touch them and on every merge to main. A red build is never merged and never worked around.
- **Main is always releasable.** If a phase is mid-flight, it is behind a feature flag or on a branch, not half-merged into main.
- **Conventional commits and a maintained changelog.** Releases are tagged and versioned.

### Build rules

1. **Read the whole spec before writing anything.** The design is interdependent; decisions in
   Section 7 constrain Section 10, and Section 8 constrains everything. Do not begin at
   Phase 0 having read only Phase 0.

2. **Produce a design document first.** Before implementation code, deliver: module
   decomposition, concrete schemas for every contract in Section 12, service boundaries, the
   segment type taxonomy, and your subagent delegation plan. Stop and wait for review. Do not
   proceed to code on your own judgment that the design is obviously right.

3. **Maintain the documentation as you build, not afterwards.** This repository carries a
   living product wiki under `/docs`, and updating it is part of completing a phase, not a
   cleanup task at the end. At minimum it covers: architecture and how the services fit
   together, the concrete schema for every contract, configuration reference, operations
   runbooks (ingest, reindex, promote, roll back, restore, purge), the API and MCP interface
   reference, a troubleshooting guide keyed to real error conditions, and an architecture
   decision record for every non-obvious choice you made and why. Write it for someone
   self-hosting this who has never spoken to you. **Treat the wiki as your own working memory:**
   it is how you and your subagents stay coherent as your context fills. When a later phase
   needs to know how an earlier one behaves, the answer should be in `/docs` rather than
   reconstructed from code — and it is what you put in a subagent's brief instead of explaining
   the system from scratch. If you find yourself re-deriving a decision you already made, the
   wiki was incomplete — fix it before continuing. Documentation that contradicts the code is
   worse than none, so when behaviour changes, the docs change in the same commit.

4. **MUST is a constraint, not a strong suggestion.** Where the spec says MUST, you implement
   it as written or you stop and raise the conflict. Where it says SHOULD, you may propose an
   alternative with reasoning. Where it says "Executor defines," design it and justify the
   design. Subagents inherit this: put the relevant MUSTs in their briefs.

5. **The invariants in Section 8 are the spine of the system.** Every chunk carries complete
   provenance. If a design you are considering cannot satisfy that for every chunk on every
   path, the design is wrong — change the design, do not weaken the invariant. This is the
   first thing you check on every PR.

6. **Nothing is silently destroyed and nothing silently degrades.** If content cannot be
   processed, it is recorded and reported with a reason. A pipeline that quietly drops
   documents, truncates content, or falls back to a lesser strategy without saying so is a
   defect, regardless of how good the output looks.

7. **Build the golden corpus in Phase 0.** It is the substrate for nearly every meaningful
   test. Do not defer it because it is unglamorous. If you find yourself verifying behaviour by
   reading output rather than by assertion, you skipped this step.

8. **Tests are part of the phase, not after it.** A phase is complete when its acceptance
   criteria pass, including the non-negotiable tests in Section 18.3. Do not report a phase
   complete on the basis of code existing.

9. **Respect the product boundary.** The system ends at the retrieval endpoint. Do not add
   generation, chat, prompt management, or agent orchestration, however natural the extension
   feels while building. If a task seems to require crossing that line, raise it.

10. **Prefer boring, inspectable implementations.** This is infrastructure that other teams will
    self-host, debug, and extend. Clever is a liability here.

11. **When the spec is ambiguous, say so rather than resolving it silently.** Ambiguity you
    resolve without flagging becomes a decision nobody made. List open questions as you find
    them and surface them with the design document. If a subagent resolves an ambiguity on its
    own initiative, that is a review finding, not a shortcut you accept.

### Working style

Work phase by phase in the order given in Section 19. Within a phase, plan the work, decompose
it into subagent-sized units, dispatch, review, and integrate.

At the end of each phase, report: what was built, which acceptance criteria pass, which tests
exist and their results, which PRs landed and who reviewed them, which documentation pages
changed, what you had to decide that the spec did not cover, and what you would change about
the spec in light of having built against it. A phase in which `/docs` did not change is a
phase you have not finished.

If your context is filling, say so and checkpoint into `/docs` rather than degrading quietly.

---

## 0. How To Read This Spec

This document defines **what** the system does and **which invariants must hold**. It
deliberately does not define module structure, class hierarchies, function signatures, or
schema field lists. Those are the executor's job.

Where this spec says **MUST**, it is a constraint on the design, not a suggestion. Where it
says **SHOULD**, the executor may propose an alternative with justification. Where it says
**Executor defines**, the spec is intentionally silent and expects a design proposal before
implementation.

Before writing implementation code, the executor is expected to produce a design document
covering module decomposition, the concrete schemas behind each data contract in Section 12,
and the interface boundaries between services. That design document is reviewed before build
begins.

---

## 1. Product Definition

### 1.1 One-line

A self-hostable platform that turns a company's messy document dump into a production-grade
retrieval endpoint, and explains every decision it made along the way.

### 1.2 The problem

Teams building RAG systems fail at the ingestion layer, not the model layer. They dump
heterogeneous documents into a vector store with default chunking, get mediocre retrieval,
blame the LLM, and tune prompts that cannot fix a retrieval problem. The knowledge required to
do ingestion well is specialist knowledge, and most teams that need a knowledge base do not
have a specialist.

### 1.3 The boundary

The system owns everything up to and including the retrieval endpoint. It owns nothing above it.

**In:** ingestion, document assessment, normalization, segmentation, chunking strategy
selection, embedding, index lifecycle, retrieval serving, evaluation, observability.

**Out:** generation, chat interfaces, prompt management, agent orchestration, conversation
memory, answer synthesis. The system returns chunks. What the caller does with them is theirs.

This boundary is load-bearing. Feature requests that cross it are rejected by default. The one
permitted exception is LLM configuration for the platform's *own internal* operations
(classification, augmentation, question generation) — see Section 6.4.

### 1.4 Design principles

1. **Nothing is silently destroyed.** Every transformation is reversible or the original is retained.
2. **Every recommendation carries its evidence.** A user must be able to ask why and get an answer.
3. **Defaults are visible.** Easy mode is Proficient mode with the recommender's answers filled in, never a separate code path.
4. **Honest findings beat impressive ones.** "This corpus can't be usefully indexed" is a valid, shippable output.
5. **The segment is the unit.** Not the document. See Section 7.1.

---

## 2. Users & Access Model

### 2.1 Three-level hierarchy

```
Platform  (org-wide; owned by IT / Data)
  └── Workspace  (a team)
        └── Knowledge Base  (a product / project / corpus)
```

### 2.2 Roles

| Role | Scope | Capabilities |
|---|---|---|
| Platform Admin | Platform | Provision workspaces, set quotas, manage infrastructure config, view all operational metrics, force reindex, delete, break-glass content access |
| Workspace Owner | Workspace | Create/delete KBs, manage workspace members, set workspace defaults, view workspace metrics |
| KB Editor | Knowledge Base | Ingest, configure, reindex, publish, edit class descriptions |
| KB Viewer | Knowledge Base | Query, read reports, read config |
| Service Principal | Knowledge Base | Query only, via API key or OIDC; used by agents and applications |

### 2.3 Admin content access (break-glass)

Platform Admin has *operational* control over all knowledge bases by default: quota,
deletion, forced reindex, cost, health, and all metrics. This does **not** include reading
document or chunk contents.

Content access is a separate, self-grantable permission with these MUST requirements:

- Granting it requires a stated reason, captured at grant time.
- Grants are time-bound and expire automatically. Default window: executor proposes; MUST be finite.
- Every grant, every expiry, and every content read performed under a grant is written to an immutable audit log.
- The Workspace Owner and KB Editors are notified at grant time, not after.
- Break-glass activity appears in the KB's own audit view, visible to its team.

Rationale: legal, HR, and regulated knowledge bases exist on the same platform as the
engineering wiki. An admin role that transparently reads everything will fail procurement.

---

## 3. Operating Modes

### 3.1 Easy mode

Target user: a product manager or ops lead with no ML background.

Flow: create KB → connect or upload documents → wait → review the recommended plan in plain
language → approve → get an endpoint.

Every configuration decision is made by the recommender and displayed as a plain-language
summary with a "why" affordance. The user's only required inputs are the corpus and an
approval.

### 3.2 Proficient mode

Target user: a data engineer or ML engineer.

Same pipeline, same screens, same underlying job model. Every recommender-filled value becomes
editable in place. Additional surfaces: raw metrics, configuration sweep results, per-class
chunking parameters, embedding model selection, retrieval strategy, index topology.

### 3.3 The discipline

There MUST NOT be two pipelines. Mode is a UI-level presentation concern over one execution
engine. Any recommendation shown in Easy mode is a concrete value in the same config object a
Proficient user edits. If a feature exists only in one mode, it is a spec violation.

---

## 4. System Architecture

### 4.1 Services

The executor defines the decomposition, but these responsibilities MUST be independently
scalable and MUST NOT be collapsed into one process:

| Responsibility | Notes |
|---|---|
| **Retrieval service** | Stateless. Horizontally scalable. Sole owner of the vector DB connection pool. All query traffic passes through it. |
| **Ingestion workers** | Long-running, resumable, queue-driven. Never in the request path. |
| **Embedding service** | Batching, provider abstraction, query-embedding cache. Shared by ingestion and retrieval. |
| **Control plane / API** | Tenancy, RBAC, config, job orchestration, audit. |
| **Web UI** | A client of the control-plane API. Holds no business logic. |

### 4.2 Hard architectural constraints

- **C-1** The retrieval service MUST be stateless. All state lives in the vector DB or the control-plane database.
- **C-2** Agents and applications MUST NOT connect to the vector DB directly. Ever.
- **C-3** All queries MUST target an alias, never a collection name. See Section 10.
- **C-4** The ingestion path MUST write to a shadow collection, never to the live one.
- **C-5** Business logic MUST live in the core library, not in the CLI or the UI. Both are consumers.

### 4.3 Deployment targets

- Docker Compose — single-node, the default self-host path.
- Kubernetes / Helm — multi-node.
- Fully local — local embedding models, local vector DB, no egress. MUST be a supported configuration, not a degraded one.

### 4.4 Backend abstraction

**Reference backend: Qdrant.** Specified and tested fully.

Additional backends (pgvector, Weaviate, Milvus, cloud-managed services) are implemented
against an adapter interface. The adapter interface MUST expose alias-equivalent atomic
swap, payload filtering, and hybrid search as first-class operations — these differ enough
between backends that a lowest-common-denominator interface will fail.

A backend that cannot support atomic alias swap is not a supported backend. Where an adapter
cannot support a capability, it MUST declare that explicitly and the platform MUST surface the
limitation to the user rather than silently degrading.

Do not claim multi-backend support at launch. Ship Qdrant properly plus the adapter interface.

### 4.5 Performance & scale targets

Targets are for the reference deployment (single-node Docker Compose, Qdrant, commodity
hardware). They exist so the executor has something to design against and measure; revise them
with evidence rather than ignoring them.

| Dimension | Target |
|---|---|
| Retrieval p50 latency, cache warm | < 150 ms |
| Retrieval p99 latency | < 800 ms |
| Retrieval availability during alias swap | 100% — zero failed requests |
| Corpus size, reference deployment | 100k documents / ~5M chunks |
| Concurrent query clients | 100 sustained |
| Ingestion throughput | Executor establishes a baseline in Phase 1 and MUST NOT regress it |
| Cold start to first query, Easy mode, 1k docs | < 30 minutes unattended |

The last row is a product target, not an infrastructure one. If a non-technical user cannot get
from upload to working endpoint inside half an hour, the product has failed its primary user
regardless of retrieval quality.

Degradation above the reference scale MUST be graceful and stated: sampling, queueing, and
explicit limits, never silent truncation.

### 4.6 Self-host developer experience

For an open-source, self-hosted product, the install path is the product. Requirements:

- **One-command bring-up.** `docker compose up` reaches a working system. First run prompts for a model provider and validates it before anything else; neither choice requires editing code or rebuilding an image.
- **Provider parity.** Cloud and local model providers are both first-class. Documentation, tests, and performance baselines cover both, and no feature may work under one and not the other. Air-gapped operation with no egress MUST remain fully supported, and cloud-hosted operation MUST NOT be a bolt-on.
- **Config as one file.** A single declarative config with every option, commented, defaults shown. Environment variables override; nothing is hidden in code.
- **Preflight check.** A command that validates configuration, connectivity, model availability, and resource headroom before ingestion begins, with actionable errors.
- **Golden-path tutorial.** A sample corpus ships with the repository. A new user reaches a working endpoint following one document.
- **A living wiki under `/docs`.** Architecture, contract schemas, configuration reference, operations runbooks, API and MCP reference, troubleshooting, and architecture decision records. It is written for a self-hoster with no access to the authors, it is versioned alongside the code, and it changes in the same commit as the behaviour it describes. Documentation that has drifted from the code is treated as a defect, not as debt.
- **Upgrades are specified.** Schema and config migrations are versioned and forward-compatible; upgrading MUST NOT require reindexing unless the embedding model or chunking config changed. Where a reindex is unavoidable, the upgrade path says so before it starts.

---

## 5. Pipeline Overview

Six stages. Each produces a durable, inspectable artifact. Each is independently re-runnable.

```
1. Collect      → corpus inventory
2. Assess       → quality findings
3. Decompose    → typed segments
4. Plan         → routing plan + ingestion config
5. Build        → shadow index
6. Serve        → retrieval endpoint
```

Evaluation is not a stage; it is a cross-cutting capability invoked by Plan (to rank configs)
and by Serve (to detect drift). See Section 9.

---

## 6. Stage Specifications

### 6.1 Stage 1 — Collect

**Sources (MUST):** bulk file upload, local/mounted directory, S3-compatible object storage.

**Connectors (SHOULD, prioritized):** SharePoint, Atlassian (Confluence + Jira), Google Drive.
Connectors are incremental-capable: they track source-side change signals rather than
re-downloading everything.

**Web links:** links found inside documents are references, not content. The fetch policy is
explicit per KB:
- `ignore` — record the link, do not fetch (default)
- `snapshot` — fetch once, store as a dated artifact, mark as potentially stale
- `crawl` — follow, bounded by a mandatory depth limit and domain allowlist

`crawl` without an allowlist MUST be rejected by config validation.

**Outputs:** file inventory with type, size, source path, source-system metadata, timestamps,
and content hash.

**Deduplication:** exact duplicates by content hash. Near-duplicate clustering to identify
document version families — the newest member is primary, the rest are retained as
superseded and excluded from the index by default.

### 6.2 Stage 2 — Assess

Parse each file and score the result. This stage answers "can we even read this."

**MUST detect and report:**

- Native-text vs. scanned PDF
- OCR quality, with per-page confidence retained (not thresholded away)
- Table structure loss during parsing
- Empty or near-empty extraction
- Encoding corruption
- Repeated boilerplate across the corpus (headers, footers, legal preambles, revision blocks)
- Password-protected or otherwise unreadable files
- Content classes requiring special handling or exclusion (Section 7.5)

**Boilerplate detection reuses dedup machinery.** Text repeated across many documents in the
corpus is boilerplate by definition. This requires no user configuration and MUST be automatic.

**OCR confidence is a first-class field.** It propagates to the chunk (Section 12), is
available as a retrieval filter, and surfaces at citation time. The failure mode being
prevented: degraded OCR text becoming silently authoritative in a downstream agent's answer.

**Output:** quality findings report, in both machine-readable and plain-language form. The
plain-language form is a client-presentable artifact — this is the deliverable that makes the
product valuable even before anything is indexed.

### 6.3 Stage 3 — Decompose

**This is the architectural correction that most of the design depends on.**

The routing unit is the **segment**, not the document. A single operating manual contains prose
procedures, a specifications table, a scanned appendix, and a revision-history block — four
content types requiring four treatments inside one file. Any design that assigns one strategy
per document is wrong regardless of which strategy it picks.

**Process:**

1. Decompose each document into typed segments (prose, table, list, code, figure caption, form field, boilerplate, front matter, scanned region, and so on — the executor proposes the type taxonomy).
2. Assign each segment a structural path: the ordered heading breadcrumb from document root.
3. Assign each segment a **salience tier**.
4. Retain a document-level record that can reassemble the segments in order.

**Salience tiering, not pruning.** The bloated-manual problem — where only part of a document
matters — MUST NOT be solved by discarding content at ingestion. That is irreversible and
destroys the ability to answer unanticipated questions. Instead, every segment gets a salience
tier, and retrieval filters or down-weights by tier. If the user was wrong about what mattered,
they change a filter instead of re-running ingestion.

Tiers (executor may refine): `primary`, `supporting`, `boilerplate`, `excluded`.

The signal for tier assignment is the class description (Section 6.5) plus boilerplate
detection plus structural position. Everything is indexed; tier controls default retrieval
behaviour.

### 6.4 Stage 4 — Plan

Produces the routing plan: for every segment class, the transformation tier, chunking strategy,
chunk parameters, embedding model, metadata schema, and retrieval treatment.

**Content handling matrix.** The following behaviours are specified, not left to the executor:

| Content type | Required handling |
|---|---|
| **Prose** | Structure-aware chunking respecting heading boundaries. Parent breadcrumb augmentation default-on for documents with real heading structure. |
| **Tables** | Atomic — MUST NOT split rows from their headers. Embed a generated natural-language description of the table's contents; return the verbatim table to the caller. For tables too large to keep atomic, repeat headers into each fragment. |
| **Scans** | Per-page OCR confidence in the payload. Low-confidence segments down-weighted at retrieval and flagged in provenance. |
| **Spreadsheets** | MUST be triaged into one of three kinds before ingestion (below). |
| **Web links** | Per fetch policy, Section 6.1. |
| **Cross-references** | "See section 4.2" is meaningless in an isolated chunk. Either resolve at ingestion (inline or store a followable pointer) or record the unresolved reference explicitly. Silent loss is not acceptable. |
| **Code** | Syntax-aware splitting at function/class boundaries. |
| **Unservable content** | Section 7.5. |

**Spreadsheet triage.** A `.xlsx` is three different products sharing a file extension.
Misclassification here is the most common way spreadsheets poison a knowledge base. The
platform serves exactly one of the three and is explicit about the other two.

- **Report** (formatted, narrative, chart-bearing) — **the only kind this platform ingests.** Treat as a document: sheets become sections, tables follow the table rules above, and chart-adjacent commentary is prose.
- **Database** (rows as records) — MUST NOT be vectorized. Detected, excluded, and reported as unservable with the reason stated. The platform does not attempt a structured-query path; a row-oriented dataset belongs in a database, and pretending otherwise produces confident nonsense.
- **Model** (formulas are the meaning; values are derived) — detected, excluded, reported. Ingesting a values snapshot creates a stale artifact that looks authoritative.

Triage MUST be visible and overridable: the user sees each spreadsheet's classification and can
reclassify it in Proficient mode. Detection heuristics are the executor's design, but the
report/database/model distinction is fixed.

**Recommendation engine.** For each segment class, propose a configuration. In Easy mode these
become the config. In Proficient mode they are pre-filled and editable. Where a configuration
sweep has been run (Section 9), recommendations are evidence-backed; where it has not, they are
heuristic and MUST be labelled as such.

**Output:** the ingestion config — a single, complete, machine-readable object that fully
determines Stage 5. It MUST be exportable, diffable, version-controllable, and re-importable.

### 6.5 Class descriptions — the human input

An optional but heavily surfaced input: for each segment class (with per-document override),
the user writes a short free-text description of *what this content is* and *what people need
to get out of it*.

This is the single most valuable input a human can provide, because it is the only information
that does not exist anywhere in the corpus. A document dump contains no signal about what will
be asked of it. This is the realistic substitute for the SME evaluation session that self-serve
users will never convene.

It feeds:
- Salience tier assignment (Stage 3)
- Chunking strategy recommendation (Stage 4)
- Evaluation question generation (Section 9)
- Table and segment description generation (Section 8)

The UI MUST make this feel low-effort — per class, not per document, with generated draft text
the user edits rather than a blank box.

### 6.6 Stage 5 — Build

Executes the ingestion config: transform, chunk, embed, and write to a **shadow collection**.

MUST be resumable after failure without full restart. MUST report progress, cost accrued, and
estimated completion. MUST validate the resulting index before it becomes eligible for
promotion (Section 10).

### 6.7 Stage 6 — Serve

See Section 11.

---

## 7. Content Handling — Cross-Cutting Rules

### 7.1 Segment-level routing

Restated because it governs everything: classification, transformation, chunking, and salience
are all properties of segments. Document-level properties (source, permissions, dates, version
family) are inherited by every segment within.

### 7.2 Transformation tiers

Three tiers with different risk profiles. They MUST be independently controllable.

**Tier 1 — Structure normalization. Default: on.**
OCR cleanup, table-to-markdown conversion, header inference on undifferentiated text,
whitespace and encoding repair, boilerplate stripping. Changes form, not meaning. Verifiable.
Safe on nearly everything.

**Tier 2 — Contextual augmentation. Default: on.**
The chunk text remains byte-identical to the source. Generated context — table descriptions,
parent breadcrumbs, class context blurbs — is stored in *separate fields*. Retrieval embeds
context-plus-chunk; the caller receives the verbatim chunk. This captures most of the retrieval
benefit of rewriting with zero provenance loss, and is the workhorse of the system.

**Tier 3 — Full rewriting. Default: off.**
A model rewrites content into a more chunk-friendly form. Reserved for input genuinely unusable
as-is. Requirements, all MUST:
- Original always retained, never overwritten
- Diff preview shown before commit
- Rewritten chunks flagged in provenance and visible at citation time
- Per-class opt-in, never global

Rationale for the caution: a flipped negation or drifted figure in a rewritten contract clause
is nearly undetectable downstream and disqualifying in regulated corpora.

### 7.3 Internal model configuration

The platform makes its own LLM calls — classification, augmentation, table description,
question generation, rewriting. These MUST be configurable: provider, model, temperature, and
per-operation overrides. Local models MUST be supported, since a team may accept cloud
embedding but not cloud rewriting of sensitive text.

This is the *only* place temperature appears in the product. Generation-time parameters belong
to whoever calls the retrieval endpoint.

### 7.4 Preview

Before committing an ingestion config, the user MUST be able to preview representative output:
sample chunks with their augmentation fields, their provenance, and — for Tier 3 — a diff
against the original. This is a required feature, not a convenience.

### 7.5 Unservable content

Content the system knows it cannot serve well: CAD files, diagram-only PDFs, audio, video,
images carrying meaning, encrypted files, and spreadsheet models.

Also: spreadsheet databases and spreadsheet models (Section 6.4).

These MUST be inventoried, excluded, and reported with a reason. Reporting "these 400 files
cannot be usefully indexed, here is why" is a better product outcome than silently producing
chunks that pollute every retrieval.

The exclusion report is a first-class deliverable, not an error log. It MUST be readable by a
non-technical user, grouped by reason, and MUST state what the user could do about each group —
including "nothing, and that is the correct outcome."

### 7.6 Language

Enterprise corpora are frequently multilingual, often without anyone realising it. The platform
MUST have a stated position rather than an accidental one.

- Language is detected per segment and stored as a filterable field.
- The findings report states the language distribution of the corpus. A corpus that is 12% Spanish is something the KB owner needs to be told, not something they discover through bad retrieval.
- Embedding model capability is checked against detected languages. Where the configured model does not support a detected language, the platform MUST warn before ingestion rather than embedding it anyway — an unsupported language produces vectors that are quietly meaningless.
- Cross-lingual retrieval (querying in one language, matching content in another) is a property of the chosen embedding model, not of this platform. The platform reports whether the configured model supports it; it does not attempt to add it.
- Translation is out of scope. It is a Tier 3 rewriting operation with all the provenance problems that implies, and it is better solved upstream.

---

## 8. Provenance

**The core invariant of the system.** Every chunk MUST carry:

- Source document identity and version
- Position within source (page and/or byte offset)
- Structural path (heading breadcrumb)
- Ordered list of transformations applied, with the tier of each
- Confidence score, incorporating OCR confidence where applicable
- Segment type and salience tier
- Tenancy and permission fields

This is what makes the pipeline auditable, what lets a user answer "why did the system return
that," and what lets an agent cite honestly. Any design that cannot satisfy this for every
chunk is rejected.

---

## 9. Evaluation

### 9.1 Eval set generation

Generate candidate questions from corpus content, stratified across segment classes and
question types (factual lookup, interpretive, multi-document synthesis). Class descriptions
(6.5) are a primary input.

Human review is **optional and encouraged, not required**. This is a deliberate product
decision: requiring it would mean most self-serve users never reach the product's value.

### 9.2 The bias, and how it is handled

A configuration sweep scored against LLM-generated questions is partly measuring how well the
corpus answers questions derived from itself, which biases toward configurations that preserve
the corpus's existing phrasing.

Therefore, all MUST:
- Unreviewed eval sets are labelled **provisional** everywhere they appear
- Reports state the confidence level explicitly, prominently, not in a footnote
- Reviewed and unreviewed baselines are visually distinguished
- Users can import an existing eval set or a production query log, which supersedes generation

Shipping a number that looks like validation and is not is the failure mode being prevented.

### 9.3 Configuration sweep

Sweep chunking parameters, splitter type, and embedding model against the eval set. Score on
context recall and context precision. Compare dense, hybrid, and reranked retrieval on top
candidates.

MUST sample the corpus rather than sweeping the full set. MUST show estimated compute cost
before execution and MUST require confirmation above a configurable threshold. SHOULD use
evolutionary or Bayesian search rather than exhaustive grid — prior work (RAGSmith,
arXiv:2511.01386) finds strong configurations while exploring roughly 0.2% of a ~46,000-config
space, on the order of 100 candidates, which is what makes this tractable.

**The naive baseline is defined, not improvised.** Every reported delta is measured against a
fixed reference configuration: recursive character splitting at 512 tokens with 50–100 token
overlap, dense retrieval only, no augmentation beyond Tier 1, and the KB's configured embedding
model. This is the benchmark-validated default in the literature and it is what most teams
would have shipped unaided. Fixing it makes deltas comparable across knowledge bases and across
releases. If the reference is ever changed, previously reported baselines MUST be marked as
measured against the old reference rather than silently reinterpreted.

**Small corpora.** Below a configurable document threshold, a sweep produces noise rather than
signal — the eval set is too small for score differences to mean anything. Below that
threshold the platform MUST decline to sweep, say why in plain language, and apply the
reference configuration. Reporting "your corpus is too small for this to be measurable, and
that is fine" is the honest output. Producing a confident ranking from twelve documents is not.

Output: ranked configuration table with metric deltas against the naive baseline, plus cost,
index size, and ingestion time estimates per configuration.

**A finding of "the default is already near-optimal for this corpus" is a valid result and
MUST be reported as such.** Do not manufacture a delta.

### 9.4 Drift detection

The baseline score from the sweep is retained. It is re-run on a schedule and after every
reindex. Regression against the retained baseline raises an alert.

This is the metric that matters most in Section 13 and the reason observability here is not
just latency and error rates.

---

## 10. Index Lifecycle & Versioning

### 10.1 Alias indirection

**C-3 restated:** all queries target an alias. The live collection name is never referenced by
any client, ever.

Promotion sequence:
1. Build shadow collection
2. Validate (Section 10.4)
3. Atomically retarget alias to shadow
4. Previous collection becomes N-1, retained hot

Qdrant has exposed alias APIs since v1.0 and documents alias swap as the standard zero-downtime
path for embedding-model changes; Weaviate added collection aliases in v1.32 for the same
purpose; Elasticsearch's long-standing guidance is to reference aliases rather than indices in
application code precisely because reindexing is inevitable.

### 10.2 Retention

Vector indexes hold their HNSW graph in memory, so N live copies cost roughly N times the RAM,
not N times the disk. RAM is the constrained resource.

- **N-1:** retained hot. Instant rollback via alias swap.
- **N-2, N-3, …:** snapshotted to object storage. Cold. Rollback requires an explicit restore.
- Retention count configurable. UI MUST display the memory cost when the hot-copy count is raised.

Default: 1 hot + 2 cold. Executor may propose otherwise.

### 10.3 Reindex triggers

Four, all independently configurable per KB:

1. **Manual** — user-initiated
2. **Change-detected** — source content hash differs. MUST distinguish content changes from metadata-only changes to avoid needless rebuilds.
3. **Scheduled** — cron-style
4. **Config-change** — editing chunking strategy, embedding model, or transformation tier necessarily invalidates the index. This trigger is easy to forget and MUST be implemented.

Incremental upsert is preferred where the config is unchanged and only documents differ. Full
rebuild is required when the config changes.

### 10.4 Pre-promotion validation

A shadow collection MUST NOT be promoted until: chunk count is within expected bounds, no
class produced zero chunks unexpectedly, the eval baseline has been run, and regression against
the current live baseline is below threshold. Failing validation blocks promotion and alerts.

### 10.5 Chunk identity & incremental correctness

Incremental updates are where retrieval systems quietly rot. The failure is specific: a document
is edited, its new chunks are written, and its old chunks are never removed. Retrieval then
returns stale content alongside current content, both looking equally authoritative, and nobody
notices until someone acts on a superseded procedure.

**MUST:**

- **Chunk IDs are deterministic**, derived from stable inputs (document identity, segment path, position, config version). The same document under the same config produces the same IDs on every run. Random or sequence-assigned IDs make correct incremental updates impossible.
- **Document updates are replace-by-document, not insert.** All chunks belonging to the previous version of a document are removed in the same operation that writes the new ones. Partial application is not an acceptable intermediate state.
- **Orphan detection runs after every incremental ingestion**, reporting any chunk whose source document no longer exists or no longer contains it. Non-zero orphans is an alert, not a log line.
- **Config version is part of chunk identity.** A chunking or transformation config change invalidates every chunk it produced, which is why config-change triggers a full rebuild (Section 10.3) rather than an incremental pass.

A test asserting that editing a document leaves no chunks from its prior version is part of the
deletion test layer (Section 18.2).

---

## 11. Retrieval & Serving

### 11.1 Interfaces

Three, all backed by the same retrieval service:

- **REST API** — the primitive. OpenAPI-documented.
- **MCP server** — so an agent can call the knowledge base as a tool with no glue code. This is a first-class interface, not an afterthought; it is the primary consumption path for agent workloads.
- **Python client** — thin wrapper over REST.

### 11.2 Query capabilities

- Dense, sparse, and hybrid retrieval
- Metadata filtering, including by salience tier, segment type, confidence, source, and date
- Optional reranking
- Configurable top-k and score threshold
- Per-request override of retrieval strategy within limits set by the KB config

Every response MUST include full provenance (Section 8) for every returned chunk.

### 11.3 Scaling

- Retrieval service stateless, scaled by replica count behind a load balancer
- Vector DB connection pool owned by the retrieval service; agents never hold connections
- Query-embedding cache — agent workloads repeat queries far more than human workloads, so hit rates are high
- Embedding service batches concurrent requests
- Per-tenant rate limits and quotas enforced at the retrieval service

The common misconception to design against: concurrent reads are not the hard part. Vector
search is built for them. The hard parts are reindexing under load (solved by 10.1), tenant
isolation (11.4), and embedding throughput.

### 11.4 Multi-tenant index topology

Single collection with tenant partitioning by payload field, plus a payload index on the tenant
field and shard keys keeping a tenant's data shard-local. This is documented Qdrant practice
and scales far past collection-per-tenant, which wastes memory on per-collection HNSW graphs
and degrades past a few dozen tenants.

The same mechanism provides document-level access control: permissions resolved at ingestion
into filterable fields, enforced at query time by the retrieval service.

**MUST:** tenant filtering is enforced server-side in the retrieval service and is not
overridable by any client-supplied parameter. A test MUST exist that attempts cross-tenant
retrieval and asserts failure.

### 11.5 Query explain

Section 13.1 states the system must always be able to answer "why did retrieval return that."
That principle needs a feature behind it, or it is an aspiration.

An explain mode — a flag on the query API and a view in the UI — MUST return, for a given
query: the parsed query and any expansion applied, the filters in effect and where each came
from (request, KB config, tenancy, permissions), each candidate chunk with its raw and reranked
scores, which chunks were excluded and by which filter, the retrieval strategy used, and full
provenance for every candidate including transformation history and confidence.

This is the debugging tool for the product's central failure mode — retrieval returning the
wrong thing for reasons nobody can see. It is also how a KB editor decides whether a
disappointing answer is a chunking problem, a filter problem, or a source-document problem.

Explain output MUST respect the same tenancy and permission rules as ordinary retrieval. It is
not a bypass.

---

## 12. Data Contracts

The executor defines the schemas. This spec defines the invariants each contract MUST satisfy.
Contracts are versioned; a stage may not consume a contract version it does not declare support
for.

| Contract | Between | Invariants |
|---|---|---|
| **Inventory** | Collect → Assess | Every file has a stable identity, content hash, source path, and source-system metadata. Duplicate and version-family relationships are explicit. |
| **Parse result** | Assess → Decompose | Every parse carries a quality score and a per-page or per-region confidence. Failures are represented, not dropped. Every extraction traces to a source location. |
| **Segment set** | Decompose → Plan | Every segment has a type, salience tier, structural path, and source location. Segments of a document reassemble to the document in order. No content is lost between parse result and segment set except explicitly recorded exclusions. |
| **Ingestion config** | Plan → Build | Fully determines Build output. Complete — no implicit defaults resolved at build time. Serializable, diffable, re-importable. Carries the provenance of each recommendation (heuristic vs. sweep-backed). |
| **Chunk** | Build → Serve | Satisfies Section 8 in full. Chunk text is byte-identical to source unless a Tier 3 transformation is recorded in its transformation list. Chunk ID is deterministic under Section 10.5 and includes config version. |
| **Eval set** | ⟷ Plan, Serve | Every question carries its generation method, review status, and source segments. Provisional status is inseparable from the data. |

---

## 13. Observability

### 13.1 Principle

Full and thorough observability across every domain where it makes sense. The question the
system must always be able to answer is **"why did retrieval return that?"**

### 13.2 Required telemetry

**Ingestion:** documents processed, failed, excluded by reason; parse quality distribution;
segment type distribution; transformation counts by tier; token and cost accrual; stage
durations.

**Index:** collection sizes, memory footprint, hot and cold version inventory, time since last
reindex, promotion and rollback history.

**Retrieval:** QPS, latency percentiles, error rates by cause, cache hit rate, top-k
distribution, filter usage, per-tenant volume, zero-result rate.

**Quality (the important one):** eval baseline scores over time, regression against retained
baselines, drift after each reindex, retrieval score distributions, and rate of queries
returning only low-confidence or low-salience chunks.

**Governance:** every break-glass grant and read, every config change with actor and diff,
every promotion and rollback, every permission change.

### 13.3 Interfaces

Structured logs, OpenTelemetry traces spanning ingestion and retrieval, Prometheus-compatible
metrics, and an in-product dashboard. Per-tenant metric views for Workspace Owners;
cross-tenant only for Platform Admin.

---

## 14. Security & Content Trust

### 14.1 Ingested content is untrusted input

This is the most under-considered risk in the product and it follows directly from the
boundary: the platform ingests arbitrary documents and returns their contents to autonomous
agents. A document containing text addressed to a model — "ignore previous instructions,"
fabricated system messages, instructions to exfiltrate — will be embedded, retrieved, and
delivered into an agent's context as though it were knowledge.

The platform cannot control what callers do with chunks. It CAN refuse to launder untrusted
content into trusted-looking content. Requirements, all MUST:

- **Chunks are labelled, not sanitized.** Retrieval responses mark content as retrieved untrusted material. The platform does not strip or rewrite suspicious text — that is both unreliable and destructive.
- **Injection-pattern detection at ingestion.** Segments containing imperative model-directed language, embedded role markers, or instruction-shaped content are flagged in provenance with a suspicion score. The score is retrievable and filterable, and is not used to silently exclude.
- **Invisible-content detection.** White-on-white text, zero-size fonts, off-page positioning, and metadata-only text are a known PDF injection vector and MUST be detected and flagged at parse time.
- **Ingestion-time model calls are isolated.** Classification, description generation, and rewriting all pass untrusted document text through a model. Those prompts MUST treat document content as data, never as instruction, and their outputs MUST be schema-validated rather than free-form and trusted. A document that can steer the classifier can steer the whole pipeline.
- **The MCP tool description states the trust level** so a consuming agent has the information at the point of use.

Documentation MUST state plainly that this platform serves retrieval and cannot guarantee the
safety of downstream generation, and that the caller owns that boundary.

### 14.2 Secrets and credentials

- Connector credentials, API keys, and model provider keys MUST be stored encrypted at rest and MUST NOT appear in logs, traces, config exports, or error messages.
- Ingestion config exports (Section 6.4) MUST be secret-free by construction so they can be committed to version control.
- Service principal keys are scoped to a single knowledge base, revocable, rotatable, and expiring. Their last-used time is recorded.
- Connector credentials belong to a workspace, not a user, so a departing employee does not break ingestion.

### 14.3 Source permission fidelity

When a connector ingests from a system with its own access controls, the platform MUST record
source-side permissions and either mirror them into filterable permission fields or refuse the
connection.

The failure to prevent: a SharePoint site with restricted folders is ingested wholesale, and
the knowledge base becomes a permission-laundering machine that answers questions the asker
was never entitled to ask. Where a connector cannot supply reliable permission data, the
platform MUST say so and require an explicit acknowledgement before ingestion.

### 14.4 Sensitive content detection

PII and sensitive-category detection at ingestion, surfaced in the findings report and
available as a filterable field. Detection is advisory — the platform reports what it found and
lets the team decide. It MUST NOT redact by default, because silent redaction violates the
provenance invariant.

---

## 15. Failure Semantics & Degradation

Every failure mode has a specified behaviour. Unspecified failure behaviour becomes whatever
the implementation happened to do.

| Failure | Required behaviour |
|---|---|
| Embedding provider unavailable mid-ingestion | Pause and retry with backoff; job resumable; no partial shadow collection promoted |
| Embedding provider unavailable at query time | Fail closed with a clear error. MUST NOT silently fall back to a different model — mismatched vector spaces return plausible garbage |
| Vector DB unavailable | Retrieval fails closed with a distinguishable error code; ingestion pauses |
| Single document fails to parse | Recorded, reported, ingestion continues |
| Whole content class fails | Ingestion halts and alerts. A class-wide failure is a config problem, not a data problem |
| Shadow collection fails validation | Promotion blocked, alias unchanged, alert raised, shadow retained for inspection |
| Alias swap fails mid-operation | Previous alias target remains authoritative; operation is idempotent and retryable |
| Ingestion worker dies | Job resumes from last checkpoint without duplication or loss |
| Rate limit hit at provider | Backoff, surface remaining budget, do not fail the job |
| Query returns zero results | Distinguish "no matches" from "filtered to nothing" from "error" in the response |

**Governing principle: fail closed and loud.** A retrieval system that returns something
plausible when it should have returned an error is worse than one that errors, because the
failure propagates silently into an agent's answer.

---

## 16. Cost & Resource Governance

Ingestion and sweeps spend real money on tokens and compute, and the primary user is the person
least equipped to notice. Requirements:

- **Estimate before execute.** Every operation that spends materially — ingestion, reindex, sweep, Tier 3 rewriting — presents an estimate before it starts. Estimates state their basis and their uncertainty.
- **Budget caps.** Per knowledge base and per workspace, configurable by Platform Admin, enforced by the platform. Hitting a cap pauses rather than fails, and alerts.
- **Confirmation thresholds.** Operations above a configurable cost require explicit confirmation. Easy mode users get this in plain language with a currency figure, not a token count.
- **Attribution.** Spend is attributed per knowledge base, per workspace, and per operation type, and is visible in the dashboard.
- **Memory accounting.** Index memory footprint per KB is displayed, and the cost of additional hot versions (Section 10.2) is shown at the point the setting is changed.
- **Runaway protection.** A scheduled reindex on a large corpus with an expensive model can recur indefinitely. Scheduled operations MUST respect budget caps and MUST alert on repeated cap-hits rather than quietly stopping.

---

## 17. Data Lifecycle & Deletion

### 17.1 Deletion must be complete

When a document is deleted, every derived artifact MUST be removed or the deletion is a lie.
That includes segments, chunks, embeddings in the live collection, embeddings in the retained
hot N-1 collection, generated augmentation fields, and eval questions derived from it.

**Cold snapshots are the hard case.** Snapshots in object storage (Section 10.2) contain
deleted content. Requirements:

- Deletion requests are recorded in a durable tombstone log.
- Restoring a snapshot MUST replay the tombstone log before the restored collection is eligible for promotion.
- Snapshot retention periods MUST be documented, since they bound how long deleted content can persist.
- A `purge` operation exists that additionally destroys affected snapshots, for genuine right-to-erasure requests.

The distinction between "deleted from service" and "purged from all copies" MUST be visible in
the UI. Presenting the first as the second is the kind of thing that turns into a regulatory
problem.

### 17.2 Retention and residency

- Source document retention is configurable per KB: retain originals, or retain only derived artifacts after successful ingestion.
- All storage locations are declared in config. No content leaves the configured boundary.
- Deletion of a knowledge base or workspace cascades completely and is confirmed by an auditable record of what was destroyed.

### 17.3 Export

A knowledge base MUST be fully exportable: ingestion config, class descriptions, eval sets,
findings reports, and — optionally — chunks with provenance. Users of a self-hosted open-source
platform should never feel locked in, and the export doubles as the backup and migration path.

---

## 18. Test Strategy

Testing is not a phase. Each build phase in Section 19 is complete only when its tests pass.

### 18.1 Golden corpus

A version-controlled fixture corpus MUST be built in Phase 0, containing at minimum: a clean
native PDF; a poorly scanned PDF with known-degraded regions; a bloated manual with
cross-references, boilerplate, and mixed content types in one file; a document with complex and
nested tables; a spreadsheet of each of the three kinds; a Confluence-style HTML export; a
document with heavy near-duplicate siblings; several unservable files; and **an adversarial
document containing injection-shaped text and invisible content** for Section 14.1 testing.

Fixtures MUST be synthetic or licence-clear. Do not seed the repository with real client
documents.

This fixture is the substrate for most integration testing and is the single highest-leverage
early investment in the build.

### 18.2 Test layers

| Layer | Focus |
|---|---|
| **Unit** | Per-module logic. Executor's discretion. |
| **Contract** | Each contract in Section 12 validated at every producer/consumer boundary. A stage MUST reject malformed input rather than degrading. |
| **Property** | Invariants that must hold for all inputs: segments reassemble to the source document; chunk text is byte-identical unless Tier 3 is recorded; provenance is complete for every chunk; no content vanishes silently between stages. |
| **Integration** | Full pipeline over the golden corpus. Snapshot-tested outputs. |
| **Retrieval quality** | Eval harness against the golden corpus with a known-good question set. Regression here fails the build. |
| **Tenancy isolation** | Cross-tenant retrieval attempts MUST fail. Break-glass MUST produce audit records. Negative tests are mandatory. |
| **Concurrency** | Query load sustained across an alias swap with zero failed requests and no stale-collection reads. |
| **Resumability** | Ingestion killed mid-run resumes without duplication or loss. |

Add to the layer table:

| Layer | Focus |
|---|---|
| **Security** | Injection-shaped and invisible content is flagged, not stripped. Ingestion-time model calls resist steering by document content. Secrets absent from logs, traces, and config exports. |
| **Failure injection** | Every row of the Section 15 table has a test that induces the failure and asserts the specified behaviour. |
| **Provider parity** | The golden-corpus integration suite runs against both a cloud and a local embedding provider. A feature that passes under one and not the other is a defect. |
| **Deletion** | Deleted content is absent from live, from N-1, and — after snapshot restore — from the restored collection. |

### 18.3 Non-negotiable tests

These MUST exist and MUST pass before any release:

1. Alias swap under sustained query load — zero errors.
2. Cross-tenant retrieval attempt — fails closed.
3. Provenance completeness — every chunk from the golden corpus, no exceptions.
4. Tier 2 byte-identity — augmented chunks return verbatim source text.
5. Break-glass audit — every content read under grant produces a record.
6. Rollback — alias reverts to N-1 and serves correctly.
7. Embedding model mismatch — a query against an index built with a different model fails closed rather than returning results.
8. Deletion completeness — deleted content absent from live, N-1, and post-restore collections.
9. Injection flagging — the adversarial fixture is flagged and its suspicion score is retrievable.
10. Secret hygiene — no credential appears in any log, trace, error, or config export.

---

## 19. Build Sequence

End-to-end thin slice first, then depth. Each phase produces something runnable.

**Standing requirement, every phase:** the `/docs` wiki is updated as part of the phase — new
architecture pages, contract schemas, runbooks, configuration options, and an ADR for every
non-obvious decision. Documentation is an acceptance criterion, not a deliverable that trails
the code. The wiki is also the executor's own reference across phases, so a later phase should
be able to answer questions about an earlier one by reading `/docs` rather than re-reading
code.

### Phase 0 — Foundations
Repository, core library skeleton, contract definitions with validation, golden corpus fixture
(including the adversarial document), test harness, CI pipeline with branch protection and
required checks, PR template, `/docs` scaffold, single-file config, `docker compose up`
bring-up.

**Tenancy fields MUST be present in every contract from Phase 0**, even though enforcement
arrives in Phase 4. Retrofitting tenant identity into chunk payloads, provenance, and audit
records after an index exists means reindexing everything and rewriting every query path. This
is the most expensive mistake available in this build sequence.

**Acceptance:** contracts validate and carry tenancy fields; empty pipeline runs end to end
over the fixture; `docker compose up` works from a clean clone; `/docs` scaffold exists with
architecture, contracts, and configuration pages populated for what has been built; CI green.

### Phase 1 — Thin end-to-end slice
Single source type (local directory), single content type (native-text PDF prose), naive
chunking, **one cloud and one local embedding provider behind the provider abstraction**,
Qdrant, alias-based promotion, REST retrieval, the
Section 15 failure behaviours for the paths that exist. No UI, no tenancy enforcement, no
recommender. **Acceptance:** documents in one end, provenance-complete chunks out the other via
alias-backed endpoint; alias swap test passes; the same corpus ingests and serves correctly
under both providers; ingestion throughput baseline recorded per provider;
embedding-mismatch test passes.

### Phase 2 — Assessment & decomposition
Full Stage 2 and Stage 3: parse quality, OCR confidence, boilerplate detection, dedup, segment
decomposition, typing, salience tiering, quality findings report, exclusion report,
injection-pattern and invisible-content detection. **Acceptance:** golden corpus produces a
correct findings report; segments reassemble; spreadsheet triage classifies all three kinds
correctly; adversarial fixture is flagged.

### Phase 3 — Planning & transformation
Content handling matrix, transformation tiers 1 and 2, table description generation, class
descriptions, recommendation engine (heuristic), ingestion config export/import, preview, cost
estimation for ingestion. **Acceptance:** per-class routing plan generated over golden corpus;
Tier 2 byte-identity test passes; config round-trips and is secret-free; cost estimate shown
before ingestion.

### Phase 4 — Tenancy, lifecycle & serving
Three-level tenancy enforcement, RBAC, break-glass with audit, multi-tenant index topology,
reindex triggers, versioning and retention, pre-promotion validation, deletion and tombstone
log, MCP server, rate limiting, budget caps. **Acceptance:** isolation and break-glass tests
pass; all four reindex triggers work; rollback test passes; deletion completeness test passes;
an agent queries via MCP and sees the trust label.

### Phase 5 — Evaluation
Eval generation, provisional labelling, review interface, configuration sweep with cost
estimation, ranked recommendations, drift detection against retained baselines.
**Acceptance:** sweep produces ranked table with cost estimates; drift alert fires on induced
regression; provisional labelling is inescapable.

### Phase 6 — UI & connectors
Web UI with Easy and Proficient modes over the same engine, dashboards, SharePoint / Atlassian
/ Google Drive connectors, onboarding flow. **Acceptance:** a non-technical user completes
create-KB-to-endpoint in Easy mode without documentation; Proficient mode exposes every value
the recommender set.

### Phase 7 — Hardening
Tier 3 rewriting with diff preview, additional backend adapters, Helm chart, air-gapped
deployment, performance work, security review.

### Release gate — v1

Phases completing is not the same as the product being ready. v1 ships when all of the
following hold:

1. All ten non-negotiable tests (Section 18.3) pass on main, on every supported backend and both provider classes.
2. A person who has never seen the repository reaches a working retrieval endpoint from the golden-path tutorial alone, within the Section 4.5 cold-start target.
3. The performance targets in Section 4.5 are met on the reference deployment and recorded.
4. Every failure row in Section 15 has a passing test.
5. `/docs` covers every shipped feature, with no page contradicting current behaviour, and an ADR exists for every non-obvious decision.
6. A security review of Sections 14 and 17 has been completed by a reviewer that did not write the code, with findings closed or explicitly accepted.
7. Every Section 20 open decision is closed or consciously deferred with its deferral recorded.
8. Upgrade and rollback paths are tested end to end, not just designed.

Anything not met is either fixed or documented as a known limitation before release. A known
limitation stated plainly is acceptable; one discovered by a user is not.

---

## 20. Open Decisions

Deferred deliberately; each needs a call before the phase that depends on it.

| # | Decision | Needed by |
|---|---|---|
| 1 | Licence model — which OSS licence, and is anything held back? | Phase 0 |
| 2 | Which cloud and which local embedding providers are the reference pair, and what the first-run prompt offers | Phase 1 |
| 3 | Segment type taxonomy — executor proposes in the design document | Phase 2 |
| 4 | Break-glass grant window default | Phase 4 |
| 5 | Snapshot retention period, and its interaction with erasure obligations | Phase 4 |
| 6 | Whether cross-KB retrieval within a workspace is supported, and its permission semantics | Phase 4 |
| 7 | Corpus scale ceiling for v1, and sampling behaviour above it | Phase 5 |
| 8 | Authentication model for the UI — local accounts, OIDC, or both | Phase 6 |
| 9 | Confirm `finecorpus` and the `corpus` CLI binary are free at registration time, and secure a domain | Before public release |

---

## Appendix A — References

- Kartal et al., *RAGSmith: A Framework for Finding the Optimal Composition of RAG Methods Across Datasets*, arXiv:2511.01386
- Zeng et al., *AutoRAGTuner: A Declarative Framework for Automatic Optimization of RAG Pipelines*, arXiv:2605.02967
- *A Systematic Investigation of Document Chunking Strategies and Embedding Sensitivity*, arXiv:2603.06976
- Vectara, chunking/embedding interaction study, NAACL 2025, arXiv:2410.13070
- de Moura Júnior et al., *Adaptive Chunking: Optimizing Chunking-Method Selection for RAG*, LREC 2026
- Qdrant documentation — collection aliases, multi-tenancy, payload indexing, custom sharding
- Weaviate documentation — collection aliases (v1.32+)
- Elasticsearch guide — index aliases and reindexing

---

## Appendix B — Glossary

| Term | Meaning |
|---|---|
| **Segment** | A typed span of content within a document. The routing unit of the system. |
| **Salience tier** | A segment's default retrieval weight class: `primary`, `supporting`, `boilerplate`, `excluded`. |
| **Class description** | Free-text user input describing what a segment class is and what people need from it. |
| **Shadow collection** | An index being built, not yet serving traffic. |
| **Alias** | The stable name all clients query. Points at exactly one collection; retargeted atomically. |
| **Hot version (N-1)** | The previous collection, retained in memory for instant rollback. |
| **Cold version** | A snapshot in object storage; requires explicit restore. |
| **Transformation tier** | 1 = structure normalization, 2 = contextual augmentation, 3 = full rewriting. |
| **Provisional baseline** | An evaluation result scored against an unreviewed, generated eval set. |
| **Break-glass** | Time-bound, audited, notified content-read access for a Platform Admin. |
| **Unservable** | Content the platform can detect but cannot usefully index; excluded and reported. |
