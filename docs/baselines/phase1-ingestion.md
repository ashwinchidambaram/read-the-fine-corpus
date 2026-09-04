# Phase 1 Ingestion Throughput Baseline

## Purpose

This document is the **canonical baseline** for ingestion throughput established
in Phase 1 (§4.5, §19).  Future phases MUST NOT regress below these numbers.

The regression **GATE** is:

> Any phase that touches the ingestion pipeline (chunker, Build stage, embedding
> provider integration, or pipeline orchestrator) MUST re-run this test suite and
> compare its results against the numbers in this table.  A regression of more
> than 20% in docs/sec or chunks/sec is a hard failure that blocks the phase
> from merging.

The test itself (``tests/phase1/test_throughput_baseline.py``) asserts only
**sanity** (throughput > 0, docs_processed > 0).  The regression gate is enforced
by CI in the phase that introduces the regression — it is a cross-phase contract,
not a single-phase assertion.

## Corpus

Native-PDF golden corpus subset: documents with ``expected_triage_class == "native_pdf"``
from ``tests/fixtures/golden/manifest.yaml``.

Phase 1 native-PDF files:
- corpus/clean_native.pdf
- corpus/policy_v1.pdf, policy_v2.pdf, policy_v3.pdf
- corpus/boilerplate_a.pdf, boilerplate_b.pdf
- corpus/adversarial.pdf
- corpus/form_filled.pdf
- corpus/malformed_structure.pdf

## Methodology

- Full pipeline: collect → assess → decompose → plan → build (NO promote)
- Wall-clock time measured from pipeline start to build artifact written
- FakeProvider is NOT used — real embedding providers only
- Parallel workers: none (single-threaded pipeline)
- Checkpoint: disabled (fresh run_id each time; no resume)

## Writing a new baseline row

Baseline rows are written only when the file is absent **or** when
``RTFC_WRITE_BASELINE=1`` is set.  This prevents duplicate rows accumulating
across repeated local runs.  To record a fresh canonical baseline:

```bash
RTFC_WRITE_BASELINE=1 uv run pytest tests/phase1/test_throughput_baseline.py \
    -v -m "qdrant_integration and provider_integration" -s
```

## Results

| Date | Provider | Docs | Chunks | Elapsed (s) | Docs/s | Chunks/s | Machine | Environment |
|------|----------|------|--------|-------------|--------|----------|---------|-------------|
| 2026-09-03 | ollama/nomic-embed-text | 9 | 35 | 1.5 | 6.06 | 23.6 | Ashwin-MacBook | local Docker Compose |
