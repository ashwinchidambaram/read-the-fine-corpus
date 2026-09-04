"""Pipeline report generator — findings report and exclusion report (§6.2, §7.5, Phase 2).

This module consumes the durable artifacts produced by the pipeline stages
(Collect → Assess → Decompose) and generates two client-presentable reports:

1. **Findings report** — a per-document quality summary with:
   - parse_status and document_kind for every document
   - triage classifications (spreadsheet kinds, with rationale)
   - security findings: invisible-content detections and injection suspicion scores
   - OCR confidence summaries (mean, per-page minimum) for scanned documents
   - mixed-PDF pages
   - near-duplicate version families with primacy and superseded members
   - boilerplate blocks detected corpus-wide
   - language distribution of the corpus
   Formats: Markdown (human-readable) and JSON (machine-readable).

2. **Exclusion report** — every document and segment excluded from indexing:
   - excluded_pre_parse documents (unservable file types, spreadsheet database/model)
   - superseded near-duplicates (D-25)
   - segments excluded by the Decompose stage (OCR-below-floor, too_short, etc.)
   - reason codes and plain-language reason_detail for every exclusion
   Zero silent gaps: every ExclusionRecord in the artifacts appears here.

Design constraints (§ architecture constraints in prompt):
  - Logic lives in the library; CLI is a thin consumer (C-5).
  - Reports are deterministic for a given artifact set (stable sort on document_id /
    exclusion_id; no additional wall-clock timestamps).
  - Read-only consumption of artifacts; no producer code modified.
  - Layer placement: pipeline layer — may import contracts and control/contracts only.

API:
    from finecorpus.pipeline.report import generate_report, ReportFormat

    result = generate_report(
        artifacts_root="/path/to/artifacts",
        run_id="my-run",
    )
    print(result.findings_md)   # markdown
    print(result.exclusions_md) # markdown
    findings_json = result.findings_json   # dict, JSON-serializable
    exclusions_json = result.exclusions_json  # dict, JSON-serializable
"""

from __future__ import annotations

import json
import pathlib
from collections import defaultdict
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from finecorpus.pipeline.artifact_store import ArtifactStore, ArtifactStoreError

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


class ReportFormat(StrEnum):
    """Output format for report generation."""

    md = "md"
    json = "json"
    both = "both"


@dataclass
class ReportResult:
    """Output of generate_report — markdown and JSON for both report types."""

    findings_md: str
    exclusions_md: str
    findings_json: dict[str, Any]
    exclusions_json: dict[str, Any]

    def write(
        self,
        out_dir: pathlib.Path,
        *,
        fmt: ReportFormat = ReportFormat.both,
        run_id: str = "run",
    ) -> dict[str, pathlib.Path]:
        """Write reports to *out_dir*.  Returns a map of label → written path."""
        out_dir.mkdir(parents=True, exist_ok=True)
        written: dict[str, pathlib.Path] = {}
        if fmt in (ReportFormat.md, ReportFormat.both):
            fp = out_dir / f"findings-{run_id}.md"
            fp.write_text(self.findings_md, encoding="utf-8")
            written["findings_md"] = fp
            fp = out_dir / f"exclusions-{run_id}.md"
            fp.write_text(self.exclusions_md, encoding="utf-8")
            written["exclusions_md"] = fp
        if fmt in (ReportFormat.json, ReportFormat.both):
            fp = out_dir / f"findings-{run_id}.json"
            fp.write_text(
                json.dumps(self.findings_json, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            written["findings_json"] = fp
            fp = out_dir / f"exclusions-{run_id}.json"
            fp.write_text(
                json.dumps(self.exclusions_json, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            written["exclusions_json"] = fp
        return written


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def generate_report(
    artifacts_root: str | pathlib.Path,
    run_id: str,
) -> ReportResult:
    """Generate findings and exclusion reports from pipeline artifacts.

    Consumes the *assess* and *decompose* artifacts written to
    ``<artifacts_root>/<run_id>/``.  Read-only: no artifacts are modified.

    Args:
        artifacts_root: Parent directory that contains run directories.
        run_id: The pipeline run whose artifacts to report on.

    Returns:
        A :class:`ReportResult` with markdown and JSON for both report types.

    Raises:
        ArtifactStoreError: If a required artifact is missing or malformed.
        ReportError: If the artifact data is structurally inconsistent.
    """
    store = ArtifactStore(artifacts_root=artifacts_root, run_id=run_id)
    ctx = _load_context(store, run_id)
    findings_json = _build_findings_json(ctx)
    exclusions_json = _build_exclusions_json(ctx)
    findings_md = _render_findings_md(findings_json, run_id)
    exclusions_md = _render_exclusions_md(exclusions_json, run_id)
    return ReportResult(
        findings_md=findings_md,
        exclusions_md=exclusions_md,
        findings_json=findings_json,
        exclusions_json=exclusions_json,
    )


# ---------------------------------------------------------------------------
# Error type
# ---------------------------------------------------------------------------


class ReportError(Exception):
    """Raised when report generation encounters inconsistent artifact data."""


# ---------------------------------------------------------------------------
# Internal: load and normalise artifacts
# ---------------------------------------------------------------------------


@dataclass
class _Context:
    """Normalised view of the pipeline artifacts for a single run."""

    run_id: str
    # ParseResult dicts keyed by document_id
    parse_results: dict[str, dict[str, Any]]
    # SegmentSet dicts keyed by document_id
    segment_sets: dict[str, dict[str, Any]]
    # Corpus-level from ParseResultBatch
    version_families: list[dict[str, Any]]
    boilerplate_blocks: list[str]
    # Source-path lookup (document_id -> source_path) from inventory
    source_paths: dict[str, str]
    # ExclusionRecord dicts from Decompose, keyed by exclusion_id
    exclusion_records: dict[str, dict[str, Any]]
    # Whether the decompose artifact was present and successfully loaded (F-07)
    decompose_artifact_present: bool = True


def _load_context(store: ArtifactStore, run_id: str) -> _Context:
    """Load and normalise the pipeline artifacts into a _Context."""
    # --- Inventory (collect artifact) ---
    source_paths: dict[str, str] = {}
    if store.exists("collect"):
        try:
            inv = store.load("collect")
            for item in inv.get("items", []):
                doc_id = item.get("document_id") or item.get("item_id")
                sp = item.get("source_path", "")
                if doc_id:
                    source_paths[doc_id] = sp
        except ArtifactStoreError:
            pass  # inventory is optional for report generation

    # --- ParseResultBatch (assess artifact) ---
    assess_raw = store.load("assess")
    version_families: list[dict[str, Any]] = assess_raw.get("version_families", [])
    boilerplate_blocks: list[str] = assess_raw.get("boilerplate_blocks", [])

    parse_results: dict[str, dict[str, Any]] = {}
    for pr in assess_raw.get("results", []):
        doc_id = pr.get("document_id", "")
        if doc_id:
            parse_results[doc_id] = pr

    # --- SegmentSetBatch (decompose artifact) ---
    segment_sets: dict[str, dict[str, Any]] = {}
    exclusion_records: dict[str, dict[str, Any]] = {}
    decompose_artifact_present = False

    if store.exists("decompose"):
        try:
            decompose_raw = store.load("decompose")
            decompose_artifact_present = True
            for ss in decompose_raw.get("segment_sets", []):
                doc_id = ss.get("document_id", "")
                if doc_id:
                    segment_sets[doc_id] = ss
                    # Collect exclusion records from this segment set
                    for exc in ss.get("exclusions", []):
                        exc_id = exc.get("exclusion_id", "")
                        if exc_id:
                            exc["_document_id"] = doc_id  # annotate for grouping
                            exclusion_records[exc_id] = exc
        except ArtifactStoreError:
            decompose_artifact_present = False

    return _Context(
        run_id=run_id,
        parse_results=parse_results,
        segment_sets=segment_sets,
        version_families=version_families,
        boilerplate_blocks=boilerplate_blocks,
        source_paths=source_paths,
        exclusion_records=exclusion_records,
        decompose_artifact_present=decompose_artifact_present,
    )


# ---------------------------------------------------------------------------
# Internal: build findings JSON
# ---------------------------------------------------------------------------


def _get_source_path(ctx: _Context, doc_id: str, pr: dict[str, Any]) -> str:
    """Resolve a human-readable source path for a document."""
    if doc_id in ctx.source_paths:
        return ctx.source_paths[doc_id]
    # Fall back to the document_id itself (best-effort)
    return doc_id


def _extract_ocr_summary(pr: dict[str, Any]) -> dict[str, Any] | None:
    """Return OCR summary for scanned/mixed PDFs; None for native-text."""
    quality = pr.get("quality") or {}
    mean_conf = quality.get("mean_ocr_confidence")
    if mean_conf is None:
        return None

    page_confs = [
        p["ocr_confidence"] for p in pr.get("pages", []) if p.get("ocr_confidence") is not None
    ]
    return {
        "mean_ocr_confidence": mean_conf,
        "min_page_confidence": min(page_confs) if page_confs else None,
        "page_count": len(pr.get("pages", [])),
        "scanned_page_count": sum(1 for p in pr.get("pages", []) if p.get("is_scanned")),
    }


def _extract_triage_finding(pr: dict[str, Any]) -> dict[str, Any] | None:
    """Return spreadsheet triage classification if present."""
    for f in pr.get("findings", []):
        if f.get("code") == "spreadsheet_triage":
            return {
                "code": "spreadsheet_triage",
                "severity": f.get("severity", "info"),
                "message": f.get("message", ""),
            }
    return None


def _extract_mixed_pdf_pages(pr: dict[str, Any]) -> list[int]:
    """Return page numbers for scanned pages in a mixed PDF."""
    if pr.get("document_kind") != "mixed_pdf":
        return []
    return sorted(p["page_number"] for p in pr.get("pages", []) if p.get("is_scanned"))


def _build_document_entry(
    ctx: _Context,
    doc_id: str,
    pr: dict[str, Any],
) -> dict[str, Any]:
    """Build a single document entry for the findings JSON."""
    source_path = _get_source_path(ctx, doc_id, pr)
    quality = pr.get("quality") or {}
    ss = ctx.segment_sets.get(doc_id, {})

    # Collect injection suspicion from segment set
    injection_scores = [
        seg.get("injection_suspicion", 0.0)
        for seg in ss.get("segments", [])
        if seg.get("injection_suspicion", 0.0) > 0.0
    ]
    injection_max = max(injection_scores) if injection_scores else 0.0
    injection_flagged = [
        {
            "segment_id": seg.get("segment_id", ""),
            "injection_suspicion": seg.get("injection_suspicion", 0.0),
            "text_snippet": (seg.get("text") or "")[:80] or None,
        }
        for seg in ss.get("segments", [])
        if seg.get("injection_suspicion", 0.0) > 0.0
    ]

    # Invisible content from pages
    invisible_detections = []
    for page in pr.get("pages", []):
        for ic in page.get("invisible_content", []):
            invisible_detections.append(
                {
                    "kind": ic.get("kind", ""),
                    "page_number": page.get("page_number"),
                    "text_snippet": (ic.get("text") or "")[:80] or None,
                }
            )

    findings_list = [
        {
            "code": f.get("code", ""),
            "severity": f.get("severity", "info"),
            "message": f.get("message", ""),
        }
        for f in pr.get("findings", [])
    ]

    # Synthesise table_structure_retained finding when quality.table_structure_retained
    # reports that table structure was actually retained by the parser.
    # The finding fires ONLY when quality.table_structure_retained is "full" or "partial"
    # — values set by HTML/spreadsheet parsers that detect table structure natively.
    # A quality value of "lost" or None means structure was NOT retained and must NOT
    # produce a finding claiming retention (F-2 fix: segment-existence-only branch removed).
    _quality_tsr = quality.get("table_structure_retained")
    _has_table_quality = _quality_tsr in ("full", "partial")
    _tsr_codes = {f.get("code") for f in pr.get("findings", [])}
    if _has_table_quality and "table_structure_retained" not in _tsr_codes:
        findings_list.append(
            {
                "code": "table_structure_retained",
                "severity": "info",
                "message": f"Table structure retained ({_quality_tsr})",
            }
        )

    return {
        "document_id": doc_id,
        "source_path": source_path,
        "parse_status": pr.get("parse_status", ""),
        "document_kind": pr.get("document_kind", ""),
        "dedup_role": pr.get("dedup_role", "unique"),
        "quality": {
            "overall": quality.get("overall"),
            "is_near_empty": quality.get("is_near_empty"),
            "text_extraction_ratio": quality.get("text_extraction_ratio"),
            "table_structure_retained": quality.get("table_structure_retained"),
        },
        "ocr_summary": _extract_ocr_summary(pr),
        "mixed_pdf_scanned_pages": _extract_mixed_pdf_pages(pr),
        "triage": _extract_triage_finding(pr),
        "security": {
            "invisible_content_count": len(invisible_detections),
            "invisible_content": invisible_detections,
            "injection_max_suspicion": injection_max,
            "injection_flagged_segments": sorted(
                injection_flagged, key=lambda x: x["injection_suspicion"], reverse=True
            ),
        },
        "boilerplate_segment_count": sum(
            1 for seg in ss.get("segments", []) if seg.get("salience_tier") == "boilerplate"
        ),
        "segment_count": len(ss.get("segments", [])),
        "exclusion_count": len(ss.get("exclusions", [])),
        "language_distribution": pr.get("language_distribution", []),
        "findings": findings_list,
        "encoding_issues": [
            {"kind": ei.get("kind", ""), "severity": ei.get("severity", "")}
            for ei in pr.get("encoding_issues", [])
        ],
    }


def _build_findings_json(ctx: _Context) -> dict[str, Any]:
    """Build the machine-readable findings report."""
    # Stable sort: by source_path (then document_id as tie-breaker)
    sorted_ids = sorted(
        ctx.parse_results.keys(),
        key=lambda d: (
            ctx.source_paths.get(d, d),
            d,
        ),
    )

    documents = [
        _build_document_entry(ctx, doc_id, ctx.parse_results[doc_id]) for doc_id in sorted_ids
    ]

    # Corpus-level language distribution
    lang_counts: dict[str, float] = defaultdict(float)
    total_docs_with_lang = 0
    for pr in ctx.parse_results.values():
        ld = pr.get("language_distribution", [])
        if ld:
            total_docs_with_lang += 1
            for entry in ld:
                lang = entry.get("language", "und")
                lang_counts[lang] += entry.get("fraction", 0.0)

    corpus_lang: list[dict[str, Any]] = []
    if total_docs_with_lang > 0:
        corpus_lang = sorted(
            [
                {
                    "language": lang,
                    "mean_fraction": round(count / total_docs_with_lang, 4),
                }
                for lang, count in lang_counts.items()
            ],
            key=lambda x: x["mean_fraction"],
            reverse=True,
        )

    # Parse status summary
    status_counts: dict[str, int] = defaultdict(int)
    for pr in ctx.parse_results.values():
        status_counts[pr.get("parse_status", "unknown")] += 1

    # Security summary
    total_invisible = sum(
        sum(len(p.get("invisible_content", [])) for p in pr.get("pages", []))
        for pr in ctx.parse_results.values()
    )
    injected_doc_ids = [
        doc_id
        for doc_id, ss in ctx.segment_sets.items()
        if any(seg.get("injection_suspicion", 0.0) > 0.0 for seg in ss.get("segments", []))
    ]

    return {
        "schema_version": "1.0.0",
        "contract": "findings_report",
        "run_id": ctx.run_id,
        "summary": {
            "total_documents": len(ctx.parse_results),
            "parse_status_counts": dict(sorted(status_counts.items())),
            "total_invisible_content_detections": total_invisible,
            "documents_with_injection_signals": len(injected_doc_ids),
            "version_family_count": len(ctx.version_families),
            "boilerplate_block_count": len(ctx.boilerplate_blocks),
            "corpus_language_distribution": corpus_lang,
        },
        "version_families": [
            {
                "family_id": vf.get("family_id", ""),
                "primary_document_id": vf.get("primary_document_id", ""),
                "primary_source_path": ctx.source_paths.get(
                    vf.get("primary_document_id", ""), vf.get("primary_document_id", "")
                ),
                "primacy_basis": vf.get("primacy_basis"),
                "superseded_document_ids": vf.get("superseded_document_ids", []),
                "superseded_source_paths": [
                    ctx.source_paths.get(d, d) for d in vf.get("superseded_document_ids", [])
                ],
                # similarity_scores is a per-member dict {member_id -> score_vs_primary}
                "similarity_scores": vf.get("similarity_scores", {}),
            }
            for vf in sorted(ctx.version_families, key=lambda v: v.get("family_id", ""))
        ],
        "boilerplate_blocks": ctx.boilerplate_blocks,
        "documents": documents,
    }


# ---------------------------------------------------------------------------
# Internal: build exclusions JSON
# ---------------------------------------------------------------------------


def _build_exclusions_json(ctx: _Context) -> dict[str, Any]:
    """Build the machine-readable exclusion report.

    Every excluded document and segment appears here.  Zero silent gaps.

    Source of truth: the ExclusionRecord entries in each SegmentSet (Decompose output).
    The DecomposeStage already creates one ExclusionRecord per excluded document/segment
    with the correct ExclusionReason enum value and reason_detail.  We enrich
    superseded_version records with primary-document info from version_families.

    Document-scope vs. segment-scope:
      - ``superseded_version``, ``unservable_content``, ``spreadsheet_database``,
        ``spreadsheet_model``, ``encrypted``, ``parse_failed`` — whole-document exclusions.
      - ``too_short``, ``empty_region``, ``duplicate``, ``other`` — segment-level.
    """
    # Reasons that indicate the whole document was excluded (not just a segment)
    _DOCUMENT_SCOPE_REASONS = frozenset(
        {
            "superseded_version",
            "unservable_content",
            "spreadsheet_database",
            "spreadsheet_model",
            "encrypted",
            "parse_failed",
        }
    )

    exclusions: list[dict[str, Any]] = []

    for exc_id in sorted(ctx.exclusion_records.keys()):
        exc = ctx.exclusion_records[exc_id]
        doc_id = exc.get("_document_id", "")
        source_path = ctx.source_paths.get(doc_id, doc_id)
        reason = exc.get("reason", "other")
        reason_detail = exc.get("reason_detail") or _default_detail_for_reason(reason)
        scope = "document" if reason in _DOCUMENT_SCOPE_REASONS else "segment"

        entry: dict[str, Any] = {
            "exclusion_id": exc_id,
            "document_id": doc_id,
            "source_path": source_path,
            "scope": scope,
            "reason": reason,
            "reason_detail": reason_detail,
            "reversible": exc.get("reversible", True),
            "source_region_ids": exc.get("source_region_ids", []),
            "user_action": _user_action_for_reason(reason),
        }

        # Enrich superseded_version records with primary-document info
        if reason == "superseded_version":
            primary_id = _find_primary_for_superseded(doc_id, ctx.version_families)
            primary_path = ctx.source_paths.get(primary_id, primary_id) if primary_id else None
            entry["primary_document_id"] = primary_id
            entry["primary_source_path"] = primary_path

        exclusions.append(entry)

    # Reason-code summary
    reason_counts: dict[str, int] = defaultdict(int)
    for exc in exclusions:
        reason_counts[exc["reason"]] += 1

    return {
        "schema_version": "1.0.0",
        "contract": "exclusion_report",
        "run_id": ctx.run_id,
        "decompose_artifact_present": ctx.decompose_artifact_present,
        "summary": {
            "total_exclusions": len(exclusions),
            "by_reason": dict(sorted(reason_counts.items())),
        },
        "exclusions": exclusions,
    }


def _find_primary_for_superseded(doc_id: str, version_families: list[dict[str, Any]]) -> str | None:
    """Return the primary document_id for a superseded document, if found."""
    for vf in version_families:
        if doc_id in vf.get("superseded_document_ids", []):
            return vf.get("primary_document_id")
    return None


def _user_action_for_reason(reason: str) -> str:
    """Return plain-language user guidance for an exclusion reason.

    Only covers codes that correspond to actual ExclusionReason enum members.
    File-type detail (audio, video, CAD, CSV, unrecognized extension) is carried
    in reason_detail under the unservable_content reason code.
    """
    _actions: dict[str, str] = {
        "unservable_content": (
            "Nothing — this content type cannot be usefully indexed. "
            "See reason_detail for the specific file-type detail (§7.5)."
        ),
        "spreadsheet_database": (
            "Nothing — a row-oriented database spreadsheet cannot be usefully vectorized. "
            "If this is a narrative report, reclassify via spreadsheet_triage override in "
            "IngestionConfig (see docs/pipeline/assess.md §Spreadsheet triage §Override)."
        ),
        "spreadsheet_model": (
            "Nothing — a formula-driven model spreadsheet cannot be usefully vectorized "
            "without becoming a stale snapshot. If the spreadsheet contains narrative, "
            "reclassify via spreadsheet_triage override in IngestionConfig."
        ),
        "encrypted": (
            "Decrypt the file and re-run the pipeline, or remove the document from the "
            "source directory if it should not be indexed."
        ),
        "empty_region": (
            "Nothing — the region contained no extractable text. This is the correct outcome."
        ),
        "superseded_version": (
            "Set index_superseded_versions=true in IngestionConfig to include "
            "superseded versions in the index. By default only the newest version is indexed."
        ),
        "duplicate": (
            "Remove exact duplicate files from the source directory, or nothing — "
            "only one copy will be indexed."
        ),
        "parse_failed": (
            "Investigate why the document could not be parsed. Check for password protection, "
            "corruption, or missing dependencies (e.g., tesseract for scanned PDFs)."
        ),
        "too_short": (
            "Nothing — short segments (below the minimum length threshold) are excluded "
            "to avoid poor-quality chunks. This is the correct outcome."
        ),
        "other": "Review the reason_detail for specific guidance.",
    }
    return _actions.get(reason, "Review the reason_detail for specific guidance.")


def _default_detail_for_reason(reason: str) -> str:
    """Return a default plain-language detail for an ExclusionReason."""
    _details: dict[str, str] = {
        "unservable_content": "Content type not servable by this platform (§7.5).",
        "spreadsheet_database": (
            "Spreadsheet classified as a row-oriented database (§6.4). "
            "Vectorizing tabular records produces confident nonsense."
        ),
        "spreadsheet_model": (
            "Spreadsheet classified as a formula-driven model (§6.4). "
            "Ingesting a values snapshot creates a stale artifact."
        ),
        "encrypted": "File is password-protected and could not be decrypted.",
        "empty_region": "Region contained no extractable text.",
        "superseded_version": (
            "Document is superseded by a newer version in the same near-duplicate family (D-25). "
            "Excluded from indexing by default; retained in corpus."
        ),
        "duplicate": "Exact duplicate of another document (same content hash).",
        "parse_failed": "Document could not be parsed (see assess findings for details).",
        "too_short": (
            "Content span is below the minimum segment length threshold "
            "and would produce a low-quality chunk."
        ),
        "other": "Excluded for an unclassified reason. See reason_detail.",
    }
    return _details.get(reason, "Excluded. See reason_detail.")


# ---------------------------------------------------------------------------
# Markdown rendering — findings report
# ---------------------------------------------------------------------------

_SEV_BADGE: dict[str, str] = {
    "error": "ERROR",
    "warning": "WARN",
    "info": "INFO",
}

_STATUS_EMOJI: dict[str, str] = {
    "parsed": "OK",
    "partial": "PARTIAL",
    "failed": "FAILED",
    "unreadable": "UNREADABLE",
    "excluded_pre_parse": "EXCLUDED",
}


def _render_findings_md(data: dict[str, Any], run_id: str) -> str:
    """Render the findings JSON to human-readable Markdown."""
    lines: list[str] = []
    summary = data.get("summary", {})
    documents = data.get("documents", [])

    lines.append(f"# Findings Report — run `{run_id}`")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    total = summary.get("total_documents", 0)
    lines.append(f"- **Total documents:** {total}")

    status_counts = summary.get("parse_status_counts", {})
    for status, count in sorted(status_counts.items()):
        badge = _STATUS_EMOJI.get(status, status.upper())
        lines.append(f"  - {badge}: {count}")

    lines.append(
        f"- **Version families (near-duplicates):** {summary.get('version_family_count', 0)}"
    )
    lines.append(f"- **Boilerplate blocks detected:** {summary.get('boilerplate_block_count', 0)}")
    lines.append(
        f"- **Invisible-content detections:** "
        f"{summary.get('total_invisible_content_detections', 0)}"
    )
    lines.append(
        f"- **Documents with injection signals:** "
        f"{summary.get('documents_with_injection_signals', 0)}"
    )
    lines.append("")

    # Language distribution
    lang_dist = summary.get("corpus_language_distribution", [])
    if lang_dist:
        lines.append("## Language Distribution")
        lines.append("")
        lines.append("| Language | Mean fraction |")
        lines.append("|---|---|")
        for entry in lang_dist:
            pct = f"{entry.get('mean_fraction', 0.0) * 100:.1f}%"
            lines.append(f"| {entry.get('language', '?')} | {pct} |")
        lines.append("")

    # Version families
    families = data.get("version_families", [])
    if families:
        lines.append("## Near-Duplicate Version Families")
        lines.append("")
        lines.append(
            "> The newest document in each family is the **primary** and will be indexed. "
            "Others are **superseded** and excluded by default."
        )
        lines.append("")
        for vf in families:
            fid = vf.get("family_id", "?")
            primary_id = vf.get("primary_document_id", "")
            primary_path = vf.get("primary_source_path", primary_id or "?")
            primacy_basis = vf.get("primacy_basis") or "unknown"
            superseded_ids = vf.get("superseded_document_ids", [])
            superseded_paths = vf.get("superseded_source_paths", [])
            sim_scores: dict[str, float] = vf.get("similarity_scores", {})
            lines.append(f"### Family `{fid}`")
            lines.append(f"- **Primary:** `{primary_path}` *(primacy basis: {primacy_basis})*")
            for sid, sp in zip(superseded_ids, superseded_paths, strict=False):
                score = sim_scores.get(sid)
                score_str = f"{score:.4f}" if score is not None else "n/a"
                lines.append(f"- Superseded: `{sp}` (similarity to primary: {score_str})")
            lines.append("")

    # Boilerplate blocks
    boilerplate = data.get("boilerplate_blocks", [])
    if boilerplate:
        lines.append("## Corpus-Wide Boilerplate Blocks")
        lines.append("")
        lines.append(
            f"> {len(boilerplate)} block(s) detected as repeated boilerplate across the corpus. "
            "These segments are reclassified as `boilerplate` tier and down-weighted at retrieval."
        )
        lines.append("")
        for i, block in enumerate(boilerplate[:5], 1):
            snippet = block[:120].replace("\n", " ").strip()
            lines.append(f"{i}. `{snippet}{'...' if len(block) > 120 else ''}`")
        if len(boilerplate) > 5:
            lines.append(f"  *(and {len(boilerplate) - 5} more — see JSON report)*")
        lines.append("")

    # Per-document table
    lines.append("## Per-Document Results")
    lines.append("")
    lines.append("| Document | Status | Kind | Dedup Role | Quality | Segments | Exclusions |")
    lines.append("|---|---|---|---|---|---|---|")
    for doc in documents:
        sp = doc.get("source_path", doc.get("document_id", "?"))
        # Show filename only in the table (full path in details)
        short_name = pathlib.Path(sp).name if sp else "?"
        status = _STATUS_EMOJI.get(doc.get("parse_status", ""), doc.get("parse_status", "?"))
        kind = doc.get("document_kind", "?")
        role = doc.get("dedup_role", "unique")
        quality = doc.get("quality", {})
        q_str = (
            f"{quality.get('overall', 0.0):.2f}" if quality.get("overall") is not None else "n/a"
        )
        seg_count = doc.get("segment_count", "n/a")
        exc_count = doc.get("exclusion_count", "n/a")
        lines.append(
            f"| `{short_name}` | {status} | {kind} | {role} | {q_str} | {seg_count} | {exc_count} |"
        )
    lines.append("")

    # Per-document detail sections (for documents with notable findings).
    # A document whose ONLY findings are INFO link_record entries does NOT qualify as notable —
    # those are aggregated into a single summary line (F-05).
    def _has_non_link_record_findings(doc: dict[str, Any]) -> bool:
        return any(f.get("code") != "link_record" for f in doc.get("findings", []))

    notable = [
        doc
        for doc in documents
        if (
            doc.get("security", {}).get("invisible_content_count", 0) > 0
            or doc.get("security", {}).get("injection_max_suspicion", 0.0) > 0.0
            or doc.get("ocr_summary") is not None
            or doc.get("mixed_pdf_scanned_pages")
            or doc.get("triage") is not None
            or doc.get("parse_status") in ("failed", "partial")
            or _has_non_link_record_findings(doc)
        )
    ]

    if notable:
        lines.append("## Document Detail")
        lines.append("")
        for doc in notable:
            sp = doc.get("source_path", doc.get("document_id", "?"))
            doc_id = doc.get("document_id", "?")
            lines.append(f"### `{sp}`")
            lines.append(f"- Document ID: `{doc_id}`")
            lines.append(f"- Parse status: **{doc.get('parse_status', '?')}**")
            lines.append(f"- Kind: {doc.get('document_kind', '?')}")
            lines.append(f"- Dedup role: {doc.get('dedup_role', 'unique')}")

            # Quality
            quality = doc.get("quality", {})
            if quality.get("overall") is not None:
                lines.append(f"- Quality score: {quality['overall']:.2f}")
            if quality.get("is_near_empty"):
                lines.append("- **Near-empty extraction detected**")

            # Triage
            if doc.get("triage"):
                t = doc["triage"]
                triage_sev = t.get("severity", "?")
                triage_msg = t.get("message", "")
                lines.append(f"- Spreadsheet triage: `{triage_sev}` — {triage_msg}")

            # OCR
            ocr = doc.get("ocr_summary")
            if ocr:
                min_conf = ocr.get("min_page_confidence")
                min_conf_str = f"{min_conf:.2f}" if min_conf is not None else "n/a"
                lines.append(
                    f"- OCR: mean confidence {ocr.get('mean_ocr_confidence', 0.0):.2f}, "
                    f"min page confidence {min_conf_str}, "
                    f"{ocr.get('scanned_page_count', 0)}/{ocr.get('page_count', 0)} scanned pages"
                )

            # Mixed PDF
            mixed_pages = doc.get("mixed_pdf_scanned_pages", [])
            if mixed_pages:
                pages_str = ", ".join(str(p) for p in mixed_pages[:10])
                if len(mixed_pages) > 10:
                    pages_str += f", ... ({len(mixed_pages)} total)"
                lines.append(f"- Mixed PDF scanned pages: {pages_str}")

            # Security
            sec = doc.get("security", {})
            if sec.get("invisible_content_count", 0) > 0:
                lines.append(
                    f"- **Invisible content: {sec['invisible_content_count']} detection(s)**"
                )
                for ic in sec.get("invisible_content", [])[:3]:
                    snip = ic.get("text_snippet") or "(no text)"
                    lines.append(
                        f"  - Page {ic.get('page_number', '?')}: {ic.get('kind', '?')} — `{snip}`"
                    )
            if sec.get("injection_max_suspicion", 0.0) > 0.0:
                lines.append(
                    f"- **Injection suspicion: max score {sec['injection_max_suspicion']:.2f}** "
                    f"({len(sec.get('injection_flagged_segments', []))} segment(s))"
                )

            # Findings — aggregate link_record INFO entries to avoid noise (F-05)
            findings = doc.get("findings", [])
            if findings:
                link_record_count = sum(1 for f in findings if f.get("code") == "link_record")
                non_link_findings = [f for f in findings if f.get("code") != "link_record"]
                if non_link_findings:
                    lines.append("- Findings:")
                    for f in non_link_findings:
                        badge = _SEV_BADGE.get(f.get("severity", "info"), "INFO")
                        lines.append(
                            f"  - [{badge}] `{f.get('code', '?')}`: {f.get('message', '')}"
                        )
                if link_record_count > 0:
                    lines.append(
                        f"- {link_record_count} link(s) recorded (see JSON report for full list)"
                    )

            lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Markdown rendering — exclusion report
# ---------------------------------------------------------------------------

_REASON_HEADING: dict[str, str] = {
    # Codes that map to actual ExclusionReason enum members
    "unservable_content": "Unservable Content",
    "spreadsheet_database": "Spreadsheet: Database Kind",
    "spreadsheet_model": "Spreadsheet: Model Kind",
    "encrypted": "Encrypted / Password-Protected",
    "empty_region": "Segments: Empty Region",
    "superseded_version": "Superseded Near-Duplicate Versions",
    "duplicate": "Exact Duplicates",
    "parse_failed": "Parse Failure",
    "too_short": "Segments: Too Short",
    "other": "Other Exclusions",
}


def _render_exclusions_md(data: dict[str, Any], run_id: str) -> str:
    """Render the exclusion JSON to human-readable Markdown."""
    lines: list[str] = []
    summary = data.get("summary", {})
    exclusions = data.get("exclusions", [])
    decompose_present = data.get("decompose_artifact_present", True)

    lines.append(f"# Exclusion Report — run `{run_id}`")
    lines.append("")

    # F-07: warn prominently when decompose artifact is missing
    if not decompose_present:
        lines.append(
            "> **WARNING: The decompose artifact is missing for this run.**  "
            "Segment-level exclusions (too_short, empty_region, etc.) cannot be reported. "
            "Re-run the pipeline through the Decompose stage to obtain a complete exclusion "
            "report. The counts below reflect ONLY pre-parse exclusions recorded by the Assess "
            "stage via ExclusionRecords; the true total exclusions may be higher."
        )
        lines.append("")
    else:
        lines.append(
            "> The exclusion report is a first-class deliverable, not an error log (§7.5). "
            "Every document and segment excluded from indexing is listed here with its reason. "
            "Nothing is silently dropped."
        )
        lines.append("")

    lines.append("## Summary")
    lines.append("")
    lines.append(f"- **Total exclusions:** {summary.get('total_exclusions', 0)}")
    by_reason = summary.get("by_reason", {})
    if by_reason:
        lines.append("- **By reason:**")
        for reason, count in sorted(by_reason.items()):
            heading = _REASON_HEADING.get(reason, reason)
            lines.append(f"  - {heading}: {count}")
    lines.append("")

    if not exclusions:
        if not decompose_present:
            lines.append(
                "*No exclusion records found. The decompose artifact is missing — "
                "segment-level exclusions are not available for this run.*"
            )
        else:
            lines.append("*No exclusions in this run.*")
        return "\n".join(lines)

    # Group by reason
    by_reason_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for exc in exclusions:
        by_reason_groups[exc.get("reason", "other")].append(exc)

    # Render each group in a consistent order (codes match ExclusionReason enum members)
    reason_order = [
        "unservable_content",
        "spreadsheet_database",
        "spreadsheet_model",
        "encrypted",
        "parse_failed",
        "superseded_version",
        "duplicate",
        "too_short",
        "empty_region",
        "other",
    ]
    # Append any reasons not in the explicit order
    for reason in sorted(by_reason_groups.keys()):
        if reason not in reason_order:
            reason_order.append(reason)

    for reason in reason_order:
        group = by_reason_groups.get(reason, [])
        if not group:
            continue

        heading = _REASON_HEADING.get(reason, reason)
        lines.append(f"## {heading}")
        lines.append("")

        # User action (same for all in group)
        sample = group[0]
        user_action = sample.get("user_action", "")
        if user_action:
            lines.append(f"> **What to do:** {user_action}")
            lines.append("")

        for exc in group:
            sp = exc.get("source_path", exc.get("document_id", "?"))
            scope = exc.get("scope", "?")
            detail = exc.get("reason_detail", "")

            if scope == "document":
                lines.append(f"- **`{sp}`** — {detail}")
                # For superseded, show primary
                if reason == "superseded_version" and exc.get("primary_source_path"):
                    lines.append(f"  - Primary version: `{exc['primary_source_path']}`")
            else:
                # Segment-level exclusion
                exc_id = exc.get("exclusion_id", "?")
                doc_short = pathlib.Path(sp).name if sp else "?"
                lines.append(f"- Segment in `{doc_short}` (exclusion `{exc_id}`): {detail}")

        lines.append("")

    return "\n".join(lines)
