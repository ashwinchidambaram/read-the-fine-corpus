# Contract 5 — Chunk

Stage boundary: **Build → Serve**. **The spine of the system.** Governing spec: §8 (provenance),
§7.2 (transformation tiers), §10.5 (chunk identity), §12 (Chunk invariants), §14.1 (trust label).

The Chunk is what Serve returns. It carries the **full §8 provenance block**. Its **text is
byte-identical to the Tier-1-normalized canonical source** unless a Tier 3 transformation is
recorded in its transformation list (§7.2, §12; see "Canonical source text" invariant below).
**Tier 2 augmentation is stored in separate fields** — embedded *with* the chunk for retrieval,
never merged into the returned text (§7.2). Its **ID is deterministic** under §10.5, derived from
stable inputs and including `config_version`, and reproducible because those inputs come from the
**frozen Segment set artifact** ([segment-set.md](segment-set.md) "Determinism and freezing").

Root model: `Chunk`.

---

## `Chunk` (root)

| Field | Type | Required | Semantics |
|---|---|---|---|
| `schema_version` | `str` (semver) | yes | Contract version. |
| `chunk_id` | `str` (see derivation) | yes | **Deterministic** ID (§10.5). Same document + same config → same ID every run. Includes `config_version`. |
| `tenancy` | `TenancyBlock` | yes | Owning workspace/KB and resolved permission fields; enforced server-side at retrieval (§11.4). |
| `provenance` | `Provenance` | yes | The complete §8 block. Non-null, all subfields present. |
| `text` | `str` | yes | **The chunk's source text.** Byte-identical to the span of the **Tier-1-normalized** canonical source unless a `tier=3, changed_text=true` transformation is recorded in `provenance.transformations` (§7.2, §12; see "Canonical source text" invariant). The RAW original is always retained and addressable via `provenance.source_location`. This is what the caller receives. |
| `augmentation` | `Augmentation` | yes | Tier 2 contextual augmentation, in **separate fields** (§7.2). Embedded with the chunk; never part of `text`. |
| `embedding_input` | `str` | yes | The exact string embedded (augmentation + chunk). Recorded for reproducibility and explain mode (§11.5). Never returned as the chunk to the caller. |
| `embedding_ref` | `EmbeddingRef` | yes | Which model/dimensions produced the vector — for mismatch-fails-closed (§15) and audit. |
| `chunk_index` | `int` | yes | 0-based position of this chunk within its segment (part of ID derivation). |
| `token_count` | `int` | no | Token length of `text`, for observability. |

## `Augmentation`

Tier 2 fields, stored separately (§7.2). All optional (a chunk may have none); when present, they
are concatenated into `embedding_input` but never into `text`.

| Field | Type | Required | Semantics |
|---|---|---|---|
| `parent_breadcrumb` | `str` | no | Heading-path context (§6.4 prose default-on). |
| `table_description` | `str` | no | Generated NL description of a table's contents (§6.4). The verbatim table stays in `text`. |
| `class_context` | `str` | no | Class-context blurb from the class description (§6.5). |
| `generated_by` | `list[str]` (model refs) | no | Which model(s) produced these fields, for audit. Never a secret (§14.2). |

## `EmbeddingRef`

| Field | Type | Required | Semantics |
|---|---|---|---|
| `provider` | `str` | yes | Provider name (not credential). |
| `model` | `str` | yes | Model identity. A query embedded with a different model fails closed (§15). |
| `dimensions` | `int` | yes | Vector dimensionality. |
| `config_version` | `str` | yes | The `config_version` this chunk was built under (§10.5); redundantly stored on the chunk for orphan/version reasoning even though it is folded into `chunk_id`. |

The vector itself is stored as the vector-DB point's vector, not a contract field; the payload
carries everything above.

---

## Deterministic chunk ID derivation (§10.5)

Chunk IDs are derived from **stable inputs only** — document identity, segment path, position,
and config version (§10.5). Random or sequence-assigned IDs are rejected (§10.5,
[ADR-0006](../adr/0006-chunk-identity-scheme.md)): they make correct replace-by-document
impossible, because the same content gets a different ID on every run and old chunks cannot be
matched for removal.

### The canonical string

Five fields, in fixed order, joined by an ASCII unit separator (`\x1f`) that cannot occur in the
component values:

```
canonical = "\x1f".join([
    document_id,        # ULID, stable file identity (Inventory)
    content_hash,       # sha256 hex of source bytes — the document VERSION (§8)
    config_version,     # sha256 hex of build-affecting config fields (ingestion-config.md)
    segment_path,       # canonical segment path within the document (segment-set.md)
    str(chunk_index),   # 0-based chunk position within the segment
])
```

Notes on component choice:
- `document_id` + `content_hash` together identify *this version of this document* (§8 requires
  identity **and** version). Because `content_hash` is document-wide, an edit rotates **every**
  chunk ID of that document — including chunks whose text is unchanged (see worked example case c).
  ID stability is guaranteed across re-runs of the same document version, not across versions;
  correctness across versions comes from replace-by-document, which keys removal on `document_id`.
- `config_version` folds the entire build-affecting config in, so any chunking/transformation/
  embedding change changes **every** chunk ID (§10.5, case b).
- `segment_path` + `chunk_index` locate the chunk deterministically within the document; they do
  not depend on iteration order, wall-clock, or run count. Their reproducibility across runs is
  guaranteed by the **frozen Segment set artifact**: Decompose's LLM-driven typing/segmentation is
  content-addressed on `(document_id, content_hash, config_version)`, persisted once, and reused by
  every Build for that key, so classification drift cannot rotate `segment_path`/`chunk_index` on a
  re-run (see [segment-set.md](segment-set.md) "Determinism and freezing"; §10.5, determinism
  review attack 3). Build **consumes the frozen artifact; it does not re-run Decompose.**

### The hash and the ID shape

```
chunk_id = "chk_" + base32_nopad( sha256( canonical.encode("utf-8") ) )[:26]
```

- **sha256** of the UTF-8 canonical string.
- Encode the digest in lowercase **base32 without padding**, take the first 26 chars, prefix
  `chk_`. Result is a stable, URL-safe, human-recognizable ID, e.g. `chk_ab3f...` (30 chars total).
- 26 base32 chars = 130 bits of the digest, collision-negligible at 5M chunks (§4.5 scale).

### Vector-DB point ID mapping

Qdrant point IDs are UUIDs or unsigned integers, not arbitrary strings. **The point ID is the
UUID rendering of the same digest:** `point_id = UUID(bytes=sha256(canonical)[:16])`. Both
`chunk_id` (string, in payload and API) and `point_id` (UUID, the DB key) derive from the *same*
digest, so they are 1:1 and either can be computed from the other's inputs. The payload stores
`chunk_id` explicitly; the API always speaks `chunk_id`, never the raw point UUID (keeps the
vector DB private to `index/`, C-2/C-3).

---

## Worked example

Concrete inputs (a prose chunk from the bloated manual):

```
document_id   = "01J9Z3K7Q..."          → "01J9Z3K7QMANUAL0001"   (ULID)
content_hash  = "9f2c...ae"              → "9f2c8b1e77a4d0c3e5b19a24d8f6c0b2e4a7913d5c8f0a2b4d6e8f1a3c5b7d9e0"
config_version= "c41d...02"              → "c41d09f7b2e3a5..."      (sha256 hex, 64 chars)
segment_path  = "Maintenance/Lubrication#3"
chunk_index   = 1
```

Canonical string (`\x1f` shown as `␟`):

```
01J9Z3K7QMANUAL0001␟9f2c8b1e77a4d0c3e5b19a24d8f6c0b2e4a7913d5c8f0a2b4d6e8f1a3c5b7d9e0␟c41d09f7b2e3a5…␟Maintenance/Lubrication#3␟1
```

Then:

```
digest    = sha256(canonical.utf8)            # 32 bytes
chunk_id  = "chk_" + base32_nopad(digest)[:26]  →  e.g.  chk_k7m2p9x4rq8h3n6v0w1t5s2y8b
point_id  = UUID(bytes=digest[:16])             →  e.g.  4b3f9c1e-...-a20d
```

**Case (a) — unchanged document version keeps all IDs across runs.** Re-run Build on the same
document version (`content_hash` unchanged) under the same config (`config_version` unchanged).
Every component of `canonical` is identical → identical digest → identical `chunk_id` and
`point_id`. Re-ingestion is idempotent; no duplication (§10.5, §15 resumability). **Cross-version
stability is NOT provided:** editing the document rotates `content_hash` and therefore *every*
chunk ID of that document, including chunks whose own `text` did not change — text stability does
not imply ID stability across versions (see case (c) and the derivation note; W-3). Correctness
across versions comes from replace-by-document, which keys removal on `document_id`, not on
matching IDs.

**Case (b) — a config-version bump changes every ID.** Edit chunking `max_tokens` 512 → 768.
`config_version` changes (it hashes `chunking`). That component changes in **every** chunk's
canonical string → **every** `chunk_id` changes. This is exactly why a config change triggers a
full rebuild, not an incremental pass (§10.3, §10.5). The old chunks (old `config_version`) are
replaced wholesale.

**Case (c) — an edited document changes only affected chunks, and replace-by-document removes all
prior-version chunks.** The manual's §3 is edited; its bytes change, so the document's
`content_hash` changes from `9f2c…` to `7a10…`. Two things follow:
1. **Every** chunk of that document gets a new `chunk_id`, because `content_hash` is in the
   canonical string. So at the ID level "only affected chunks change content" is realized as:
   unchanged *segments* still map to the same `segment_path`+`chunk_index`, and their `text` is
   identical, but their ID moves because the document version moved. Retrieval quality is
   unaffected (same text, same vectors for unchanged spans).
2. **Replace-by-document** (§10.5) keys removal on `document_id`, **not** on `chunk_id`: Build
   deletes *all* points whose payload `provenance.source_document_id == document_id` in the same
   operation that writes the new version's chunks (a single atomic upsert+delete against the shadow
   collection). Because removal is by `document_id`, it removes the prior version's chunks whatever
   their old `content_hash` was — no orphan from the old version can survive (§10.5
   replace-by-document; §17.1 deletion completeness). Orphan detection runs afterward and any
   residual is an alert, not a log line (§10.5).

> Design note: keying the *ID* on `content_hash` (so an edit rotates IDs) plus keying *removal* on
> `document_id` (so the old version is swept regardless of its IDs) is what makes incremental
> updates correct. An alternative that keyed the ID on `document_id`+`segment_path` only (no
> `content_hash`) would keep IDs stable across edits but then relies entirely on exact overwrite;
> the chosen scheme is robust even if a segment's `chunk_index` count changes between versions,
> because replace-by-document sweeps by document, not by matching IDs. See ADR-0006.

---

## Invariants

- **Provenance complete** for every chunk — non-null `Provenance`, every subfield present (§8,
  §12). Non-negotiable test §18.3.3.
- **Canonical source text (byte-identity).** `text` is byte-identical to the span of the
  **Tier-1-normalized** canonical source, not to the raw original. The RAW original is always
  retained and addressable via `provenance.source_location`; nothing is destroyed. Rules:
  - Every **Tier 1** op records its own `changed_text` flag (OCR cleanup, `table_to_markdown`,
    whitespace/encoding repair set `true`; no-op normalizations set `false`). The literal reading
    "byte-identical to the raw source" is unsatisfiable because Tier 1 legitimately changes bytes
    (e.g. table-to-markdown) — hence byte-identity is defined against the Tier-1-normalized text.
  - **Tier 2 never touches `text`** — every Tier 2 op has `changed_text=false`; augmentation lives
    only in `augmentation`/`embedding_input`. **This is exactly what §18.3 test 4 asserts.**
  - **Tier 3** may change `text` only with its recorded `tier=3, changed_text=true` flag; the
    pre-rewrite text is retained (§7.2).
  - This byte-identity definition is the accepted interpretation of §12 (owner ruling 2026-09-03,
    D-11 CLOSED). See also the `TransformationRecord` "Canonical source text" invariant in
    [contracts/README.md](README.md).
- **Tier 2 separation:** all augmentation is in `augmentation`/`embedding_input`, never in `text`
  (§7.2).
- **Deterministic ID:** `chunk_id` derives solely from `document_id`, `content_hash`,
  `config_version`, `segment_path`, `chunk_index` — no randomness, no sequence, no wall-clock
  (§10.5). Its inputs come from the **frozen Segment set artifact** so a re-run reproduces them by
  reuse ([segment-set.md](segment-set.md) "Determinism and freezing"). The property test MUST
  re-run the **full Decompose→Build pipeline** (not merely re-hash fixed inputs) and assert
  identical chunk IDs across two cold runs (attack 3).
- **Config version in identity:** a config change changes `config_version` and therefore every
  `chunk_id` (§10.5), forcing full rebuild (§10.3).
- **Replace-by-document:** removal keys on `source_document_id`, atomic with the new write; no
  partial intermediate state (§10.5).
- **Point-ID mapping:** `point_id = UUID(sha256(canonical)[:16])`; API never exposes the raw point
  UUID, only `chunk_id` (C-2/C-3).
- `tenancy` present and server-enforced at retrieval (§11.4, Phase 0 MUST for the fields).

## Golden-corpus expressibility

- **Scanned PDF degraded region:** `provenance.ocr_confidence` low, `confidence` reduced,
  `salience`/retrieval down-weighting via config; `text` is byte-identical to the
  **Tier-1-normalized** OCR output. If `ocr_cleanup` corrected characters or repaired encoding it
  records `changed_text=true` (Tier 1); if it was a no-op it records `changed_text=false`. Either
  way the RAW OCR string is retained and addressable via `source_location` (C-R2).
- **Boilerplate segment:** a corpus-repeated preamble is its **own** `boilerplate`-typed segment
  with byte-identical `text`, tier `boilerplate`, default-filtered at retrieval. No Tier 1 op
  removes its bytes from any other chunk (R6/C-R1); there is no `boilerplate_strip` text mutation.
- **Adversarial doc:** `provenance.injection_suspicion` high, `invisible_content_flags` populated,
  `trust_level=untrusted_ingested`; text **not** stripped (§14.1); all retrievable/filterable.
- **Complex table:** `text` = verbatim table (atomic), `augmentation.table_description` = generated
  NL description embedded with it (§6.4, §7.2).
- **Bloated manual:** chunks at varying `salience_tier`; cross-reference context surfaced via
  provenance/`structural_path`.

## Open questions

1. **base32 length / prefix.** 26 chars (130 bits) proposed. **Flagged** — could shorten if IDs
   feel long, but not below ~80 bits at 5M-chunk scale.
2. **Storing `content_hash` twice.** It is in the canonical string (via ID) and in
   `provenance.source_document_version`. **Proposed default:** keep both — the payload copy is what
   replace-by-document and orphan detection query; the ID copy makes the ID self-describing.
   **Flagged.**
3. **Point-ID collision handling.** Truncating sha256 to 128 bits for the UUID is
   collision-negligible but nonzero. **Proposed default:** on the astronomically-unlikely collision
   at write, fail loud (never silently overwrite a different chunk). **Flagged.**
4. **Empty `augmentation` vs null.** **Proposed default:** always present as an object with null
   subfields (so "no augmentation" is distinguishable from "augmentation not computed"). **Flagged.**
