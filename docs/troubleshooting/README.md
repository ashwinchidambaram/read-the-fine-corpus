# Troubleshooting

Status: placeholder — populated from Phase 1 onward, keyed to real error conditions as they
ship (spec §4.6). Every error code the platform can emit gets an entry here in the same PR
that introduces it.

Designed error conditions so far (see the pages that define them):

- `EMBEDDING_MODEL_MISMATCH` — [provider abstraction](../architecture/provider-abstraction.md)
- `VECTOR_DB_UNAVAILABLE`, result-status taxonomy — [retrieval response](../contracts/retrieval-response.md)
- Validation-gate failures, tombstone replay failure — [index lifecycle](../architecture/index-lifecycle.md)
