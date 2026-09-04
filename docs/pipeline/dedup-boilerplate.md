# Corpus-Wide Deduplication and Boilerplate Detection (Phase 2)

> **Status:** Implemented — Phase 2.  Algorithm defaults are set; tunable via `corpus.yaml`.

---

## Overview

Two corpus-level passes run between the Assess stage (per-document parsing) and the Decompose
stage (segmentation), implemented in
`src/finecorpus/pipeline/assess/corpus_passes.py`:

1. **Near-duplicate clustering** (§6.1): groups document versions into families; oldest versions
   are suppressed from indexing by default (D-25).
2. **Corpus-wide boilerplate detection** (§6.2): identifies text blocks that appear in more than a
   threshold proportion of corpus documents; those blocks are reclassified to the `boilerplate`
   salience tier during Decompose.

Both passes require the full extracted text of every document and therefore cannot run per-document
— they are inherently corpus-level.

---

## 1. Near-Duplicate Clustering

### Algorithm

1. For each parseable document, extract all region text and tokenise into word tokens
   (`re.findall(r"[a-z0-9]+")`).
2. Build a word **5-gram shingle set** (frozenset of 5-tuples of consecutive word tokens).
3. Compute **pairwise Jaccard similarity** for all document pairs:
   `|A ∩ B| / |A ∪ B|`
4. Two documents are near-duplicates when Jaccard ≥ `near_duplicate_threshold` (default **0.50**).
5. Build a near-dup graph; find connected components via union-find.  Each component with ≥ 2
   members becomes a **version family**.
6. Within each family, elect the **primary** document: the member with the latest
   `source_modified_at` timestamp (tie-break: lexicographically largest `content_hash`).
7. All other family members are marked **superseded**.

### Primacy selection

`source_modified_at` is the file modification timestamp propagated from the inventory into each
`ParseResult` dict by `AssessStage._produce()`.  If absent (e.g., the connector did not supply a
modification time), `discovered_at` (the corpus-ingestion discovery timestamp) is used next.  If
neither timestamp is available, the tie-break (`content_hash` lexicographic order) applies
exclusively.  The primacy basis is recorded in the version-family dict as `primacy_basis`:

| Value | When set |
|---|---|
| `"source_modified_at"` | `source_modified_at` is truthy on the primary's ParseResult |
| `"discovered_at"` | `source_modified_at` is absent; `discovered_at` is truthy |
| `"content_hash"` | Neither timestamp is present; content-hash ordering used as tie-break |

### Version-family schema

Each family is a dict with these fields:

| Field | Type | Description |
|---|---|---|
| `family_id` | str | Stable SHA-256–derived ID from sorted member document IDs |
| `member_document_ids` | list[str] | All members (sorted) |
| `primary_document_id` | str | The elected primary member |
| `superseded_document_ids` | list[str] | All non-primary members (sorted) |
| `similarity_method` | str | `"word_5gram_jaccard_exact"` |
| `similarity_scores` | dict[str, float] | Jaccard score of each superseded member vs. the primary |
| `primacy_basis` | str | `"source_modified_at"`, `"discovered_at"`, or `"content_hash"` |

**`similarity_scores` and transitivity:** a member's `similarity_score` reflects its
direct Jaccard similarity to the elected primary.  When a member is included in the
family via transitivity — that is, A and B are near-duplicates, and B and C are
near-duplicates, but A and C have no direct edge that exceeds the threshold — C's
similarity score to the primary A is recorded as `0.0` (no direct pair with A exceeds
the threshold; only the transitive A → B → C chain justifies membership).

Version families are stored in `ParseResultBatch.version_families` (added in schema 1.1.0).

### Threshold selection

The default threshold **0.50** was chosen empirically from the golden corpus:

| Pair | Jaccard |
|---|---|
| policy_v1 vs policy_v2 | 0.651 |
| policy_v1 vs policy_v3 | 0.527 |
| policy_v2 vs policy_v3 | 0.563 |

A threshold of 0.70 would have missed the v1–v3 link, breaking the connected component and leaving
v1 as a false singleton.  0.50 groups documents that share roughly half their 5-gram vocabulary —
a reliable "same document, different version" signal for real policy and manual corpora.

Operators can raise the threshold if their corpus shows false positives (unrelated documents
grouped together).  Config key: `ingestion.dedup.near_duplicate_threshold`.

### Scaling ceiling (Phase 5 / D-07 note)

Exact pairwise Jaccard is **O(N² × |shingles|)**.  For the corpora this system handles in Phase
2 (tens to low hundreds of documents) this is fast.  At corpus sizes above approximately **5,000
documents**, pairwise exact Jaccard becomes expensive and should be replaced with **MinHash-LSH**
approximate similarity (see D-07).  The threshold and family-formation logic remain unchanged;
only the similarity computation changes.  Phase 5 should gate on corpus size and switch algorithm
accordingly.

---

## 2. D-25 Enforcement — Superseded Documents Produce No Segments

Owner ruling (2026-09-03, D-25): superseded near-duplicate documents are **not indexed by
default**.  The Decompose stage checks the `dedup_role` annotation on each `ParseResult` before
running any pass:

- `dedup_role == "superseded"` **and** `index_superseded_versions == False` (default):
  → produce an **empty `SegmentSet`** with a single `ExclusionRecord`:
  - `reason = "superseded_version"`
  - `reason_detail` = `"Superseded near-duplicate; primary document_id: <primary_id>"`
  - No segments are produced.  The document is inventoried and retained in object storage but
    contributes no content to the index.

- `dedup_role == "superseded"` **and** `index_superseded_versions == True`:
  → Decompose proceeds normally (all passes run on the superseded document), followed by the
  `SupersededVersionPass` (the final pass in the pipeline).  This pass forces every segment
  to `salience_tier=excluded` with a `superseded_version` winning signal, overriding all
  other tier assignments (type-prior, boilerplate, etc.).  This path is intended for operators
  who need older versions to be explicitly retrievable via an `excluded`-tier filter.

- `dedup_role == "primary"` or `"unique"`:
  → Normal decomposition.

Config key: `ingestion.dedup.index_superseded_versions` (bool, default `false`).

### Nothing-dropped invariant

The invariant that "every inventory item must appear in the decompose output" (§6 rule 6) is
preserved: superseded documents produce an empty `SegmentSet` with an exclusion record rather
than being silently omitted.

---

## 3. Corpus-Wide Boilerplate Detection

### Algorithm

1. For each parseable document, extract all region text.
2. Split into **block candidates**: the text is split on blank lines to get paragraph blocks; each
   paragraph is also split into individual lines.  This two-level splitting is required because
   pypdf extracts long legal preambles as sequences of short lines separated by `\n`, not blank
   lines — line-level blocks are needed to detect them reliably.
3. Normalise each block: lower-case + whitespace-collapse (`re.sub(r"\s+", " ", ...)` ).
4. Discard blocks shorter than **20 characters** (not enough signal).
5. Count the number of distinct documents each block appears in (one count per document, even if
   the block appears multiple times within one document).
6. Apply the threshold:
   - Normal corpus (≥ 10 documents): block is boilerplate if `count / n_docs > 0.30`.
   - Small corpus (< 10 documents): block is boilerplate if `count / n_docs > 0.50`.
     The raised threshold avoids false positives when there are few documents.
7. The resulting set of normalised boilerplate block strings is stored in
   `ParseResultBatch.boilerplate_blocks` (added in schema 1.1.0).

### Boilerplate pass (Decompose)

`BoilerplatePass` is the third pass in the Decompose pipeline
(`src/finecorpus/pipeline/decompose/passes/boilerplate.py`), running after `SegmentationPass`
and `SaliencePass`.

For each segment:

1. **Exact match**: normalise the segment's full text; if it equals a boilerplate block, the
   segment is boilerplate.
2. **Line-level match**: split the segment text on `\n`; if ≥ **70%** of the non-empty normalised
   lines are in the boilerplate block set, the segment is boilerplate.
   - Minimum 3 lines required to avoid false positives from very short segments.
   - The 70% threshold accommodates PDF page-boundary artefacts: when a long preamble is split
     across a page boundary, the last few lines of the first-page region may differ between
     documents (depending on layout), so 100% line matching would miss it.

When a segment is classified as boilerplate:

- `segment_type` → `SegmentType.boilerplate`
- `salience_tier` → `SalienceTier.boilerplate`
- `salience_basis` → `SalienceSignalKind.boilerplate_detection`
- `salience_signals`: existing signals are preserved with `won=False`; a new
  `boilerplate_detection` signal is appended with `won=True`.
- The segment **text is never stripped** — boilerplate is handled structurally, not by byte
  removal (segment-taxonomy.md §2.1).

### Tier effect on retrieval

Segments at `salience_tier=boilerplate` are **excluded from default retrieval**.  The default
salience filter (`["primary"]`) does not include `boilerplate`.  Operators can include boilerplate
segments by explicitly adding `"boilerplate"` to the salience filter in a retrieval request.

---

## 4. ParseResultBatch Schema 1.1.0

Phase 2 adds two corpus-level fields to `ParseResultBatch` (MINOR bump, backward-compatible):

| Field | Type | Description |
|---|---|---|
| `version_families` | `list[dict]` | VersionFamily dicts for all detected near-dup families |
| `boilerplate_blocks` | `list[str]` | Sorted list of normalised boilerplate block strings |

Consumers that declared `min_minor=0` for `ParseResultBatch` (the supported version range) will
continue to work without modification — the new fields are additive.

---

## 5. Configuration Keys

See `docs/configuration/reference.md §2.5` for full descriptions.

| Key | Default | Effect |
|---|---|---|
| `ingestion.dedup.near_duplicate_threshold` | `0.50` | Minimum Jaccard for near-dup grouping |
| `ingestion.dedup.index_superseded_versions` | `false` | Allow/suppress superseded doc indexing |
| `assessment.boilerplate_corpus_proportion` | `0.30` | Boilerplate block frequency threshold (normal corpus) |
| `assessment.boilerplate_small_corpus_proportion` | `0.50` | Boilerplate threshold for small corpora |
| `assessment.boilerplate_small_corpus_doc_count` | `10` | Corpus size below which "small" threshold applies |

---

## 6. Stub: Primacy-Flip Payload-Update Job

When a new document version is ingested that supersedes a previously-indexed primary, the old
primary's chunks must be retired and the new primary's chunks must be indexed.  This primacy-flip
payload-update job is a **Phase 5 concern** (§7.5) and is not implemented in Phase 2.

A stub exists at `src/finecorpus/pipeline/collect/neardup.py` with an honest not-implemented
notice.  Phase 5 should implement the job there, referencing the version-family data from the
`ParseResultBatch`.

---

## See Also

- `docs/contracts/parse-result.md` — ParseResult and ParseResultBatch contract
- `docs/configuration/reference.md §2.5` — Full config key listing
- `src/finecorpus/pipeline/assess/corpus_passes.py` — Implementation
- `src/finecorpus/pipeline/decompose/passes/boilerplate.py` — BoilerplatePass
- `tests/pipeline/test_dedup_boilerplate.py` — Phase 2 acceptance tests
