"""Stage 2 — Assess (Phase 1: real native-text PDF parsing via pypdf).

Consumes Inventory, emits ParseResultBatch (official versioned contract per D-26).

Phase 1 scope: native-text PDFs only. All other file types are honestly excluded
(parse_status=excluded_pre_parse) with a finding explaining why.

Real parsing: per-page text extraction, quality scoring, encoding issue detection,
encrypted/malformed/image-only detection.
"""

from finecorpus.pipeline.assess.stage import AssessStage

__all__ = ["AssessStage"]
