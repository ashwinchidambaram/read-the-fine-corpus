"""Stage 3 — Decompose (Phase 1: real prose segmentation for native-text PDFs).

Consumes ParseResultBatch (official versioned contract per D-26), emits SegmentSetBatch.

Phase 1 scope:
- Parsed/partial PDFs: paragraph segmentation, heading detection, segment typing.
- Excluded/failed documents: empty SegmentSet with ExclusionRecord.
- Frozen-artifact semantics: (document_id, content_hash, config_version) keyed cache.
"""

from finecorpus.pipeline.decompose.stage import DecomposeStage

__all__ = ["DecomposeStage"]
