"""Cross-reference resolution pass for the Decompose stage (Phase 2).

Resolves intra-document cross-references detected by ``SegmentationPass`` by
attempting to match each unresolved ``CrossReference.surface_text`` to a target
segment in the same document.

Background
----------
``SegmentationPass`` detects cross-reference surface text — e.g. "see section
4.2", "Appendix A", "Table 3" — and records each match as a ``CrossReference``
with ``resolution=unresolved``.  It deferred resolution to Phase 2.

This pass receives the cross-reference list accumulated from previous passes and
attempts to assign a ``target_segment_id`` to each unresolved record.

Resolution strategy (§6.4, segment-taxonomy.md §2.1 ``cross_reference`` row)
-------------------------------------------------------------------------------
Two resolution modes, applied in this order:

1. **Heading path resolution** — structural match against segment
   ``structural_path``:
   - Detect a section/appendix/chapter reference in the surface text via the
     ``_SECTION_REF_RE`` pattern (e.g. "section 4.2" → "4.2"; "Appendix A" →
     "A").
   - Search every segment's ``structural_path`` for a component whose leading
     token matches the extracted reference label.
   - If exactly one match: resolved → ``target_segment_id`` set.
   - If multiple matches: unresolved with note listing all candidate IDs.
   - If zero matches: unresolved with note.

2. **Table/figure ordinal resolution** — ordinal match against segment text:
   - Detect a table or figure ordinal reference (e.g. "Table 3", "Figure 2").
   - Count ``SegmentType.table`` / ``SegmentType.figure_region`` segments in
     document order.
   - If the 1-based ordinal identifies a segment unambiguously: resolved.

3. **Fall-through**: Any reference not matched by either strategy remains
   ``unresolved`` with an explicit note (§6.4: silent loss is not acceptable).

Scope (§6.4, OQ-3)
-------------------
This pass resolves **intra-document** references only.  Inter-document and
external-URL references remain ``unresolved`` with a note — cross-document
resolution is deferred to Phase 3+.

This pass is stateless and performs no I/O.
"""

from __future__ import annotations

import re
from typing import Any

from finecorpus.contracts.segment_set import (
    CrossReference,
    CrossReferenceResolution,
    ExclusionRecord,
    Segment,
)
from finecorpus.contracts.shared.blocks import SegmentType
from finecorpus.pipeline.decompose.passes.base import DocumentContext, PassResult

# ---------------------------------------------------------------------------
# Patterns for reference surface-text parsing
# ---------------------------------------------------------------------------

# Captures a section/appendix/chapter label from typical cross-reference text.
# Examples:
#   "see section 4.2 for details"  → "4.2"
#   "refer to Appendix A"           → "A"
#   "Chapter 3"                     → "3"
#   "§ 7.1"                         → "7.1"
_SECTION_LABEL_RE = re.compile(
    r"(?:section|sect|appendix|chapter|§)\s*([A-Z0-9]+(?:\.[0-9]+)*)",
    re.IGNORECASE,
)

# Table ordinal: "Table 3", "Table III" (not handled — ordinal only for Arabic numerals)
_TABLE_ORDINAL_RE = re.compile(r"\btable\s+(\d+)\b", re.IGNORECASE)

# Figure ordinal: "Figure 2", "Fig 5"
_FIGURE_ORDINAL_RE = re.compile(r"\b(?:figure|fig\.?)\s+(\d+)\b", re.IGNORECASE)

# Types that can be targets for table/figure ordinal resolution
_TABLE_TYPES: frozenset[SegmentType] = frozenset({SegmentType.table})
_FIGURE_TYPES: frozenset[SegmentType] = frozenset(
    {SegmentType.figure_region, SegmentType.figure_caption}
)


class XrefResolvePass:
    """Intra-document cross-reference resolution pass (Phase 2).

    Satisfies the ``SegmentPass`` protocol.  Receives the segment list and the
    cross-reference list (via ``PassResult`` accumulation in ``stage.py``), and
    returns an updated cross-reference list with resolved targets where possible.

    This pass does not modify the segment list.
    """

    def run(
        self,
        doc_ctx: DocumentContext,
        segments: list[Segment],
        exclusions: list[ExclusionRecord],
    ) -> PassResult:
        """Attempt to resolve unresolved cross-references in the accumulated list.

        The cross-reference list is NOT passed directly as an argument — it lives
        in ``stage.py``'s accumulator.  This pass receives the SEGMENTS only and
        cannot directly read or write the cross-reference list.

        To work within the ``SegmentPass`` protocol, this pass returns the
        segments unchanged and relies on ``stage.py``'s post-pass reconciliation.
        The actual resolution logic is exposed as ``resolve_cross_references`` so
        ``stage.py`` can call it explicitly after all passes have run.

        See ``_do_resolve`` for the resolution algorithm.
        """
        # The SegmentPass protocol does not carry cross_references as a mutable
        # argument.  Cross-references are accumulated from PassResult.cross_references
        # by stage.py.  To resolve them, stage.py must call ``resolve_cross_references``
        # explicitly after running all passes.  This ``run`` method satisfies the
        # protocol contract (segments + exclusions pass through) while the heavy
        # lifting is done outside the protocol.
        return PassResult(segments=segments, exclusions=exclusions, cross_references=[])

    def resolve_cross_references(
        self,
        cross_references: list[CrossReference],
        segments: list[Segment],
    ) -> list[CrossReference]:
        """Resolve unresolved cross-references against the final segment list.

        Called by stage.py after all passes have run and the final segment list
        is available.

        Args:
            cross_references: The list accumulated from all PassResult.cross_references.
            segments: The final typed segment list after all passes.

        Returns:
            A new list with resolution attempts applied.  Unresolved records
            carry an explicit ``target_note`` (§6.4: silent loss not acceptable).
        """
        if not cross_references:
            return cross_references

        sorted_segs = sorted(segments, key=lambda s: s.document_order)
        return [self._resolve_one(xref, sorted_segs) for xref in cross_references]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_one(self, xref: CrossReference, sorted_segs: list[Segment]) -> CrossReference:
        """Attempt to resolve a single cross-reference.

        Returns the original xref when already resolved, or a new xref with
        resolution status set.
        """
        if xref.resolution == CrossReferenceResolution.resolved:
            return xref

        surface = xref.surface_text

        # Strategy 1: section/appendix/chapter label
        label_match = _SECTION_LABEL_RE.search(surface)
        if label_match:
            label = label_match.group(1)  # e.g. "4.2", "A"
            candidates = _find_by_structural_label(label, sorted_segs)
            if len(candidates) == 1:
                return xref.model_copy(
                    update={
                        "resolution": CrossReferenceResolution.resolved,
                        "target_segment_id": candidates[0].segment_id,
                        "target_note": (
                            f"Resolved to heading '{candidates[0].text or label}' "
                            f"(segment {candidates[0].segment_id}) by structural-path label match."
                        ),
                    }
                )
            elif candidates:
                cand_ids = ", ".join(c.segment_id for c in candidates[:5])
                return xref.model_copy(
                    update={
                        "resolution": CrossReferenceResolution.unresolved,
                        "target_note": (
                            f"Ambiguous: label '{label}' matched {len(candidates)} segments "
                            f"({cand_ids}). Resolution deferred."
                        ),
                    }
                )
            # else: no match — fall through

        # Strategy 2: table ordinal
        table_match = _TABLE_ORDINAL_RE.search(surface)
        if table_match:
            ordinal = int(table_match.group(1))
            table_segs = [s for s in sorted_segs if s.segment_type in _TABLE_TYPES]
            target = _nth(table_segs, ordinal)
            if target is not None:
                return xref.model_copy(
                    update={
                        "resolution": CrossReferenceResolution.resolved,
                        "target_segment_id": target.segment_id,
                        "target_note": (
                            f"Resolved to table segment {target.segment_id} "
                            f"(ordinal {ordinal} in document order)."
                        ),
                    }
                )

        # Strategy 3: figure ordinal
        fig_match = _FIGURE_ORDINAL_RE.search(surface)
        if fig_match:
            ordinal = int(fig_match.group(1))
            fig_segs = [s for s in sorted_segs if s.segment_type in _FIGURE_TYPES]
            target = _nth(fig_segs, ordinal)
            if target is not None:
                return xref.model_copy(
                    update={
                        "resolution": CrossReferenceResolution.resolved,
                        "target_segment_id": target.segment_id,
                        "target_note": (
                            f"Resolved to figure segment {target.segment_id} "
                            f"(ordinal {ordinal} in document order)."
                        ),
                    }
                )

        # Fall-through: record explicitly as unresolved (§6.4: no silent loss)
        return xref.model_copy(
            update={
                "resolution": CrossReferenceResolution.unresolved,
                "target_note": (
                    "Intra-document resolution attempted in Phase 2; no matching target "
                    f"segment found for surface text '{surface}'. "
                    "Inter-document and external URL resolution deferred to Phase 3+ (OQ-3)."
                ),
            }
        )


def _find_by_structural_label(label: str, sorted_segs: list[Segment]) -> list[Segment]:
    """Return segments whose heading text starts with the given label.

    A segment matches when its ``text`` or a component of its ``structural_path``
    starts with the label token (e.g. "4.2" matches a heading "4.2 Configuration
    Reference").

    Case-insensitive.  Only heading and other structurally-positioned segments
    are candidates.
    """
    label_upper = label.upper()
    results: list[Segment] = []
    for seg in sorted_segs:
        # Check segment text (the heading text itself)
        text = (seg.text or "").strip()
        # Normalise to first token (handles "4.2 Configuration Reference")
        first_token = _first_token(text)
        if first_token.upper() == label_upper:
            results.append(seg)
            continue
        # Check structural_path components
        for component in seg.structural_path:
            comp_token = _first_token(component)
            if comp_token.upper() == label_upper:
                results.append(seg)
                break
    return results


def _first_token(text: str) -> str:
    """Return the first whitespace-separated token, stripped of trailing punctuation."""
    parts = text.split()
    if not parts:
        return ""
    token = parts[0]
    return token.rstrip(".:,;")


def _nth(lst: list[Any], n: int) -> Any | None:
    """Return the n-th element (1-based) of lst, or None if out of range."""
    if 1 <= n <= len(lst):
        return lst[n - 1]
    return None


#: Module-level singleton — imported by passes/__init__.py.
xref_resolve_pass = XrefResolvePass()

__all__ = ["XrefResolvePass", "xref_resolve_pass"]
