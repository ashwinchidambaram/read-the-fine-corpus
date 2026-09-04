"""SegmentPass protocol for the Decompose stage.

Every decompose pass must implement the ``SegmentPass`` protocol.  Passes are
pure functions over the document context and the segment list: they receive
the current segment list (and document context), transform it, and return the
updated list.  They must be side-effect-free and deterministic.

Extension points (Phase 2)
--------------------------
To add a new decompose pass (e.g. boilerplate detection, language tagging,
injection scoring, typing enrichment):

1.  Create ``src/finecorpus/pipeline/decompose/passes/<name>.py`` and define
    a class or module-level singleton that satisfies ``SegmentPass``.

2.  Implement ``run(doc_ctx, segments) -> list[Segment]``.  The pass receives
    the full document context (parse result dict, tenancy, timestamps) and the
    current segment list.  It must return a new list — do not mutate in place.

3.  Append (or insert at the correct position) your pass instance to ``PASSES``
    in ``passes/__init__.py``.

Pass ordering rule
------------------
*  ``SegmentationPass`` must run first — it produces the initial segment list
   from raw region text.
*  ``SaliencePass`` must run after segmentation — it assigns salience tiers.
*  Future passes (boilerplate, language, injection) append after salience.

The final segment list returned by the last pass is used verbatim by
``stage.py`` to build the ``SegmentSet``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from finecorpus.contracts.segment_set import CrossReference, ExclusionRecord, Segment
from finecorpus.contracts.shared.blocks import TenancyBlock


@dataclass
class DocumentContext:
    """Context threaded through every decompose pass.

    Passes that need access to the source parse result (for regions, findings,
    parse_status) read it from ``parse_result``.  The other fields provide
    traceability context (tenancy, timestamps, IDs) without requiring each pass
    to accept them as separate arguments.

    Attributes:
        document_id: Stable document identifier from the inventory.
        content_hash: SHA-256 content fingerprint.
        tenancy: Workspace / KB / permission block.
        parse_result: Full parse result dict for this document (the entry from
            ``ParseResultBatch.results``).
        decomposed_at: Run timestamp from the orchestrator.
    """

    document_id: str
    content_hash: str
    tenancy: TenancyBlock
    parse_result: dict[str, Any]
    decomposed_at: datetime


@dataclass
class PassResult:
    """Return type from a ``SegmentPass``.

    A pass may produce new segments and/or new exclusion records.  Exclusions
    produced during segmentation (empty region, too-short paragraph) are
    accumulated here so ``stage.py`` can build the final ``SegmentSet``
    exclusion list without each pass mutating shared state.

    Attributes:
        segments: Updated segment list (replace the previous list in full).
        exclusions: Any new ExclusionRecords produced by this pass.
        cross_references: Any CrossReference records detected by this pass.
            Most passes leave this empty; only the segmentation pass populates it
            in Phase 1.  The stage accumulates cross-references from all passes.
    """

    segments: list[Segment]
    exclusions: list[ExclusionRecord]
    cross_references: list[CrossReference] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.cross_references is None:
            self.cross_references = []


class SegmentPass(Protocol):
    """Protocol every decompose pass must satisfy.

    A pass is a stateless object with a single ``run`` method.  It transforms
    the segment list and may accumulate exclusion records for content that
    cannot be segmented (empty regions, too-short paragraphs, etc.).

    ``run(doc_ctx, segments, exclusions)``
        Receives the document context, the current segment list, and the
        accumulated exclusion list from previous passes.  Returns a
        ``PassResult`` with the updated segments and any new exclusions.
    """

    def run(
        self,
        doc_ctx: DocumentContext,
        segments: list[Segment],
        exclusions: list[ExclusionRecord],
    ) -> PassResult:
        """Transform segments and return updated lists.

        Must be pure and deterministic — same inputs must always produce the
        same outputs.  Must not perform I/O or mutate arguments in place.
        """
        ...
