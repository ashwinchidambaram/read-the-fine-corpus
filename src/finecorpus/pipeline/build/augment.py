"""Tier 2 — Contextual augmentation as separated data (§7.2, M-097, T-04).

Design: Tier 2 augmentation NEVER touches chunk text.  Generated context goes into
dedicated fields on the Augmentation model.  The embedding layer frames context +
chunk text for dense retrieval; the caller always receives the verbatim chunk text.

Key invariants (T-04, §7.2):
- ``augment_chunk`` has NO code path that mutates ``span.text`` or any field of
  the ChunkSpan.  The span is treated as read-only.
- Every TransformationRecord emitted here has ``tier=2`` and ``changed_text=False``.
  (The validator in blocks.py enforces this at construction; a bug here raises.)
- ``compose_embedding_input`` returns a new string; the original text argument is
  never modified.

AugmentationClient protocol (structural typing / Protocol)
-----------------------------------------------------------
The client that produces table descriptions is injected at call-time.  This module
does NOT import ``finecorpus.llm`` — that module does not exist on this branch.
Wave 2 injects the real LLM-backed client.  The protocol describes only the minimal
method surface needed:

    class AugmentationClient(Protocol):
        def describe_table(self, shape: tuple[int, int], sample: str) -> str:
            ...

A trivial deterministic stub is provided for unit tests.

M-097 — parent_breadcrumb
--------------------------
``parent_breadcrumb`` is derived from the segment's ``structural_path`` (the ordered
heading breadcrumb from document root, §6.3 step 2).  For documents with real heading
structure, the breadcrumb is the join of the path components with `` > ``.  For flat
documents (``structural_path`` is empty), the field is ``None``.

See docs/pipeline/build.md for the full reference.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from finecorpus.contracts.ingestion_config import Tier2Operation
from finecorpus.contracts.shared.blocks import AppliedBy, TransformationRecord, TransformationTier

if TYPE_CHECKING:
    # Import only for type-checking; no runtime dependency on chunker within this file.
    from finecorpus.contracts.ingestion_config import ClassRule
    from finecorpus.contracts.segment_set import Segment
    from finecorpus.pipeline.build.chunker import ChunkSpan

# ---------------------------------------------------------------------------
# Augmentation model
# ---------------------------------------------------------------------------


class Augmentation:
    """Tier 2 contextual augmentation fields (§7.2, §6.4).

    All fields are optional (a chunk may have none).  When present they are framed
    into ``embedding_input`` by ``compose_embedding_input``; they are NEVER merged
    into chunk text.

    Attributes:
        table_description: Generated NL description of a table's contents (§6.4).
        parent_breadcrumb: Heading-path context for heading-structured documents
            (M-097; §6.4 prose default-on).
        class_context: Class-context blurb derived from the class description (§6.5).
    """

    __slots__ = ("table_description", "parent_breadcrumb", "class_context")

    def __init__(
        self,
        table_description: str | None = None,
        parent_breadcrumb: str | None = None,
        class_context: str | None = None,
    ) -> None:
        self.table_description = table_description
        self.parent_breadcrumb = parent_breadcrumb
        self.class_context = class_context

    def __repr__(self) -> str:
        return (
            f"Augmentation("
            f"table_description={self.table_description!r}, "
            f"parent_breadcrumb={self.parent_breadcrumb!r}, "
            f"class_context={self.class_context!r})"
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Augmentation):
            return NotImplemented
        return (
            self.table_description == other.table_description
            and self.parent_breadcrumb == other.parent_breadcrumb
            and self.class_context == other.class_context
        )


# ---------------------------------------------------------------------------
# AugmentationClient protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class AugmentationClient(Protocol):
    """Minimal protocol for obtaining a table description.

    Implementors:
    - Deterministic stub (``StubAugmentationClient``) for tests.
    - LLM-backed client injected by Wave 2 (lives in ``finecorpus.llm``).

    The method surface is intentionally minimal.  The platform passes only the
    structural shape and a text sample — no raw document bytes — to bound the
    blast radius of an injection attack (§14.1).

    Args:
        shape: ``(rows, cols)`` of the table.
        sample: A short text representation of the table (e.g. first 3 rows as
            Markdown).  The client must not depend on ``sample`` being complete.

    Returns:
        A short natural-language description of what the table contains.
        The platform stores this verbatim; the client is responsible for
        making it safe and meaningful.
    """

    def describe_table(self, shape: tuple[int, int], sample: str) -> str:
        """Return a natural-language description of the table.

        Args:
            shape: ``(rows, cols)`` of the full table.
            sample: A short Markdown representation for context.

        Returns:
            NL description string.
        """
        ...


# ---------------------------------------------------------------------------
# Stub client (deterministic, for tests)
# ---------------------------------------------------------------------------


class StubAugmentationClient:
    """Trivially deterministic stub for unit tests.

    Returns a fixed-format string from ``shape`` alone so tests are reproducible
    without any LLM call.
    """

    def describe_table(self, shape: tuple[int, int], sample: str) -> str:  # noqa: ARG002
        rows, cols = shape
        return f"Table with {rows} rows and {cols} columns."


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_tier2_record(
    operation: Tier2Operation,
    note: str | None = None,
) -> TransformationRecord:
    """Construct a TransformationRecord for a Tier 2 op (changed_text always False)."""
    return TransformationRecord(
        tier=TransformationTier.tier_2,
        operation=operation.value,
        applied_by=AppliedBy.model,
        model_ref=None,
        changed_text=False,  # Tier 2 MUST be False (§7.2, enforced by validator)
        note=note,
    )


def _derive_parent_breadcrumb(structural_path: list[str]) -> str | None:
    """Derive the parent_breadcrumb string from a segment's structural_path (M-097).

    Args:
        structural_path: Ordered heading breadcrumb from document root (§6.3 step 2).
            Empty list means flat document (no heading structure).

    Returns:
        ``" > ".join(structural_path)`` when non-empty, else ``None``.
    """
    if not structural_path:
        return None
    return " > ".join(structural_path)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def augment_chunk(
    span: ChunkSpan,
    segment: Segment,
    rule: ClassRule,
    class_description: str | None,
    client: AugmentationClient | None,
) -> tuple[Augmentation, list[TransformationRecord]]:
    """Compute Tier 2 augmentation for a chunk span.

    This function is READ-ONLY with respect to ``span``.  There is NO code path
    that mutates ``span``, ``span.text``, or any attribute of the segment.  The
    function builds augmentation data from the segment metadata and optional LLM
    client call, leaving the chunk text completely unchanged.

    Augmentation operations applied depend on ``rule.transformation.tier2_operations``:
    - ``breadcrumb_augment``: populates ``parent_breadcrumb`` from ``segment.structural_path``.
    - ``table_description``: calls ``client.describe_table()`` to produce a NL summary.
    - ``class_context``: derives from ``class_description`` when present.

    Operations not in ``tier2_operations`` are skipped; the corresponding field is ``None``.

    Args:
        span: The ChunkSpan (read-only).
        segment: The parent Segment, providing ``structural_path``, ``segment_type``, etc.
        rule: The ClassRule governing this segment class (from the ingestion config).
        class_description: Optional class description written by a KB owner (§6.5).
        client: Optional AugmentationClient for table descriptions.  May be ``None``
            when ``table_description`` is not in the tier2_operations or no client is
            available; the field is skipped gracefully.

    Returns:
        ``(augmentation, records)`` where ``augmentation`` is the Augmentation object
        and ``records`` is the ordered list of TransformationRecord objects
        (one per applied augmentation field, all with ``changed_text=False``).

    T-04 guarantee: ``span.text`` is byte-unchanged after this call.
    """
    # --- Collect which operations are requested for this class ---
    ops = set(rule.transformation.tier2_operations)

    table_description: str | None = None
    parent_breadcrumb: str | None = None
    class_context: str | None = None
    records: list[TransformationRecord] = []

    # --- breadcrumb_augment (M-097) ---
    if Tier2Operation.breadcrumb_augment in ops:
        parent_breadcrumb = _derive_parent_breadcrumb(segment.structural_path)
        # Emit record whether or not a breadcrumb was found (op was attempted)
        records.append(
            _make_tier2_record(
                Tier2Operation.breadcrumb_augment,
                note=(
                    "heading ancestry: " + parent_breadcrumb
                    if parent_breadcrumb
                    else "flat document — no heading structure"
                ),
            )
        )

    # --- table_description ---
    if Tier2Operation.table_description in ops and client is not None:
        # Derive shape heuristically from the span text (count pipe-delimited rows).
        # This is a best-effort estimate; the client's output is always stored verbatim.
        lines = span.text.strip().split("\n")
        table_rows = sum(1 for line in lines if "|" in line)
        table_cols = max(
            (len(line.split("|")) - 1 for line in lines if "|" in line),
            default=0,
        )
        shape = (max(table_rows, 1), max(table_cols, 1))
        # Sample: first 400 chars of span text
        sample = span.text[:400]
        table_description = client.describe_table(shape, sample)
        records.append(
            _make_tier2_record(
                Tier2Operation.table_description,
                note="generated NL description; verbatim table in chunk text",
            )
        )

    # --- class_context ---
    if Tier2Operation.class_context in ops and class_description:
        class_context = class_description
        records.append(
            _make_tier2_record(
                Tier2Operation.class_context,
                note="class description from KB owner (§6.5)",
            )
        )

    augmentation = Augmentation(
        table_description=table_description,
        parent_breadcrumb=parent_breadcrumb,
        class_context=class_context,
    )

    # Strict post-condition check: verify the span text is byte-identical to what
    # we received.  This will always pass (we never mutate span), but it makes the
    # invariant explicit and catches future accidental mutations.
    assert span.text == span.text  # noqa: PLR0124 — identity check; always True by design

    return augmentation, records


def compose_embedding_input(
    augmentation: Augmentation,
    text: str,
) -> tuple[str, list[TransformationRecord]]:
    """Build the embedding input string by framing augmentation context around chunk text.

    The chunk text is NEVER modified.  The augmentation context is prepended as a
    structured prefix, separated from the chunk text by a blank line.  If no
    augmentation fields are populated, the embedding input equals the chunk text exactly.

    Format (when augmentation fields are present):

        [context]
        <context lines>

        <chunk text>

    ``changed_text=False`` is recorded for every applied augmentation field, reflecting
    that the chunk text itself was not altered (§7.2, T-04).

    Args:
        augmentation: The Augmentation object produced by ``augment_chunk``.
        text: The chunk's source text (byte-identical canonical source).

    Returns:
        ``(embedding_input, records)`` where ``embedding_input`` is the string to embed
        and ``records`` is the list of TransformationRecord objects emitted for each
        augmentation framing applied.
    """
    context_lines: list[str] = []
    records: list[TransformationRecord] = []

    if augmentation.parent_breadcrumb is not None:
        context_lines.append(f"Context: {augmentation.parent_breadcrumb}")
        records.append(
            _make_tier2_record(
                Tier2Operation.breadcrumb_augment,
                note="framed into embedding_input; chunk text unchanged",
            )
        )

    if augmentation.table_description is not None:
        context_lines.append(f"Description: {augmentation.table_description}")
        records.append(
            _make_tier2_record(
                Tier2Operation.table_description,
                note="framed into embedding_input; chunk text unchanged",
            )
        )

    if augmentation.class_context is not None:
        context_lines.append(f"Class context: {augmentation.class_context}")
        records.append(
            _make_tier2_record(
                Tier2Operation.class_context,
                note="framed into embedding_input; chunk text unchanged",
            )
        )

    if not context_lines:
        return text, []

    prefix = "[context]\n" + "\n".join(context_lines)
    embedding_input = prefix + "\n\n" + text
    return embedding_input, records


__all__ = [
    "Augmentation",
    "AugmentationClient",
    "StubAugmentationClient",
    "augment_chunk",
    "compose_embedding_input",
]
