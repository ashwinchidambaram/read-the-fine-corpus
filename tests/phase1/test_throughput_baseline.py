"""Ingestion throughput baseline — §4.5 / §19 Phase 1.

Acceptance criterion (§19 Phase 1):
  "Ingestion throughput baseline recorded per provider."

Spec reference (§4.5):
  "Ingestion throughput: Executor establishes a baseline in Phase 1 and
  MUST NOT regress it."

What this test does
-------------------
Times the full pipeline collect → assess → decompose → plan → build (WITHOUT
promote) over the native-PDF golden corpus subset, per available provider:

  - Ollama (localhost:11434, nomic-embed-text): always run if reachable.
  - OpenAI: run only when OPENAI_API_KEY is present.

For each provider it computes:
  - elapsed_seconds: wall-clock time for the full pipeline.
  - docs_processed: number of documents successfully processed (non-skipped).
  - chunks_produced: total chunks written to the shadow collection.
  - docs_per_sec: throughput in documents/second.
  - chunks_per_sec: throughput in chunks/second.

Results are written to docs/baselines/phase1-ingestion.md.  This document is
the canonical MUST-NOT-REGRESS reference for future phases.

Assertions in this test
-----------------------
The test itself asserts only SANITY conditions:
  - throughput > 0 (something was produced)
  - docs_processed > 0 (no documents were all-skipped)

The regression GATE (comparing against the baseline) comes in later phases.
See docs/baselines/phase1-ingestion.md §Regression for the gate protocol.

Environment metadata recorded in the baseline document is supplied via
environment variables — no wall-clock in the test logic itself:

  RTFC_BASELINE_MACHINE   — machine description (default: "unknown")
  RTFC_BASELINE_ENV       — deployment description (default: "local Docker Compose")

Markers
-------
``qdrant_integration``   — requires live Qdrant + Postgres (for shadow collection)
``provider_integration`` — requires at least one live embedding provider

Run:
    docker compose -f docker-compose.yml -f docker-compose.integration.yml up -d
    uv run pytest tests/phase1/test_throughput_baseline.py -v \\
        -m "qdrant_integration and provider_integration" \\
        -s   # -s shows print output (baseline table)
    docker compose -f docker-compose.yml -f docker-compose.integration.yml down
"""

from __future__ import annotations

import os
import pathlib
import platform
import shutil
import tempfile
import time
import uuid
from typing import Any

import pytest
import yaml

from conftest import qdrant_integration_mark

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

FIXTURE_ROOT = pathlib.Path(__file__).parent.parent / "fixtures" / "golden"
MANIFEST_PATH = FIXTURE_ROOT / "manifest.yaml"
CORPUS_ROOT = FIXTURE_ROOT / "corpus"

BASELINES_DOC = (
    pathlib.Path(__file__).parent.parent.parent / "docs" / "baselines" / "phase1-ingestion.md"
)

QDRANT_URL = "http://localhost:6333"
POSTGRES_DSN = "postgresql+psycopg://finecorpus:finecorpus@localhost:5432/finecorpus"

OLLAMA_URL = "http://localhost:11434"
OLLAMA_MODEL = "nomic-embed-text"

WORKSPACE_ID = "ws-throughput-test"


# ---------------------------------------------------------------------------
# Helpers: reachability
# ---------------------------------------------------------------------------


def _ollama_reachable() -> bool:
    try:
        import httpx

        r = httpx.get(f"{OLLAMA_URL}/api/tags", timeout=3.0)
        return r.status_code == 200
    except Exception:
        return False


def _openai_key_present() -> bool:
    return bool(
        os.environ.get("OPENAI_API_KEY", "").strip()
        or os.environ.get("FINECORPUS_OPENAI_API_KEY", "").strip()
    )


# ---------------------------------------------------------------------------
# Corpus subset helpers
# ---------------------------------------------------------------------------


def _native_pdf_files() -> list[pathlib.Path]:
    with MANIFEST_PATH.open() as f:
        manifest = yaml.safe_load(f)
    return [
        CORPUS_ROOT / pathlib.Path(entry["file"]).name
        for entry in manifest["fixtures"]
        if entry.get("expected_triage_class") == "native_pdf"
        and (CORPUS_ROOT / pathlib.Path(entry["file"]).name).exists()
    ]


def _make_native_pdf_subset_dir() -> pathlib.Path:
    """Create a temp dir containing symlinks to the native-PDF corpus files."""
    subset_dir = pathlib.Path(tempfile.mkdtemp(prefix="rtfc-throughput-"))
    for src in _native_pdf_files():
        (subset_dir / src.name).symlink_to(src.resolve())
    return subset_dir


# ---------------------------------------------------------------------------
# Core timing function
# ---------------------------------------------------------------------------


def _time_pipeline(
    *,
    source_dir: pathlib.Path,
    provider: Any,
    provider_label: str,
    engine: Any,
    qdrant_adapter: Any,
) -> dict[str, Any]:
    """Run the full pipeline (no promote) and return timing + result metrics.

    Returns:
        dict with keys:
          provider_label, elapsed_seconds, docs_processed, docs_skipped,
          chunks_produced, docs_per_sec, chunks_per_sec, shadow_collection
    """
    from sqlalchemy.orm import Session

    from finecorpus.pipeline import run_pipeline
    from finecorpus.pipeline.artifact_store import ArtifactStore
    from finecorpus.pipeline.build.stage import BuildResult

    kb_id = f"kb-throughput-{uuid.uuid4().hex[:8]}"
    run_id = f"run-throughput-{uuid.uuid4().hex[:8]}"
    # Sanitize provider_label for use as a temp-dir prefix (remove slashes)
    safe_label = provider_label.replace("/", "-")
    artifacts_root = pathlib.Path(tempfile.mkdtemp(prefix=f"rtfc-tp-{safe_label}-"))

    try:
        t0 = time.monotonic()
        with Session(engine) as session:
            run_pipeline(
                source_dir=source_dir,
                artifacts_root=artifacts_root,
                run_id=run_id,
                workspace_id=WORKSPACE_ID,
                kb_id=kb_id,
                embedding_provider=provider,
                index_adapter=qdrant_adapter,
                build_id=1,
                promote=False,  # baseline: do NOT promote; measure build only
                db_session=session,
            )
        elapsed = time.monotonic() - t0

        # Load BuildResult to extract metrics
        store = ArtifactStore(artifacts_root=artifacts_root, run_id=run_id)
        raw = store.load_with_model_validation("build", BuildResult)
        build_result = BuildResult.model_validate(raw.model_dump())

        docs_processed = len(build_result.chunks_by_document)
        docs_skipped = len(build_result.skipped_documents)
        chunks_produced = build_result.chunk_count
        shadow = build_result.shadow_collection or ""

        return {
            "provider_label": provider_label,
            "elapsed_seconds": elapsed,
            "docs_processed": docs_processed,
            "docs_skipped": docs_skipped,
            "chunks_produced": chunks_produced,
            "docs_per_sec": docs_processed / elapsed if elapsed > 0 else 0.0,
            "chunks_per_sec": chunks_produced / elapsed if elapsed > 0 else 0.0,
            "shadow_collection": shadow,
            "kb_id": kb_id,
        }
    finally:
        # Always clean up artifacts temp dir
        shutil.rmtree(artifacts_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Baseline document writer
# ---------------------------------------------------------------------------


def _write_baseline_doc(results: list[dict[str, Any]]) -> None:
    """Append a new baseline entry to docs/baselines/phase1-ingestion.md.

    Creates the file (and parent directory) on first run.
    On subsequent runs, appends a new row to the results table.

    The document is structured as the canonical Phase 1 baseline record with:
    - A fixed header section (written once on first create).
    - A results table that future phases append to.
    """
    BASELINES_DOC.parent.mkdir(parents=True, exist_ok=True)

    machine = os.environ.get("RTFC_BASELINE_MACHINE", platform.node() or "unknown")
    env_desc = os.environ.get("RTFC_BASELINE_ENV", "local Docker Compose")
    date_str = os.environ.get("RTFC_BASELINE_DATE", "2026-09-03")

    if not BASELINES_DOC.exists():
        _write_baseline_header(machine=machine, env_desc=env_desc)

    # Append result rows
    with BASELINES_DOC.open("a") as f:
        for r in results:
            f.write(
                f"| {date_str} | {r['provider_label']} "
                f"| {r['docs_processed']} | {r['chunks_produced']} "
                f"| {r['elapsed_seconds']:.1f} "
                f"| {r['docs_per_sec']:.2f} "
                f"| {r['chunks_per_sec']:.1f} "
                f"| {machine} | {env_desc} |\n"
            )


def _write_baseline_header(*, machine: str, env_desc: str) -> None:
    """Write the fixed header for docs/baselines/phase1-ingestion.md on first create."""
    content = """\
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

## Results

| Date | Provider | Docs | Chunks | Elapsed (s) | Docs/s | Chunks/s | Machine | Environment |
|------|----------|------|--------|-------------|--------|----------|---------|-------------|
"""
    BASELINES_DOC.write_text(content)


# ---------------------------------------------------------------------------
# Availability checks (module-level, same pattern as test_parity.py)
# ---------------------------------------------------------------------------

_OLLAMA_UP = _ollama_reachable()
_OPENAI_KEY_PRESENT = _openai_key_present()
_NO_PROVIDERS = not _OLLAMA_UP and not _OPENAI_KEY_PRESENT

_PROVIDER_SKIP_REASON = (
    "No embedding providers available: Ollama not reachable at localhost:11434 "
    "and OPENAI_API_KEY is not set.  Throughput baseline requires a real provider."
)


def provider_integration_mark(obj: Any) -> Any:
    """Composite: provider_integration + qdrant_integration + availability skipif."""
    obj = pytest.mark.provider_integration(obj)
    obj = qdrant_integration_mark(obj)
    obj = pytest.mark.skipif(_NO_PROVIDERS, reason=_PROVIDER_SKIP_REASON)(obj)
    return obj


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def engine() -> Any:
    from sqlalchemy import create_engine

    from finecorpus.control.metadata import create_tables

    eng = create_engine(POSTGRES_DSN)
    create_tables(eng)
    return eng


@pytest.fixture(scope="module")
def qdrant_adapter() -> Any:
    from finecorpus.index.qdrant.backend import QdrantAdapter

    return QdrantAdapter(url=QDRANT_URL, timeout=60)


@pytest.fixture(scope="module")
def native_pdf_subset_dir() -> Any:
    d = _make_native_pdf_subset_dir()
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture(scope="module")
def ollama_provider() -> Any:
    if not _OLLAMA_UP:
        return None
    from finecorpus.embedding.ollama_provider import OllamaProvider

    return OllamaProvider(base_url=OLLAMA_URL, model_id=OLLAMA_MODEL)


@pytest.fixture(scope="module")
def openai_provider() -> Any:
    if not _OPENAI_KEY_PRESENT:
        return None
    from finecorpus.embedding.openai_provider import OpenAIProvider

    api_key = (
        os.environ.get("FINECORPUS_OPENAI_API_KEY", "").strip()
        or os.environ.get("OPENAI_API_KEY", "").strip()
    )
    return OpenAIProvider(api_key=api_key)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@provider_integration_mark
class TestThroughputBaseline:
    """Ingestion throughput: sanity gates + baseline doc recorder."""

    def test_ollama_throughput_sanity(
        self,
        native_pdf_subset_dir: pathlib.Path,
        ollama_provider: Any,
        engine: Any,
        qdrant_adapter: Any,
    ) -> None:
        """Ollama: full pipeline over native-PDF subset → throughput > 0."""
        if not _OLLAMA_UP or ollama_provider is None:
            pytest.skip("Ollama not reachable at localhost:11434")

        result = _time_pipeline(
            source_dir=native_pdf_subset_dir,
            provider=ollama_provider,
            provider_label=f"ollama/{OLLAMA_MODEL}",
            engine=engine,
            qdrant_adapter=qdrant_adapter,
        )

        print(
            f"\n[Throughput/Ollama] elapsed={result['elapsed_seconds']:.1f}s "
            f"docs={result['docs_processed']} chunks={result['chunks_produced']} "
            f"docs/s={result['docs_per_sec']:.2f} chunks/s={result['chunks_per_sec']:.1f}"
        )

        # Sanity assertions — NOT regression gates (those come later phases)
        assert result["docs_processed"] > 0, (
            "Ollama pipeline processed 0 documents — ingestion may have failed"
        )
        assert result["chunks_produced"] > 0, (
            "Ollama pipeline produced 0 chunks — chunker or Build stage may have failed"
        )
        assert result["docs_per_sec"] > 0.0, "docs_per_sec must be positive"
        assert result["chunks_per_sec"] > 0.0, "chunks_per_sec must be positive"

        # Record baseline
        _write_baseline_doc([result])

        # Cleanup shadow collection (not promoted; drop explicitly)
        _cleanup_shadow(result, qdrant_adapter)

    def test_openai_throughput_sanity(
        self,
        native_pdf_subset_dir: pathlib.Path,
        openai_provider: Any,
        engine: Any,
        qdrant_adapter: Any,
    ) -> None:
        """OpenAI: full pipeline over native-PDF subset → throughput > 0."""
        if not _OPENAI_KEY_PRESENT or openai_provider is None:
            pytest.skip("OPENAI_API_KEY not set — OpenAI throughput baseline skipped")

        result = _time_pipeline(
            source_dir=native_pdf_subset_dir,
            provider=openai_provider,
            provider_label="openai/text-embedding-3-small",
            engine=engine,
            qdrant_adapter=qdrant_adapter,
        )

        print(
            f"\n[Throughput/OpenAI] elapsed={result['elapsed_seconds']:.1f}s "
            f"docs={result['docs_processed']} chunks={result['chunks_produced']} "
            f"docs/s={result['docs_per_sec']:.2f} chunks/s={result['chunks_per_sec']:.1f}"
        )

        assert result["docs_processed"] > 0, (
            "OpenAI pipeline processed 0 documents — ingestion may have failed"
        )
        assert result["chunks_produced"] > 0, (
            "OpenAI pipeline produced 0 chunks — chunker or Build stage may have failed"
        )
        assert result["docs_per_sec"] > 0.0, "docs_per_sec must be positive"
        assert result["chunks_per_sec"] > 0.0, "chunks_per_sec must be positive"

        _write_baseline_doc([result])

        _cleanup_shadow(result, qdrant_adapter)


# ---------------------------------------------------------------------------
# Cleanup helper
# ---------------------------------------------------------------------------


def _cleanup_shadow(result: dict[str, Any], qdrant_adapter: Any) -> None:
    """Drop the shadow collection created by a no-promote pipeline run."""
    shadow = result.get("shadow_collection", "")
    if shadow:
        try:
            if qdrant_adapter.collection_exists(shadow):
                qdrant_adapter.drop_collection(shadow)
        except Exception:
            pass
