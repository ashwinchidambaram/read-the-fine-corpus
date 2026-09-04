"""Stage 2 — Assess (Phase 1: real native-text PDF parsing via pypdf).

Produces a ParseResultBatch artifact: one ParseResult per inventory item,
plus corpus-level near-duplicate clustering and boilerplate detection (Phase 2).

This module owns:
  - Routing (first parser in REGISTRY whose can_parse matches wins).
  - Batch assembly and contract validation.
  - Tenancy reconstruction from the inventory envelope.
  - Corpus-level passes (near-dup clustering, boilerplate detection) invoked
    after all per-document parsing is done (Phase 2 addition).

Format-specific logic lives in ``parsers/``:
  - ``parsers/pdf_native.py``   — native-text PDF (pypdf extraction, quality
    scoring, malformed/partial handling, encrypted/image-only detection)
  - ``parsers/unsupported.py``  — honest exclusion for xlsx/html/wav/mp4/dwg
    and any other unrecognised extension.

The ordered parser registry is declared in ``parsers/__init__.py``.

Phase 1 scope — native-text PDF prose:
  - Native-text PDFs (.pdf extension, not encrypted, not image-only): real per-page
    text extraction via pypdf, quality scoring, page-level confidence 1.0, near-empty
    detection, encoding issue detection.
  - Scanned PDFs, HTML, spreadsheets, and all other file types:
    HONESTLY excluded with parse_status=excluded_pre_parse or appropriate failure.
    Phase 2 adds OCR confidence, boilerplate dedup, and other content types.

Phase 2 addition — corpus-level passes:
  - Near-duplicate clustering: word 5-gram Jaccard similarity across all parsed
    documents; version families formed at ``ingestion.dedup.near_duplicate_threshold``
    (default 0.50).  Results stored in ParseResultBatch.version_families.
  - Boilerplate detection: normalised paragraph blocks appearing in more than the
    configured proportion of corpus documents are classified as boilerplate.
    Results stored in ParseResultBatch.boilerplate_blocks for BoilerplatePass.
  - Each ParseResult in the results list is annotated with ``dedup_role``
    (``"unique"``, ``"primary"``, or ``"superseded"``).

D-26 resolution: ParseResultBatch is now an official versioned contract.
DecomposeStage checks schema_version against SUPPORTED_PARSE_RESULT_BATCH.

Nothing is silently dropped — every Inventory item gets exactly one ParseResult
entry (§6 rule 6, §12).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from finecorpus.contracts.parse_result_batch import (
    BATCH_SCHEMA_VERSION,
    ParseResultBatch,
)
from finecorpus.contracts.shared.blocks import (
    PermissionFidelity,
    PermissionMode,
    PermissionSource,
    TenancyBlock,
)
from finecorpus.contracts.versions import SUPPORTED_INVENTORY
from finecorpus.pipeline.assess.corpus_passes import run_corpus_passes
from finecorpus.pipeline.assess.parsers import REGISTRY, ParserContext
from finecorpus.pipeline.stage import Stage

# ---------------------------------------------------------------------------
# Default parser context (executor-defined thresholds — documented in
# docs/pipeline/assess.md and parsers/base.py).
# ---------------------------------------------------------------------------

_DEFAULT_CTX = ParserContext(
    min_chars_per_page=200,
    near_empty_threshold=0.20,
    page_nonempty_chars=50,
)


# ---------------------------------------------------------------------------
# Stage
# ---------------------------------------------------------------------------


class AssessStage(Stage):
    """Stage 2 — Assess (Phase 1: real native-text PDF parsing).

    Consumes Inventory, emits ParseResultBatch.

    Routing: each inventory item is passed to the first parser in ``REGISTRY``
    whose ``can_parse`` returns ``True`` (ordered, first-match wins).  The
    registry is declared in ``parsers/__init__.py``; future parsers (OCR, HTML,
    spreadsheet) are inserted there.

    D-26 resolution: ParseResultBatch is now an official versioned contract;
    DecomposeStage version-checks it.

    Args:
        run_id: Pipeline run identifier (§12 traceability), threaded from the orchestrator.
        run_started_at: Timestamp threaded from the orchestrator (not wall-clock).
    """

    name = "assess"
    consumed_contract = "inventory"
    consumed_version_range = SUPPORTED_INVENTORY
    produced_contract = "parse_result_batch"
    output_model = ParseResultBatch

    def __init__(
        self,
        run_id: str = "",
        run_started_at: datetime | None = None,
    ) -> None:
        self._run_id = run_id
        self._run_started_at = run_started_at or datetime.now(tz=UTC)

    def _produce(self, input_data: dict[str, Any] | None) -> dict[str, Any]:
        """Produce one ParseResult per inventory item.

        Nothing is silently dropped — every item gets exactly one entry.

        Phase 2 addition: after all per-document parsing, run corpus-level passes
        (near-dup clustering and boilerplate detection) over the full results list.
        Results are stored in the batch envelope for DecomposeStage to consume.
        """
        assert input_data is not None, "Assess requires Inventory input"

        # Reconstruct tenancy from inventory
        tenancy_raw = input_data.get("tenancy", {})
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

        parsed_at = self._run_started_at
        ctx = _DEFAULT_CTX
        results: list[dict[str, Any]] = []

        for item in input_data.get("items", []):
            # Propagate source timestamps from inventory into parse results so
            # corpus_passes can use source_modified_at for primary selection.
            parse_result_extra = {
                "source_modified_at": item.get("source_modified_at"),
                "discovered_at": item.get("discovered_at"),
            }

            # Route to the first parser that claims this item.
            parser = next((p for p in REGISTRY if p.can_parse(item)), None)
            if parser is None:
                # Should never happen — FallbackUnsupportedParser always matches.
                raise RuntimeError(
                    f"No parser found for item {item.get('source_path')!r}. "
                    "Registry is missing a catch-all parser."
                )
            parse_result = parser.parse(item, tenancy, parsed_at, ctx)
            pr_dict = parse_result.model_dump(mode="json")
            # Merge inventory timestamps into the result dict for corpus_passes.
            pr_dict.update(parse_result_extra)
            results.append(pr_dict)

        # --- Phase 2: corpus-level passes ---
        # Run after all per-document parsing so the full corpus is available.
        version_families, boilerplate_blocks = run_corpus_passes(results)

        batch = ParseResultBatch(
            schema_version=BATCH_SCHEMA_VERSION,
            contract="parse_result_batch",
            run_id=self._run_id,
            produced_at=self._run_started_at,
            skeleton=None,
            results=results,
            version_families=version_families,
            boilerplate_blocks=sorted(boilerplate_blocks),  # deterministic ordering
        )
        return batch.model_dump(mode="json")
