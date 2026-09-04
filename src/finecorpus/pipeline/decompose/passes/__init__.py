"""Decompose pass pipeline.

``PASSES`` is an **ordered list** of ``SegmentPass`` instances.  The Decompose
stage runs passes in sequence; each receives the segment list produced by the
previous pass.

Current pass order (Phase 2)
-----------------------------
1.  ``segmentation_pass``  — paragraph/heading segmentation; produces the base
    segment list from region text.
2.  ``salience_pass``      — salience tier assignment (Phase 1: no-op; Phase 2
    extension point for class-description / LLM-scored signals).
3.  ``boilerplate_pass``   — corpus-wide boilerplate reclassification (Phase 2):
    retypes segments whose normalised text matches the detected boilerplate block
    set.  Must run after salience so it can override the segment_type_prior tier.
4.  ``injection_pass``     — injection-suspicion scoring and invisible-content
    flag propagation (§14.1, Phase 2).

Pass ordering rule
------------------
*  ``segmentation_pass`` must be first — it produces the initial segment list.
*  ``salience_pass`` must follow — it has access to all structural context.
*  ``boilerplate_pass`` must follow salience — it overrides tier assignments
   made by the type prior.
*  ``injection_pass`` runs after tier-assigning passes — it reads segment text
   and page-level parse data but must NOT modify salience (M-105), so its
   position among non-tier passes is free.

Phase 3+ extension
------------------
To register a new pass (e.g. language tagging):

1.  Create ``src/finecorpus/pipeline/decompose/passes/<name>.py`` and implement
    the ``SegmentPass`` protocol (see ``base.py``).
2.  Import the module-level singleton here.
3.  Append it to ``PASSES`` at the correct position (see ordering rule above).
"""

from finecorpus.pipeline.decompose.passes.base import DocumentContext, PassResult, SegmentPass
from finecorpus.pipeline.decompose.passes.boilerplate import boilerplate_pass
from finecorpus.pipeline.decompose.passes.injection import injection_pass
from finecorpus.pipeline.decompose.passes.salience import salience_pass
from finecorpus.pipeline.decompose.passes.segmentation import segmentation_pass

#: Ordered pass list.  DecomposeStage runs these in sequence.
PASSES: list[SegmentPass] = [
    segmentation_pass,
    salience_pass,
    boilerplate_pass,
    injection_pass,  # §14.1 Phase 2 — after tier passes, never modifies tier (M-105)
]

__all__ = [
    "PASSES",
    "DocumentContext",
    "PassResult",
    "SegmentPass",
    "boilerplate_pass",
    "injection_pass",
]
