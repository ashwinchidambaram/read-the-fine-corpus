"""Generated draft class descriptions (M-027).

§6.5 requires that the class-description input "feel low-effort — per class, not
per document, with generated draft text the user edits rather than a blank box."

This module produces a non-empty, plain-language *draft* for each segment class
observed in the corpus.  The user edits the draft in place (Easy or Proficient
mode); it is never a blank textarea.  The drafts are deterministic and derive
only from the segment class + observed count, so the same corpus produces the
same starting text.

The draft is a starting point, NOT a recommender-set config value — it exists to
lower the effort of providing the single most valuable human input (§6.5).  When
the user keeps or edits a draft it becomes a real ``ClassDescription`` that is
folded into ``config_version`` (attack-8 / S-R14) exactly like a hand-written one.
"""

from __future__ import annotations

from finecorpus.contracts.shared.blocks import SegmentType

# Per-class draft templates.  Each names *what the content is* and *what people
# need to get out of it* — the two questions §6.5 asks the human to answer.
_DRAFTS: dict[str, str] = {
    "prose": (
        "Narrative paragraphs and explanatory text. Readers ask about the rules, "
        "definitions, requirements, and reasoning described here."
    ),
    "heading": (
        "Section titles and headings that organise the documents. They give "
        "navigational context to the content beneath them."
    ),
    "table": (
        "Data tables of figures, comparisons, or specifications. Readers need "
        "specific values, rows, and the relationships between columns."
    ),
    "list": (
        "Bulleted or numbered lists of steps, items, or options. Readers need the "
        "individual items and their order."
    ),
    "code": (
        "Source code, commands, or configuration snippets. Readers need working "
        "examples and the exact syntax to copy."
    ),
    "figure_caption": (
        "Captions describing figures, diagrams, or images. Readers use them to "
        "find and interpret the visual they refer to."
    ),
    "figure_region": (
        "Text extracted from within figures or diagrams. Readers need the labels "
        "and annotations shown in the image."
    ),
    "form_field": (
        "Labelled form fields and their entries. Readers ask what a field means "
        "and what value it should hold."
    ),
    "boilerplate": (
        "Repeated headers, footers, and legal boilerplate. Rarely the subject of a "
        "question; kept out of default retrieval but still indexed."
    ),
    "front_matter": (
        "Title pages, tables of contents, and other front matter. Useful for "
        "identifying a document rather than answering questions about its body."
    ),
    "revision_history": (
        "Change logs and revision tables. Readers ask what changed, when, and by "
        "whom across versions."
    ),
    "cross_reference": (
        "Pointers to other sections or documents ('see section 4'). Readers follow "
        "them to the referenced content."
    ),
    "scanned_region": (
        "Text recovered from scanned or photographed pages via OCR. Quality varies; "
        "readers still expect to find the underlying content."
    ),
    "unknown": (
        "Content that did not fit a known category. Describe what it is and what "
        "readers will want from it so it can be routed correctly."
    ),
}

_FALLBACK = (
    "Describe what this content is and what readers need to get out of it. "
    "This single sentence is the most valuable input you can give — it does not "
    "exist anywhere in the documents themselves."
)


def draft_for_class(segment_class: str, *, observed_count: int | None = None) -> str:
    """Return a non-empty draft description for a segment class (M-027).

    Args:
        segment_class: The ``SegmentType`` value (e.g. ``"prose"``).
        observed_count: How many segments of this class the corpus contains, if
            known. Appended as a low-key hint so the draft is corpus-aware.

    Returns:
        A non-empty plain-language draft the user can edit. Never blank.
    """
    base = _DRAFTS.get(segment_class, _FALLBACK)
    if observed_count is not None and observed_count > 0:
        return f"{base} ({observed_count:,} segment(s) of this type were found.)"
    return base


def draft_map(class_names: list[str], counts: dict[str, int] | None = None) -> dict[str, str]:
    """Build ``{class_name: draft}`` for every observed class (M-027)."""
    counts = counts or {}
    result: dict[str, str] = {}
    for name in class_names:
        # Validate against the taxonomy so an unknown class does not silently pass.
        try:
            SegmentType(name)
        except ValueError:
            continue
        result[name] = draft_for_class(name, observed_count=counts.get(name))
    return result


__all__ = ["draft_for_class", "draft_map"]
