"""Salience pass for the Decompose stage (Phase 1).

Phase 1: This pass is intentionally a no-op — salience tiers and signals are
assigned by ``SegmentationPass`` via segment-type priors because for Phase 1
the only signal is ``segment_type_prior`` and it is cheapest to assign it
during segmentation.

This module exists as the **designated extension point** for Phase 2+ salience
work.  When Phase 2 adds new signals (class-description-based, LLM-scored,
injection-suspicion, etc.) they should be implemented here so that segmentation
remains a pure structural concern.

Extension points (Phase 2)
--------------------------
To add a new salience signal:

1.  Implement the signal logic in this module (or a helper imported here).
2.  In ``run``, iterate over ``segments``, compute the new signal, and update
    ``seg.salience_signals`` and ``seg.salience_tier`` / ``seg.salience_basis``
    as appropriate.
3.  Return a ``PassResult`` with the updated segment list.

Because ``SaliencePass`` receives the full segment list from ``SegmentationPass``,
it has access to all structural context (segment types, structural paths,
document order) needed to make cross-segment salience decisions.
"""

from __future__ import annotations

from finecorpus.contracts.segment_set import ExclusionRecord, Segment
from finecorpus.pipeline.decompose.passes.base import DocumentContext, PassResult


class SaliencePass:
    """Salience tier assignment pass (Phase 1: no-op; Phase 2+ extension point).

    Satisfies the ``SegmentPass`` protocol.  Must run after ``SegmentationPass``.
    """

    def run(
        self,
        doc_ctx: DocumentContext,
        segments: list[Segment],
        exclusions: list[ExclusionRecord],
    ) -> PassResult:
        """Phase 1: pass segments through unchanged.

        Phase 2: replace this body with signal computation logic.
        """
        return PassResult(segments=segments, exclusions=exclusions)


#: Module-level singleton — import and add to PASSES in passes/__init__.py.
salience_pass = SaliencePass()
