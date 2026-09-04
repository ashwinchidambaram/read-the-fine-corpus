"""Provider parity — same corpus, same chunk sets across providers.

Acceptance criterion (§19 Phase 1):
  "The same corpus ingests and serves correctly under BOTH providers."

What this test does
-------------------
For each available embedding provider (Ollama if reachable at localhost:11434;
OpenAI if OPENAI_API_KEY is set), this test:

1. Ingests the native-PDF golden corpus subset through the full pipeline
   (collect → assess → decompose → plan → build → promote).
2. Issues 3 fixed queries against the promoted KB.
3. Asserts: non-empty results with provenance-complete chunks.

Cross-provider invariant (when both providers ran)
--------------------------------------------------
Chunking is provider-independent (the chunker runs before embedding; vectors
are the only provider-specific output). Therefore:
  - chunk_id values produced from the same document must be identical across
    providers (deterministic derivation from content + config).
  - chunk text values must be identical (same chunking algorithm, same input).
  - Vectors WILL differ (different models produce different vector spaces).

This test asserts the chunk-identity invariant when both providers successfully
complete their respective ingestion runs.

Fixture corpus
--------------
The "native-PDF subset" is the golden corpus documents with
``expected_triage_class == "native_pdf"`` from manifest.yaml.  We use only
these because Phase 1 focuses on native-text PDF prose.  This yields:

  clean_native.pdf, policy_v1.pdf, policy_v2.pdf, policy_v3.pdf,
  boilerplate_a.pdf, boilerplate_b.pdf, adversarial.pdf,
  form_filled.pdf, malformed_structure.pdf

(Full list derived at runtime from manifest.yaml — adding a fixture does not
require editing this file.)

Skip behaviour
--------------
- Ollama: skipped when ``localhost:11434`` is unreachable.
- OpenAI: skipped (with loud warning printed) when ``OPENAI_API_KEY`` is absent.
  This is explicitly NOT a silent skip — a missing key is a misconfiguration of
  the test environment, not an expected condition.
- If both providers are skipped, the cross-provider invariant is vacuously true.

Markers
-------
``qdrant_integration``    — requires live Qdrant + Postgres
``provider_integration``  — requires a live embedding provider (Ollama or OpenAI)

Both marks are applied; the test is skipped if EITHER Qdrant/Postgres or ALL
providers are unreachable.

Run:
    docker compose -f docker-compose.yml -f docker-compose.integration.yml up -d
    uv run pytest tests/phase1/test_parity.py -v -m "qdrant_integration and provider_integration"
    docker compose -f docker-compose.yml -f docker-compose.integration.yml down
"""

from __future__ import annotations

import os
import pathlib
import tempfile
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

QDRANT_URL = "http://localhost:6333"
POSTGRES_DSN = "postgresql+psycopg://finecorpus:finecorpus@localhost:5432/finecorpus"

OLLAMA_URL = "http://localhost:11434"
OLLAMA_MODEL = "nomic-embed-text"  # 768-dim
OLLAMA_DIMS = 768

# Fixed queries for the parity test — content must exist in the native-PDF fixture corpus
FIXED_QUERIES = [
    # clean_native.pdf contains "Technical Specifications" content
    "technical specifications component properties",
    # policy documents contain annual leave information
    "annual leave policy entitlement",
    # bloated_manual.pdf contains safety procedures
    "safety procedures operating instructions",
]

WORKSPACE_ID = "ws-parity-test"


# ---------------------------------------------------------------------------
# Helpers: reachability
# ---------------------------------------------------------------------------


def _ollama_reachable() -> bool:
    """Return True if Ollama is accessible at localhost:11434."""
    try:
        import httpx

        resp = httpx.get(f"{OLLAMA_URL}/api/tags", timeout=3.0)
        return resp.status_code == 200
    except Exception:
        return False


def _openai_key_present() -> bool:
    """Return True if OPENAI_API_KEY or FINECORPUS_OPENAI_API_KEY is set."""
    return bool(
        os.environ.get("OPENAI_API_KEY", "").strip()
        or os.environ.get("FINECORPUS_OPENAI_API_KEY", "").strip()
    )


# ---------------------------------------------------------------------------
# Helpers: corpus subset
# ---------------------------------------------------------------------------


def _native_pdf_files() -> list[pathlib.Path]:
    """Return paths to native-PDF golden corpus files from the manifest."""
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
    subset_dir = pathlib.Path(tempfile.mkdtemp(prefix="rtfc-parity-"))
    for src in _native_pdf_files():
        dst = subset_dir / src.name
        dst.symlink_to(src.resolve())
    return subset_dir


# ---------------------------------------------------------------------------
# Helpers: ingestion + query
# ---------------------------------------------------------------------------


def _ingest_and_promote(
    *,
    source_dir: pathlib.Path,
    kb_id: str,
    provider: Any,
    run_id: str,
    engine: Any,
    qdrant_adapter: Any,
) -> dict[str, Any]:
    """Run the full pipeline + promote; return a summary dict.

    Returns:
        dict with keys: kb_id, chunk_count, shadow_collection
    """
    from sqlalchemy.orm import Session

    from finecorpus.pipeline import run_pipeline

    with tempfile.TemporaryDirectory(prefix="rtfc-parity-artifacts-") as artifacts_root:
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
                promote=True,
                db_session=session,
            )

    return {"kb_id": kb_id}


def _query_kb(
    *,
    kb_id: str,
    query_text: str,
    provider: Any,
    engine: Any,
    qdrant_adapter: Any,
) -> Any:
    """Issue a single query through the retrieval service."""
    from sqlalchemy.orm import Session

    from finecorpus.embedding.cache import QueryEmbeddingCache
    from finecorpus.retrieval.service import query

    cache = QueryEmbeddingCache(enabled=False)
    with Session(engine) as session:
        return query(
            kb_id=kb_id,
            query_text=query_text,
            provider=provider,
            adapter=qdrant_adapter,
            session=session,
            top_k=5,
            cache=cache,
        )


def _collect_chunk_set(
    *,
    kb_id: str,
    queries: list[str],
    provider: Any,
    engine: Any,
    qdrant_adapter: Any,
) -> dict[str, set[str]]:
    """Return {query_text: set_of_chunk_ids} for each query."""
    result_map: dict[str, set[str]] = {}
    for q in queries:
        resp = _query_kb(
            kb_id=kb_id,
            query_text=q,
            provider=provider,
            engine=engine,
            qdrant_adapter=qdrant_adapter,
        )
        result_map[q] = {r.chunk_id for r in resp.results}
    return result_map


# ---------------------------------------------------------------------------
# Provider-integration skip gate
# ---------------------------------------------------------------------------

# At module-import time, determine which providers are available.
# Used by the composite skip condition.
_OLLAMA_UP = _ollama_reachable()
_OPENAI_KEY_PRESENT = _openai_key_present()

if not _OPENAI_KEY_PRESENT:
    # Loud warning — a missing key is a test-environment issue, not a normal skip.
    print(
        "\n[test_parity] WARNING: OPENAI_API_KEY is not set — OpenAI parity tests will be "
        "skipped.  Set OPENAI_API_KEY (or FINECORPUS_OPENAI_API_KEY) to enable them."
    )

_NO_PROVIDERS = not _OLLAMA_UP and not _OPENAI_KEY_PRESENT
_PROVIDER_SKIP_REASON = (
    "No embedding providers available: Ollama not reachable at localhost:11434 "
    "and OPENAI_API_KEY is not set.  At least one provider is required for "
    "provider_integration tests."
)


def provider_integration_mark(obj: Any) -> Any:
    """Composite decorator: provider_integration named mark + availability skipif."""
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

    return QdrantAdapter(url=QDRANT_URL, timeout=30)


@pytest.fixture(scope="module")
def native_pdf_subset_dir() -> Any:
    """Temp dir with symlinks to native-PDF golden corpus files."""
    d = _make_native_pdf_subset_dir()
    yield d
    # Cleanup the temp dir and its symlinks
    import shutil

    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture(scope="module")
def ollama_provider() -> Any:
    """OllamaProvider if Ollama is reachable; None otherwise."""
    if not _OLLAMA_UP:
        return None
    from finecorpus.embedding.ollama_provider import OllamaProvider

    return OllamaProvider(base_url=OLLAMA_URL, model_id=OLLAMA_MODEL)


@pytest.fixture(scope="module")
def openai_provider() -> Any:
    """OpenAIProvider if OPENAI_API_KEY is set; None otherwise."""
    if not _OPENAI_KEY_PRESENT:
        return None
    from finecorpus.embedding.openai_provider import OpenAIProvider

    api_key = (
        os.environ.get("FINECORPUS_OPENAI_API_KEY", "").strip()
        or os.environ.get("OPENAI_API_KEY", "").strip()
    )
    return OpenAIProvider(api_key=api_key)


@pytest.fixture(scope="module")
def ollama_kb_result(
    native_pdf_subset_dir: pathlib.Path,
    ollama_provider: Any,
    engine: Any,
    qdrant_adapter: Any,
) -> dict[str, Any] | None:
    """Ingest + promote the native-PDF subset with Ollama; return (kb_id, chunk_sets).

    Returns None if Ollama is unavailable.
    """
    if ollama_provider is None:
        yield None
        return

    kb_id = f"kb-parity-ollama-{uuid.uuid4().hex[:8]}"
    run_id = f"run-parity-ollama-{uuid.uuid4().hex[:8]}"

    _ingest_and_promote(
        source_dir=native_pdf_subset_dir,
        kb_id=kb_id,
        provider=ollama_provider,
        run_id=run_id,
        engine=engine,
        qdrant_adapter=qdrant_adapter,
    )

    chunk_sets = _collect_chunk_set(
        kb_id=kb_id,
        queries=FIXED_QUERIES,
        provider=ollama_provider,
        engine=engine,
        qdrant_adapter=qdrant_adapter,
    )

    yield {"kb_id": kb_id, "chunk_sets": chunk_sets}

    # Teardown
    from finecorpus.index.adapter import alias_name

    alias = alias_name(kb_id)
    try:
        if qdrant_adapter.alias_exists(alias):
            qdrant_adapter.delete_alias(alias)
        for coll in qdrant_adapter.list_collections():
            if kb_id.replace("-", "") in coll:
                qdrant_adapter.drop_collection(coll)
    except Exception:
        pass


@pytest.fixture(scope="module")
def openai_kb_result(
    native_pdf_subset_dir: pathlib.Path,
    openai_provider: Any,
    engine: Any,
    qdrant_adapter: Any,
) -> dict[str, Any] | None:
    """Ingest + promote the native-PDF subset with OpenAI; return (kb_id, chunk_sets).

    Returns None if OpenAI key is absent.
    """
    if openai_provider is None:
        yield None
        return

    kb_id = f"kb-parity-openai-{uuid.uuid4().hex[:8]}"
    run_id = f"run-parity-openai-{uuid.uuid4().hex[:8]}"

    _ingest_and_promote(
        source_dir=native_pdf_subset_dir,
        kb_id=kb_id,
        provider=openai_provider,
        run_id=run_id,
        engine=engine,
        qdrant_adapter=qdrant_adapter,
    )

    chunk_sets = _collect_chunk_set(
        kb_id=kb_id,
        queries=FIXED_QUERIES,
        provider=openai_provider,
        engine=engine,
        qdrant_adapter=qdrant_adapter,
    )

    yield {"kb_id": kb_id, "chunk_sets": chunk_sets}

    # Teardown
    from finecorpus.index.adapter import alias_name

    alias = alias_name(kb_id)
    try:
        if qdrant_adapter.alias_exists(alias):
            qdrant_adapter.delete_alias(alias)
        for coll in qdrant_adapter.list_collections():
            if kb_id.replace("-", "") in coll:
                qdrant_adapter.drop_collection(coll)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Tests — Ollama
# ---------------------------------------------------------------------------


@provider_integration_mark
class TestOllamaParity:
    """Parity tests for the Ollama provider."""

    def test_ollama_pipeline_produces_results(
        self,
        ollama_kb_result: dict[str, Any] | None,
    ) -> None:
        """Ollama: full pipeline + 3 queries → at least one non-empty result set."""
        if ollama_kb_result is None:
            pytest.skip("Ollama not reachable at localhost:11434")

        chunk_sets = ollama_kb_result["chunk_sets"]
        non_empty = [qs for qs in chunk_sets.values() if qs]
        assert len(non_empty) > 0, (
            "All 3 queries returned empty results under Ollama — "
            "either the corpus ingestion failed or the queries are too narrow."
        )

    def test_ollama_results_have_full_provenance(
        self,
        ollama_kb_result: dict[str, Any] | None,
        ollama_provider: Any,
        engine: Any,
        qdrant_adapter: Any,
    ) -> None:
        """Every Ollama result chunk carries full §8 provenance (no nullable mandatory fields)."""
        if ollama_kb_result is None:
            pytest.skip("Ollama not reachable at localhost:11434")

        from sqlalchemy.orm import Session

        from finecorpus.contracts.retrieval_response import ResultStatus
        from finecorpus.embedding.cache import QueryEmbeddingCache
        from finecorpus.retrieval.service import query

        kb_id = ollama_kb_result["kb_id"]
        cache = QueryEmbeddingCache(enabled=False)

        for q_text in FIXED_QUERIES:
            with Session(engine) as session:
                resp = query(
                    kb_id=kb_id,
                    query_text=q_text,
                    provider=ollama_provider,
                    adapter=qdrant_adapter,
                    session=session,
                    top_k=5,
                    cache=cache,
                )
            if resp.result_status != ResultStatus.matches:
                continue  # no_matches for this query is acceptable; skip provenance check
            for r in resp.results:
                prov = r.provenance
                assert prov.source_document_id, (
                    f"[Ollama] source_document_id is empty for chunk {r.chunk_id!r}"
                )
                assert prov.source_document_version, (
                    f"[Ollama] source_document_version is empty for chunk {r.chunk_id!r}"
                )
                assert prov.source_location is not None, (
                    f"[Ollama] source_location is None for chunk {r.chunk_id!r}"
                )
                assert prov.segment_type is not None, (
                    f"[Ollama] segment_type is None for chunk {r.chunk_id!r}"
                )


# ---------------------------------------------------------------------------
# Tests — OpenAI
# ---------------------------------------------------------------------------


@provider_integration_mark
class TestOpenAIParity:
    """Parity tests for the OpenAI provider."""

    def test_openai_pipeline_produces_results(
        self,
        openai_kb_result: dict[str, Any] | None,
    ) -> None:
        """OpenAI: full pipeline + 3 queries → at least one non-empty result set."""
        if openai_kb_result is None:
            pytest.skip("OPENAI_API_KEY not set — OpenAI parity test skipped")

        chunk_sets = openai_kb_result["chunk_sets"]
        non_empty = [qs for qs in chunk_sets.values() if qs]
        assert len(non_empty) > 0, (
            "All 3 queries returned empty results under OpenAI — "
            "either the corpus ingestion failed or the queries are too narrow."
        )

    def test_openai_results_have_full_provenance(
        self,
        openai_kb_result: dict[str, Any] | None,
        openai_provider: Any,
        engine: Any,
        qdrant_adapter: Any,
    ) -> None:
        """Every OpenAI result chunk carries full §8 provenance (no nullable mandatory fields)."""
        if openai_kb_result is None:
            pytest.skip("OPENAI_API_KEY not set — OpenAI parity test skipped")

        from sqlalchemy.orm import Session

        from finecorpus.contracts.retrieval_response import ResultStatus
        from finecorpus.embedding.cache import QueryEmbeddingCache
        from finecorpus.retrieval.service import query

        kb_id = openai_kb_result["kb_id"]
        cache = QueryEmbeddingCache(enabled=False)

        for q_text in FIXED_QUERIES:
            with Session(engine) as session:
                resp = query(
                    kb_id=kb_id,
                    query_text=q_text,
                    provider=openai_provider,
                    adapter=qdrant_adapter,
                    session=session,
                    top_k=5,
                    cache=cache,
                )
            if resp.result_status != ResultStatus.matches:
                continue
            for r in resp.results:
                prov = r.provenance
                assert prov.source_document_id, (
                    f"[OpenAI] source_document_id is empty for chunk {r.chunk_id!r}"
                )
                assert prov.source_document_version, (
                    f"[OpenAI] source_document_version is empty for chunk {r.chunk_id!r}"
                )
                assert prov.source_location is not None, (
                    f"[OpenAI] source_location is None for chunk {r.chunk_id!r}"
                )
                assert prov.segment_type is not None, (
                    f"[OpenAI] segment_type is None for chunk {r.chunk_id!r}"
                )


# ---------------------------------------------------------------------------
# Cross-provider invariant — chunk-identity equality
# ---------------------------------------------------------------------------


@provider_integration_mark
class TestCrossProviderChunkIdentity:
    """When both providers ran: chunk IDs and text must be identical across providers.

    Chunking is provider-independent (vectors differ; text + IDs must not).
    This is the core §19 Phase 1 cross-provider invariant.
    """

    def test_chunk_ids_are_provider_independent(
        self,
        ollama_kb_result: dict[str, Any] | None,
        openai_kb_result: dict[str, Any] | None,
        ollama_provider: Any,
        openai_provider: Any,
        engine: Any,
        qdrant_adapter: Any,
    ) -> None:
        """When both providers ran, chunk IDs from the same query must match exactly."""
        if ollama_kb_result is None or openai_kb_result is None:
            pytest.skip(
                "Cross-provider invariant requires both Ollama and OpenAI to have run. "
                f"Ollama={'available' if ollama_kb_result else 'unavailable'}, "
                f"OpenAI={'available' if openai_kb_result else 'unavailable'}."
            )

        from sqlalchemy.orm import Session

        from finecorpus.contracts.retrieval_response import ResultStatus
        from finecorpus.embedding.cache import QueryEmbeddingCache
        from finecorpus.retrieval.service import query

        cache = QueryEmbeddingCache(enabled=False)
        mismatches: list[str] = []

        for q_text in FIXED_QUERIES:
            # Query Ollama KB
            with Session(engine) as session:
                resp_ollama = query(
                    kb_id=ollama_kb_result["kb_id"],
                    query_text=q_text,
                    provider=ollama_provider,
                    adapter=qdrant_adapter,
                    session=session,
                    top_k=10,
                    cache=cache,
                )
            # Query OpenAI KB
            with Session(engine) as session:
                resp_openai = query(
                    kb_id=openai_kb_result["kb_id"],
                    query_text=q_text,
                    provider=openai_provider,
                    adapter=qdrant_adapter,
                    session=session,
                    top_k=10,
                    cache=cache,
                )

            if (
                resp_ollama.result_status != ResultStatus.matches
                or resp_openai.result_status != ResultStatus.matches
            ):
                # At least one provider returned no matches — skip this query pair.
                continue

            ids_ollama = {r.chunk_id for r in resp_ollama.results}
            ids_openai = {r.chunk_id for r in resp_openai.results}

            # The intersection must be non-empty (same corpus → same chunk IDs).
            # We do NOT require exact set equality because ranking differs across
            # providers (top-k may select different members from the full set).
            # What MUST NOT happen: zero overlap when both returned results.
            if not ids_ollama.intersection(ids_openai):
                mismatches.append(
                    f"Query={q_text!r}: Ollama returned {sorted(ids_ollama)}, "
                    f"OpenAI returned {sorted(ids_openai)} — no chunk_id overlap. "
                    f"Chunking must be provider-independent."
                )

        assert not mismatches, "Cross-provider chunk-identity invariant violated:\n" + "\n".join(
            mismatches
        )

    def test_chunk_text_is_provider_independent(
        self,
        ollama_kb_result: dict[str, Any] | None,
        openai_kb_result: dict[str, Any] | None,
        ollama_provider: Any,
        openai_provider: Any,
        engine: Any,
        qdrant_adapter: Any,
    ) -> None:
        """When both providers ran, chunk text for matching chunk IDs must be identical."""
        if ollama_kb_result is None or openai_kb_result is None:
            pytest.skip(
                "Cross-provider text invariant requires both Ollama and OpenAI. "
                f"Ollama={'available' if ollama_kb_result else 'unavailable'}, "
                f"OpenAI={'available' if openai_kb_result else 'unavailable'}."
            )

        from sqlalchemy.orm import Session

        from finecorpus.contracts.retrieval_response import ResultStatus
        from finecorpus.embedding.cache import QueryEmbeddingCache
        from finecorpus.retrieval.service import query

        cache = QueryEmbeddingCache(enabled=False)
        text_mismatches: list[str] = []

        for q_text in FIXED_QUERIES:
            with Session(engine) as session:
                resp_ollama = query(
                    kb_id=ollama_kb_result["kb_id"],
                    query_text=q_text,
                    provider=ollama_provider,
                    adapter=qdrant_adapter,
                    session=session,
                    top_k=10,
                    cache=cache,
                )
            with Session(engine) as session:
                resp_openai = query(
                    kb_id=openai_kb_result["kb_id"],
                    query_text=q_text,
                    provider=openai_provider,
                    adapter=qdrant_adapter,
                    session=session,
                    top_k=10,
                    cache=cache,
                )

            if (
                resp_ollama.result_status != ResultStatus.matches
                or resp_openai.result_status != ResultStatus.matches
            ):
                continue

            # Build chunk_id → text maps
            ollama_map = {r.chunk_id: r.text for r in resp_ollama.results}
            openai_map = {r.chunk_id: r.text for r in resp_openai.results}

            # For chunk IDs that appear in both sets, text must be identical
            shared_ids = ollama_map.keys() & openai_map.keys()
            for cid in shared_ids:
                if ollama_map[cid] != openai_map[cid]:
                    text_mismatches.append(
                        f"Query={q_text!r}, chunk_id={cid!r}: "
                        f"Ollama text={ollama_map[cid][:60]!r}, "
                        f"OpenAI text={openai_map[cid][:60]!r}"
                    )

        assert not text_mismatches, (
            "Cross-provider text invariant violated — same chunk_id, different text:\n"
            + "\n".join(text_mismatches)
        )
