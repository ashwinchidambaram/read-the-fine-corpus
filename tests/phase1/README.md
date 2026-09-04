# Phase 1 Acceptance Tests

This package contains the formal acceptance layer for Phase 1 of Read The Fine Corpus.
Tests here prove the phase acceptance criteria end-to-end and will run for the life of
the project.

## Spec mapping

| Test file | §18.3 test | §19 Phase 1 acceptance line |
|-----------|------------|-----------------------------|
| `test_t01_alias_swap_under_load.py` | **Test 1** — alias swap under sustained query load, zero errors | "alias swap test passes" (§4.5: 100% availability during swap) |
| `test_t07_mismatch.py` | **Test 7** — embedding model mismatch fails closed, provider never called | "embedding-mismatch test passes" |
| `test_parity.py` | — (provider parity, not numbered in §18.3) | "the same corpus ingests and serves correctly under BOTH providers" |
| `test_throughput_baseline.py` | — (baseline recorder, §4.5) | "ingestion throughput baseline recorded per provider" |

## Marks

| Mark | Meaning | How to run |
|------|---------|------------|
| `qdrant_integration` | Requires live Qdrant + Postgres | `pytest -m qdrant_integration` |
| `provider_integration` | Requires live embedding provider (Ollama and/or OpenAI) | `pytest -m "qdrant_integration and provider_integration"` |

## Running the full suite

```bash
# 1. Bring up infrastructure
docker compose -f docker-compose.yml -f docker-compose.integration.yml up -d

# 2. Run the full phase1 suite (integration tier)
uv run pytest tests/phase1/ -v -m "qdrant_integration" -s

# 3. Run with provider tier (requires Ollama at localhost:11434)
uv run pytest tests/phase1/ -v -m "qdrant_integration and provider_integration" -s

# 4. Tear down
docker compose -f docker-compose.yml -f docker-compose.integration.yml down
```

## Notes

- **T-01** (`test_t01_alias_swap_under_load.py`) replaces the Phase 1 proxy in
  `tests/index/test_integration.py::TestAtomicAliasSwap`. The proxy remains as a
  lightweight structural check of the Qdrant adapter layer alone.

- **T-07** (`test_t07_mismatch.py`) goes through the REAL alias lifecycle (ingest
  → promote → query with wrong model), whereas the unit-level test in
  `tests/retrieval/test_integration.py::TestRoundTrip::test_model_mismatch_fail_closed`
  seeds data directly without exercising the real promotion path.

- **Throughput baseline** numbers are written to `docs/baselines/phase1-ingestion.md`.
  That document is the canonical MUST-NOT-REGRESS reference for §4.5. The regression
  gate (comparing future runs against this baseline) is enforced in later phases.
