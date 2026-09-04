"""Decompose pass pipeline.

``PASSES`` is an **ordered list** of ``SegmentPass`` instances.  The Decompose
stage runs passes in sequence; each receives the segment list produced by the
previous pass.

Current pass order (Phase 1)
-----------------------------
1.  ``segmentation_pass`` — paragraph/heading segmentation; produces the base
    segment list from region text.
2.  ``salience_pass``     — salience tier assignment (Phase 1: no-op; Phase 2
    extension point for class-description / LLM-scored signals).

Phase 2 extension
-----------------
To register a new pass (e.g. language tagging, boilerplate detection,
injection scoring, typing enrichment):

1.  Create ``src/finecorpus/pipeline/decompose/passes/<name>.py`` and implement
    the ``SegmentPass`` protocol (see ``base.py``).
2.  Import the module-level singleton here.
3.  Append it to ``PASSES`` (or insert at the correct position — after
    segmentation, usually after salience for signal-dependent passes).

Pass ordering rule
------------------
*  ``segmentation_pass`` must be first — it produces the initial segment list.
*  ``salience_pass`` must follow — it has access to all structural context.
*  Future passes (boilerplate, language, injection) append after salience.

Example — adding a language-tagging pass::

    from finecorpus.pipeline.decompose.passes.language import language_pass

    PASSES = [
        segmentation_pass,
        salience_pass,
        language_pass,   # ← new
    ]
"""

from finecorpus.pipeline.decompose.passes.base import DocumentContext, PassResult, SegmentPass
from finecorpus.pipeline.decompose.passes.salience import salience_pass
from finecorpus.pipeline.decompose.passes.segmentation import segmentation_pass

#: Ordered pass list.  DecomposeStage runs these in sequence.
PASSES: list[SegmentPass] = [
    segmentation_pass,
    salience_pass,
]

__all__ = [
    "PASSES",
    "DocumentContext",
    "PassResult",
    "SegmentPass",
]
