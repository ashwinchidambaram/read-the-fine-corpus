"""Near-duplicate primacy-flip payload-update job stub (Phase 5 / §7.5).

NOT IMPLEMENTED IN PHASE 2.

When a new document version is ingested that supersedes a previously-indexed primary
document, the old primary's chunks must be retired and the new primary's chunks must
be indexed.  This "primacy-flip payload-update job" is referenced in the spec at §7.5
and is deferred to Phase 5.

Design sketch (for Phase 5 implementer)
-----------------------------------------
1. After a new ingestion run completes, compare the new `ParseResultBatch.version_families`
   against the previously stored version-family data from the prior run.
2. For each family where the primary has changed (primacy flip):
   a. Retire the old primary's chunks: mark them inactive in the chunk store.
   b. Index the new primary's chunks: run Decompose → Plan → Build for the new primary.
   c. Update the inventory entry for the old primary to reflect its new role (superseded).
3. This job must be idempotent: re-running it with the same inputs must not produce
   duplicate chunks or retire already-retired chunks.
4. Atomic flip: the old primary's chunks must be deactivated at the same transaction as
   the new primary's chunks are activated, so there is no gap in retrieval coverage.

Dependencies
------------
- `ParseResultBatch.version_families` (Phase 2 schema 1.1.0) carries the family data
  needed to detect primacy flips.
- The chunk store must support bulk retirement by document_id.
- The ingestion worker (Phase 5) must persist and compare version-family snapshots
  across runs.

See
---
- docs/pipeline/dedup-boilerplate.md §6 (stub reference)
- docs/configuration/reference.md §2.5 `ingestion.dedup.index_superseded_versions`
- spec §7.5 primacy-flip handling
"""

# Phase 5 TODO: implement primacy_flip_payload_update_job() here.
# The function signature should be approximately:
#
#   def primacy_flip_payload_update_job(
#       prior_families: list[dict],
#       new_families: list[dict],
#       chunk_store: ...,
#   ) -> list[str]:  # list of document_ids that were flipped
#       raise NotImplementedError("Phase 5 — see module docstring")
