"""Stage 2 — Assess (Phase 0 skeleton).

Pass-through skeleton: consumes Inventory, emits a minimal but contract-VALID
ParseResult for each document in the inventory.

Each ParseResult is marked with:
  parse_status=excluded_pre_parse — the honest "not yet parsed" value from ParseStatus.
  (alternative: 'failed' — but excluded_pre_parse is more honest for a skeleton stage
   that has not attempted parsing at all)

Every inventory item gets an entry — nothing silently dropped (§6 rule 6).

Phase 1 will replace this with real parsing.
"""

from finecorpus.pipeline.assess.stage import AssessStage

__all__ = ["AssessStage"]
