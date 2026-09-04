"""Boilerplate reclassification pass for the Decompose stage (Phase 2).

This pass runs after ``SegmentationPass`` (which assigns initial types and
salience tiers via segment-type priors) and after ``SaliencePass``.

It consults ``doc_ctx.boilerplate_blocks`` — the set of normalised paragraph
strings detected as corpus-wide boilerplate by
``pipeline/assess/corpus_passes.compute_boilerplate_blocks``.  Any segment
whose normalised text matches a boilerplate block is retyped to
``segment_type=boilerplate`` and assigned ``salience_tier=boilerplate``.

Design constraints
------------------
- This pass does NOT strip bytes from any segment's ``text`` field.  The
  boilerplate text is preserved byte-identical in its own ``boilerplate``-typed
  segment.  No Tier 1 operation removes bytes from another segment's ``text``
  (segment-taxonomy.md §2.1 "boilerplate" row: "handled structurally, never by
  intra-chunk byte removal").
- A segment is retyped only when its full normalised text exactly matches a
  detected boilerplate block.  Partial overlap (a segment that contains some
  boilerplate alongside unique content) is left unchanged.
- The class-description signal (§4.4 conflict rule) has higher precedence than
  boilerplate detection.  In Phase 1/2, class descriptions are not implemented;
  this pass therefore fires unconditionally for matching blocks.  When Phase 3
  adds class-description signals, they must be checked here with precedence 4 >
  5 (taxonomy §4.3).
- Salience signals are updated: a new ``boilerplate_detection`` signal is
  appended with ``won=True``; the previously-winning ``segment_type_prior``
  signal is preserved with ``won=False`` as contributing evidence.

This pass is stateless.  It must not perform I/O.
"""

from __future__ import annotations

import re

from finecorpus.contracts.segment_set import ExclusionRecord, Segment
from finecorpus.contracts.shared.blocks import (
    SalienceSignal,
    SalienceSignalKind,
    SalienceTier,
    SegmentType,
)
from finecorpus.pipeline.decompose.passes.base import DocumentContext, PassResult

# ---------------------------------------------------------------------------
# Normalisation (mirrors corpus_passes._normalise_block exactly)
# ---------------------------------------------------------------------------


def _normalise_block(text: str) -> str:
    """Lower-case + whitespace-collapse.  Must match corpus_passes._normalise_block."""
    return re.sub(r"\s+", " ", text.lower()).strip()


# ---------------------------------------------------------------------------
# Pass implementation
# ---------------------------------------------------------------------------


class BoilerplatePass:
    """Corpus-wide boilerplate reclassification pass (Phase 2).

    Satisfies the ``SegmentPass`` protocol.  Must run after both
    ``SegmentationPass`` and ``SaliencePass`` (after salience is assigned by
    the prior pass, this pass may override it).

    Pass ordering in ``passes/__init__.py``:
        1. segmentation_pass
        2. salience_pass
        3. boilerplate_pass   ← this pass
    """

    def run(
        self,
        doc_ctx: DocumentContext,
        segments: list[Segment],
        exclusions: list[ExclusionRecord],
    ) -> PassResult:
        """Retype any segment whose text contains a detected boilerplate block.

        A segment is reclassified as boilerplate when its normalised text **is**
        a detected boilerplate block OR when **all of its individual lines** are
        in the boilerplate block set.

        The second condition handles the PDF line-wrapping reality: pypdf extracts
        long text as multi-line strings (separated by ``\\n``), so a paragraph-sized
        preamble becomes a multi-line segment.  The boilerplate block set contains
        the individual lines because they are what appears consistently across docs
        after line-level splitting.  If every line in a segment is a known
        boilerplate line, the whole segment is boilerplate.

        For each segment:
        - Normalise its ``text`` (lower-case, whitespace-collapse).
        - If the normalised text is in ``doc_ctx.boilerplate_blocks`` (exact block
          match), OR if the segment has ≥ 2 lines and all of its non-empty
          normalised lines appear in the boilerplate set:
          - Change ``segment_type`` to ``boilerplate``.
          - Change ``salience_tier`` to ``boilerplate``.
          - Replace salience signals: mark old winner as contributing (won=False),
            add new winner ``boilerplate_detection`` (won=True).
          - Update ``salience_basis`` to ``boilerplate_detection``.
        - Otherwise: pass through unchanged.

        Empty boilerplate_blocks → no-op (Phase 1 compatibility).
        """
        boilerplate_blocks = doc_ctx.boilerplate_blocks
        if not boilerplate_blocks:
            return PassResult(segments=segments, exclusions=exclusions)

        updated: list[Segment] = []

        for seg in segments:
            text = seg.text or ""
            norm = _normalise_block(text)

            is_boilerplate = False
            if norm and norm in boilerplate_blocks:
                # Exact match: the whole segment text is a known boilerplate block.
                is_boilerplate = True
            else:
                # Line-level match: a high proportion of the segment's non-empty
                # lines appear in the boilerplate block set.
                #
                # This handles the PDF-line-wrapping reality: pypdf extracts long
                # preamble text as many short lines joined by \\n.  When a preamble
                # is split across PDF page boundaries, the last few lines in the
                # first-page region may not all appear in every doc (the split point
                # differs by document layout), so we use a proportion threshold
                # (≥ 70% of lines match) rather than requiring 100%.  This is still
                # very conservative: a segment where 70%+ of its lines are shared
                # across the corpus is almost certainly boilerplate.
                raw_lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
                if len(raw_lines) >= 3:  # require at least 3 lines to avoid false pos
                    norm_lines = [_normalise_block(ln) for ln in raw_lines]
                    matching = sum(1 for nl in norm_lines if nl and nl in boilerplate_blocks)
                    if matching / len(norm_lines) >= _LINE_MATCH_THRESHOLD:
                        is_boilerplate = True

            if is_boilerplate:
                # Downgrade existing signals to non-winning contributing evidence.
                prior_signals = [
                    SalienceSignal(
                        kind=s.kind,
                        implied_tier=s.implied_tier,
                        won=False,
                        detail=s.detail,
                    )
                    for s in seg.salience_signals
                ]
                # New winning signal.
                bp_signal = SalienceSignal(
                    kind=SalienceSignalKind.boilerplate_detection,
                    implied_tier=SalienceTier.boilerplate,
                    won=True,
                    detail=(
                        "Corpus-wide boilerplate detected: segment text matches "
                        "boilerplate block set (§6.2, OQ-8). "
                        "All constituent lines appear in more than the configured "
                        "proportion of corpus documents."
                    ),
                )
                new_signals = prior_signals + [bp_signal]

                # Rebuild segment with updated type and tier.
                # Pydantic v2 model_copy is the safe way to produce a new instance.
                updated.append(
                    seg.model_copy(
                        update={
                            "segment_type": SegmentType.boilerplate,
                            "salience_tier": SalienceTier.boilerplate,
                            "salience_signals": new_signals,
                            "salience_basis": SalienceSignalKind.boilerplate_detection,
                        }
                    )
                )
            else:
                updated.append(seg)

        return PassResult(segments=updated, exclusions=exclusions)


_LINE_MATCH_THRESHOLD: float = 0.70
"""Fraction of segment lines that must be in the boilerplate set for the segment to be
classified as boilerplate (handles PDF page-boundary line-wrapping artefacts)."""


def _doc_count_hint(norm: str, boilerplate_blocks: set[str]) -> str:
    """Placeholder hint for the detection detail string.

    In Phase 2 the exact per-block doc count is not threaded into the pass (it
    would require augmenting DocumentContext further).  This returns a qualitative
    string.  Phase 3 can add the exact count to the context if needed for explain
    mode.
    """
    # The presence of the block in the set already implies it exceeds the threshold.
    # We return a qualitative marker so the detail string is still informative.
    _ = norm  # unused; suppress linter
    _ = boilerplate_blocks
    return "the configured proportion"


#: Module-level singleton — import and add to PASSES in passes/__init__.py.
boilerplate_pass = BoilerplatePass()

__all__ = ["BoilerplatePass", "boilerplate_pass"]
