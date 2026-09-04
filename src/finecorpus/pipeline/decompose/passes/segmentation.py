"""Segmentation pass for the Decompose stage (Phase 1 + Phase 2 scanned).

Produces the initial segment list from raw region text in the parse result.

Behaviour:
  - Iterates regions in document order.
  - Empty/failed regions → ExclusionRecord (parse_failed or empty_region).
  - Non-empty regions:
      * Native-text regions → paragraph splitting on blank lines (Phase 1).
      * Scanned regions (ocr_confidence present):
          - confidence >= ocr_sub_decompose_confidence_floor (default 0.85):
            paragraph segmentation applied; each sub-segment carries
            ocr_confidence from the source region (OQ-4).
          - confidence < ocr_sub_decompose_confidence_floor:
            one scanned_region segment per page (OQ-4 default).
  - Too-short paragraphs (< _MIN_SEGMENT_CHARS chars) → ExclusionRecord (too_short).
  - Heading detection via line-level heuristics (Phase 1 — see decompose.md).
  - Segment types: heading, front_matter, revision_history, prose, unknown,
    scanned_region (for below-threshold OCR pages).
  - Cross-reference detection (surface-text match; unresolved in Phase 1).
  - Structural path breadcrumb maintained across headings.
  - document_order: dense, gapless, 0-based.

HEADING DETECTION LIMITS — see docs/pipeline/decompose.md for the full
honest assessment.

Salience assignment
-------------------
This pass sets ``salience_tier`` and ``salience_signals`` inline (matching the
original stage.py behaviour).  ``SaliencePass`` then applies OCR-specific
overrides: it reads ``ocr_confidence`` on scanned_region segments and adjusts
the tier per the configured thresholds (floor → excluded, warn → supporting +
flag).  The winning signal on the segment is updated accordingly.
"""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from typing import Any

from finecorpus.contracts.segment_set import (
    CrossReference,
    CrossReferenceResolution,
    ExclusionReason,
    ExclusionRecord,
    Segment,
)
from finecorpus.contracts.shared.blocks import (
    LocatorKind,
    SalienceSignal,
    SalienceSignalKind,
    SalienceTier,
    SegmentType,
    SourceLocation,
)
from finecorpus.pipeline.decompose.passes.base import DocumentContext, PassResult

# ---------------------------------------------------------------------------
# Constants (mirrors original stage.py)
# ---------------------------------------------------------------------------

_HEADING_MAX_CHARS = 120
_HEADING_ALLCAPS_MIN_LEN = 4
_MIN_SEGMENT_CHARS = 5
_NEAR_EMPTY_PAGE_CHARS = 50  # noqa: F841 (kept for doc completeness)

_HEADING_NUMBERING_RE = re.compile(
    r"^(?:"
    r"\d+\."
    r"|\d+\.\d+"
    r"|\d+\.\d+\.\d+"
    r"|[A-Z]\."
    r"|Section\s+\d+"
    r"|§\s*\d+"
    r"|Appendix\s+[A-Z0-9]+"
    r"|Chapter\s+\d+"
    r")"
    r"\s+\S",
    re.IGNORECASE,
)

_SENTENCE_END_RE = re.compile(r"[.?!]\s*$")

_CROSS_REF_RE = re.compile(
    r"(?:see|refer to|as (per|defined in|described in|shown in|noted in))\s+"
    r"(?:section|sect|fig|figure|table|appendix|chapter|§)\s*[\d.A-Z]+",
    re.IGNORECASE,
)

_REVISION_HISTORY_RE = re.compile(
    r"^(?:revision\s+history|change\s+log|change\s+history|revision\s+log)$",
    re.IGNORECASE,
)

_FRONT_MATTER_RE = re.compile(
    r"(?:version|rev(?:ision)?|date|author|document\s+no|effective\s+date"
    r"|prepared\s+by|approved\s+by|classification)",
    re.IGNORECASE,
)

# Salience tier map (mirrors original stage.py _TYPE_TO_TIER)
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
# Helpers (mirrors original stage.py private functions)
# ---------------------------------------------------------------------------


def _segment_id(document_id: str, order: int) -> str:
    raw = f"{document_id}:seg:{order}".encode()
    return "seg-" + hashlib.sha256(raw).hexdigest()[:24]


def _exclusion_id(document_id: str, suffix: str) -> str:
    raw = f"{document_id}:excl:{suffix}".encode()
    return "excl-" + hashlib.sha256(raw).hexdigest()[:24]


def _is_heading(line: str, is_first_in_paragraph: bool) -> bool:
    stripped = line.strip()
    if not stripped or len(stripped) > _HEADING_MAX_CHARS:
        return False
    if _SENTENCE_END_RE.search(stripped):
        return False
    if _HEADING_NUMBERING_RE.match(stripped):
        return True
    if stripped.isupper() and len(stripped) >= _HEADING_ALLCAPS_MIN_LEN and not stripped.isdigit():
        return True
    if is_first_in_paragraph and len(stripped) <= 60:  # noqa: PLR2004
        return True
    return False


def _split_into_paragraphs(text: str) -> list[str]:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    raw_blocks = re.split(r"\n\s*\n+", text)
    return [block.strip() for block in raw_blocks if block.strip()]


def _paragraph_to_lines(para: str) -> list[str]:
    return para.split("\n")


def _heading_level(heading_text: str) -> int:
    m = re.match(r"^(\d+(?:\.\d+)*)\s", heading_text.strip())
    if m:
        return len(m.group(1).split("."))
    return 1


def _update_structural_path(path: list[str], heading: str, level: int) -> list[str]:
    new_path = path[: level - 1]
    new_path.append(heading.strip())
    return new_path


def _build_segment_path(structural_path: list[str], ordinal: int) -> str:
    base = "/".join(structural_path)
    return f"{base}#{ordinal}"


def _type_prior_signal(seg_type: SegmentType) -> SalienceSignal:
    tier = _TYPE_TO_TIER[seg_type]
    return SalienceSignal(
        kind=SalienceSignalKind.segment_type_prior,
        implied_tier=tier,
        won=True,
        detail=f"Phase 1 type prior: {seg_type.value} → {tier.value}",
    )


def _classify_prose(
    text: str,
    is_first_page: bool,
    para_idx: int,
    page_num: int,
) -> SegmentType:
    stripped = text.strip()
    if not stripped:
        return SegmentType.unknown
    if is_first_page and page_num == 1 and para_idx < 3 and _FRONT_MATTER_RE.search(stripped):
        return SegmentType.front_matter
    if len(stripped) < _MIN_SEGMENT_CHARS * 2:
        return SegmentType.unknown
    return SegmentType.prose


# ---------------------------------------------------------------------------
# Cross-reference accumulator (returned separately so stage.py can build
# the CrossReference list after all regions are processed)
# ---------------------------------------------------------------------------


def _build_cross_references(
    document_id: str,
    cross_ref_candidates: list[dict[str, Any]],
) -> list[CrossReference]:
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
    return cross_references


# ---------------------------------------------------------------------------
# Pass implementation
# ---------------------------------------------------------------------------


class SegmentationPass:
    """Paragraph/heading segmentation pass (Phase 1).

    Satisfies the ``SegmentPass`` protocol.  Must run first in the pass list.

    This pass produces the base segment list from region text, including:
    - Paragraph splitting
    - Heading detection (Phase 1 heuristics)
    - front_matter / revision_history / prose / unknown classification
    - Exclusion records for empty/failed regions and too-short paragraphs
    - Cross-reference surface detection (returned in PassResult.cross_references)
    - Salience tier assignment (segment_type_prior signal)
    """

    def run(
        self,
        doc_ctx: DocumentContext,
        segments: list[Segment],
        exclusions: list[ExclusionRecord],
    ) -> PassResult:
        """Produce segments from regions in the parse result."""
        document_id = doc_ctx.document_id
        parse_result = doc_ctx.parse_result
        regions = parse_result.get("regions", [])

        doc_order = 0
        new_segments: list[Segment] = list(segments)
        new_exclusions: list[ExclusionRecord] = list(exclusions)

        structural_path: list[str] = []
        path_ordinals: dict[tuple[str, ...], int] = defaultdict(int)
        cross_ref_candidates: list[dict[str, Any]] = []

        first_page_done = False
        first_page_num: int | None = None

        for region in regions:
            region_id = region.get("region_id", "")
            extract_status = region.get("extract_status", "")
            text = region.get("text") or ""
            location_raw = region.get("location", {})
            page_num = location_raw.get("page_start", 1)

            # Failed/empty regions → exclusion record
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
                new_exclusions.append(
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

            is_first_page = first_page_num is None or page_num == first_page_num
            if first_page_num is None:
                first_page_num = page_num

            # ------------------------------------------------------------------
            # Scanned-region handling (Phase 2 OCR path)
            # ------------------------------------------------------------------
            region_ocr_confidence: float | None = region.get("ocr_confidence")
            is_scanned_region = region_ocr_confidence is not None

            if is_scanned_region:
                sub_decompose_floor = doc_ctx.ocr_sub_decompose_confidence_floor
                if region_ocr_confidence < sub_decompose_floor:  # type: ignore[operator]
                    # Below sub-decompose threshold → one scanned_region segment per page (OQ-4)
                    seg_loc = SourceLocation(
                        locator_kind=LocatorKind.page,
                        page_start=page_num,
                        page_end=page_num,
                    )
                    path_key = tuple(structural_path)
                    seg_path = _build_segment_path(structural_path, path_ordinals[path_key])
                    path_ordinals[path_key] += 1
                    prior_signal = _type_prior_signal(SegmentType.scanned_region)
                    new_segments.append(
                        Segment(
                            segment_id=_segment_id(document_id, doc_order),
                            document_order=doc_order,
                            segment_type=SegmentType.scanned_region,
                            salience_tier=_TYPE_TO_TIER[SegmentType.scanned_region],
                            structural_path=list(structural_path),
                            segment_path=seg_path,
                            location=seg_loc,
                            source_region_ids=[region_id],
                            language="und",
                            ocr_confidence=region_ocr_confidence,
                            injection_suspicion=0.0,
                            invisible_content_flags=[],
                            sensitivity_flags=[],
                            salience_signals=[prior_signal],
                            salience_basis=SalienceSignalKind.segment_type_prior,
                            text=text.strip(),
                        )
                    )
                    doc_order += 1
                    if not first_page_done and page_num == first_page_num:
                        first_page_done = True
                    continue
                # else: confidence >= sub_decompose_floor → fall through to paragraph
                # segmentation below, with ocr_confidence propagated to each segment.

            paragraphs = _split_into_paragraphs(text)

            for para_idx, para_text in enumerate(paragraphs):
                if not para_text.strip():
                    continue

                lines = _paragraph_to_lines(para_text)
                first_line = lines[0].strip() if lines else ""
                is_single_line_para = len(lines) == 1

                if first_line and _is_heading(first_line, is_first_in_paragraph=True):
                    heading_text = first_line

                    if _REVISION_HISTORY_RE.match(heading_text.strip()):
                        seg_type = SegmentType.revision_history
                    elif is_single_line_para:
                        seg_type = SegmentType.heading
                    else:
                        seg_type = SegmentType.heading

                    level = _heading_level(heading_text)
                    structural_path = _update_structural_path(structural_path, heading_text, level)

                    path_key = tuple(structural_path)
                    path_ordinals[path_key]  # ensure key exists
                    seg_path = _build_segment_path(structural_path, path_ordinals[path_key])
                    path_ordinals[path_key] += 1

                    seg_loc = SourceLocation(
                        locator_kind=LocatorKind.page,
                        page_start=page_num,
                        page_end=page_num,
                    )
                    prior_signal = _type_prior_signal(seg_type)
                    new_segments.append(
                        Segment(
                            segment_id=_segment_id(document_id, doc_order),
                            document_order=doc_order,
                            segment_type=seg_type,
                            salience_tier=_TYPE_TO_TIER[seg_type],
                            structural_path=list(structural_path[:-1]),
                            segment_path=seg_path,
                            location=seg_loc,
                            source_region_ids=[region_id],
                            language="und",
                            ocr_confidence=region_ocr_confidence,  # propagated from OCR region
                            injection_suspicion=0.0,
                            invisible_content_flags=[],
                            sensitivity_flags=[],
                            salience_signals=[prior_signal],
                            salience_basis=SalienceSignalKind.segment_type_prior,
                            text=heading_text,
                        )
                    )
                    doc_order += 1

                    remaining_lines = lines[1:]
                    remaining_text = "\n".join(remaining_lines).strip()
                    if remaining_text and len(remaining_text) >= _MIN_SEGMENT_CHARS:
                        path_key2 = tuple(structural_path)
                        seg_path2 = _build_segment_path(structural_path, path_ordinals[path_key2])
                        path_ordinals[path_key2] += 1

                        prose_type = _classify_prose(
                            remaining_text, is_first_page, para_idx, page_num
                        )
                        prior2 = _type_prior_signal(prose_type)
                        new_segments.append(
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
                                ocr_confidence=region_ocr_confidence,  # propagated from OCR region
                                injection_suspicion=0.0,
                                invisible_content_flags=[],
                                sensitivity_flags=[],
                                salience_signals=[prior2],
                                salience_basis=SalienceSignalKind.segment_type_prior,
                                text=remaining_text,
                            )
                        )
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
                        excl_suffix = f"short-remainder-{region_id}-{para_idx}"
                        new_exclusions.append(
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
                    stripped_para = para_text.strip()
                    if not stripped_para:
                        continue
                    if len(stripped_para) < _MIN_SEGMENT_CHARS:
                        excl_suffix = f"short-para-{region_id}-{para_idx}"
                        seg_loc_short = SourceLocation(
                            locator_kind=LocatorKind.page,
                            page_start=page_num,
                            page_end=page_num,
                        )
                        new_exclusions.append(
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
                    new_segments.append(
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
                            ocr_confidence=region_ocr_confidence,  # propagated from OCR region
                            injection_suspicion=0.0,
                            invisible_content_flags=[],
                            sensitivity_flags=[],
                            salience_signals=[prior_signal],
                            salience_basis=SalienceSignalKind.segment_type_prior,
                            text=para_text.strip(),
                        )
                    )
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

        cross_references = _build_cross_references(document_id, cross_ref_candidates)

        return PassResult(
            segments=new_segments,
            exclusions=new_exclusions,
            cross_references=cross_references,
        )


#: Module-level singleton — import and add to PASSES in passes/__init__.py.
segmentation_pass = SegmentationPass()
