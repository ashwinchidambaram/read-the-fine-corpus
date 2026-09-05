# ADR-0007 — Per-KB collections for v1 multi-tenant topology

Status: **accepted**.
Governing spec: §11.4 (multi-tenant index topology), §4.2 (C-3/C-4), §4.4 (adapter capabilities).
Related: [index/adapter.py](../../src/finecorpus/index/adapter.py), [index/lifecycle.py](../../src/finecorpus/index/lifecycle.py).

## Context

§11.4 states two options for multi-tenant Qdrant topology: (a) a single shared collection with
tenant partitioning by payload field ("single-collection"), or (b) a separate collection per
knowledge base ("per-KB collections").  The spec explicitly names single-collection + shard keys
as the documented Qdrant practice that "scales far past collection-per-tenant."

At the same time, the platform's Phase 1 architecture established:
- Stable KB aliases (`rtfc_{kb_id}`) that resolve to per-KB collections.
- Atomic alias swaps for zero-downtime promotion and rollback (§4.1).
- Shadow-collection build isolation (§4.2 C-4): ingestion writes to a shadow, not the live
  collection.
- Payload tenancy filtering (§11.4 MUST clause) as a defense-in-depth layer on every search path.

Re-platforming to a single collection would require changing the alias/lifecycle contract, the
control-plane alias record schema, and the retrieval service filter construction — a large
migration surface with no immediate scaling need.

## Decision

**Retain per-KB collections for the v1 multi-tenant topology.**

One Qdrant collection per knowledge base, with the following invariants that MUST hold regardless
of this topology choice:

1. **Payload tenancy filter is mandatory defense-in-depth (§11.4).** Every search operation MUST
   inject a `tenancy.kb_id == {kb_id}` filter (and, for permission-scoped KBs, any additional
   permission principal filters).  The filter is NOT optional even though the per-KB collection
   boundary already isolates tenants at the HNSW graph level.  Defense-in-depth means a
   misconfigured alias that resolves to the wrong collection does not expose cross-tenant data.

2. **Aliases remain the API surface (C-3).** Callers never reference a collection name.  The
   lifecycle layer controls which collection an alias points to; the retrieval service resolves
   aliases to collections internally.

3. **Shadow-collection isolation is preserved (C-4).** Ingestion writes to a shadow collection;
   promotion is an atomic alias swap.  This property is collection-topology-independent and MUST
   remain in any future migration.

## Migration path and revisit trigger

The single-collection-with-shard-keys topology (the §11.4 alternative) SHOULD be adopted when
**any** of the following conditions are met:

- **KB count exceeds 200** in a single Qdrant cluster, causing HNSW graph memory overhead to
  become the dominant RAM cost (rule of thumb: each collection with a loaded HNSW graph costs
  ~50–200 MB depending on vector count; 200 collections × 100 MB ≈ 20 GB).
- **Collection creation/deletion rate exceeds Qdrant's tested envelope** — i.e., workspace
  onboarding/offboarding frequency creates measurable latency in collection management operations.
- **A multi-KB cross-search feature is required** that needs a single search operation to span
  multiple knowledge bases simultaneously; per-KB collections force N searches for N KBs,
  whereas a single collection allows a single search with a shard-key or payload-filter union.

The migration from per-KB to single-collection requires:
1. A new collection schema with shard keys keyed on `tenancy.kb_id`.
2. A bulk re-ingestion or online migration for existing KBs.
3. Control-plane alias record changes (the alias record currently maps alias → per-KB collection
   name; in the new topology it would map alias → shared collection + shard key).
4. Retrieval service filter changes (tenancy filter already includes `kb_id`; the migration
   is additive rather than structural).

The decision is intentionally revisited at the KB-count/scale trigger above, not proactively.
Premature migration introduces migration risk without a demonstrated scaling problem.

## Consequences

**Positive**
- No migration work for v1; no impact on the alias, lifecycle, or retrieval service contracts.
- Simple mental model: one KB, one collection, one alias — easy to trace from UI to Qdrant.
- Per-KB HNSW graph isolation means a large KB's memory footprint does not affect another KB's
  search latency.
- §10.2 hot/cold retention (N-1 collection retained; N-2+ snapshotted) maps cleanly to
  per-collection semantics.

**Negative / trade-offs**
- Memory scales linearly with KB count.  At tens of KBs this is negligible; at hundreds it is a
  consideration (see revisit trigger above).
- Qdrant cluster initialization time grows with collection count (Qdrant loads HNSW graphs on
  startup).
- A cross-KB search feature (not in v1 scope) would require N parallel searches instead of one.

## Alternatives considered

1. **Single collection with shard keys (§11.4's stated preferred approach).**
   *Not adopted in v1* because it requires a larger migration surface than the current Phase 1
   architecture supports, and no scaling pressure has materialized to justify the migration cost.
   This remains the recommended path at scale (see revisit trigger).
2. **Weaviate-style shared collection with per-tenant class filter.**
   *Out of scope.* The platform is committed to Qdrant (ADR-0003).
