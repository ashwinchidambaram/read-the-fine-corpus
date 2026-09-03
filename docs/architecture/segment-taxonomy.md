# Segment Type Taxonomy

**Status:** Accepted — design PR #1 approved; D-03 CLOSED 2026-09-03 (blocking tension OQ-11 resolved by owner ruling D-25 same date)
**Spec cross-references:** §6.3, §6.4, §6.5, §7, §8, §12, §18.1
**Phase target:** Phase 2

---

## 1. Purpose and scope

This document is the executor's answer to Open Decision #3: the definitive, closed segment type taxonomy for Read The Fine Corpus. Every segment produced by the Decompose stage (§6.3) MUST carry exactly one type from this list.

The taxonomy drives routing: segment type is the primary input to the content handling matrix (§6.4) and, together with the class description (§6.5) and structural position, determines salience tier. Every segment, regardless of tier, is indexed — this taxonomy never causes content to be discarded.

The taxonomy is intentionally small. Types that cannot be given meaningfully different handling rules are collapsed. Types that the golden corpus or the content handling matrix requires but the spec's starter list omits are added explicitly.

---

## 2. Segment type definitions

### 2.1 Closed type list

Twelve content types plus one sentinel, for a total of **13 types**.

| Identifier | Definition | Concrete example | Default handling notes |
|---|---|---|---|
| `prose` | Continuous natural-language text that conveys the main informational content of a document or section. Paragraphs, narrative, explanations, procedures. | The "Installation" section of a software README; a policy document paragraph; the body of a memo. | Structure-aware chunking at heading boundaries. Parent breadcrumb augmentation default-on where heading structure exists (§6.4). |
| `heading` | A structural label that titles a section of the document without itself containing substantive content. Includes headings at all levels and section titles. | "3.2 Configuration Reference"; "Appendix A — References"; "Chapter 4: Safety Procedures". | Not independently chunked. Contributes to the structural path (breadcrumb) carried by every other segment in its scope. Stored in provenance; not emitted as a standalone chunk unless the document has no prose at all. **Open question OQ-1.** |
| `table` | A grid of data with rows and columns, where the header row(s) define the semantics of the data. | A specifications table listing part numbers, tolerances, and units; a pricing matrix; a comparison table of configuration options. | Atomic: MUST NOT split rows from headers (§6.4). A generated natural-language description is stored in a separate augmentation field (Tier 2). The verbatim table is returned to the caller. For oversized tables, headers are repeated into each fragment. |
| `list` | An ordered or unordered enumeration of items, not structured as a table. May be nested. | A bulleted list of prerequisites; a numbered step sequence (when not embedded in `prose`); an inventory list. | Chunked as a unit where small enough. Where a list is long, split at top-level items, not sub-items. Parent heading breadcrumb augmented as for `prose`. |
| `code` | A verbatim literal block intended to be read and executed or interpreted, not paraphrased. Source code, shell commands, configuration file snippets, query strings, data format examples. | A Python function body in a code fence; a YAML configuration block; a SQL query in a tutorial. | Syntax-aware splitting at function/class/declaration boundaries (§6.4). Language detected and stored as a metadata field. Not rewritten under any tier; verbatim is the point. |
| `figure_caption` | The textual label accompanying a figure, chart, diagram, or image. Includes both the caption text and any alt-text or title attribute associated with the visual. | "Figure 3: Monthly active user growth, Q1–Q4 2025."; "Diagram showing the three-tier architecture." | Stored with a reference to the adjacent `figure_region` or `scanned_region` segment. Independently indexed because it is often the only searchable text near a figure. |
| `figure_region` | A non-textual visual element — a chart, diagram, drawing, photograph, or other image — embedded in a document. Does not include pure text rendered as an image, which is `scanned_region`. | A bar chart of quarterly results in a report spreadsheet; an architecture diagram in a PDF; a photograph in a product manual. | For chart-bearing spreadsheet report sheets, chart-adjacent commentary is treated as `prose` (§6.4 spreadsheet triage). The visual itself is flagged as potentially unservable (§7.5) if no caption or alt-text is present. A generated description may be produced via Tier 2 augmentation where the platform can interpret the visual. **Open question OQ-2.** |
| `form_field` | A named input element and its associated label or prompt, as found in form-structured documents. May contain a value if the form has been filled out. | "Employee name: ___"; "Signature date: 2025-09-01"; a checked checkbox labelled "Reviewed". | Typically chunked as label-value pairs. Field names and values are both indexed. Salience depends heavily on class description: a filled incident-report form is `primary`; a blank template is likely `boilerplate`. |
| `boilerplate` | Text that appears verbatim or near-verbatim across many documents in the corpus, carrying no document-specific informational content. Headers, footers, legal preambles, copyright notices, standard disclaimers, repeated navigation chrome in HTML exports. | "Confidential — Property of Acme Corp. Not for distribution."; page number headers; a legal disclaimer block on every page of a contract template. | Boilerplate is handled **structurally, never by intra-chunk byte removal.** Corpus-wide boilerplate detection (§6.2) is the primary signal. Decompose emits corpus-repeated content as its own `boilerplate`-typed segments; those segments' chunks carry byte-identical `text` and are assigned salience tier `boilerplate` by default, so default retrieval filters them out. **No Tier 1 operation removes bytes from another chunk's `text`.** The boilerplate text is retained byte-identical in its own chunk (and in provenance), never stripped from a neighbouring chunk. |
| `front_matter` | Structured metadata at the start of a document: title page, author list, version information, document number, effective date, abstract, table of contents, and similar preamble material. Distinct from `boilerplate` in that it is document-specific, and from `prose` in that it is primarily navigational or administrative rather than informational. | The title page of a policy document; a table of contents; an abstract; "Document no. OP-2025-004, Rev. 3, Effective 2025-06-01". | Stored and indexed, not stripped. Document-level metadata fields are populated from front matter where parseable. Salience tier is `supporting` by default — it is navigational rather than the answer to most queries, but it is not boilerplate. |
| `revision_history` | A structured or semi-structured block recording the change history of a document: version numbers, dates, authors, and change descriptions. | A "Revision History" table at the end of a specification; a "Change Log" section listing "v2.1 — 2025-03-15 — Added Section 6.4". | Indexed but down-weighted by default. Often near-boilerplate in structure but document-specific in content. Distinguished from `boilerplate` because its content is unique per document. Distinguished from `table` and `front_matter` because its semantics are specifically about document change history. Salience tier is `supporting` by default. |
| `cross_reference` | A pointer within a document to another location — another section, figure, table, or external document — that carries no substantive content itself. Includes inline citations, footnote references, and "See also" entries. | "See section 4.2 for configuration details."; "Refer to Table 3 on page 12."; "As defined in ISO 27001:2022."; a numbered footnote reference. | Silent loss is explicitly prohibited (§6.4). At ingestion, the platform MUST either: (a) resolve the reference inline — annotating the segment with the target segment's identifier — or (b) store the unresolved reference text as a structured field in provenance with resolution status `unresolved`. The referencing segment is always indexed. Resolution attempts are recorded. **Open question OQ-3.** |
| `scanned_region` | A span of content captured by OCR from a non-native-text source: a scanned page, a photographed document, a rasterised image of text. Distinguished from native-text content by origin, not appearance. | A scanned appendix in a PDF manual; a photographed receipt; a faxed document converted to PDF. | Per-page OCR confidence stored in payload (§6.4, §8). Low-confidence segments are down-weighted at retrieval and flagged in provenance. Tier 1 cleanup applied. May overlap other types (a scanned region may contain a table or a form) — in that case the type is `scanned_region` and the sub-structure is recorded in the structural path and augmentation fields. **Open question OQ-4.** |
| `unknown` | A segment whose type cannot be determined by the decomposer with sufficient confidence. | A block of content whose structure matches no known type; a parse output region with ambiguous signals. | Never silently dropped. Flagged for human review in the findings report. Indexed at salience tier `supporting` by default so it participates in retrieval. A segment of type `unknown` with unresolved type after human review is a pipeline defect and MUST appear in the quality report. |

### 2.2 Type exclusivity

Each segment has exactly one type. Where content is ambiguous (for example, a paragraph that contains both prose and an inline code snippet), the dominant content type wins and the minority content is not separately segmented unless it spans more than a configurable minimum length. **Open question OQ-5.**

### 2.3 Type granularity rationale

Types not in this list and why:

- **Footnote / endnote** — footnotes are either `prose` (where they contain substantive content) or `cross_reference` (where they are citation pointers). A separate type adds routing complexity without enabling meaningfully different handling.
- **Abstract / summary** — a first-class abstract is `front_matter`. An in-body summary section is `prose`. The routing treatment does not differ enough to warrant a type.
- **Equation / formula** — treated as `code` for routing purposes. Rendering fidelity is a display concern, not a retrieval concern. **Open question OQ-6.**
- **Sidebar / callout** — these are `prose` with a structural annotation in the breadcrumb path. The routing treatment is identical.
- **Spreadsheet cell range** — spreadsheet report sheets are decomposed into the types above (table, prose, figure_region, figure_caption). Spreadsheet database and model kinds are excluded at the document level before segment decomposition begins (§6.4).

---

## 3. Salience tiers

Tier assignment controls default retrieval behaviour. It does not control indexing. Every segment from every document is indexed regardless of tier.

### 3.1 Tier definitions and retrieval behaviour

| Tier | Meaning | Default retrieval behaviour | Filter vs. down-weight |
|---|---|---|---|
| `primary` | The segment carries the main substantive information users are most likely to query. Core instructional prose, the body of a policy, the text of a contract clause, a specifications table. | Included in all retrieval by default. Full score weight. | Neither — always in play unless a caller explicitly excludes it. |
| `supporting` | The segment carries context that aids understanding but is not the direct answer to most queries. Introductions, cross-references with resolved targets, revision history, front matter, appendices with reference material, figure captions. | Included in default retrieval. Score may be down-weighted by a configurable factor relative to `primary`. | Down-weighted. Configurable at KB level; caller may override per query. |
| `boilerplate` | The segment is corpus-wide repeated text with no document-specific informational content. Copyright notices, page headers/footers, standard disclaimers, nav chrome. | Excluded from default retrieval. Retained in the index and retrievable by explicit filter. | Filtered out by default. |
| `excluded` | The segment is unservable or has been explicitly excluded by user configuration. Scanned regions below the minimum OCR confidence threshold (user-configurable), superseded near-duplicate documents when the `ingestion.dedup.index_superseded_versions` toggle is on, content the user has manually excluded. | Excluded from retrieval by default. Retained in the index for provenance and for re-inclusion if the exclusion decision changes. Never permanently discarded at ingestion. | Filtered out by default. Must be explicitly included by caller filter. |

### 3.2 The indexing invariant

Everything admitted to the pipeline is indexed. Tier controls default retrieval behaviour only. This is the correct answer to the bloated-manual problem (§6.3): tier assignments can be changed in config; discarded content cannot be recovered without re-running ingestion.

The `excluded` tier is not a deletion. An excluded segment retains full provenance and appears in the segment set. It does not appear in retrieval results by default.

**Exception — superseded near-duplicate documents (owner ruling 2026-09-03, D-25):** By default, documents whose `dedup_role=superseded` are not admitted to decomposition and produce no segments and no chunks. They are inventoried, retained in object storage, and reported in the exclusion report. When the per-KB toggle `ingestion.dedup.index_superseded_versions=true` is set, superseded documents are decomposed and their segments are indexed at tier `excluded`. This exception does not contradict §6.3's salience-not-pruning principle: §6.3 governs segments that enter the pipeline; the ruling governs whether superseded docs enter the pipeline at all.

### 3.3 Tier semantics for the `unknown` type

A segment of type `unknown` is assigned tier `supporting` until its type is resolved by human review. This ensures it participates in retrieval (it might be important) without being promoted to the top result weight. After resolution, the tier is reassigned according to the resolved type's signal matrix.

---

## 4. Tier assignment signal matrix

Tier assignment is computed at Decompose time (§6.3). The following signals are evaluated in precedence order: the first signal that fires at a given precedence level wins. Lower precedence signals apply only when no higher-precedence signal has fired.

### 4.1 Signal definitions

| Signal | Source | What it contributes |
|---|---|---|
| **Explicit user exclusion** | KB config or per-document override | Hard `excluded`. Highest precedence. Cannot be overridden by any other signal. |
| **Unservable detection** | §7.5 content class detection | Hard `excluded`. Fires on: spreadsheet database kind, spreadsheet model kind, encrypted content, CAD files, audio, video, image-only content with no caption. |
| **OCR confidence below threshold** | Parse result (§6.2) | `excluded` when below the configured floor; `supporting` with low-confidence flag when between floor and a configurable warning level. Threshold is user-configurable; default is executor's proposal. **Open question OQ-7.** |
| **Class description** | §6.5 user input | Moves tier in the direction implied by the description's expressed importance. Processed via LLM classification against the description text. This is the single highest-value user signal and overrides structural priors when present. |
| **Corpus-wide boilerplate detection** | §6.2 dedup machinery | Moves to `boilerplate` when a text block appears in more than a configurable proportion of corpus documents. Default threshold: executor proposes. **Open question OQ-8.** |
| **Segment type prior** | Type from §2 | Default tier per type (see §4.2 below). Applied when no higher-precedence signal has fired. |
| **Structural position** | Position within document heading hierarchy | Modifies the type prior. Content under a heading whose title matches a boilerplate-pattern list (e.g., "Revision History", "Legal Notice") is nudged toward `boilerplate` or `supporting`. Content under the first heading or the primary body area is nudged toward `primary`. |
| **No signal** | — | Default tier when no signal fires: `supporting`. Never `primary` by default — promotion to `primary` requires an affirmative signal. |

### 4.2 Default tier by segment type

These are the type priors applied when no higher-precedence signal has fired.

| Segment type | Default tier | Rationale |
|---|---|---|
| `prose` | `primary` | Main informational content by definition. |
| `heading` | *(not independently chunked)* | Contributes to breadcrumb. No standalone tier. **Open question OQ-1.** |
| `table` | `primary` | Tables are substantive content. Down-grade to `supporting` only if class description or position indicates otherwise. |
| `list` | `primary` | Enumerated content is typically informational. |
| `code` | `primary` | Code blocks are the authoritative form of technical content. |
| `figure_caption` | `supporting` | Captions add context; they are not the answer by themselves. |
| `figure_region` | `supporting` | The visual itself may not be indexable; the caption or adjacent prose carries retrieval weight. |
| `form_field` | `supporting` | Tier is highly context-dependent; promote to `primary` via class description if the corpus is form-centric. |
| `boilerplate` | `boilerplate` | By definition. |
| `front_matter` | `supporting` | Navigational, not informational. |
| `revision_history` | `supporting` | Document-specific but rarely the answer to a content query. |
| `cross_reference` | `supporting` | A pointer, not content. May be promoted if the reference is resolved and the target is `primary`. |
| `scanned_region` | `supporting` | Pending OCR confidence; confidence signal then takes over. |
| `unknown` | `supporting` | Conservative default pending review. |

### 4.3 Precedence order (explicit)

When multiple signals fire, the signal highest in this list governs tier assignment. Lower signals are recorded in provenance as contributing evidence even when they do not win.

1. Explicit user exclusion (hard `excluded`)
2. Unservable detection (hard `excluded`)
3. OCR confidence below floor (hard `excluded`)
4. Class description (overrides structural priors)
5. Corpus-wide boilerplate detection (`boilerplate`)
6. OCR confidence warning level (`supporting` + flag)
7. Segment type prior (see §4.2)
8. Structural position modifier (adjusts prior up or down one level)
9. Default: `supporting`

### 4.4 Conflict rule

When a class description signal and a boilerplate detection signal fire on the same segment, the class description wins (precedence 4 > 5). Rationale: the user has explicitly told the system what this content is worth; automatic detection is a fallback.

When structural position would promote a segment to `primary` but the type prior is `boilerplate`, the type prior wins (precedence 7 > 8 for downward movement). A heading position cannot promote `boilerplate`-typed content.

All firing signals and the winner are recorded in the segment's provenance.

---

## 5. Coverage mapping

### 5.1 Content handling matrix coverage (§6.4)

Every row of the §6.4 content handling matrix maps to one or more segment types and an expected tier.

| §6.4 content type | Segment type(s) | Expected tier(s) | Notes |
|---|---|---|---|
| **Prose** | `prose` | `primary` | Structure-aware chunking. Breadcrumb augmentation default-on. |
| **Tables** | `table` | `primary` | Atomic chunking with header repetition for large tables. Tier 2 natural-language description generated. |
| **Scans** | `scanned_region` | `supporting` (OCR confidence then governs) | Per-page confidence in payload. Low-confidence down-weighted. |
| **Spreadsheets — Report kind** | `table`, `prose`, `figure_region`, `figure_caption`, `front_matter` | Varies by sub-type | Sheet-as-section. Chart-adjacent commentary is `prose`. Chart visuals are `figure_region`. |
| **Spreadsheets — Database kind** | *(document-level exclusion)* | `excluded` | Excluded before decomposition. Reported as unservable. No segments produced. |
| **Spreadsheets — Model kind** | *(document-level exclusion)* | `excluded` | Excluded before decomposition. Reported as unservable. No segments produced. |
| **Web links** | `cross_reference` | `supporting` | Fetch policy controls whether link content is fetched and ingested as a separate document. The reference itself is always recorded. |
| **Cross-references** | `cross_reference` | `supporting` | Resolve inline or record as unresolved. Silent loss prohibited (§6.4). |
| **Code** | `code` | `primary` | Syntax-aware splitting at function/class boundaries. |
| **Unservable content** | *(document-level exclusion)* | `excluded` | CAD, audio, video, image-only PDFs, encrypted files, spreadsheet non-report kinds. Excluded at assessment, not decomposed. |

### 5.2 Golden corpus fixture coverage (§18.1)

Every named fixture in §18.1 is mapped here. The fixture list in §18.1 uses descriptions rather than filenames; the mapping uses the description as the fixture identifier.

| §18.1 fixture description | Segment types expected | Tiers expected | Notes / findings |
|---|---|---|---|
| Clean native PDF | `prose`, `heading`, `table` (if present), `list` (if present), `front_matter` (if present) | `primary`, `supporting` | The baseline happy-path case. All segment types should be cleanly extracted. |
| Poorly scanned PDF with known-degraded regions | `scanned_region`, `prose` (for any native-text pages), `heading` (if recoverable) | `supporting` (scanned, pending OCR); `excluded` (below OCR floor for degraded regions) | OCR confidence field must be populated per region. Degraded regions must be flagged, not silently included at full weight. |
| Bloated manual with cross-references, boilerplate, and mixed content types | `prose`, `heading`, `table`, `list`, `boilerplate`, `front_matter`, `revision_history`, `cross_reference`, `figure_caption`, `figure_region` | All four tiers expected | This is the primary integration test for the full taxonomy. Every type except `code`, `form_field`, `scanned_region`, and `unknown` should appear. Cross-references must be recorded; boilerplate must be corpus-detected. |
| Document with complex and nested tables | `table`, `prose`, `heading` | `primary`, `supporting` | Nested table handling: inner tables must not merge their headers with outer table headers. Each logical table is one `table` segment. **Open question OQ-9.** |
| Spreadsheet — Report kind | `table`, `prose`, `figure_region`, `figure_caption` | `primary`, `supporting` | Triage must classify as Report. Chart regions produce `figure_region` + `figure_caption`. Sheet names become `heading` entries in the structural path. |
| Spreadsheet — Database kind | *(no segments; document-level exclusion)* | `excluded` | Triage must classify as Database. Exclusion report entry required. No vectors produced. |
| Spreadsheet — Model kind | *(no segments; document-level exclusion)* | `excluded` | Triage must classify as Model. Exclusion report entry required. No vectors produced. |
| Confluence-style HTML export | `prose`, `heading`, `table`, `list`, `code`, `boilerplate` (navigation chrome), `front_matter` (page metadata), `cross_reference` (internal wiki links) | All except `scanned_region`, `form_field` | Navigation chrome (sidebar, breadcrumb nav, "last edited by" blocks) is `boilerplate`. Internal Confluence page links are `cross_reference`. Macro-rendered content (e.g., Jira issue tables) is `table`. |
| Document with heavy near-duplicate siblings | `prose`, `heading`, and whatever the document normally contains | `primary`, `supporting` for the primary version; no segments for superseded near-duplicates (default) | Near-duplicate clustering (§6.1) marks superseded versions. **Default (owner ruling 2026-09-03, D-25):** superseded docs produce no segments and no chunks; they appear in the exclusion report with reason "superseded by \<primary document_id\>". **When `ingestion.dedup.index_superseded_versions=true`:** superseded docs are decomposed and indexed at tier `excluded` (recoverable via explicit filter). The newest member is the primary version at full tier in all cases. |
| Unservable files (several) | *(no segments; document-level exclusion)* | `excluded` | Each unservable file type produces an exclusion report entry with a stated reason. Specific unservable types the spec implies: CAD files, diagram-only PDFs, audio files, video files. **Finding:** §18.1 says "several unservable files" without specifying types. The fixture should include at minimum: one encrypted PDF, one audio file, one video file, one image-only PDF (no OCR possible), and one CAD or binary format. **Open question OQ-10.** |
| Adversarial document (injection-shaped text and invisible content) | `prose` (injection text is not a separate type; it is flagged in provenance), `scanned_region` (if OCR is used), all other types present in the document | Varies by segment content; injection flag is a provenance field, not a tier | Injection-pattern detection (§14.1) fires on segments containing model-directed language. The suspicion score is stored in provenance; the segment type and tier are unchanged. Invisible content (white-on-white, zero-size font) is detected at parse time (§14.1) and flagged. These are not segment types; they are provenance flags on otherwise-typed segments. The adversarial document produces typed segments; those segments carry additional security provenance fields. |

### 5.3 Coverage gaps and findings

No fixture maps to `form_field`. The golden corpus as described in §18.1 does not include a form-structured document. This is a gap in test coverage for the `form_field` type. **Finding F-1:** Recommend adding a filled-out form fixture (e.g., an HR onboarding form PDF or an incident report) to the golden corpus in Phase 0.

No fixture maps cleanly to `unknown`. The `unknown` type is a safety net for decomposer failures; it cannot be tested with a clean document. **Finding F-2:** The adversarial document or the poorly-scanned PDF may produce `unknown` segments if decomposition fails on corrupted regions. A fixture with intentionally malformed structure (e.g., a PDF with a corrupted object stream that partially parses) would provide a cleaner test. Recommend adding one.

---

## 6. Open questions

These ambiguities were encountered during taxonomy design. None is resolved silently. Each carries a proposed default and a flag for product owner review. All are binding on Phase 2 implementation.

| ID | Question | Where it arises | Proposed default | Flag |
|---|---|---|---|---|
| **OQ-1** | Should `heading` segments be emitted as standalone chunks (with their own provenance record) or only used to construct breadcrumb paths for other segments? Emitting them enables retrieval of section titles directly, but adds noise to results. | §6.3 structural path assignment; §12 segment set contract ("segments reassemble to document in order" implies headings must appear in the segment set, but chunking behaviour is unspecified) | Headings are present in the segment set and carry full provenance (so they count for the reassembly contract), but are assigned tier `supporting` and down-weighted, not `primary`. They are not independently chunked into the vector index unless the document is heading-only. | Needs product owner decision. |
| **OQ-2** | For `figure_region` segments in PDFs and report spreadsheets: the platform may not be able to interpret the visual without a multimodal model call. When no multimodal model is configured, is a `figure_region` segment unservable (→ `excluded`) or is it indexed as a placeholder with the caption/adjacent text as its retrieval representation? | §7.5 (unservable content), §7.3 (model calls are configurable), §6.4 (chart-adjacent commentary is prose) | When no multimodal model is configured, `figure_region` segments are indexed with their caption and adjacent prose as the retrieval representation. The segment type remains `figure_region` and the absence of a visual description is noted in provenance. No vector is generated for the figure itself; the caption segment carries the retrieval weight. This avoids requiring a multimodal model for the baseline configuration. | Needs product owner decision on whether this constitutes "unservable" under §7.5. |
| **OQ-3** | For `cross_reference` segments, what is the resolution strategy for intra-document references vs. inter-document references vs. external URL references? The spec says "resolve at ingestion (inline or store a followable pointer) or record the unresolved reference explicitly" (§6.4) but does not specify which strategy is the default. | §6.4 cross-reference handling | Proposed defaults: (a) intra-document references: resolve at ingestion by annotating with the target segment's ID if it can be determined; (b) inter-document references: store as `unresolved` with the referenced document name/number — cross-document resolution is deferred to Phase 3+; (c) external URLs: governed by the fetch policy (§6.1), recorded as `unresolved` unless the fetch policy is `snapshot` or `crawl`. | Needs product owner decision on whether inter-document resolution is a Phase 2 or Phase 3 concern. |
| **OQ-4** | When a `scanned_region` contains sub-structure (e.g., a scanned page that contains a table), should the system produce a single `scanned_region` segment or attempt to decompose the OCR output into typed sub-segments? Sub-decomposition produces better routing but requires higher OCR confidence to be reliable. | §6.3 decomposition process, §6.4 scans handling | Proposed default: attempt sub-decomposition of OCR output if OCR confidence is above a configurable threshold (proposed: 0.85 page-level confidence). Below that threshold, the entire region is `scanned_region`. Sub-decomposed segments carry the OCR confidence of their parent region. | Needs product owner decision and executor design for the confidence threshold. |
| **OQ-5** | What is the minimum length threshold for splitting an inline minority content type (e.g., a code snippet within a prose paragraph) into its own segment? Without a threshold, every inline code word becomes a `code` segment; with too high a threshold, multi-line code blocks are absorbed into `prose`. | §2.2 type exclusivity | Proposed default: inline content shorter than 3 lines or 200 characters is absorbed into the parent segment type. Inline content longer than this threshold is emitted as its own segment. Threshold is user-configurable. | Needs executor design proposal and product owner confirmation that the threshold approach is acceptable. |
| **OQ-6** | Should mathematical equations and formulas be typed as `code` or given a distinct type? They have different rendering requirements and may warrant different embedding strategies (e.g., LaTeX-aware tokenisation) but do not require syntactically different chunking rules. | §2.3 type granularity rationale | Proposed default: `code` with a `language: latex` or `language: math` metadata field. If a future embedding model supports formula-specific encoding, the metadata field enables retroactive routing without a taxonomy change. | Low urgency; deferred to Phase 3 or later. Flag if a corpus with heavy mathematical content appears in early testing. |
| **OQ-7** | What is the default OCR confidence threshold below which a `scanned_region` segment is assigned tier `excluded`? The spec says low-confidence segments are down-weighted (§6.4) but does not specify the threshold or whether down-weighting vs. exclusion is the default behaviour below a given level. | §4.1 signal matrix, §6.4 scans | Proposed defaults: below 0.60 page-level confidence → `excluded`; between 0.60 and 0.80 → `supporting` with low-confidence flag; above 0.80 → normal tier assignment. All thresholds are user-configurable. | Needs product owner confirmation of defaults. |
| **OQ-8** | What proportion of corpus documents must contain a text block for it to be classified as corpus-wide boilerplate? The spec says "text repeated across many documents" (§6.2) without a threshold. | §4.1 boilerplate detection signal, §6.2 | Proposed default: a text block appearing in more than 30% of corpus documents (by content hash or near-hash) is classified as `boilerplate`. This is user-configurable. At corpus sizes below 10 documents, the threshold is raised to 50% to avoid false positives in small corpora. | Needs product owner confirmation. |
| **OQ-9** | How should nested tables (a table cell containing another table) be represented in the segment set? Options: (a) the outer and inner tables are separate `table` segments linked by structural path; (b) the entire nested structure is one `table` segment. | §5.2 complex nested table fixture | Proposed default: nested tables are separate `table` segments. The inner table's structural path includes the outer table's position (e.g., `Section 3 > Table 1 > Row 2 > Cell 3 > Table`). This preserves the atomic-row invariant for each logical table independently. | Needs executor design validation that the structural path encoding handles this cleanly. |
| **OQ-11** | §6.1 says superseded near-duplicate documents are "retained as superseded and **excluded from the index** by default", while §6.3 mandates everything-is-indexed with tier controlling retrieval. | §5.2 near-duplicate fixture row; §6.1 vs §6.3 | **CLOSED 2026-09-03 (owner ruling, D-25).** Default: superseded docs are inventoried, retained in object storage, and reported in the exclusion report with reason "superseded by \<primary document_id\>" — they produce **no segments and no chunks**. A per-KB config toggle (`ingestion.dedup.index_superseded_versions`, default `false`) enables indexing at tier `excluded` (recoverable via explicit filter). See configuration/reference.md §2.5 and inventory.md version-family section. | CLOSED |
| **OQ-10** | §18.1 specifies "several unservable files" without listing types. The golden corpus fixture must be specific to test the exclusion report. Which file types should be included? | §18.1 golden corpus, §7.5 | Proposed fixture set: one encrypted PDF (password-protected), one audio file (.mp3 or .wav), one video file (.mp4), one image-only PDF (rasterised, no text layer), one binary CAD file (.dwg or .step), and one spreadsheet of each non-report kind (already covered above). | Needs product owner confirmation before Phase 0 fixture build. |

---

## 7. Constraints summary

The following constraints from the spec bind this taxonomy and any implementation derived from it:

- **Segment is the routing unit (§7.1).** Document-level properties (source, permissions, dates, version family) are inherited by every segment within the document. A segment does not carry these fields independently — it references its parent document record.
- **Salience tiering, not pruning (§6.3).** The `excluded` tier is not a deletion. Every segment that enters the pipeline is indexed; tier controls default retrieval behaviour. **Exception (owner ruling D-25, 2026-09-03):** by default, superseded near-duplicate documents do not enter the pipeline and produce no segments — see §3.2.
- **Segments reassemble to the document in order (§12).** The segment set contract requires that segments, taken in sequence, reconstruct the source document. This means headings must appear in the segment set even if they are not independently chunked as vectors. Segment ordering is positional (page/offset), not semantic.
- **No silent loss (§6.3, §6.4, build rule 6).** A segment of type `unknown` or a `cross_reference` with `unresolved` status is flagged and indexed, never dropped.
- **Spreadsheet database and model kinds are excluded at document level (§6.4).** They do not enter decomposition. The taxonomy handles only what enters the Decompose stage.
- **Provenance carries segment type and salience tier (§8).** Every chunk emitted by the Build stage must carry its originating segment's type and tier as payload fields, available for retrieval filtering.
- **Language is detected per segment (§7.6).** Language detection is a segment-level property, not a document-level property. It is stored as a filterable field on each segment.

---

*This document is maintained for the life of the project. When the taxonomy changes, this file and the relevant ADR change in the same commit.*
