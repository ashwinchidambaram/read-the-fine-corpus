"""Phase 1 acceptance tests — §19 Phase 1 acceptance criteria.

This package contains the formal acceptance layer for Phase 1 of Read The Fine
Corpus.  Tests here prove that the phase acceptance criteria are met end-to-end
and will run for the life of the project.

Test inventory (see README.md for the mapping to spec sections):

- test_t01_alias_swap_under_load.py — T-01: alias swap, zero query errors
- test_t07_mismatch.py             — T-07: embedding model mismatch, fail-closed
- test_parity.py                   — Provider parity (Ollama / OpenAI)
- test_throughput_baseline.py      — Ingestion throughput baseline recorder

Marks:
  qdrant_integration   — requires live Qdrant + Postgres (see conftest)
  provider_integration — requires live embedding provider (Ollama or OpenAI)
"""
