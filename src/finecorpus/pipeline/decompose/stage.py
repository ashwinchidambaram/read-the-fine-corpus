"""Stage 3 — Decompose (Phase 1: real prose segmentation for native-text PDFs).

Produces a SegmentSetBatch artifact: one SegmentSet per document.

This module owns:
  - The frozen-artifact store helpers (load / save / key computation).
  - The empty-SegmentSet builder for excluded/failed documents.
  - The ordered pass pipeline execution (PASSES from passes/__init__.py).
  - The reassembly record computation.
  - Batch assembly and contract validation.
  - D-25 enforcement: superseded near-duplicate documents produce no segments
    by default (Phase 2 addition).

Pass-specific logic lives in ``passes/``:
  - ``passes/segmentation.py`` — paragraph/heading segmentation, exclusion
    recording, cross-reference surface detection.
  - ``passes/salience.py``     — salience tier assignment (Phase 1: no-op;
    Phase 2 extension point).
  - ``passes/boilerplate.py``  — corpus-wide boilerplate reclassification
    (Phase 2: retypes matching segments to boilerplate tier).

The ordered pass list is declared in ``passes/__init__.py``.

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

Phase 2 addition — D-25 enforcement and boilerplate pass:
  - Before running passes, check parse_result["dedup_role"].  If the document is
    ``"superseded"`` and ``index_superseded_versions=False`` (the default), skip all
    passes and emit an empty SegmentSet with a single ExclusionRecord whose reason is
    ``superseded_version``.  The reason_detail names the primary document_id so the
    exclusion report is informative (D-25, owner ruling 2026-09-03).
  - When ``index_superseded_versions=True``, superseded documents are decomposed
    normally and their segments are forced to tier ``excluded`` by the final pass
    in the pipeline (``SupersededVersionPass``, which wins over all other tier
    assignments and records a ``superseded_version`` winning signal).
  - The ``boilerplate_blocks`` set from ParseResultBatch is threaded into the pass
    context so BoilerplatePass can retype corpus-wide repeated segments.

D-26 resolution: SegmentSetBatch is now an official versioned contract.
PlanStage checks schema_version against SUPPORTED_SEGMENT_SET_BATCH.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from finecorpus.contracts.segment_set import (
    ExclusionReason,
    ExclusionRecord,
    ReassemblyMethod,
    ReassemblyRecord,
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
    SourceLocation,
    TenancyBlock,
)
from finecorpus.contracts.versions import SUPPORTED_PARSE_RESULT_BATCH
from finecorpus.pipeline.decompose.passes import PASSES
from finecorpus.pipeline.decompose.passes.base import DocumentContext
from finecorpus.pipeline.stage import Stage

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CONFIG_VERSION = "p1.0"
"""Phase 1 config version — no class descriptions, no LLM signals."""

_EMPTY_REASSEMBLY_DIGEST = hashlib.sha256(b"").hexdigest()


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
# Empty SegmentSet for excluded/failed documents
# ---------------------------------------------------------------------------


def _make_superseded_segment_set(
    document_id: str,
    content_hash: str,
    tenancy: TenancyBlock,
    parse_result: dict[str, Any],
    decomposed_at: datetime,
) -> SegmentSet:
    """Build an empty SegmentSet for a superseded near-duplicate document (D-25).

    D-25 enforcement (owner ruling 2026-09-03): when
    ``ingestion.dedup.index_superseded_versions=false`` (the default), a
    superseded document produces NO segments and NO chunks.  It is inventoried,
    retained in object storage, and reported in the exclusion report with a
    reason that names the primary document_id.

    The ExclusionRecord is doc-level (no region breakdown needed — the document
    itself is excluded, not individual regions).
    """
    primary_id = parse_result.get("dedup_primary_document_id") or "unknown"
    reason_detail = (
        f"Superseded near-duplicate: this document has been identified as an older "
        f"version of primary document '{primary_id}' (D-25, owner ruling 2026-09-03). "
        f"No segments produced by default. Enable ingestion.dedup.index_superseded_versions "
        f"to index superseded versions at salience tier 'excluded'."
    )

    excl_loc = SourceLocation(
        locator_kind=LocatorKind.byte_range,
        byte_start=0,
        byte_end=0,
    )
    excl_raw = f"{document_id}:excl:superseded".encode()
    exclusion_id = "excl-" + hashlib.sha256(excl_raw).hexdigest()[:24]

    exclusion = ExclusionRecord(
        exclusion_id=exclusion_id,
        location=excl_loc,
        source_region_ids=[],
        reason=ExclusionReason.superseded_version,
        reason_detail=reason_detail,
        reversible=True,
    )

    return SegmentSet(
        schema_version="1.2.0",
        tenancy=tenancy,
        document_id=document_id,
        content_hash=content_hash,
        segments=[],
        reassembly=ReassemblyRecord(
            method=ReassemblyMethod.document_order_concat,
            covered_region_ids=[],
            reassembly_digest=_EMPTY_REASSEMBLY_DIGEST,
        ),
        exclusions=[exclusion],
        cross_references=[],
        decomposed_at=decomposed_at,
    )


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
    """
    regions = parse_result.get("regions", [])
    findings = parse_result.get("findings", [])
    parse_status = parse_result.get("parse_status", "excluded_pre_parse")

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
        excl_raw = f"{document_id}:excl:{region_id}".encode()
        exclusion_id = "excl-" + hashlib.sha256(excl_raw).hexdigest()[:24]
        exclusions.append(
            ExclusionRecord(
                exclusion_id=exclusion_id,
                location=excl_loc,
                source_region_ids=[region_id],
                reason=reason,
                reason_detail=reason_detail,
                reversible=True,
            )
        )

    if not exclusions and not regions:
        excl_loc = SourceLocation(
            locator_kind=LocatorKind.byte_range,
            byte_start=0,
            byte_end=0,
        )
        excl_raw = f"{document_id}:excl:doc-level".encode()
        exclusion_id = "excl-" + hashlib.sha256(excl_raw).hexdigest()[:24]
        exclusions.append(
            ExclusionRecord(
                exclusion_id=exclusion_id,
                location=excl_loc,
                source_region_ids=[],
                reason=reason,
                reason_detail=reason_detail,
                reversible=True,
            )
        )

    return SegmentSet(
        schema_version="1.2.0",
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
# Pass pipeline execution for parsed documents
# ---------------------------------------------------------------------------


def _run_passes(
    document_id: str,
    content_hash: str,
    tenancy: TenancyBlock,
    parse_result: dict[str, Any],
    decomposed_at: datetime,
    boilerplate_blocks: set[str] | None = None,
) -> SegmentSet:
    """Run the ordered PASSES pipeline and assemble the final SegmentSet.

    Args:
        boilerplate_blocks: Normalised boilerplate paragraph strings detected by
            corpus_passes.  Threaded into DocumentContext so BoilerplatePass can
            retype matching segments.  None = no boilerplate detection (Phase 1
            behaviour).
    """
    doc_ctx = DocumentContext(
        document_id=document_id,
        content_hash=content_hash,
        tenancy=tenancy,
        parse_result=parse_result,
        decomposed_at=decomposed_at,
        boilerplate_blocks=boilerplate_blocks or set(),
    )

    # Run each pass in sequence; accumulate segments, exclusions, cross_references.
    segments: list = []
    exclusions: list = []
    cross_references: list = []

    for p in PASSES:
        result = p.run(doc_ctx, segments, exclusions)
        segments = result.segments
        exclusions = result.exclusions
        # Accumulate cross_references produced by any pass (typically segmentation only)
        cross_references.extend(result.cross_references)

    # Determine covered region IDs (regions that contributed segments)
    covered_region_ids: list[str] = []
    seen: set[str] = set()
    for seg in sorted(segments, key=lambda s: s.document_order):
        for rid in seg.source_region_ids:
            if rid not in seen:
                seen.add(rid)
                covered_region_ids.append(rid)

    # Build reassembly record
    reassembly_text = "".join(
        (seg.text or "") for seg in sorted(segments, key=lambda s: s.document_order)
    )
    reassembly_digest = hashlib.sha256(reassembly_text.encode()).hexdigest()

    reassembly = ReassemblyRecord(
        method=ReassemblyMethod.document_order_concat,
        covered_region_ids=covered_region_ids,
        reassembly_digest=reassembly_digest,
    )

    return SegmentSet(
        schema_version="1.2.0",
        tenancy=tenancy,
        document_id=document_id,
        content_hash=content_hash,
        segments=segments,
        reassembly=reassembly,
        exclusions=exclusions,
        cross_references=cross_references,
        decomposed_at=decomposed_at,
    )


# ---------------------------------------------------------------------------
# Stage
# ---------------------------------------------------------------------------


class DecomposeStage(Stage):
    """Stage 3 — Decompose (Phase 1: real prose segmentation for native-text PDFs).

    Consumes ParseResultBatch, emits SegmentSetBatch.

    The ordered pass pipeline (PASSES) is declared in ``passes/__init__.py``.
    Future passes (typing enrichment, boilerplate, injection scoring, language)
    append there.

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
    consumed_version_range = SUPPORTED_PARSE_RESULT_BATCH
    produced_contract = "segment_set_batch"
    output_model = SegmentSetBatch

    def __init__(
        self,
        run_started_at: datetime | None = None,
        artifacts_root: Path | str | None = None,
        run_id: str = "",
        index_superseded_versions: bool = False,
    ) -> None:
        self._run_started_at = run_started_at or datetime.now(tz=UTC)
        self._artifacts_root = Path(artifacts_root) if artifacts_root else None
        self._run_id = run_id
        self._index_superseded_versions = index_superseded_versions
        """D-25: when False (default), superseded near-dup docs produce no segments."""

    def _produce(self, input_data: dict[str, Any] | None) -> dict[str, Any]:
        """Produce one SegmentSet per parse result entry.

        For documents that parsed (or partially parsed): run the ordered PASSES.
        For excluded/failed documents: empty SegmentSet with ExclusionRecord.
        For superseded near-duplicate documents (D-25): empty SegmentSet with
          ExclusionRecord(reason=superseded_version) by default; full decomposition
          at excluded tier when index_superseded_versions=True.

        Frozen-artifact reuse: if artifacts_root is set and the frozen artifact
        exists for (document_id, content_hash, config_version), load it without
        recomputing.
        """
        assert input_data is not None, "Decompose requires ParseResultBatch input"

        results = input_data.get("results", [])

        # Phase 2: extract boilerplate blocks from the batch envelope (1.1.0+).
        # Empty set is safe — BoilerplatePass will simply not retype anything.
        boilerplate_blocks: set[str] = set(input_data.get("boilerplate_blocks", []))

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
            dedup_role = parse_result.get("dedup_role", "unique")

            # --- D-25 enforcement ---
            # A superseded document produces no segments by default.
            # Check BEFORE the frozen-artifact cache so we do not cache a full
            # decomposition for a document that should be suppressed.
            if dedup_role == "superseded" and not self._index_superseded_versions:
                segment_set = _make_superseded_segment_set(
                    document_id=document_id,
                    content_hash=content_hash,
                    tenancy=tenancy,
                    parse_result=parse_result,
                    decomposed_at=decomposed_at,
                )
                segment_sets.append(segment_set.model_dump(mode="json"))
                continue

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
                segment_set = _run_passes(
                    document_id=document_id,
                    content_hash=content_hash,
                    tenancy=tenancy,
                    parse_result=parse_result,
                    decomposed_at=decomposed_at,
                    boilerplate_blocks=boilerplate_blocks,
                )
            else:
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
            skeleton=None,
            segment_sets=segment_sets,
        )
        return batch.model_dump(mode="json")
