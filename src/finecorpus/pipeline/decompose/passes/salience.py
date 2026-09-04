"""Salience pass for the Decompose stage (Phase 1 + Phase 2 OCR signals).

Phase 1 base: segment-type prior signal (assigned inline by SegmentationPass).

Phase 2 extension: OCR-confidence signals for scanned regions.

OCR salience rules (taxonomy OQ-7, docs/configuration/reference.md §2.5)
-------------------------------------------------------------------------
Applies to any segment that carries a non-None ``ocr_confidence`` value,
regardless of segment type (scanned_region, or sub-decomposed prose/heading
from a high-confidence OCR page).

  - confidence < ocr_confidence_exclude_floor (default 0.60):
      Signal ``ocr_confidence_floor`` fires as the WINNING signal.
      Tier → ``excluded`` (still indexed; tier controls retrieval, §6.3).

  - ocr_confidence_exclude_floor <= confidence < ocr_confidence_warn_level
    (default 0.60 – 0.80):
      Signal ``ocr_confidence_warn`` fires as the WINNING signal.
      Tier → ``supporting`` + a low-confidence flag recorded on the segment.
      (The flag is recorded in the signal detail; a future Phase 3 field
      or report can surface it.)

  - confidence >= ocr_confidence_warn_level (default 0.80):
      No OCR override — segment keeps its type-prior tier (``primary`` or
      ``supporting`` per the taxonomy).

Signal precedence (taxonomy §4.1):
  ocr_confidence_floor > ocr_confidence_warn > segment_type_prior

The winning signal's ``won`` field is True; all others are False.  This pass
updates the existing ``salience_signals`` list on the segment: it marks the
prior signal as won=False when overridden, appends the new OCR signal as won=True,
and updates ``salience_tier`` and ``salience_basis`` accordingly.
"""

from __future__ import annotations

from finecorpus.contracts.segment_set import ExclusionRecord, Segment
from finecorpus.contracts.shared.blocks import (
    SalienceSignal,
    SalienceSignalKind,
    SalienceTier,
)
from finecorpus.pipeline.decompose.passes.base import DocumentContext, PassResult


class SaliencePass:
    """Salience tier assignment pass (Phase 1 no-op extended in Phase 2 for OCR signals).

    Satisfies the ``SegmentPass`` protocol.  Must run after ``SegmentationPass``.

    Phase 1 documents (native-text PDFs) pass through unchanged — all segments
    have ``ocr_confidence=None`` and the type-prior tier set by SegmentationPass
    is correct.

    Phase 2 documents (scanned PDFs) have segments with ``ocr_confidence`` set.
    This pass overrides the tier for those segments per the OCR confidence rules
    (taxonomy OQ-7).
    """

    def run(
        self,
        doc_ctx: DocumentContext,
        segments: list[Segment],
        exclusions: list[ExclusionRecord],
    ) -> PassResult:
        """Apply OCR-confidence salience overrides where applicable.

        Segments with ``ocr_confidence=None`` (native text) pass through unchanged.
        Segments with ``ocr_confidence`` set receive tier overrides per the
        configured thresholds from ``doc_ctx``.
        """
        floor = doc_ctx.ocr_confidence_exclude_floor
        warn = doc_ctx.ocr_confidence_warn_level

        updated: list[Segment] = []
        for seg in segments:
            confidence = seg.ocr_confidence
            if confidence is None:
                # Native-text segment: no OCR override
                updated.append(seg)
                continue

            if confidence < floor:
                # Below exclude floor → excluded tier (OQ-7)
                new_seg = _override_salience(
                    seg,
                    new_tier=SalienceTier.excluded,
                    signal_kind=SalienceSignalKind.ocr_confidence_floor,
                    detail=(
                        f"OCR confidence {confidence:.4f} < exclude floor {floor:.2f} "
                        "→ excluded tier (§6.4, OQ-7)."
                    ),
                )
            elif confidence < warn:
                # Between floor and warn → supporting + low-confidence flag (OQ-7)
                new_seg = _override_salience(
                    seg,
                    new_tier=SalienceTier.supporting,
                    signal_kind=SalienceSignalKind.ocr_confidence_warn,
                    detail=(
                        f"OCR confidence {confidence:.4f} in warn band [{floor:.2f}, {warn:.2f}) "
                        "→ supporting tier with low-confidence flag (§6.4, OQ-7)."
                    ),
                )
            else:
                # Above warn level → keep type-prior tier, no override needed
                updated.append(seg)
                continue

            updated.append(new_seg)

        return PassResult(segments=updated, exclusions=exclusions)


def _override_salience(
    seg: Segment,
    new_tier: SalienceTier,
    signal_kind: SalienceSignalKind,
    detail: str,
) -> Segment:
    """Return a new Segment with OCR-confidence signal overriding the type-prior tier.

    The existing type-prior signal is retained in ``salience_signals`` but
    marked ``won=False``.  The new OCR signal is appended with ``won=True``.

    Uses ``dataclasses.replace`` on the underlying pydantic model (via
    ``model_copy``) to keep the change functional (no mutation).
    """
    # Mark all existing signals as not-won
    old_signals = [
        SalienceSignal(
            kind=s.kind,
            implied_tier=s.implied_tier,
            won=False,
            detail=s.detail,
        )
        for s in seg.salience_signals
    ]

    # Append the winning OCR signal
    ocr_signal = SalienceSignal(
        kind=signal_kind,
        implied_tier=new_tier,
        won=True,
        detail=detail,
    )
    new_signals = old_signals + [ocr_signal]

    return seg.model_copy(
        update={
            "salience_tier": new_tier,
            "salience_signals": new_signals,
            "salience_basis": signal_kind,
        }
    )


#: Module-level singleton — import and add to PASSES in passes/__init__.py.
salience_pass = SaliencePass()
