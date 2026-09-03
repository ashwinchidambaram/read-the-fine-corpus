# ADR-0006 — Chunk identity scheme

Status: **proposed** (design review pending).
Governing spec: §10.5 (chunk identity & incremental correctness), §8 (provenance), §12 (Chunk
contract), §17.1 (deletion completeness).
Related: [contracts/chunk.md](../contracts/chunk.md), [contracts/ingestion-config.md](../contracts/ingestion-config.md).

## Context

Incremental updates are where retrieval systems quietly rot (§10.5): a document is edited, its new
chunks are written, and its old chunks are never removed, so retrieval returns stale and current
content side by side, both looking authoritative. §10.5 makes four MUSTs binding: chunk IDs are
**deterministic** from stable inputs; document updates are **replace-by-document**; **orphan
detection** runs after every incremental ingestion; and **config version is part of chunk
identity**. §10.5 explicitly states that *random or sequence-assigned IDs make correct incremental
updates impossible.*

We must choose exactly what a chunk ID is derived from, how it is hashed and formatted, and how it
maps onto the vector-DB point ID (Qdrant, which keys points by UUID or unsigned integer, not
arbitrary strings).

## Decision

**Chunk IDs are a content-addressed hash of five stable inputs.**

Canonical string, fields joined by ASCII unit separator `\x1f`:

```
canonical = document_id ␟ content_hash ␟ config_version ␟ segment_path ␟ chunk_index
```

- `document_id` — stable file identity (Inventory).
- `content_hash` — sha256 of source bytes = the document **version** (§8 requires identity *and*
  version).
- `config_version` — sha256 of the build-affecting fields of the ingestion config
  (see [ingestion-config.md](../contracts/ingestion-config.md)); folds chunking, transformation,
  and embedding config into identity, satisfying §10.5's "config version is part of chunk
  identity."
- `segment_path` — canonical stable path of the segment within the document (Segment set).
- `chunk_index` — 0-based position of the chunk within the segment.

Derivation:

```
digest    = sha256(canonical.encode("utf-8"))
chunk_id  = "chk_" + base32_nopad(digest)[:26]          # 130 bits, URL-safe, self-describing
point_id  = UUID(bytes=digest[:16])                     # Qdrant point key, same digest
```

`chunk_id` (string) is the API- and payload-level identity; `point_id` (UUID) is the vector-DB
key. Both come from the same digest, so they are 1:1 and neither the API nor any client ever sees
a raw point UUID (keeps the vector DB private to `index/`, C-2/C-3).

**Removal is keyed on `document_id`, not on `chunk_id`.** Document updates are replace-by-document
(§10.5): Build deletes all points whose payload `provenance.source_document_id == document_id` in
the same atomic operation that writes the new version's chunks. Because removal is by document, the
prior version's chunks are swept regardless of their old IDs — even if the segment count or
per-segment chunk count changed between versions. Orphan detection runs afterward; non-zero orphans
is an alert (§10.5).

## Consequences

**Positive**
- **Deterministic and reproducible:** same document version + same config → identical IDs on every
  run. Re-ingestion is idempotent; resumability (§15) never duplicates.
- **Config change invalidates every chunk automatically:** `config_version` is in the string, so a
  chunking/transformation/embedding change rotates every ID, which is precisely why a config change
  forces a full rebuild (§10.3) rather than an unsafe incremental pass.
- **Edits are safe:** `content_hash` in the string rotates IDs on edit; replace-by-document (keyed
  on `document_id`) removes the entire prior version regardless of ID changes, so no stale chunk
  survives (§17.1 deletion completeness; non-negotiable test §18.3.8).
- **Self-describing IDs** aid debugging and the explain view (§11.5) without leaking secrets.
- **Clean point-ID mapping** to Qdrant's UUID key with no side table.

**Negative / trade-offs**
- **Every edit rotates all of a document's chunk IDs**, even for unchanged segments (because
  `content_hash` is document-wide). We accept this: correctness (no stale chunks) dominates ID
  stability, and unchanged spans keep identical text and vectors, so retrieval quality is
  unaffected. Replace-by-document does not rely on matching IDs across versions, so nothing breaks.
- **Truncation collisions** are theoretically possible (130-bit `chunk_id`, 128-bit `point_id`).
  Negligible at the §4.5 scale (5M chunks); on the astronomically-unlikely write-time collision we
  fail loud rather than overwrite (chunk.md open question 3).
- **`config_version` correctness is load-bearing:** if the set of build-affecting fields hashed
  into `config_version` is wrong, we either rebuild needlessly (over-inclusion) or leave stale
  chunks (under-inclusion). This is pinned and unit-tested (ingestion-config.md open question 1).

## Alternatives considered

1. **Random UUIDs (uuid4) per chunk.** *Rejected by §10.5.* A re-run produces new IDs, so old
   chunks cannot be matched and correct incremental removal is impossible; you are forced into
   drop-and-rebuild-everything or you leak orphans.
2. **Sequence-assigned IDs (auto-increment).** *Rejected by §10.5.* Same defect as random plus
   ordering coupling: IDs depend on processing order, so a re-run or a resumed job renumbers
   chunks and breaks replace/orphan logic.
3. **ID keyed on `document_id` + `segment_path` + `chunk_index` only (no `content_hash`).** Keeps
   IDs stable across edits (attractive for diffs). *Rejected:* it makes the ID silently mean two
   different contents before and after an edit, and correctness then depends entirely on exact
   overwrite of the same ID set — which breaks if an edit changes how many chunks a segment yields
   (the old chunk `#4` has no new counterpart to overwrite and becomes an orphan). Our scheme
   sidesteps this by keying *removal* on `document_id`.
4. **ID keyed without `config_version`.** *Rejected:* violates §10.5's explicit requirement that
   config version be part of chunk identity; a chunking change would not rotate IDs and stale
   chunks from the old config could persist through an incremental pass.
5. **Composite natural key stored in a side table, DB-generated surrogate as the point ID.**
   *Rejected:* adds a stateful mapping the stateless retrieval path (C-1) and replace logic would
   have to consult, and reintroduces a non-deterministic surrogate — the very thing §10.5 forbids.

## Open questions

- Exact base32 length / prefix (26 chars proposed; ≥80 bits floor).
- Whether to store `content_hash` on the payload in addition to encoding it in the ID (proposed:
  yes — payload copy drives replace-by-document and orphan queries).
- Exact build-affecting field set feeding `config_version` (pinned in ingestion-config.md open
  question 1; must be unit-tested before Phase 1).
