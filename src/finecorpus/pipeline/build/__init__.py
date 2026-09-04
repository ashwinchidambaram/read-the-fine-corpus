"""Stage 5 — Build (Phase 1 real implementation).

Consumes SegmentSetBatch (version-checked) + IngestionConfig; produces Chunks via
the recursive-char chunker with complete provenance (§8); embeds via an injected
EmbeddingProvider; writes to a shadow Qdrant collection; validates; makes the
shadow eligible for promotion.

See docs/pipeline/build.md for the full reference.
"""

from finecorpus.pipeline.build.stage import BuildResult, BuildStage

__all__ = ["BuildStage", "BuildResult"]
