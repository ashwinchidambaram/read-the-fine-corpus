"""Ingestion throughput benchmark — §4.5 (MUST NOT regress the P1 baseline).

Times the REAL pipeline (collect → assess → decompose → plan → build, no
promote) over the golden native-PDF subset — the same corpus and methodology
as ``tests/phase1/test_throughput_baseline.py`` — and reports docs/sec and
chunks/sec.

FAKE mode
---------
FakeProvider + FakeAdapter.  The embedding step is a deterministic sha256
transform with NO network call, so FAKE-mode throughput is dominated by parse +
chunk CPU work and is much HIGHER than any real-provider baseline.  It is a
useful gross-regression guard for the parse/chunk/build path but it is NOT
comparable to the Phase-1 per-provider baseline (which measured a real
embedding provider).  We therefore compare FAKE-mode throughput against a
LOOSE floor, not against the provider baseline.

LIVE mode
---------
Real Qdrant + real embedding provider.  Directly comparable to the Phase-1
baseline rows; this is the number the regression gate is defined against
(> 20% drop in docs/sec or chunks/sec is a hard failure — see
docs/baselines/phase1-ingestion.md).
"""

from __future__ import annotations

import re
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from benchmarks.corpus import cleanup_dir, make_native_pdf_subset_dir

_REPO_ROOT = Path(__file__).resolve().parent.parent
_BASELINE_DOC = _REPO_ROOT / "docs" / "baselines" / "phase1-ingestion.md"


@dataclass(frozen=True)
class IngestionBenchResult:
    """Outcome of an ingestion-throughput benchmark run."""

    mode: str
    provider_label: str
    docs_processed: int
    chunks_produced: int
    elapsed_seconds: float
    docs_per_sec: float
    chunks_per_sec: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "provider_label": self.provider_label,
            "docs_processed": self.docs_processed,
            "chunks_produced": self.chunks_produced,
            "elapsed_seconds": round(self.elapsed_seconds, 4),
            "docs_per_sec": round(self.docs_per_sec, 4),
            "chunks_per_sec": round(self.chunks_per_sec, 4),
        }


@dataclass(frozen=True)
class BaselineRow:
    """A parsed row from docs/baselines/phase1-ingestion.md."""

    date: str
    provider: str
    docs: int
    chunks: int
    elapsed_s: float
    docs_per_sec: float
    chunks_per_sec: float


def parse_baseline_rows() -> list[BaselineRow]:
    """Parse the Phase-1 ingestion baseline table, if present.

    Returns an empty list when the doc has no data rows (only the header was
    written because the integration test has not been run in this environment).
    """
    if not _BASELINE_DOC.exists():
        return []
    rows: list[BaselineRow] = []
    for line in _BASELINE_DOC.read_text().splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        # Header: Date | Provider | Docs | Chunks | Elapsed (s) | Docs/s | Chunks/s | Machine | Env
        if len(cells) < 7:
            continue
        if cells[0].lower() == "date" or set(cells[0]) <= set("-: "):
            continue
        try:
            rows.append(
                BaselineRow(
                    date=cells[0],
                    provider=cells[1],
                    docs=int(cells[2]),
                    chunks=int(cells[3]),
                    elapsed_s=float(cells[4]),
                    docs_per_sec=float(cells[5]),
                    chunks_per_sec=float(cells[6]),
                )
            )
        except (ValueError, IndexError):
            continue
    return rows


def _time_pipeline(
    *,
    source_dir: Path,
    provider: Any,
    index_adapter: Any,
    provider_label: str,
    mode: str,
    db_session: Any = None,
) -> IngestionBenchResult:
    """Run the real no-promote pipeline once and compute throughput."""
    from finecorpus.pipeline import run_pipeline
    from finecorpus.pipeline.artifact_store import ArtifactStore
    from finecorpus.pipeline.build.stage import BuildResult

    run_id = f"run-bench-{uuid.uuid4().hex[:8]}"
    kb_id = f"kb-bench-{uuid.uuid4().hex[:8]}"
    artifacts_root = Path(tempfile.mkdtemp(prefix="rtfc-bench-art-"))
    try:
        t0 = time.monotonic()
        run_pipeline(
            source_dir=source_dir,
            artifacts_root=artifacts_root,
            run_id=run_id,
            workspace_id="ws-bench",
            kb_id=kb_id,
            embedding_provider=provider,
            index_adapter=index_adapter,
            build_id=1,
            promote=False,
            db_session=db_session,
        )
        elapsed = time.monotonic() - t0

        store = ArtifactStore(artifacts_root=artifacts_root, run_id=run_id)
        raw = store.load_with_model_validation("build", BuildResult)
        build_result = BuildResult.model_validate(raw.model_dump())

        docs = len(build_result.chunks_by_document)
        chunks = build_result.chunk_count
        return IngestionBenchResult(
            mode=mode,
            provider_label=provider_label,
            docs_processed=docs,
            chunks_produced=chunks,
            elapsed_seconds=elapsed,
            docs_per_sec=(docs / elapsed) if elapsed > 0 else 0.0,
            chunks_per_sec=(chunks / elapsed) if elapsed > 0 else 0.0,
        )
    finally:
        cleanup_dir(artifacts_root)


def run_fake() -> IngestionBenchResult:
    """Ingestion throughput over the golden native-PDF subset with Fake doubles."""
    import sys

    tests_dir = _REPO_ROOT / "tests"
    if str(tests_dir) not in sys.path:
        sys.path.insert(0, str(tests_dir))

    from retrieval.helpers import FakeAdapter  # type: ignore[import-not-found]

    from finecorpus.embedding.fake import FakeProvider

    source_dir = make_native_pdf_subset_dir()
    try:
        return _time_pipeline(
            source_dir=source_dir,
            provider=FakeProvider(dimensions=64, model_id="fake-embed-v1"),
            index_adapter=FakeAdapter(),
            provider_label="fake/fake-embed-v1",
            mode="fake",
        )
    finally:
        cleanup_dir(source_dir)


def run_live(
    *,
    qdrant_url: str = "http://localhost:6333",
    postgres_dsn: str = "postgresql+psycopg://finecorpus:finecorpus@localhost:5432/finecorpus",
) -> IngestionBenchResult | None:
    """Ingestion throughput over the golden subset against live Qdrant + provider.

    Returns None when Qdrant is unreachable.  This is the number that is
    directly comparable to the Phase-1 baseline and gates regression.
    """
    from benchmarks.bench_retrieval import live_infra_reachable

    if not live_infra_reachable():
        return None

    import os

    from sqlalchemy import create_engine

    from finecorpus.control.metadata import create_tables
    from finecorpus.index.qdrant.backend import QdrantAdapter

    if (
        os.environ.get("OPENAI_API_KEY", "").strip()
        or os.environ.get("FINECORPUS_OPENAI_API_KEY", "").strip()
    ):
        from finecorpus.embedding.openai_provider import OpenAIProvider

        api_key = (
            os.environ.get("FINECORPUS_OPENAI_API_KEY", "").strip()
            or os.environ.get("OPENAI_API_KEY", "").strip()
        )
        provider: Any = OpenAIProvider(api_key=api_key)
        label = "openai/text-embedding-3-small"
    else:
        from finecorpus.embedding.ollama_provider import OllamaProvider

        provider = OllamaProvider(
            base_url=os.environ.get("OLLAMA_URL", "http://localhost:11434"),
            model_id=os.environ.get("OLLAMA_MODEL", "nomic-embed-text"),
        )
        label = "ollama/nomic-embed-text"

    engine = create_engine(postgres_dsn)
    create_tables(engine)
    adapter = QdrantAdapter(url=qdrant_url, timeout=60)
    return _time_pipeline(
        source_dir=make_native_pdf_subset_dir(),
        provider=provider,
        index_adapter=adapter,
        provider_label=label,
        mode="live",
    )


def compare_against_baseline(
    result: IngestionBenchResult,
    *,
    regression_pct: float = 20.0,
) -> dict[str, Any]:
    """Compare a LIVE result against the matching Phase-1 baseline row.

    A > ``regression_pct`` drop in docs/sec or chunks/sec is a regression.
    Only meaningful for LIVE results (FAKE uses a different provider).

    Returns a verdict dict; ``verdict`` is one of:
      "no-baseline"  — the baseline doc has no data rows in this environment.
      "provider-mismatch" — no baseline row matches this provider label.
      "pass" / "regressed".
    """
    rows = parse_baseline_rows()
    if not rows:
        return {"verdict": "no-baseline", "reason": "phase1-ingestion.md has no data rows"}

    def _norm(s: str) -> str:
        return re.sub(r"[^a-z0-9]", "", s.lower())

    match = next((r for r in rows if _norm(r.provider) == _norm(result.provider_label)), None)
    if match is None:
        return {
            "verdict": "provider-mismatch",
            "reason": f"no baseline row for provider {result.provider_label!r}",
            "available": [r.provider for r in rows],
        }

    docs_drop = _pct_drop(match.docs_per_sec, result.docs_per_sec)
    chunks_drop = _pct_drop(match.chunks_per_sec, result.chunks_per_sec)
    regressed = docs_drop > regression_pct or chunks_drop > regression_pct
    return {
        "verdict": "regressed" if regressed else "pass",
        "baseline_docs_per_sec": match.docs_per_sec,
        "baseline_chunks_per_sec": match.chunks_per_sec,
        "measured_docs_per_sec": round(result.docs_per_sec, 4),
        "measured_chunks_per_sec": round(result.chunks_per_sec, 4),
        "docs_per_sec_drop_pct": round(docs_drop, 2),
        "chunks_per_sec_drop_pct": round(chunks_drop, 2),
        "regression_threshold_pct": regression_pct,
    }


def _pct_drop(baseline: float, measured: float) -> float:
    """Percentage drop of ``measured`` below ``baseline`` (0 if equal/faster)."""
    if baseline <= 0:
        return 0.0
    drop = (baseline - measured) / baseline * 100.0
    return max(0.0, drop)


__all__ = [
    "IngestionBenchResult",
    "BaselineRow",
    "parse_baseline_rows",
    "run_fake",
    "run_live",
    "compare_against_baseline",
]
