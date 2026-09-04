"""Unit tests for finecorpus.retrieval.service.

Tests are organized around the §15 four-way taxonomy and the fail-closed
conditions. All tests use FakeProvider + FakeAdapter; no live services required.

Coverage:
- happy path: matches with full provenance
- no_matches: search returns empty
- filtered_to_zero: score_threshold eliminates all candidates
- EMBEDDING_MODEL_MISMATCH: provider never called (asserted via call counter)
- PROVIDER_UNAVAILABLE: embed_batch raises ProviderUnavailableError
- VECTOR_DB_UNAVAILABLE: adapter.search raises IndexError
- KB_NOT_READY: no alias record / no promoted collection
- cache: provider called once on two identical queries (cache hit on 2nd)
- top_k bounds: clamped to _TOP_K_MAX
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from unittest.mock import patch

import pytest

from finecorpus.contracts.retrieval_response import ErrorCode, ResultStatus
from finecorpus.embedding.cache import QueryEmbeddingCache
from finecorpus.embedding.fake import FakeProvider
from finecorpus.index.adapter import alias_name
from finecorpus.retrieval.service import _TOP_K_MAX, query

from .helpers import (
    FakeAdapter,
    FakeAliasRecord,
    FakeAliasRepository,
    make_alias_record,
    make_chunk_payload,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

KB_ID = "kb-test-001"
ALIAS = alias_name(KB_ID)
COLL = f"rtfc_{KB_ID.replace('-', '').lower()}_00000001"
MODEL_ID = "fake-embed-v1"
DIMENSIONS = 64


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def provider() -> FakeProvider:
    return FakeProvider(dimensions=DIMENSIONS, model_id=MODEL_ID)


@pytest.fixture()
def alias_record() -> FakeAliasRecord:
    return make_alias_record(KB_ID, model_id=MODEL_ID, dimensions=DIMENSIONS)


@pytest.fixture()
def fresh_cache() -> QueryEmbeddingCache:
    return QueryEmbeddingCache(ttl_seconds=3600, max_entries=128, enabled=True)


@pytest.fixture()
def adapter_with_chunks(alias_record: FakeAliasRecord) -> FakeAdapter:
    """FakeAdapter seeded with two chunks."""
    adapter = FakeAdapter()
    adapter.seed_collection(
        alias=ALIAS,
        coll=COLL,
        points=[
            make_chunk_payload(
                chunk_id="chk_001",
                text="First chunk text.",
                kb_id=KB_ID,
                score=0.92,
            ),
            make_chunk_payload(
                chunk_id="chk_002",
                text="Second chunk text.",
                kb_id=KB_ID,
                score=0.75,
            ),
        ],
    )
    return adapter


@contextmanager
def _fake_repo(record: FakeAliasRecord | None) -> Generator:
    """Context manager that patches AliasRepository.get."""
    records = {record.alias: record} if record else {}
    with patch(
        "finecorpus.retrieval.service.AliasRepository",
        return_value=FakeAliasRepository(records),
    ):
        yield


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_query(
    *,
    provider: FakeProvider,
    adapter: FakeAdapter,
    alias_record: FakeAliasRecord | None,
    cache: QueryEmbeddingCache,
    query_text: str = "What is the policy?",
    top_k: int = 10,
    score_threshold: float | None = None,
) -> object:

    records = {alias_record.alias: alias_record} if alias_record else {}
    fake_repo = FakeAliasRepository(records)
    # Use a fake session (not needed because we patch AliasRepository)
    fake_session = object()

    with patch(
        "finecorpus.retrieval.service.AliasRepository",
        return_value=fake_repo,
    ):
        return query(
            kb_id=KB_ID,
            query_text=query_text,
            provider=provider,
            adapter=adapter,
            session=fake_session,  # type: ignore[arg-type]
            top_k=top_k,
            score_threshold=score_threshold,
            cache=cache,
        )


# ---------------------------------------------------------------------------
# Happy path: matches
# ---------------------------------------------------------------------------


class TestHappyPath:
    def test_result_status_matches(
        self,
        provider: FakeProvider,
        adapter_with_chunks: FakeAdapter,
        alias_record: FakeAliasRecord,
        fresh_cache: QueryEmbeddingCache,
    ) -> None:
        result = _run_query(
            provider=provider,
            adapter=adapter_with_chunks,
            alias_record=alias_record,
            cache=fresh_cache,
        )
        assert result.result_status == ResultStatus.matches  # type: ignore[union-attr]

    def test_results_nonempty(
        self,
        provider: FakeProvider,
        adapter_with_chunks: FakeAdapter,
        alias_record: FakeAliasRecord,
        fresh_cache: QueryEmbeddingCache,
    ) -> None:
        result = _run_query(
            provider=provider,
            adapter=adapter_with_chunks,
            alias_record=alias_record,
            cache=fresh_cache,
        )
        assert len(result.results) > 0  # type: ignore[union-attr]

    def test_provenance_complete(
        self,
        provider: FakeProvider,
        adapter_with_chunks: FakeAdapter,
        alias_record: FakeAliasRecord,
        fresh_cache: QueryEmbeddingCache,
    ) -> None:
        """Every result must carry full §8 provenance."""
        result = _run_query(
            provider=provider,
            adapter=adapter_with_chunks,
            alias_record=alias_record,
            cache=fresh_cache,
        )
        for r in result.results:  # type: ignore[union-attr]
            prov = r.provenance
            assert prov.source_document_id
            assert prov.source_document_version
            assert prov.source_location is not None
            assert isinstance(prov.transformations, list)
            assert isinstance(prov.structural_path, list)
            assert 0.0 <= prov.confidence <= 1.0
            assert prov.segment_type is not None
            assert prov.salience_tier is not None
            assert prov.language

    def test_trust_level_always_untrusted_ingested(
        self,
        provider: FakeProvider,
        adapter_with_chunks: FakeAdapter,
        alias_record: FakeAliasRecord,
        fresh_cache: QueryEmbeddingCache,
    ) -> None:
        """§14.1: trust_level must always be untrusted_ingested."""
        from finecorpus.contracts.shared.blocks import TrustLevel

        result = _run_query(
            provider=provider,
            adapter=adapter_with_chunks,
            alias_record=alias_record,
            cache=fresh_cache,
        )
        for r in result.results:  # type: ignore[union-attr]
            assert r.trust_level == TrustLevel.untrusted_ingested

    def test_chunk_id_and_text_present(
        self,
        provider: FakeProvider,
        adapter_with_chunks: FakeAdapter,
        alias_record: FakeAliasRecord,
        fresh_cache: QueryEmbeddingCache,
    ) -> None:
        result = _run_query(
            provider=provider,
            adapter=adapter_with_chunks,
            alias_record=alias_record,
            cache=fresh_cache,
        )
        for r in result.results:  # type: ignore[union-attr]
            assert r.chunk_id
            assert r.text

    def test_request_echo_present(
        self,
        provider: FakeProvider,
        adapter_with_chunks: FakeAdapter,
        alias_record: FakeAliasRecord,
        fresh_cache: QueryEmbeddingCache,
    ) -> None:
        result = _run_query(
            provider=provider,
            adapter=adapter_with_chunks,
            alias_record=alias_record,
            cache=fresh_cache,
            query_text="search text",
        )
        assert result.request_echo.query == "search text"  # type: ignore[union-attr]
        # Tenancy filter is always present
        origins = [f.origin for f in result.request_echo.filters_applied]  # type: ignore[union-attr]
        from finecorpus.contracts.retrieval_response import FilterOrigin

        assert FilterOrigin.tenancy in origins

    def test_schema_version_present(
        self,
        provider: FakeProvider,
        adapter_with_chunks: FakeAdapter,
        alias_record: FakeAliasRecord,
        fresh_cache: QueryEmbeddingCache,
    ) -> None:
        result = _run_query(
            provider=provider,
            adapter=adapter_with_chunks,
            alias_record=alias_record,
            cache=fresh_cache,
        )
        assert result.schema_version  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# no_matches
# ---------------------------------------------------------------------------


class TestNoMatches:
    def test_no_matches_when_search_empty(
        self,
        provider: FakeProvider,
        alias_record: FakeAliasRecord,
        fresh_cache: QueryEmbeddingCache,
    ) -> None:
        """Empty search results → result_status=no_matches (not filtered_to_zero)."""
        # Adapter returns nothing (alias exists but empty collection)
        adapter = FakeAdapter()
        adapter.seed_collection(alias=ALIAS, coll=COLL, points=[])

        result = _run_query(
            provider=provider,
            adapter=adapter,
            alias_record=alias_record,
            cache=fresh_cache,
        )
        assert result.result_status == ResultStatus.no_matches  # type: ignore[union-attr]
        assert result.results == []  # type: ignore[union-attr]
        assert result.error is None  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# filtered_to_zero
# ---------------------------------------------------------------------------


class TestFilteredToZero:
    def test_score_threshold_eliminates_all(
        self,
        provider: FakeProvider,
        adapter_with_chunks: FakeAdapter,
        alias_record: FakeAliasRecord,
        fresh_cache: QueryEmbeddingCache,
    ) -> None:
        """score_threshold that eliminates all candidates → filtered_to_zero."""
        result = _run_query(
            provider=provider,
            adapter=adapter_with_chunks,
            alias_record=alias_record,
            cache=fresh_cache,
            score_threshold=0.999,  # above all seeded scores (0.92, 0.75)
        )
        assert result.result_status == ResultStatus.filtered_to_zero  # type: ignore[union-attr]
        assert result.results == []  # type: ignore[union-attr]
        assert result.error is None  # type: ignore[union-attr]

    def test_score_threshold_applied_filter_recorded(
        self,
        provider: FakeProvider,
        adapter_with_chunks: FakeAdapter,
        alias_record: FakeAliasRecord,
        fresh_cache: QueryEmbeddingCache,
    ) -> None:
        """The score threshold filter should appear in request_echo.filters_applied."""
        from finecorpus.contracts.retrieval_response import FilterOrigin

        result = _run_query(
            provider=provider,
            adapter=adapter_with_chunks,
            alias_record=alias_record,
            cache=fresh_cache,
            score_threshold=0.999,
        )
        origins = [f.origin for f in result.request_echo.filters_applied]  # type: ignore[union-attr]
        assert FilterOrigin.request in origins

    def test_distinction_from_no_matches(
        self,
        provider: FakeProvider,
        alias_record: FakeAliasRecord,
        fresh_cache: QueryEmbeddingCache,
    ) -> None:
        """filtered_to_zero ≠ no_matches: search found candidates, threshold killed them."""
        # Seeded with a chunk that scores 0.5; threshold 0.9 → filtered_to_zero
        adapter = FakeAdapter()
        adapter.seed_collection(
            alias=ALIAS,
            coll=COLL,
            points=[
                make_chunk_payload(chunk_id="chk_001", score=0.5, kb_id=KB_ID),
            ],
        )
        result = _run_query(
            provider=provider,
            adapter=adapter,
            alias_record=alias_record,
            cache=fresh_cache,
            score_threshold=0.9,
        )
        assert result.result_status == ResultStatus.filtered_to_zero  # type: ignore[union-attr]

    def test_matches_when_above_threshold(
        self,
        provider: FakeProvider,
        adapter_with_chunks: FakeAdapter,
        alias_record: FakeAliasRecord,
        fresh_cache: QueryEmbeddingCache,
    ) -> None:
        """When some results exceed threshold → matches (not filtered_to_zero)."""
        result = _run_query(
            provider=provider,
            adapter=adapter_with_chunks,
            alias_record=alias_record,
            cache=fresh_cache,
            score_threshold=0.8,  # 0.92 > 0.8, 0.75 < 0.8 → 1 result
        )
        assert result.result_status == ResultStatus.matches  # type: ignore[union-attr]
        assert len(result.results) == 1  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# EMBEDDING_MODEL_MISMATCH
# ---------------------------------------------------------------------------


class TestModelMismatch:
    def test_mismatch_returns_error(
        self,
        adapter_with_chunks: FakeAdapter,
        fresh_cache: QueryEmbeddingCache,
    ) -> None:
        """Mismatch between provider and alias record → error, EMBEDDING_MODEL_MISMATCH."""
        alias_record = make_alias_record(
            KB_ID,
            model_id="other-model",
            dimensions=128,  # mismatch with provider
        )
        provider = FakeProvider(dimensions=DIMENSIONS, model_id=MODEL_ID)

        result = _run_query(
            provider=provider,
            adapter=adapter_with_chunks,
            alias_record=alias_record,
            cache=fresh_cache,
        )
        assert result.result_status == ResultStatus.error  # type: ignore[union-attr]
        assert result.error.code == ErrorCode.EMBEDDING_MODEL_MISMATCH  # type: ignore[union-attr]

    def test_mismatch_provider_never_called(
        self,
        adapter_with_chunks: FakeAdapter,
        fresh_cache: QueryEmbeddingCache,
    ) -> None:
        """On mismatch, the provider's embed_batch must never be called (§15, §3.3)."""

        class CallTracker:
            call_count = 0

            def embed_batch(self, texts: list[str], model_id: str) -> object:
                CallTracker.call_count += 1
                return FakeProvider(dimensions=DIMENSIONS, model_id=MODEL_ID).embed_batch(
                    texts, model_id
                )

        provider = FakeProvider(dimensions=DIMENSIONS, model_id=MODEL_ID)
        alias_record = make_alias_record(KB_ID, model_id="other-model", dimensions=128)

        # Wrap provider to count embed_batch calls
        original_embed = provider.embed_batch
        embed_calls: list[bool] = []

        def counting_embed(texts: list, model_id: str) -> object:
            embed_calls.append(True)
            return original_embed(texts, model_id)

        provider.embed_batch = counting_embed  # type: ignore[method-assign]

        _run_query(
            provider=provider,
            adapter=adapter_with_chunks,
            alias_record=alias_record,
            cache=fresh_cache,
        )
        assert len(embed_calls) == 0, "embed_batch must NOT be called on model mismatch"

    def test_mismatch_not_retriable(
        self,
        adapter_with_chunks: FakeAdapter,
        fresh_cache: QueryEmbeddingCache,
    ) -> None:
        """Mismatch is a config problem, not a transient error — retriable=False."""
        alias_record = make_alias_record(KB_ID, model_id="other-model", dimensions=128)
        provider = FakeProvider(dimensions=DIMENSIONS, model_id=MODEL_ID)
        result = _run_query(
            provider=provider,
            adapter=adapter_with_chunks,
            alias_record=alias_record,
            cache=fresh_cache,
        )
        assert result.error.retriable is False  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# PROVIDER_UNAVAILABLE
# ---------------------------------------------------------------------------


class TestProviderUnavailable:
    def test_provider_down_returns_error(
        self,
        adapter_with_chunks: FakeAdapter,
        alias_record: FakeAliasRecord,
        fresh_cache: QueryEmbeddingCache,
    ) -> None:
        provider = FakeProvider(dimensions=DIMENSIONS, model_id=MODEL_ID, fail_on_embed=True)
        result = _run_query(
            provider=provider,
            adapter=adapter_with_chunks,
            alias_record=alias_record,
            cache=fresh_cache,
        )
        assert result.result_status == ResultStatus.error  # type: ignore[union-attr]
        assert result.error.code == ErrorCode.PROVIDER_UNAVAILABLE  # type: ignore[union-attr]

    def test_provider_down_retriable(
        self,
        adapter_with_chunks: FakeAdapter,
        alias_record: FakeAliasRecord,
        fresh_cache: QueryEmbeddingCache,
    ) -> None:
        provider = FakeProvider(dimensions=DIMENSIONS, model_id=MODEL_ID, fail_on_embed=True)
        result = _run_query(
            provider=provider,
            adapter=adapter_with_chunks,
            alias_record=alias_record,
            cache=fresh_cache,
        )
        assert result.error.retriable is True  # type: ignore[union-attr]

    def test_provider_down_results_empty(
        self,
        adapter_with_chunks: FakeAdapter,
        alias_record: FakeAliasRecord,
        fresh_cache: QueryEmbeddingCache,
    ) -> None:
        provider = FakeProvider(dimensions=DIMENSIONS, model_id=MODEL_ID, fail_on_embed=True)
        result = _run_query(
            provider=provider,
            adapter=adapter_with_chunks,
            alias_record=alias_record,
            cache=fresh_cache,
        )
        assert result.results == []  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# VECTOR_DB_UNAVAILABLE
# ---------------------------------------------------------------------------


class TestVectorDbUnavailable:
    def test_db_down_returns_error(
        self,
        provider: FakeProvider,
        alias_record: FakeAliasRecord,
        fresh_cache: QueryEmbeddingCache,
    ) -> None:
        adapter = FakeAdapter(fail_on_search=True)
        adapter.seed_collection(alias=ALIAS, coll=COLL, points=[])  # alias must exist

        result = _run_query(
            provider=provider,
            adapter=adapter,
            alias_record=alias_record,
            cache=fresh_cache,
        )
        assert result.result_status == ResultStatus.error  # type: ignore[union-attr]
        assert result.error.code == ErrorCode.VECTOR_DB_UNAVAILABLE  # type: ignore[union-attr]

    def test_db_down_distinguishable_from_provider_down(
        self,
        provider: FakeProvider,
        alias_record: FakeAliasRecord,
        fresh_cache: QueryEmbeddingCache,
    ) -> None:
        """VECTOR_DB_UNAVAILABLE must be distinguishable from PROVIDER_UNAVAILABLE (§15)."""
        adapter = FakeAdapter(fail_on_search=True)
        adapter.seed_collection(alias=ALIAS, coll=COLL, points=[])

        result = _run_query(
            provider=provider,
            adapter=adapter,
            alias_record=alias_record,
            cache=fresh_cache,
        )
        assert result.error.code == ErrorCode.VECTOR_DB_UNAVAILABLE  # type: ignore[union-attr]
        assert result.error.code != ErrorCode.PROVIDER_UNAVAILABLE  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# KB_NOT_READY
# ---------------------------------------------------------------------------


class TestKBNotReady:
    def test_no_alias_record_returns_error(
        self,
        provider: FakeProvider,
        adapter_with_chunks: FakeAdapter,
        fresh_cache: QueryEmbeddingCache,
    ) -> None:
        """No alias record in the control plane → KB_NOT_READY error."""
        result = _run_query(
            provider=provider,
            adapter=adapter_with_chunks,
            alias_record=None,  # no record
            cache=fresh_cache,
        )
        assert result.result_status == ResultStatus.error  # type: ignore[union-attr]
        assert result.error.code == ErrorCode.KB_NOT_READY  # type: ignore[union-attr]

    def test_no_promoted_collection_returns_error(
        self,
        provider: FakeProvider,
        adapter_with_chunks: FakeAdapter,
        fresh_cache: QueryEmbeddingCache,
    ) -> None:
        """Alias record exists but collection_name is None → KB_NOT_READY."""
        record = FakeAliasRecord(
            alias=ALIAS,
            kb_id=KB_ID,
            collection=None,  # not promoted yet
        )
        result = _run_query(
            provider=provider,
            adapter=adapter_with_chunks,
            alias_record=record,
            cache=fresh_cache,
        )
        assert result.result_status == ResultStatus.error  # type: ignore[union-attr]
        assert result.error.code == ErrorCode.KB_NOT_READY  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Cache behaviour
# ---------------------------------------------------------------------------


class TestQueryCache:
    def test_cache_hit_skips_provider(
        self,
        adapter_with_chunks: FakeAdapter,
        alias_record: FakeAliasRecord,
        fresh_cache: QueryEmbeddingCache,
    ) -> None:
        """On two identical queries, provider.embed_batch is called exactly once."""
        provider = FakeProvider(dimensions=DIMENSIONS, model_id=MODEL_ID)

        embed_calls: list[bool] = []
        original_embed = provider.embed_batch

        def counting_embed(texts: list, model_id: str) -> object:
            embed_calls.append(True)
            return original_embed(texts, model_id)

        provider.embed_batch = counting_embed  # type: ignore[method-assign]

        query_text = "What is the refund policy?"
        _run_query(
            provider=provider,
            adapter=adapter_with_chunks,
            alias_record=alias_record,
            cache=fresh_cache,
            query_text=query_text,
        )
        _run_query(
            provider=provider,
            adapter=adapter_with_chunks,
            alias_record=alias_record,
            cache=fresh_cache,
            query_text=query_text,
        )
        assert len(embed_calls) == 1, (
            "Provider should be called exactly once; second query is a cache hit"
        )

    def test_cache_miss_on_different_query(
        self,
        adapter_with_chunks: FakeAdapter,
        alias_record: FakeAliasRecord,
        fresh_cache: QueryEmbeddingCache,
    ) -> None:
        """Different query texts produce cache misses (two provider calls)."""
        provider = FakeProvider(dimensions=DIMENSIONS, model_id=MODEL_ID)

        embed_calls: list[bool] = []
        original_embed = provider.embed_batch

        def counting_embed(texts: list, model_id: str) -> object:
            embed_calls.append(True)
            return original_embed(texts, model_id)

        provider.embed_batch = counting_embed  # type: ignore[method-assign]

        _run_query(
            provider=provider,
            adapter=adapter_with_chunks,
            alias_record=alias_record,
            cache=fresh_cache,
            query_text="query A",
        )
        _run_query(
            provider=provider,
            adapter=adapter_with_chunks,
            alias_record=alias_record,
            cache=fresh_cache,
            query_text="query B",
        )
        assert len(embed_calls) == 2

    def test_cache_disabled_always_calls_provider(
        self,
        adapter_with_chunks: FakeAdapter,
        alias_record: FakeAliasRecord,
    ) -> None:
        """Disabled cache always calls provider, even for repeated query."""
        disabled_cache = QueryEmbeddingCache(enabled=False)
        provider = FakeProvider(dimensions=DIMENSIONS, model_id=MODEL_ID)

        embed_calls: list[bool] = []
        original_embed = provider.embed_batch

        def counting_embed(texts: list, model_id: str) -> object:
            embed_calls.append(True)
            return original_embed(texts, model_id)

        provider.embed_batch = counting_embed  # type: ignore[method-assign]

        _run_query(
            provider=provider,
            adapter=adapter_with_chunks,
            alias_record=alias_record,
            cache=disabled_cache,
            query_text="same query",
        )
        _run_query(
            provider=provider,
            adapter=adapter_with_chunks,
            alias_record=alias_record,
            cache=disabled_cache,
            query_text="same query",
        )
        assert len(embed_calls) == 2


# ---------------------------------------------------------------------------
# top_k bounds
# ---------------------------------------------------------------------------


class TestTopKBounds:
    def test_top_k_clamped_to_max(
        self,
        provider: FakeProvider,
        alias_record: FakeAliasRecord,
        fresh_cache: QueryEmbeddingCache,
    ) -> None:
        """top_k is clamped to _TOP_K_MAX regardless of the value passed."""
        # Seed many chunks
        adapter = FakeAdapter()
        adapter.seed_collection(
            alias=ALIAS,
            coll=COLL,
            points=[
                make_chunk_payload(chunk_id=f"chk_{i:03d}", score=0.9, kb_id=KB_ID)
                for i in range(200)
            ],
        )
        result = _run_query(
            provider=provider,
            adapter=adapter,
            alias_record=alias_record,
            cache=fresh_cache,
            top_k=99999,  # WAY above max
        )
        assert len(result.results) <= _TOP_K_MAX  # type: ignore[union-attr]

    def test_top_k_1_returns_at_most_1(
        self,
        provider: FakeProvider,
        adapter_with_chunks: FakeAdapter,
        alias_record: FakeAliasRecord,
        fresh_cache: QueryEmbeddingCache,
    ) -> None:
        result = _run_query(
            provider=provider,
            adapter=adapter_with_chunks,
            alias_record=alias_record,
            cache=fresh_cache,
            top_k=1,
        )
        assert len(result.results) <= 1  # type: ignore[union-attr]
