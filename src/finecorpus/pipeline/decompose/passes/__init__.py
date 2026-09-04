"""Decompose pass pipeline.

``PASSES`` is an **ordered list** of ``SegmentPass`` instances.  The Decompose
stage runs passes in sequence; each receives the segment list produced by the
previous pass.

Current pass order (Phase 2)
-----------------------------
1.  ``segmentation_pass`` — paragraph/heading segmentation; produces the base
    segment list from region text.
2.  ``salience_pass``     — salience tier assignment (Phase 1: no-op; Phase 2+
    extension point for class-description / LLM-scored signals).
3.  ``injection_pass``    — injection-suspicion scoring and invisible-content
    flag propagation (§14.1, Phase 2).

Pass ordering rule
------------------
*  ``segmentation_pass`` must be first — it produces the initial segment list.
*  ``salience_pass`` must follow — it has access to all structural context.
*  ``injection_pass`` runs after salience — it reads segment text and page-level
   parse data but must NOT modify salience (M-105).

Adding future passes
---------------------
To register a new pass (e.g. language tagging, boilerplate detection):

1.  Create ``src/finecorpus/pipeline/decompose/passes/<name>.py`` and implement
    the ``SegmentPass`` protocol (see ``base.py``).
2.  Import the module-level singleton here.
3.  Append it to ``PASSES`` at the correct position.
"""

from finecorpus.pipeline.decompose.passes.base import DocumentContext, PassResult, SegmentPass
from finecorpus.pipeline.decompose.passes.injection import injection_pass
from finecorpus.pipeline.decompose.passes.salience import salience_pass
from finecorpus.pipeline.decompose.passes.segmentation import segmentation_pass

#: Ordered pass list.  DecomposeStage runs these in sequence.
PASSES: list[SegmentPass] = [
    segmentation_pass,
    salience_pass,
    injection_pass,  # §14.1 Phase 2 — after salience, never modifies tier (M-105)
]

__all__ = [
    "PASSES",
    "DocumentContext",
    "PassResult",
    "SegmentPass",
]
