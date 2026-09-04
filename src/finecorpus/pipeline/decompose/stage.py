"""Stage 3 — Decompose (Phase 1: real prose segmentation for native-text PDFs).

Produces a SegmentSetBatch artifact: one SegmentSet per document.

Phase 1 scope — prose segmentation of native-text PDF content:
  - Paragraph segmentation: blank-line / layout-based splitting of extracted text.
  - Heading detection (simple heuristics — see HEADING DETECTION LIMITS below).
  - Segment types from the taxonomy: prose, heading, front_matter (trivially detectable
    first-page title block), unknown for undecidable content.
  - Structural path breadcrumbs derived from detected headings.
  - Salience via type priors only (segment_type_prior signal — class descriptions
    are Phase 3).
  - document_order: dense, gapless, 0-based.
  - Reassembly record: sha256 of concatenated segment text, proving reassembly.
  - Frozen-artifact semantics: SegmentSet persisted on (document_id, content_hash,
    config_version) key; second run reuses without recomputing.

HEADING DETECTION LIMITS (documented honestly):
  - Phase 1 uses a line-level heuristic: a line is treated as a heading if it:
    (a) is short (≤ _HEADING_MAX_CHARS chars),
    (b) does not end with a sentence-ending punctuation ('.', '?', '!'),
    (c) matches at least one of:
        - starts with a numbering pattern (e.g. "1.", "2.1", "Section 4", "§6"),
        - is ALL CAPS (>= _HEADING_ALLCAPS_MIN_LEN chars),
        - appears at the start of a paragraph after a blank line and is isolated
          (next line is blank or the paragraph is only one line — this catches
          section titles that have their own paragraph).
  - Accuracy on fixtures: headings in FPDF-generated PDFs extract cleanly on separate
    lines with font-size changes, so detection works well for the clean_native, policy,
    form, bloated_manual and boilerplate fixtures. Heading lines are not tagged with
    font metadata by pypdf, so font-size detection is NOT used.
  - FALSE POSITIVES: short prose sentences without terminal punctuation can be
    mis-classified as headings. This is a known Phase 1 limitation — Phase 2 will
    add font-size metadata from pypdf for more reliable detection.
  - FALSE NEGATIVES: headings without numbering patterns and in mixed-case that are
    embedded in flow text may not be detected.
  - pypdf outline: used when present; outline entries are projected onto the closest
    matching text segment to override heuristic classification.

NON-PDF DOCUMENTS:
  Documents with parse_status=excluded_pre_parse or failed (non-PDF, encrypted, etc.)
  produce an empty SegmentSet with an ExclusionRecord explaining the exclusion.
  The reassembly_digest is sha256("") and covered_region_ids is empty.

FROZEN ARTIFACT SEMANTICS:
  The SegmentSet is keyed on (document_id, content_hash, config_version).
  config_version = "p1.0" for Phase 1 (no class descriptions, no LLM signals).
  If the ArtifactStore already holds a SegmentSet JSON for the key, it is loaded
  and returned without recomputation. The reuse path is implemented via the
  ArtifactStore's segment_set cache directory under <artifacts_root>/segment_sets/.

D-26 resolution: SegmentSetBatch is now an official versioned contract.
PlanStage checks schema_version against SUPPORTED_SEGMENT_SET_BATCH.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from finecorpus.contracts.segment_set import (
    CrossReferenceResolution,
    ExclusionReason,
    ExclusionRecord,
    ReassemblyMethod,
    ReassemblyRecord,
    Segment,
    SegmentSet,
)
from finecorpus.contracts.segment_set_batch import (
    BATCH_SCHEMA_VERSION,
    SegmentSetBatch,
)
from finecorpus.contracts.shared.blocks import (
    LocatorKind,
    PermissionFidelity,
    PermissionMode,
    PermissionSource,
    SalienceSignal,
    SalienceSignalKind,
    SalienceTier,
    SegmentType,
    SourceLocation,
    TenancyBlock,
)
from finecorpus.contracts.versions import SUPPORTED_PARSE_RESULT_BATCH
from finecorpus.pipeline.stage import Stage

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CONFIG_VERSION = "p1.0"
"""Phase 1 config version — no class descriptions, no LLM signals."""

_HEADING_MAX_CHARS = 120
"""Maximum character count for a line to be a heading candidate."""

_HEADING_ALLCAPS_MIN_LEN = 4
"""Minimum chars for an ALL-CAPS line to qualify as a heading (avoids "A.", "OK", etc.)."""

_MIN_SEGMENT_CHARS = 5
"""Minimum chars for a non-empty segment to be retained (avoids micro-segments)."""

_NEAR_EMPTY_PAGE_CHARS = 50
"""Chars below which a page text is considered near-empty."""

# Heading patterns: numbering-style headings.
_HEADING_NUMBERING_RE = re.compile(
    r"^(?:"
    r"\d+\."  # "1."
    r"|\d+\.\d+"  # "2.1"
    r"|\d+\.\d+\.\d+"  # "3.2.1"
    r"|[A-Z]\."  # "A."
    r"|Section\s+\d+"  # "Section 4"
    r"|§\s*\d+"  # "§6"
    r"|Appendix\s+[A-Z0-9]+"  # "Appendix A"
    r"|Chapter\s+\d+"  # "Chapter 3"
    r")"
    r"\s+\S",  # followed by a space and at least one non-space char
    re.IGNORECASE,
)

_SENTENCE_END_RE = re.compile(r"[.?!]\s*$")

# Cross-reference detection pattern
_CROSS_REF_RE = re.compile(
    r"(?:see|refer to|as (per|defined in|described in|shown in|noted in))\s+"
    r"(?:section|sect|fig|figure|table|appendix|chapter|§)\s*[\d.A-Z]+",
    re.IGNORECASE,
)

# Known revision-history section title patterns
_REVISION_HISTORY_RE = re.compile(
    r"^(?:revision\s+history|change\s+log|change\s+history|revision\s+log)$",
    re.IGNORECASE,
)

# Front-matter keywords for first-page detection
_FRONT_MATTER_RE = re.compile(
    r"(?:version|rev(?:ision)?|date|author|document\s+no|effective\s+date"
    r"|prepared\s+by|approved\s+by|classification)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# ULID-like deterministic ID generator (content-addressed, not time-based)
# ---------------------------------------------------------------------------


def _segment_id(document_id: str, order: int) -> str:
    """Generate a stable segment ID from document_id + order.

    Not a true ULID (no timestamp); deterministic from inputs so IDs are
    stable across runs for the same document version.
    """
    raw = f"{document_id}:seg:{order}".encode()
    return "seg-" + hashlib.sha256(raw).hexdigest()[:24]


def _exclusion_id(document_id: str, suffix: str) -> str:
    raw = f"{document_id}:excl:{suffix}".encode()
    return "excl-" + hashlib.sha256(raw).hexdigest()[:24]


# ---------------------------------------------------------------------------
# Heading detection
# ---------------------------------------------------------------------------


def _is_heading(line: str, is_first_in_paragraph: bool) -> bool:
    """Return True if `line` looks like a heading.

    Heuristics (Phase 1, documented in module docstring):
    1. Length ≤ _HEADING_MAX_CHARS
    2. Does NOT end with sentence-ending punctuation
    3. AND one of:
       a. Matches a numbering pattern
       b. Is ALL-CAPS and long enough to avoid acronyms
       c. Is an isolated first line (no sentence end, short, first in paragraph)
    """
    stripped = line.strip()
    if not stripped or len(stripped) > _HEADING_MAX_CHARS:
        return False
    if _SENTENCE_END_RE.search(stripped):
        return False

    # Criterion a: numbering pattern
    if _HEADING_NUMBERING_RE.match(stripped):
        return True

    # Criterion b: ALL CAPS
    if stripped.isupper() and len(stripped) >= _HEADING_ALLCAPS_MIN_LEN and not stripped.isdigit():
        return True

    # Criterion c: isolated first-in-paragraph short line
    if is_first_in_paragraph and len(stripped) <= 60:  # noqa: PLR2004
        return True

    return False


# ---------------------------------------------------------------------------
# Paragraph + segment splitting
# ---------------------------------------------------------------------------


def _split_into_paragraphs(text: str) -> list[str]:
    """Split page text into paragraphs by blank lines.

    Two or more consecutive newlines (possibly with whitespace between) delimit
    paragraph boundaries. Single newlines within a paragraph are preserved but
    treated as soft line breaks (they stay in the paragraph text).
    """
    # Normalize: replace \r\n and \r to \n
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # Split on blank lines (one or more blank lines)
    raw_blocks = re.split(r"\n\s*\n+", text)
    paragraphs = []
    for block in raw_blocks:
        block = block.strip()
        if block:
            paragraphs.append(block)
    return paragraphs


def _paragraph_to_lines(para: str) -> list[str]:
    """Split a paragraph into its constituent lines."""
    return para.split("\n")


# ---------------------------------------------------------------------------
# Segment type prior → salience tier
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


def _type_prior_signal(seg_type: SegmentType) -> SalienceSignal:
    """Build a segment_type_prior SalienceSignal for the given type."""
    tier = _TYPE_TO_TIER[seg_type]
    return SalienceSignal(
        kind=SalienceSignalKind.segment_type_prior,
        implied_tier=tier,
        won=True,
        detail=f"Phase 1 type prior: {seg_type.value} → {tier.value}",
    )


# ---------------------------------------------------------------------------
# Full document decomposition
# ---------------------------------------------------------------------------


def _decompose_parsed_document(
    document_id: str,
    content_hash: str,
    tenancy: TenancyBlock,
    parse_result: dict[str, Any],
    decomposed_at: datetime,
) -> SegmentSet:
    """Decompose a successfully-parsed (or partial) document into segments.

    Processes each page's region text in document order, splitting into paragraphs,
    detecting headings, and building Segment records.

    Returns a SegmentSet with:
    - segments: list of Segment objects in document order
    - reassembly: digest over concatenated segment text
    - exclusions: any regions explicitly excluded (empty regions)
    - cross_references: intra-document cross-references detected
    """
    regions = parse_result.get("regions", [])
    doc_order = 0
    segments: list[Segment] = []
    exclusions: list[ExclusionRecord] = []
    all_region_ids: list[str] = []
    covered_region_ids: list[str] = []

    # Current heading breadcrumb (stack)
    structural_path: list[str] = []

    # Within-heading-scope ordinal per path (for segment_path stability)
    # Key: tuple(structural_path), Value: next ordinal
    path_ordinals: dict[tuple[str, ...], int] = defaultdict(int)

    # Cross-reference candidates (collected post-decomposition)
    cross_ref_candidates: list[dict[str, Any]] = []

    # Track if we're on the first page (for front_matter detection)
    first_page_done = False
    first_page_num: int | None = None

    for region in regions:
        region_id = region.get("region_id", "")
        all_region_ids.append(region_id)
        extract_status = region.get("extract_status", "")
        text = region.get("text") or ""
        location_raw = region.get("location", {})
        page_num = location_raw.get("page_start", 1)

        # --- Failed/empty regions → exclusion record ---
        if extract_status in ("failed", "empty") or not text.strip():
            excl_loc = SourceLocation(
                locator_kind=LocatorKind.page,
                page_start=location_raw.get("page_start", 1),
                page_end=location_raw.get("page_end", location_raw.get("page_start", 1)),
            )
            reason = (
                ExclusionReason.parse_failed
                if extract_status == "failed"
                else ExclusionReason.empty_region
            )
            exclusions.append(
                ExclusionRecord(
                    exclusion_id=_exclusion_id(document_id, region_id),
                    location=excl_loc,
                    source_region_ids=[region_id],
                    reason=reason,
                    reason_detail=(
                        "Page content stream decompression failed — partial parse."
                        if extract_status == "failed"
                        else "No text extracted from this page."
                    ),
                    reversible=True,
                )
            )
            continue

        covered_region_ids.append(region_id)

        # --- Determine if this is a first-page front_matter block ---
        is_first_page = first_page_num is None or page_num == first_page_num
        if first_page_num is None:
            first_page_num = page_num

        # --- Paragraph splitting ---
        paragraphs = _split_into_paragraphs(text)

        for para_idx, para_text in enumerate(paragraphs):
            if not para_text.strip():
                continue

            lines = _paragraph_to_lines(para_text)
            first_line = lines[0].strip() if lines else ""
            is_single_line_para = len(lines) == 1

            # --- Heading detection ---
            # First line of paragraph: check if it's a heading
            if first_line and _is_heading(first_line, is_first_in_paragraph=True):
                # Emit heading segment
                heading_text = first_line

                # Check for revision history section
                if _REVISION_HISTORY_RE.match(heading_text.strip()):
                    seg_type = SegmentType.revision_history
                    # revision history headings contribute to the path normally
                elif is_single_line_para:
                    seg_type = SegmentType.heading
                else:
                    seg_type = SegmentType.heading

                # Update structural path: heading level from numbering depth
                level = _heading_level(heading_text)
                structural_path = _update_structural_path(structural_path, heading_text, level)

                path_key = tuple(structural_path)
                path_ordinals[path_key]  # ensure key exists
                seg_path = _build_segment_path(structural_path, path_ordinals[path_key])
                path_ordinals[path_key] += 1

                # Only emit heading as a separate segment if it's isolated (single-line para)
                # or if the remaining lines need a different type.
                seg_loc = SourceLocation(
                    locator_kind=LocatorKind.page,
                    page_start=page_num,
                    page_end=page_num,
                )
                prior_signal = _type_prior_signal(seg_type)
                segments.append(
                    Segment(
                        segment_id=_segment_id(document_id, doc_order),
                        document_order=doc_order,
                        segment_type=seg_type,
                        salience_tier=_TYPE_TO_TIER[seg_type],
                        structural_path=list(structural_path[:-1]),  # path *above* this heading
                        segment_path=seg_path,
                        location=seg_loc,
                        source_region_ids=[region_id],
                        language="und",  # Phase 2: language detection
                        ocr_confidence=None,
                        injection_suspicion=0.0,
                        invisible_content_flags=[],
                        sensitivity_flags=[],
                        salience_signals=[prior_signal],
                        salience_basis=SalienceSignalKind.segment_type_prior,
                        text=heading_text,
                    )
                )
                doc_order += 1

                # Remaining lines of paragraph become prose
                remaining_lines = lines[1:]
                remaining_text = "\n".join(remaining_lines).strip()
                if remaining_text and len(remaining_text) >= _MIN_SEGMENT_CHARS:
                    path_key2 = tuple(structural_path)
                    seg_path2 = _build_segment_path(structural_path, path_ordinals[path_key2])
                    path_ordinals[path_key2] += 1

                    prose_type = _classify_prose(remaining_text, is_first_page, para_idx, page_num)
                    prior2 = _type_prior_signal(prose_type)
                    segments.append(
                        Segment(
                            segment_id=_segment_id(document_id, doc_order),
                            document_order=doc_order,
                            segment_type=prose_type,
                            salience_tier=_TYPE_TO_TIER[prose_type],
                            structural_path=list(structural_path),
                            segment_path=seg_path2,
                            location=seg_loc,
                            source_region_ids=[region_id],
                            language="und",
                            ocr_confidence=None,
                            injection_suspicion=0.0,
                            invisible_content_flags=[],
                            sensitivity_flags=[],
                            salience_signals=[prior2],
                            salience_basis=SalienceSignalKind.segment_type_prior,
                            text=remaining_text,
                        )
                    )
                    # Check for cross-references
                    if _CROSS_REF_RE.search(remaining_text):
                        cross_ref_candidates.append(
                            {
                                "segment_id": _segment_id(document_id, doc_order),
                                "text": remaining_text,
                                "page_num": page_num,
                            }
                        )
                    doc_order += 1
                elif remaining_text:
                    # Post-heading remainder is too short to segment — record as exclusion (F-02)
                    excl_suffix = f"short-remainder-{region_id}-{para_idx}"
                    exclusions.append(
                        ExclusionRecord(
                            exclusion_id=_exclusion_id(document_id, excl_suffix),
                            location=seg_loc,
                            source_region_ids=[region_id],
                            reason=ExclusionReason.too_short,
                            reason_detail=(
                                f"Post-heading remainder ({len(remaining_text)} chars) "
                                f"below minimum segment length ({_MIN_SEGMENT_CHARS} chars). "
                                "Recorded as exclusion per §12 (no silent content loss)."
                            ),
                            reversible=True,
                        )
                    )
            else:
                # Not a heading paragraph — classify and emit as prose/front_matter/unknown
                stripped_para = para_text.strip()
                if not stripped_para:
                    continue
                if len(stripped_para) < _MIN_SEGMENT_CHARS:
                    # Too-short paragraph: record as exclusion instead of silently dropping (F-02)
                    excl_suffix = f"short-para-{region_id}-{para_idx}"
                    seg_loc_short = SourceLocation(
                        locator_kind=LocatorKind.page,
                        page_start=page_num,
                        page_end=page_num,
                    )
                    exclusions.append(
                        ExclusionRecord(
                            exclusion_id=_exclusion_id(document_id, excl_suffix),
                            location=seg_loc_short,
                            source_region_ids=[region_id],
                            reason=ExclusionReason.too_short,
                            reason_detail=(
                                f"Paragraph ({len(stripped_para)} chars) below minimum "
                                f"segment length ({_MIN_SEGMENT_CHARS} chars). "
                                "Recorded as exclusion per §12 (no silent content loss)."
                            ),
                            reversible=True,
                        )
                    )
                    continue

                prose_type = _classify_prose(para_text, is_first_page, para_idx, page_num)

                path_key = tuple(structural_path)
                seg_path = _build_segment_path(structural_path, path_ordinals[path_key])
                path_ordinals[path_key] += 1

                seg_loc = SourceLocation(
                    locator_kind=LocatorKind.page,
                    page_start=page_num,
                    page_end=page_num,
                )
                prior_signal = _type_prior_signal(prose_type)
                segments.append(
                    Segment(
                        segment_id=_segment_id(document_id, doc_order),
                        document_order=doc_order,
                        segment_type=prose_type,
                        salience_tier=_TYPE_TO_TIER[prose_type],
                        structural_path=list(structural_path),
                        segment_path=seg_path,
                        location=seg_loc,
                        source_region_ids=[region_id],
                        language="und",
                        ocr_confidence=None,
                        injection_suspicion=0.0,
                        invisible_content_flags=[],
                        sensitivity_flags=[],
                        salience_signals=[prior_signal],
                        salience_basis=SalienceSignalKind.segment_type_prior,
                        text=para_text.strip(),
                    )
                )
                # Check for cross-references in prose
                if _CROSS_REF_RE.search(para_text):
                    cross_ref_candidates.append(
                        {
                            "segment_id": _segment_id(document_id, doc_order),
                            "text": para_text,
                            "page_num": page_num,
                        }
                    )
                doc_order += 1

        if not first_page_done and page_num == first_page_num:
            first_page_done = True

    # Build reassembly record
    # Reassemble: order segments by document_order, concat text (None → "").
    # Excluded regions do not contribute text (they were failed/empty extractions).
    reassembly_text = "".join(
        (seg.text or "") for seg in sorted(segments, key=lambda s: s.document_order)
    )
    reassembly_digest = hashlib.sha256(reassembly_text.encode()).hexdigest()

    # Build cross-references (unresolved for Phase 1 — intra-doc resolution is Phase 2)
    from finecorpus.contracts.segment_set import CrossReference  # noqa: PLC0415

    cross_references = []
    for xref in cross_ref_candidates:
        match = _CROSS_REF_RE.search(xref["text"])
        if match:
            surface = match.group(0)
            xref_loc = SourceLocation(
                locator_kind=LocatorKind.page,
                page_start=xref["page_num"],
                page_end=xref["page_num"],
            )
            xref_id = _exclusion_id(document_id, f"xref-{xref['segment_id']}")
            cross_references.append(
                CrossReference(
                    xref_id=xref_id,
                    from_segment_id=xref["segment_id"],
                    surface_text=surface,
                    location=xref_loc,
                    resolution=CrossReferenceResolution.unresolved,
                    target_segment_id=None,
                    target_note=(
                        "Intra-document cross-reference recorded as unresolved in Phase 1. "
                        "Resolution deferred to Phase 2."
                    ),
                )
            )

    reassembly = ReassemblyRecord(
        method=ReassemblyMethod.document_order_concat,
        covered_region_ids=covered_region_ids,
        reassembly_digest=reassembly_digest,
    )

    return SegmentSet(
        schema_version="1.1.0",
        tenancy=tenancy,
        document_id=document_id,
        content_hash=content_hash,
        segments=segments,
        reassembly=reassembly,
        exclusions=exclusions,
        cross_references=cross_references,
        decomposed_at=decomposed_at,
    )


def _classify_prose(
    text: str,
    is_first_page: bool,
    para_idx: int,
    page_num: int,
) -> SegmentType:
    """Classify a prose paragraph as front_matter, prose, or unknown.

    Phase 1 heuristics:
    - front_matter: if on page 1, para_idx < 3, and contains front-matter keywords.
    - unknown: if the text is very short and ambiguous (< _MIN_SEGMENT_CHARS * 2).
    - prose: everything else.
    """
    stripped = text.strip()
    if not stripped:
        return SegmentType.unknown

    if is_first_page and page_num == 1 and para_idx < 3 and _FRONT_MATTER_RE.search(stripped):
        return SegmentType.front_matter

    if len(stripped) < _MIN_SEGMENT_CHARS * 2:
        return SegmentType.unknown

    return SegmentType.prose


def _heading_level(heading_text: str) -> int:
    """Estimate heading level from text (1 = top-level).

    Uses numbering depth: "1." = 1, "1.1" = 2, "1.1.1" = 3, else 1.
    Non-numbered headings default to level 1.
    """
    m = re.match(r"^(\d+(?:\.\d+)*)\s", heading_text.strip())
    if m:
        return len(m.group(1).split("."))
    # ALL-CAPS or Section X: treat as level 1
    return 1


def _update_structural_path(path: list[str], heading: str, level: int) -> list[str]:
    """Return an updated structural path after encountering a heading.

    Trims the path to `level - 1` depth (popping deeper levels) then appends
    the new heading. This models a heading hierarchy.
    """
    # Keep only the first `level - 1` entries
    new_path = path[: level - 1]
    new_path.append(heading.strip())
    return new_path


def _build_segment_path(structural_path: list[str], ordinal: int) -> str:
    """Build the canonical segment_path string.

    Format: "/".join(structural_path) + "#" + ordinal
    An empty structural_path yields "#0", "#1", etc.

    This matches the proposed default in segment-set.md OQ-1:
    `"/".join(structural_path) + "#" + within_heading_ordinal`
    The ordinal counts segments under the same breadcrumb in document_order.
    """
    base = "/".join(structural_path)
    return f"{base}#{ordinal}"


# ---------------------------------------------------------------------------
# Empty segment set for excluded/failed documents
# ---------------------------------------------------------------------------

_EMPTY_REASSEMBLY_DIGEST = hashlib.sha256(b"").hexdigest()


def _make_empty_segment_set(
    document_id: str,
    content_hash: str,
    tenancy: TenancyBlock,
    parse_result: dict[str, Any],
    decomposed_at: datetime,
) -> SegmentSet:
    """Build an empty SegmentSet for documents that cannot be decomposed.

    Used for:
    - parse_status=excluded_pre_parse (non-PDF, HTML, spreadsheet, etc.)
    - parse_status=failed (encrypted, unreadable)

    An ExclusionRecord is added for each region in the parse result
    (typically empty for pre-parse excluded docs, but present for failed ones).
    """
    regions = parse_result.get("regions", [])
    findings = parse_result.get("findings", [])
    parse_status = parse_result.get("parse_status", "excluded_pre_parse")

    # Determine exclusion reason
    finding_codes = [f.get("code", "") for f in findings]
    if "password_protected" in finding_codes:
        reason = ExclusionReason.encrypted
        reason_detail = "Document is password-protected. No text extraction possible."
    elif "unservable_image_only" in finding_codes:
        reason = ExclusionReason.unservable_content
        reason_detail = "Image-only PDF: no text layer present. OCR required (Phase 2)."
    elif "unservable_audio" in finding_codes:
        reason = ExclusionReason.unservable_content
        reason_detail = "Audio file: no text extraction possible."
    elif "unservable_video" in finding_codes:
        reason = ExclusionReason.unservable_content
        reason_detail = "Video file: no text extraction possible."
    elif "unservable_cad" in finding_codes:
        reason = ExclusionReason.unservable_content
        reason_detail = "CAD/binary file: no text extraction possible."
    elif "excluded_content_type_html" in finding_codes:
        reason = ExclusionReason.other
        reason_detail = "HTML excluded in Phase 1 scope (native-text PDF only)."
    elif "excluded_content_type_spreadsheet" in finding_codes:
        reason = ExclusionReason.other
        reason_detail = "Spreadsheet excluded in Phase 1 scope (native-text PDF only)."
    elif parse_status == "failed":
        reason = ExclusionReason.parse_failed
        reason_detail = "Document parse failed; no segments produced."
    else:
        reason = ExclusionReason.other
        reason_detail = "Document type not supported in Phase 1 (native-text PDF scope only)."

    # Build exclusion records for any non-empty regions
    exclusions: list[ExclusionRecord] = []
    for region in regions:
        region_id = region.get("region_id", "")
        if not region_id:
            continue
        region_loc_raw = region.get("location", {})
        excl_loc = SourceLocation(
            locator_kind=LocatorKind.page,
            page_start=region_loc_raw.get("page_start", 1),
            page_end=region_loc_raw.get("page_end", region_loc_raw.get("page_start", 1)),
        )
        exclusions.append(
            ExclusionRecord(
                exclusion_id=_exclusion_id(document_id, region_id),
                location=excl_loc,
                source_region_ids=[region_id],
                reason=reason,
                reason_detail=reason_detail,
                reversible=True,
            )
        )

    # If no regions (typical for pre-parse excluded), add a single document-level exclusion
    if not exclusions and not regions:
        excl_loc = SourceLocation(
            locator_kind=LocatorKind.byte_range,
            byte_start=0,
            byte_end=0,
        )
        exclusions.append(
            ExclusionRecord(
                exclusion_id=_exclusion_id(document_id, "doc-level"),
                location=excl_loc,
                source_region_ids=[],
                reason=reason,
                reason_detail=reason_detail,
                reversible=True,
            )
        )

    return SegmentSet(
        schema_version="1.1.0",
        tenancy=tenancy,
        document_id=document_id,
        content_hash=content_hash,
        segments=[],
        reassembly=ReassemblyRecord(
            method=ReassemblyMethod.document_order_concat,
            covered_region_ids=[],
            reassembly_digest=_EMPTY_REASSEMBLY_DIGEST,
        ),
        exclusions=exclusions,
        cross_references=[],
        decomposed_at=decomposed_at,
    )


# ---------------------------------------------------------------------------
# Frozen-artifact store helpers
# ---------------------------------------------------------------------------


def _frozen_artifact_key(document_id: str, content_hash: str, config_version: str) -> str:
    """Build the cache key string for a frozen segment set.

    Uses the full 64-char content_hash (sha256 hex) per the documented key spec.
    Truncation increases collision probability and must not be used.
    """
    return f"{document_id}__{content_hash}__{config_version}"


def _frozen_artifact_path(
    artifacts_root: Path,
    document_id: str,
    content_hash: str,
    config_version: str,
) -> Path:
    """Return the path where a frozen SegmentSet JSON is stored."""
    key = _frozen_artifact_key(document_id, content_hash, config_version)
    cache_dir = artifacts_root / "segment_sets"
    return cache_dir / f"{key}.json"


def _load_frozen_artifact(path: Path) -> dict[str, Any] | None:
    """Load a frozen segment set artifact if it exists, else return None."""
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


def _save_frozen_artifact(path: Path, segment_set_dict: dict[str, Any]) -> None:
    """Persist a segment set to the frozen artifact cache."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(segment_set_dict, indent=2, default=str, ensure_ascii=False),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Stage
# ---------------------------------------------------------------------------


class DecomposeStage(Stage):
    """Stage 3 — Decompose (Phase 1: real prose segmentation for native-text PDFs).

    Consumes ParseResultBatch, emits SegmentSetBatch.

    D-26 resolution: consumed ParseResultBatch is now version-checked via
    SUPPORTED_PARSE_RESULT_BATCH (no longer opts out of check_version).

    Frozen-artifact semantics: for each document, the SegmentSet is persisted
    to <artifacts_root>/segment_sets/<key>.json on first computation. Subsequent
    runs with the same (document_id, content_hash, config_version) load the
    persisted artifact without recomputing.

    Args:
        run_started_at: Single run timestamp threaded from the orchestrator.
        artifacts_root: Used for the frozen-artifact cache; required for reuse.
    """

    name = "decompose"
    consumed_contract = "parse_result_batch"
    consumed_version_range = SUPPORTED_PARSE_RESULT_BATCH  # D-26: version-checked now
    produced_contract = "segment_set_batch"
    output_model = SegmentSetBatch

    def __init__(
        self,
        run_started_at: datetime | None = None,
        artifacts_root: Path | str | None = None,
        run_id: str = "",
    ) -> None:
        self._run_started_at = run_started_at or datetime.now(tz=UTC)
        self._artifacts_root = Path(artifacts_root) if artifacts_root else None
        self._run_id = run_id

    def _produce(self, input_data: dict[str, Any] | None) -> dict[str, Any]:
        """Produce one SegmentSet per parse result entry.

        For documents that parsed (or partially parsed): real segment decomposition.
        For excluded/failed documents: empty SegmentSet with ExclusionRecord.

        Frozen-artifact reuse: if artifacts_root is set and the frozen artifact
        exists for (document_id, content_hash, config_version), load it without
        recomputing.
        """
        assert input_data is not None, "Decompose requires ParseResultBatch input"

        results = input_data.get("results", [])

        # Extract tenancy from the first parse result (all share the same tenancy)
        if results:
            tenancy_raw = results[0].get("tenancy", {})
        else:
            tenancy_raw = {}

        tenancy = TenancyBlock(
            workspace_id=tenancy_raw.get("workspace_id", ""),
            kb_id=tenancy_raw.get("kb_id", ""),
            permission_mode=tenancy_raw.get("permission_mode", PermissionMode.public_to_kb),
            permission_principals=tenancy_raw.get("permission_principals", []),
            permission_source=tenancy_raw.get("permission_source", PermissionSource.platform),
            permission_fidelity=tenancy_raw.get(
                "permission_fidelity", PermissionFidelity.authoritative
            ),
            permission_resolved_at=None,
        )

        decomposed_at = self._run_started_at
        segment_sets: list[dict[str, Any]] = []

        for parse_result in results:
            document_id = parse_result["document_id"]
            content_hash = parse_result["content_hash"]
            parse_status = parse_result.get("parse_status", "excluded_pre_parse")

            # Check frozen artifact cache
            if self._artifacts_root is not None:
                frozen_path = _frozen_artifact_path(
                    self._artifacts_root, document_id, content_hash, _CONFIG_VERSION
                )
                cached = _load_frozen_artifact(frozen_path)
                if cached is not None:
                    segment_sets.append(cached)
                    continue

            # Compute segment set
            if parse_status in ("parsed", "partial"):
                segment_set = _decompose_parsed_document(
                    document_id=document_id,
                    content_hash=content_hash,
                    tenancy=tenancy,
                    parse_result=parse_result,
                    decomposed_at=decomposed_at,
                )
            else:
                # excluded_pre_parse, failed, unreadable → empty segment set
                segment_set = _make_empty_segment_set(
                    document_id=document_id,
                    content_hash=content_hash,
                    tenancy=tenancy,
                    parse_result=parse_result,
                    decomposed_at=decomposed_at,
                )

            segment_set_dict = segment_set.model_dump(mode="json")

            # Persist to frozen artifact cache
            if self._artifacts_root is not None:
                frozen_path = _frozen_artifact_path(
                    self._artifacts_root, document_id, content_hash, _CONFIG_VERSION
                )
                _save_frozen_artifact(frozen_path, segment_set_dict)

            segment_sets.append(segment_set_dict)

        batch = SegmentSetBatch(
            schema_version=BATCH_SCHEMA_VERSION,
            contract="segment_set_batch",
            run_id=self._run_id,
            produced_at=self._run_started_at,
            skeleton=None,  # real run
            segment_sets=segment_sets,
        )
        return batch.model_dump(mode="json")
