"""Superseded-version override pass for the Decompose stage (Phase 2 / D-25).

This pass runs LAST in the pipeline, after all other passes have completed.
It enforces owner ruling D-25 (2026-09-03): when
``ingestion.dedup.index_superseded_versions=True``, every segment produced from
a superseded near-duplicate document must be assigned ``salience_tier=excluded``
with a ``superseded_version`` winning signal.

This override has higher precedence than all other signals (segment_type_prior,
boilerplate_detection, etc.) because the document-level dedup ruling supersedes
any per-segment classification: the entire document is treated as an excluded
version, regardless of how its individual segments are typed.

Pass ordering in ``passes/__init__.py``:
    1. segmentation_pass
    2. salience_pass
    3. boilerplate_pass
    4. superseded_version_pass   ← this pass (wins over all prior tier assignments)

This pass is a no-op when:
- The document is not a superseded near-duplicate (``dedup_role != "superseded"``).
- The toggle is off (``index_superseded_versions=False``); in that case the
  document produces no segments at all (handled by the D-25 early-return in
  ``DecomposeStage._produce``).

The pass is always registered in PASSES.  It is cheap (a short-circuit no-op
for non-superseded documents) and having it unconditionally in the pipeline
means the toggle toggle effect is fully encapsulated in the pass logic.

This pass is stateless.  It must not perform I/O.
"""

from __future__ import annotations

from finecorpus.contracts.segment_set import ExclusionRecord, Segment
from finecorpus.contracts.shared.blocks import (
    SalienceSignal,
    SalienceSignalKind,
    SalienceTier,
)
from finecorpus.pipeline.decompose.passes.base import DocumentContext, PassResult

# ---------------------------------------------------------------------------
# Pass implementation
# ---------------------------------------------------------------------------


class SupersededVersionPass:
    """D-25 excluded-tier override pass (Phase 2).

    Satisfies the ``SegmentPass`` protocol.  Must run last — after all other
    passes — so its tier assignment wins unconditionally.

    When the document is a superseded near-duplicate and
    ``index_superseded_versions=True``:
    - Forces every segment to ``salience_tier=excluded``.
    - Replaces the winning salience signal with ``superseded_version`` (won=True);
      preserves prior signals as contributing evidence (won=False).
    - Sets ``salience_basis=superseded_version``.

    In all other cases (not superseded, or toggle=False — but toggle=False means
    the document has already been short-circuited before passes run, so this
    branch is unreachable): passes segments through unchanged.
    """

    def run(
        self,
        doc_ctx: DocumentContext,
        segments: list[Segment],
        exclusions: list[ExclusionRecord],
    ) -> PassResult:
        """Override salience tier to excluded for all segments from a superseded document.

        No-op when the document is not marked superseded.
        """
        dedup_role = doc_ctx.parse_result.get("dedup_role", "unique")
        if dedup_role != "superseded":
            # Not a superseded document — pass through unchanged.
            return PassResult(segments=segments, exclusions=exclusions)

        # Document is superseded and the toggle is on (otherwise no segments would
        # have been produced — the early-return in DecomposeStage._produce fires first).
        primary_id = doc_ctx.parse_result.get("dedup_primary_document_id") or "unknown"
        signal_detail = (
            f"Document is a superseded near-duplicate of primary '{primary_id}' "
            f"(D-25, owner ruling 2026-09-03). Indexed at excluded tier per "
            f"ingestion.dedup.index_superseded_versions=True."
        )

        override_signal = SalienceSignal(
            kind=SalienceSignalKind.superseded_version,
            implied_tier=SalienceTier.excluded,
            won=True,
            detail=signal_detail,
        )

        updated: list[Segment] = []
        for seg in segments:
            # Demote all existing signals to contributing (won=False).
            prior_signals = [
                SalienceSignal(
                    kind=s.kind,
                    implied_tier=s.implied_tier,
                    won=False,
                    detail=s.detail,
                )
                for s in seg.salience_signals
            ]
            new_signals = prior_signals + [override_signal]

            updated.append(
                seg.model_copy(
                    update={
                        "salience_tier": SalienceTier.excluded,
                        "salience_signals": new_signals,
                        "salience_basis": SalienceSignalKind.superseded_version,
                    }
                )
            )

        return PassResult(segments=updated, exclusions=exclusions)


#: Module-level singleton — imported and added to PASSES in passes/__init__.py.
superseded_version_pass = SupersededVersionPass()

__all__ = ["SupersededVersionPass", "superseded_version_pass"]
