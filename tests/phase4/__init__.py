"""Phase 4 tests — control-plane schema, repositories, and immutability.

Test inventory:
  - test_control_schema.py — schema creation, immutability triggers,
    break-glass, job queue, API keys, budget guard, tombstone, cost ledger.

Marks:
  qdrant_integration — requires live Qdrant + Postgres (see conftest).
                       Used for PG-gated tests (triggers, FOR UPDATE SKIP LOCKED).
"""
