"""Decompose pass pipeline.

``PASSES`` is an **ordered list** of ``SegmentPass`` instances.  The Decompose
stage runs passes in sequence; each receives the segment list produced by the
previous pass.

Current pass order (Phase 2)
-----------------------------
1.  ``segmentation_pass``      — paragraph/heading segmentation; produces the base
    segment list from region text.
2.  ``salience_pass``          — salience tier assignment (Phase 1: no-op; Phase 2
    extension point for class-description / LLM-scored signals).
3.  ``boilerplate_pass``       — corpus-wide boilerplate reclassification (Phase 2):
    retypes segments whose normalised text matches the detected boilerplate block
    set.  Must run after salience so it can override the segment_type_prior tier.
4.  ``taxonomy_pass``          — full taxonomy typing (Phase 2): promotes
    ``detected_class_hint`` values from parsers into the complete §6.3 SegmentType
    assignments (code, list_, figure_region, form_field).  Must run after
    boilerplate_pass — boilerplate wins (taxonomy §4.3 precedence 5 > 7).
5.  ``language_pass``          — per-segment BCP-47 language detection (Phase 2,
    §7.6).  Runs after taxonomy_pass so segment types are final before tagging.
6.  ``xref_resolve_pass``      — intra-document cross-reference resolution (Phase 2,
    §6.4).  Resolves ``CrossReference`` records produced by segmentation_pass by
    attempting to match surface text to target segments.  The actual resolution
    is called from stage.py after all passes complete (the SegmentPass.run call
    here is a no-op pass-through; see xref_resolve.py for details).
7.  ``injection_pass``         — injection-suspicion scoring and invisible-content
    flag propagation (§14.1, Phase 2).
8.  ``superseded_version_pass``— D-25 override (Phase 2): forces ``salience_tier=excluded``
    on every segment produced from a superseded near-duplicate document when
    ``ingestion.dedup.index_superseded_versions=True``.  Must run LAST so its
    tier assignment wins over all prior passes.  No-op for non-superseded documents.

Pass ordering rule
------------------
*  ``segmentation_pass`` must be first — it produces the initial segment list.
*  ``salience_pass`` must follow — it has access to all structural context.
*  ``boilerplate_pass`` must follow salience — it overrides tier assignments
   made by the type prior.
*  ``taxonomy_pass`` must follow boilerplate_pass — boilerplate types are
   immutable by design (taxonomy §4.3 precedence 5 > 7).
*  ``language_pass`` follows taxonomy_pass — segment types must be final before
   language tagging.
*  ``xref_resolve_pass`` runs in the pass list to register its presence; actual
   resolution is called by stage.py after all passes (needs the final segment list).
*  ``injection_pass`` runs after tier-assigning passes — it reads segment text
   and page-level parse data but must NOT modify salience (M-105), so its
   position among non-tier passes is free.
*  ``superseded_version_pass`` must run last — it overrides ALL prior tier
   assignments for documents that are superseded near-duplicates (D-25, owner
   ruling 2026-09-03).

Phase 4+ extension
------------------
To register a new pass:

1.  Create ``src/finecorpus/pipeline/decompose/passes/<name>.py`` and implement
    the ``SegmentPass`` protocol (see ``base.py``).
2.  Import the module-level singleton here.
3.  Append it to ``PASSES`` at the correct position (see ordering rule above).
"""

from finecorpus.pipeline.decompose.passes.base import DocumentContext, PassResult, SegmentPass
from finecorpus.pipeline.decompose.passes.boilerplate import boilerplate_pass
from finecorpus.pipeline.decompose.passes.injection import injection_pass
from finecorpus.pipeline.decompose.passes.language import language_pass
from finecorpus.pipeline.decompose.passes.salience import salience_pass
from finecorpus.pipeline.decompose.passes.segmentation import segmentation_pass
from finecorpus.pipeline.decompose.passes.superseded import superseded_version_pass
from finecorpus.pipeline.decompose.passes.taxonomy import taxonomy_pass
from finecorpus.pipeline.decompose.passes.xref_resolve import xref_resolve_pass

#: Ordered pass list.  DecomposeStage runs these in sequence.
PASSES: list[SegmentPass] = [
    segmentation_pass,
    salience_pass,
    boilerplate_pass,
    taxonomy_pass,  # Phase 2: hint-driven full taxonomy typing (after boilerplate)
    language_pass,  # Phase 2: per-segment BCP-47 language detection (§7.6)
    xref_resolve_pass,  # Phase 2: xref resolution registration (actual work in stage.py)
    injection_pass,  # §14.1 Phase 2 — after tier passes, never modifies tier (M-105)
    superseded_version_pass,  # D-25 — must be last; overrides all prior tiers
]

__all__ = [
    "PASSES",
    "DocumentContext",
    "PassResult",
    "SegmentPass",
    "boilerplate_pass",
    "injection_pass",
    "language_pass",
    "superseded_version_pass",
    "taxonomy_pass",
    "xref_resolve_pass",
]
