"""Cold-start benchmark — §4.5 (create-KB → first query, 1k docs, < 30 min).

This is a PRODUCT target: a non-technical user must get from upload to a working
endpoint inside half an hour.

FAKE mode
---------
Measures the wall-clock of the pipeline STAGES (collect → assess → decompose →
plan → build) over a synthetic ~1k-doc corpus with FakeProvider + FakeAdapter,
then issues one query against the freshly-built in-memory collection.  This is a
PROXY: it excludes container bring-up, real embedding latency, real Qdrant
indexing, and promotion I/O.  The proxy establishes that the pipeline CODE PATH
scales to 1k docs without pathological slowdown; it does NOT prove the 30-minute
wall-clock target, which depends on the reference deployment and a real provider.

LIVE mode
---------
Measures create-KB → ingest → promote → first successful query against live
Qdrant + Postgres + a real provider, over the synthetic 1k-doc corpus.  This is
the number that speaks to the §4.5 product target — but note that even LIVE mode
in a dev environment is not the "reference deployment (single-node Docker
Compose, commodity hardware)" the target is written against, and it excludes
`docker compose up` bring-up time.  The orchestrator must capture the full
cold-start (including container bring-up) under the overlay at phase close.
"""

from __future__ import annotations

import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from benchmarks.corpus import cleanup_dir, make_synthetic_corpus_dir

# §4.5 product target for the cold-start row.
COLD_START_TARGET_SECONDS = 30 * 60


@dataclass(frozen=True)
class ColdStartBenchResult:
    """Outcome of a cold-start benchmark run."""

    mode: str
    docs_requested: int
    docs_processed: int
    chunks_produced: int
    ingest_seconds: float
    first_query_seconds: float
    total_seconds: float
    first_query_status: str
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "docs_requested": self.docs_requested,
            "docs_processed": self.docs_processed,
            "chunks_produced": self.chunks_produced,
            "ingest_seconds": round(self.ingest_seconds, 3),
            "first_query_seconds": round(self.first_query_seconds, 4),
            "total_seconds": round(self.total_seconds, 3),
            "first_query_status": self.first_query_status,
            "target_seconds": COLD_START_TARGET_SECONDS,
            "note": self.note,
        }


def run_fake(*, docs: int = 1000) -> ColdStartBenchResult:
    """Proxy cold-start: pipeline stages + first query over a synthetic corpus.

    Deterministic (FakeProvider is seeded by content).  Measures the code-path
    wall-clock only — see module docstring for the caveat.
    """
    import sys
    from unittest.mock import patch

    tests_dir = Path(__file__).resolve().parent.parent / "tests"
    if str(tests_dir) not in sys.path:
        sys.path.insert(0, str(tests_dir))

    from retrieval.helpers import (  # type: ignore[import-not-found]
        FakeAdapter,
        FakeAliasRepository,
        make_alias_record,
    )

    from finecorpus.embedding.cache import QueryEmbeddingCache
    from finecorpus.embedding.fake import FakeProvider
    from finecorpus.index.adapter import alias_name
    from finecorpus.pipeline import run_pipeline
    from finecorpus.pipeline.artifact_store import ArtifactStore
    from finecorpus.pipeline.build.stage import BuildResult
    from finecorpus.retrieval.service import query as retrieval_query

    kb_id = "kb-coldstart"
    model_id = "fake-embed-v1"
    dims = 64
    alias = alias_name(kb_id)

    provider = FakeProvider(dimensions=dims, model_id=model_id)
    adapter = FakeAdapter()

    source_dir = make_synthetic_corpus_dir(docs)
    artifacts_root = Path(tempfile.mkdtemp(prefix="rtfc-bench-cs-"))
    run_id = f"run-cs-{uuid.uuid4().hex[:8]}"
    try:
        t0 = time.monotonic()
        run_pipeline(
            source_dir=source_dir,
            artifacts_root=artifacts_root,
            run_id=run_id,
            workspace_id="ws-bench",
            kb_id=kb_id,
            embedding_provider=provider,
            index_adapter=adapter,
            build_id=1,
            promote=False,
        )
        ingest_seconds = time.monotonic() - t0

        store = ArtifactStore(artifacts_root=artifacts_root, run_id=run_id)
        build_result = BuildResult.model_validate(
            store.load_with_model_validation("build", BuildResult).model_dump()
        )
        shadow = build_result.shadow_collection or ""

        # Promote-by-hand for the fake path: point the alias at the shadow
        # collection so the query path resolves it (no DB involved).
        adapter.aliases[alias] = shadow
        coll = shadow
        record = make_alias_record(kb_id, model_id=model_id, dimensions=dims, collection=coll)

        cache = QueryEmbeddingCache(ttl_seconds=3600, max_entries=64, enabled=True)
        session: Any = object()
        with patch(
            "finecorpus.retrieval.service.AliasRepository",
            return_value=FakeAliasRepository({alias: record}),
        ):
            tq = time.monotonic()
            resp = retrieval_query(
                kb_id=kb_id,
                query_text="What does the policy say?",
                provider=provider,
                adapter=adapter,
                session=session,
                top_k=10,
                cache=cache,
            )
            first_query_seconds = time.monotonic() - tq

        total = ingest_seconds + first_query_seconds
        return ColdStartBenchResult(
            mode="fake",
            docs_requested=docs,
            docs_processed=len(build_result.chunks_by_document),
            chunks_produced=build_result.chunk_count,
            ingest_seconds=ingest_seconds,
            first_query_seconds=first_query_seconds,
            total_seconds=total,
            first_query_status=str(resp.result_status.value),
            note=(
                "PROXY: pipeline-stage wall-clock + in-memory first query. Excludes "
                "container bring-up, real embedding latency, real Qdrant indexing, "
                "and promotion I/O. Does NOT prove the 30-minute reference target."
            ),
        )
    finally:
        cleanup_dir(source_dir)
        cleanup_dir(artifacts_root)


def run_live(
    *,
    docs: int = 1000,
    qdrant_url: str = "http://localhost:6333",
    postgres_dsn: str = "postgresql+psycopg://finecorpus:finecorpus@localhost:5432/finecorpus",
) -> ColdStartBenchResult | None:
    """Cold-start against live infra: ingest+promote a fresh KB, then first query.

    Returns None when Qdrant is unreachable.  Excludes `docker compose up`
    bring-up time — the orchestrator must add that separately at phase close.
    """
    from benchmarks.bench_retrieval import live_infra_reachable

    if not live_infra_reachable():
        return None

    import os

    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from finecorpus.control.metadata import create_tables
    from finecorpus.index.qdrant.backend import QdrantAdapter
    from finecorpus.pipeline import run_pipeline
    from finecorpus.pipeline.artifact_store import ArtifactStore
    from finecorpus.pipeline.build.stage import BuildResult
    from finecorpus.retrieval.service import query as retrieval_query

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
    else:
        from finecorpus.embedding.ollama_provider import OllamaProvider

        provider = OllamaProvider(
            base_url=os.environ.get("OLLAMA_URL", "http://localhost:11434"),
            model_id=os.environ.get("OLLAMA_MODEL", "nomic-embed-text"),
        )

    engine = create_engine(postgres_dsn)
    create_tables(engine)
    adapter = QdrantAdapter(url=qdrant_url, timeout=120)

    kb_id = f"kb-cs-{uuid.uuid4().hex[:8]}"
    source_dir = make_synthetic_corpus_dir(docs)
    artifacts_root = Path(tempfile.mkdtemp(prefix="rtfc-bench-cs-"))
    run_id = f"run-cs-{uuid.uuid4().hex[:8]}"
    try:
        with Session(engine) as session:
            t0 = time.monotonic()
            run_pipeline(
                source_dir=source_dir,
                artifacts_root=artifacts_root,
                run_id=run_id,
                workspace_id="ws-bench",
                kb_id=kb_id,
                embedding_provider=provider,
                index_adapter=adapter,
                build_id=1,
                promote=True,  # promote so the KB is queryable
                db_session=session,
            )
            ingest_seconds = time.monotonic() - t0

            store = ArtifactStore(artifacts_root=artifacts_root, run_id=run_id)
            build_result = BuildResult.model_validate(
                store.load_with_model_validation("build", BuildResult).model_dump()
            )

            tq = time.monotonic()
            resp = retrieval_query(
                kb_id=kb_id,
                query_text="What does the policy say?",
                provider=provider,
                adapter=adapter,
                session=session,
                top_k=10,
            )
            first_query_seconds = time.monotonic() - tq

        total = ingest_seconds + first_query_seconds
        return ColdStartBenchResult(
            mode="live",
            docs_requested=docs,
            docs_processed=len(build_result.chunks_by_document),
            chunks_produced=build_result.chunk_count,
            ingest_seconds=ingest_seconds,
            first_query_seconds=first_query_seconds,
            total_seconds=total,
            first_query_status=str(resp.result_status.value),
            note=(
                "Live ingest+promote+first-query. Excludes `docker compose up` "
                "bring-up; not the reference-hardware deployment."
            ),
        )
    finally:
        cleanup_dir(source_dir)
        cleanup_dir(artifacts_root)


__all__ = [
    "ColdStartBenchResult",
    "COLD_START_TARGET_SECONDS",
    "run_fake",
    "run_live",
]
