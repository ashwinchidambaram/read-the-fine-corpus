"""Taxonomy typing pass for the Decompose stage (Phase 2).

This pass promotes ``detected_class_hint`` values emitted by parsers (HTML,
spreadsheet, PDF) into the full §6.3 SegmentType assignments, completing the
typing story that segmentation_pass begins.

Background
----------
Parsers emit a ``detected_class_hint`` field on each region (§6.3, parse-result
contract): ``prose``, ``table``, ``code``, ``list``, ``figure``, ``form_field``,
``other``.  ``SegmentationPass`` already promotes ``table`` hints to
``SegmentType.table`` (D-11).  All other hints arrived at Decompose as ``prose``
or ``unknown`` typed segments.  This pass completes the promotion.

Design constraints
------------------
- **Boilerplate wins** (taxonomy §4.3, §4.4): This pass MUST NOT retype a
  segment whose ``segment_type`` is already ``boilerplate``.  Boilerplate is set
  by ``BoilerplatePass`` (which runs before this pass) and its precedence (5) is
  higher than the type-prior (7).  Since taxonomy typing is a type-prior signal,
  it cannot override boilerplate.
- **OCR-confidence salience is never modified**: This pass retypes segments but
  does NOT alter ``salience_tier`` or ``salience_signals`` on any segment where
  the winning signal is ``ocr_confidence_floor`` or ``ocr_confidence_warn``.
  Those signals come from ``SaliencePass`` (taxonomy §4.1 precedences 3 and 6)
  and must not be overridden by a type-prior (precedence 7).
- **scanned_region segments are left as-is**: Their type is set by
  ``SegmentationPass`` for low-confidence OCR pages and must not be changed.
- **Salience update**: When a segment's type changes, its ``salience_tier`` and
  ``salience_signals`` are updated using the new type's default tier from §4.2,
  UNLESS a higher-precedence signal already won (see above).
- **Table stays table**: Segments already typed ``table`` by ``SegmentationPass``
  (D-11) are passed through; this pass does not reprocess them.
- **Dominant-type rule** (taxonomy §2.2): A segment takes the type of its source
  region's hint.  Mixed-content minority-type splitting is OQ-5 and not
  implemented here.

Hint-to-type mapping
--------------------
| ``detected_class_hint`` | ``SegmentType`` |
|---|---|
| ``code``       | ``code``          |
| ``list``       | ``list_``         |
| ``figure``     | ``figure_region`` |
| ``form_field`` | ``form_field``    |
| ``prose``      | (no change)       |
| ``other``      | (no change)       |
| ``table``      | already handled by ``SegmentationPass``; no-op here |
| ``None``       | (no change)       |

Pass ordering
-------------
Registered AFTER ``boilerplate_pass`` and BEFORE ``superseded_version_pass``
(see ``passes/__init__.py``).  The ordering ensures:
  1. ``SegmentationPass`` produces the initial type/salience.
  2. ``SaliencePass`` applies OCR-confidence overrides.
  3. ``BoilerplatePass`` retypes corpus-wide boilerplate.
  4. **This pass** (``TaxonomyPass``) promotes hint-driven types.
  5. ``InjectionPass`` scores injection suspicion (reads type; never changes tier).
  6. ``SupersededVersionPass`` forces tier=excluded (D-25; runs last, wins all).

This pass is stateless and performs no I/O.
"""

from __future__ import annotations

from finecorpus.contracts.segment_set import ExclusionRecord, Segment
from finecorpus.contracts.shared.blocks import (
    SalienceSignal,
    SalienceSignalKind,
    SalienceTier,
    SegmentType,
)
from finecorpus.pipeline.decompose.passes.base import DocumentContext, PassResult

# ---------------------------------------------------------------------------
# Default salience tier by type (mirrors segmentation.py _TYPE_TO_TIER, §4.2)
# ---------------------------------------------------------------------------

_TYPE_TO_TIER: dict[SegmentType, SalienceTier] = {
    SegmentType.prose: SalienceTier.primary,
    SegmentType.heading: SalienceTier.supporting,
    SegmentType.table: SalienceTier.primary,
    SegmentType.list_: SalienceTier.primary,
    SegmentType.code: SalienceTier.primary,
    SegmentType.figure_caption: SalienceTier.supporting,
    SegmentType.figure_region: SalienceTier.supporting,
    SegmentType.form_field: SalienceTier.supporting,
    SegmentType.boilerplate: SalienceTier.boilerplate,
    SegmentType.front_matter: SalienceTier.supporting,
    SegmentType.revision_history: SalienceTier.supporting,
    SegmentType.cross_reference: SalienceTier.supporting,
    SegmentType.scanned_region: SalienceTier.supporting,
    SegmentType.unknown: SalienceTier.supporting,
}

# ---------------------------------------------------------------------------
# Hint-to-SegmentType promotion table
# ---------------------------------------------------------------------------

# Only hints that change the type are listed.  prose/other/table/None are no-ops.
_HINT_TO_TYPE: dict[str, SegmentType] = {
    "code": SegmentType.code,
    "list": SegmentType.list_,
    "figure": SegmentType.figure_region,
    "form_field": SegmentType.form_field,
}

# Signal kinds that occupy precedence > segment_type_prior (taxonomy §4.1 §4.3).
# When any of these is the winning signal on a segment, we must NOT change the
# salience_tier (the type may still change, but salience is locked by the prior winner).
_OCR_WINNING_SIGNALS: frozenset[SalienceSignalKind] = frozenset(
    {
        SalienceSignalKind.ocr_confidence_floor,
        SalienceSignalKind.ocr_confidence_warn,
    }
)

# Segment types that must never be retyped by this pass.
_IMMUTABLE_TYPES: frozenset[SegmentType] = frozenset(
    {
        SegmentType.boilerplate,  # boilerplate_pass wins (precedence 5 > 7)
        SegmentType.heading,  # heading type is set by heuristic; hint does not refine it
        SegmentType.scanned_region,  # OCR path; type is not hint-driven
        SegmentType.table,  # already promoted by SegmentationPass (D-11)
        SegmentType.front_matter,  # structural; not promoted by hint
        SegmentType.revision_history,  # structural; not promoted by hint
    }
)


class TaxonomyPass:
    """Full taxonomy typing pass (Phase 2).

    Satisfies the ``SegmentPass`` protocol.

    For each segment:
    1.  Build a ``region_id → detected_class_hint`` index from the parse result.
    2.  Skip segments whose type is in ``_IMMUTABLE_TYPES``.
    3.  Look up the dominant hint for the segment's source regions.
    4.  If the hint maps to a SegmentType in ``_HINT_TO_TYPE``:
        a.  Update ``segment_type``.
        b.  Update ``salience_tier`` using the new type's default tier from §4.2,
            UNLESS the winning signal is an OCR-confidence override (which has
            higher precedence than the type-prior).
        c.  Rebuild ``salience_signals``: mark the prior winner as contributing
            (won=False), append a new ``segment_type_prior`` signal for the new
            type (won=True) — unless salience is locked by an OCR signal, in
            which case only the type changes and signals are left intact.
    5.  Return the updated segment list.

    Segments with no hint, with a hint that maps to no change (prose/other/table),
    or with types in ``_IMMUTABLE_TYPES`` pass through unchanged.
    """

    def run(
        self,
        doc_ctx: DocumentContext,
        segments: list[Segment],
        exclusions: list[ExclusionRecord],
    ) -> PassResult:
        """Apply hint-driven type promotion to segments.

        Pure and deterministic — same inputs produce same outputs.
        """
        # Build region_id → hint index from the parse result.
        region_hints: dict[str, str] = {}
        for region in doc_ctx.parse_result.get("regions", []):
            rid = region.get("region_id", "")
            hint = region.get("detected_class_hint")
            if hint is not None:
                # hint may be a RegionClassHint enum instance or a string
                if hasattr(hint, "value"):
                    hint = hint.value
                region_hints[rid] = hint

        updated: list[Segment] = []
        for seg in segments:
            promoted = _promote_segment(seg, region_hints)
            updated.append(promoted)

        return PassResult(segments=updated, exclusions=exclusions)


def _dominant_hint(source_region_ids: list[str], region_hints: dict[str, str]) -> str | None:
    """Return the dominant detected_class_hint for a segment's source regions.

    When a segment spans multiple regions (rare but possible after sub-decomposition),
    the first region's hint is used — the segment inherits the class of its first
    source region.  ``None`` when no hint is available.
    """
    for rid in source_region_ids:
        hint = region_hints.get(rid)
        if hint is not None:
            return hint
    return None


def _salience_locked_by_ocr(seg: Segment) -> bool:
    """Return True if the winning salience signal is an OCR-confidence override.

    When True, this pass must not modify salience_tier or salience_signals —
    OCR-confidence signals have precedence 3 and 6 (taxonomy §4.3), which is
    higher than segment_type_prior (precedence 7).
    """
    return seg.salience_basis in _OCR_WINNING_SIGNALS


def _promote_segment(seg: Segment, region_hints: dict[str, str]) -> Segment:
    """Return a (potentially retyped) segment.

    Returns the original segment object unchanged when no promotion applies
    (avoids an unnecessary model_copy allocation on the hot path).
    """
    if seg.segment_type in _IMMUTABLE_TYPES:
        return seg

    hint = _dominant_hint(seg.source_region_ids, region_hints)
    if hint is None:
        return seg

    new_type = _HINT_TO_TYPE.get(hint)
    if new_type is None or new_type == seg.segment_type:
        return seg

    # Determine new salience
    ocr_locked = _salience_locked_by_ocr(seg)

    if ocr_locked:
        # Salience is locked — only retype; leave salience_tier and signals intact.
        return seg.model_copy(update={"segment_type": new_type})

    # Salience is free — update tier and signals to reflect the new type.
    new_tier = _TYPE_TO_TIER[new_type]

    # Mark all existing signals as contributing (not winning).
    prior_signals = [
        SalienceSignal(
            kind=s.kind,
            implied_tier=s.implied_tier,
            won=False,
            detail=s.detail,
        )
        for s in seg.salience_signals
    ]

    # Append new winning signal for the promoted type.
    new_signal = SalienceSignal(
        kind=SalienceSignalKind.segment_type_prior,
        implied_tier=new_tier,
        won=True,
        detail=(
            f"Taxonomy pass: promoted hint '{hint}' → {new_type.value} "
            f"(§6.3, segment-taxonomy.md §2.1). "
            f"Default tier for {new_type.value}: {new_tier.value} (taxonomy §4.2)."
        ),
    )
    new_signals = prior_signals + [new_signal]

    return seg.model_copy(
        update={
            "segment_type": new_type,
            "salience_tier": new_tier,
            "salience_signals": new_signals,
            "salience_basis": SalienceSignalKind.segment_type_prior,
        }
    )


#: Module-level singleton — imported by passes/__init__.py.
taxonomy_pass = TaxonomyPass()

__all__ = ["TaxonomyPass", "taxonomy_pass"]
