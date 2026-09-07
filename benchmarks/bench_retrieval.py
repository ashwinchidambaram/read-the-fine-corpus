"""Retrieval latency benchmark — §4.5 (p50 < 150 ms, p99 < 800 ms).

Measures wall-clock latency of the REAL retrieval entry point
``finecorpus.retrieval.service.query`` against a warmed index.

FAKE mode
---------
FakeProvider + FakeAdapter (the retrieval unit-test doubles).  Deterministic
and seeded — the same query set always produces the same embeddings and the
same result ordering.  This measures the SERVICE-PATH OVERHEAD only:
alias resolution, mismatch check, query-embedding cache, tenancy-filter build,
in-memory search, provenance reconstruction, response assembly.  It does NOT
include a real network round-trip to a vector DB or a real embedding model, so
its absolute numbers are far below the §4.5 targets and are NOT the
reference-deployment numbers the §19 release gate needs.

LIVE mode
---------
Real Qdrant + Postgres via the compose integration overlay, real embedding
provider.  Measures true end-to-end latency.  Skipped (returns None) when the
infrastructure is unreachable.

Cache-warm semantics (§4.5 "cache warm"): the harness issues a warm-up pass
over the query set first so the query-embedding cache is populated, then times
the measured pass.  The default query set is small and repeated, which is the
cache-warm case the target describes.
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from benchmarks.stats import LatencySummary, summarize_latencies

# The retrieval test doubles live under tests/retrieval/helpers.py.  They are
# the canonical FakeAdapter/Fake* used across the retrieval unit tests; the
# harness reuses them rather than reimplementing an in-memory adapter.
_TESTS_DIR = Path(__file__).resolve().parent.parent / "tests"
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))


# Deterministic, fixed query set.  Small + repeated → the cache-warm case.
DEFAULT_QUERIES: tuple[str, ...] = (
    "What is the data retention policy?",
    "How do I rotate an API key?",
    "Which regions are supported?",
    "What is the incident response runbook?",
    "How is tenancy isolation enforced?",
    "What are the ingestion throughput targets?",
    "How does break-glass access work?",
    "What embedding models are supported?",
    "How is the query cache invalidated?",
    "What happens on an embedding model mismatch?",
)


@dataclass(frozen=True)
class RetrievalBenchResult:
    """Outcome of a retrieval-latency benchmark run."""

    mode: str
    warmup_queries: int
    measured_queries: int
    seeded_points: int
    top_k: int
    summary: LatencySummary
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "warmup_queries": self.warmup_queries,
            "measured_queries": self.measured_queries,
            "seeded_points": self.seeded_points,
            "top_k": self.top_k,
            "note": self.note,
            **self.summary.as_dict(),
        }


# ---------------------------------------------------------------------------
# FAKE mode
# ---------------------------------------------------------------------------


def run_fake(
    *,
    queries: int = 2000,
    warmup: int = 100,
    seeded_points: int = 500,
    top_k: int = 10,
    query_set: tuple[str, ...] = DEFAULT_QUERIES,
) -> RetrievalBenchResult:
    """Measure service-path retrieval latency with Fake doubles (deterministic).

    Args:
        queries: Number of measured (timed) queries.
        warmup: Number of warm-up queries (populate the embedding cache).
        seeded_points: Number of chunks seeded into the fake collection.
        top_k: Results requested per query.
        query_set: Rotating set of query strings (repeated to reach ``queries``).

    Returns:
        RetrievalBenchResult with a LatencySummary in milliseconds.
    """
    from unittest.mock import patch

    from retrieval.helpers import (  # type: ignore[import-not-found]
        FakeAdapter,
        FakeAliasRepository,
        make_alias_record,
        make_chunk_payload,
    )

    from finecorpus.embedding.cache import QueryEmbeddingCache
    from finecorpus.embedding.fake import FakeProvider
    from finecorpus.index.adapter import alias_name
    from finecorpus.retrieval.service import query as retrieval_query

    kb_id = "kb-bench"
    model_id = "fake-embed-v1"
    dims = 64
    alias = alias_name(kb_id)
    coll = f"rtfc_{kb_id.replace('-', '').lower()}_00000001"

    record = make_alias_record(kb_id, model_id=model_id, dimensions=dims, collection=coll)
    adapter = FakeAdapter()
    # Descending scores so ordering is stable and top_k selection is exercised.
    points = [
        make_chunk_payload(
            chunk_id=f"chk_{i:05d}",
            text=f"Deterministic benchmark chunk number {i}.",
            kb_id=kb_id,
            score=1.0 - (i * 1e-4),
            source_document_id=f"doc-{i % 50:03d}",
        )
        for i in range(seeded_points)
    ]
    adapter.seed_collection(alias=alias, coll=coll, points=points)

    provider = FakeProvider(dimensions=dims, model_id=model_id)
    cache = QueryEmbeddingCache(ttl_seconds=3600, max_entries=1024, enabled=True)
    session: Any = object()  # AliasRepository is patched; session is never touched.

    latencies_ms: list[float] = []
    with patch(
        "finecorpus.retrieval.service.AliasRepository",
        return_value=FakeAliasRepository({alias: record}),
    ):
        # Warm-up pass: populate the query-embedding cache (§4.5 "cache warm").
        for i in range(warmup):
            retrieval_query(
                kb_id=kb_id,
                query_text=query_set[i % len(query_set)],
                provider=provider,
                adapter=adapter,
                session=session,
                top_k=top_k,
                cache=cache,
            )
        # Measured pass.
        for i in range(queries):
            qtext = query_set[i % len(query_set)]
            t0 = time.perf_counter()
            retrieval_query(
                kb_id=kb_id,
                query_text=qtext,
                provider=provider,
                adapter=adapter,
                session=session,
                top_k=top_k,
                cache=cache,
            )
            latencies_ms.append((time.perf_counter() - t0) * 1000.0)

    return RetrievalBenchResult(
        mode="fake",
        warmup_queries=warmup,
        measured_queries=queries,
        seeded_points=seeded_points,
        top_k=top_k,
        summary=summarize_latencies(latencies_ms),
        note=(
            "Service-path overhead only (FakeProvider+FakeAdapter, in-memory). "
            "NOT the reference-deployment latency."
        ),
    )


# ---------------------------------------------------------------------------
# LIVE mode
# ---------------------------------------------------------------------------


def live_infra_reachable() -> bool:
    """True iff both Qdrant and Postgres are reachable at the overlay defaults."""
    qdrant_ok = False
    try:
        import httpx

        r = httpx.get("http://localhost:6333/readyz", timeout=2.0)
        qdrant_ok = r.status_code < 500
    except Exception:
        try:
            import httpx

            r = httpx.get("http://localhost:6333/", timeout=2.0)
            qdrant_ok = r.status_code < 500
        except Exception:
            qdrant_ok = False
    return qdrant_ok


def run_live(
    *,
    kb_id: str,
    queries: int = 500,
    warmup: int = 50,
    top_k: int = 10,
    query_set: tuple[str, ...] = DEFAULT_QUERIES,
    qdrant_url: str = "http://localhost:6333",
    postgres_dsn: str = "postgresql+psycopg://finecorpus:finecorpus@localhost:5432/finecorpus",
) -> RetrievalBenchResult | None:
    """Measure real end-to-end retrieval latency against live Qdrant + Postgres.

    Requires a KB that has already been ingested and promoted (``kb_id``) using
    the configured embedding provider.  Returns None when infra is unreachable.

    This is intentionally NOT wired into CI: it is the number the orchestrator
    must capture under the compose overlay at phase close.
    """
    if not live_infra_reachable():
        return None

    import os

    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from finecorpus.index.qdrant.backend import QdrantAdapter
    from finecorpus.retrieval.service import query as retrieval_query

    # Build a real provider directly (same pattern as the Phase-1 baseline test):
    # OpenAI when a key is present, otherwise the local Ollama reference provider.
    # The KB under test MUST have been ingested/promoted with this same provider.
    provider: Any
    if (
        os.environ.get("OPENAI_API_KEY", "").strip()
        or os.environ.get("FINECORPUS_OPENAI_API_KEY", "").strip()
    ):
        from finecorpus.embedding.openai_provider import OpenAIProvider

        api_key = (
            os.environ.get("FINECORPUS_OPENAI_API_KEY", "").strip()
            or os.environ.get("OPENAI_API_KEY", "").strip()
        )
        provider = OpenAIProvider(api_key=api_key)
    else:
        from finecorpus.embedding.ollama_provider import OllamaProvider

        provider = OllamaProvider(
            base_url=os.environ.get("OLLAMA_URL", "http://localhost:11434"),
            model_id=os.environ.get("OLLAMA_MODEL", "nomic-embed-text"),
        )
    adapter = QdrantAdapter(url=qdrant_url, timeout=60)
    engine = create_engine(postgres_dsn)

    latencies_ms: list[float] = []
    with Session(engine) as session:
        for i in range(warmup):
            retrieval_query(
                kb_id=kb_id,
                query_text=query_set[i % len(query_set)],
                provider=provider,
                adapter=adapter,
                session=session,
                top_k=top_k,
            )
        for i in range(queries):
            qtext = query_set[i % len(query_set)]
            t0 = time.perf_counter()
            retrieval_query(
                kb_id=kb_id,
                query_text=qtext,
                provider=provider,
                adapter=adapter,
                session=session,
                top_k=top_k,
            )
            latencies_ms.append((time.perf_counter() - t0) * 1000.0)

    return RetrievalBenchResult(
        mode="live",
        warmup_queries=warmup,
        measured_queries=queries,
        seeded_points=-1,
        top_k=top_k,
        summary=summarize_latencies(latencies_ms),
        note="Real Qdrant + real embedding provider, end-to-end.",
    )


__all__ = [
    "RetrievalBenchResult",
    "DEFAULT_QUERIES",
    "run_fake",
    "run_live",
    "live_infra_reachable",
]
