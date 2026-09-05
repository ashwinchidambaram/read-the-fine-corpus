"""Phase 4 explain mode tests (M-062/M-063, D-24).

Covers:
- M-063: Tenancy parity — explain on KB-A shows zero KB-B chunk_ids in candidates
  AND exclusions (cross-tenant data is structurally invisible).
- Exclusion attribution — each excluded candidate names the exact AppliedFilter.
- Filter echo — filters_applied contains origins matching what was applied.
- Candidate provenance completeness — every candidate has provenance + scores.
- D-24: filtered_to_zero counter incremented when score filter alone empties survivor set.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from typing import Any
from unittest.mock import patch

from finecorpus.contracts.retrieval_response import FilterOrigin, ResultStatus
from finecorpus.embedding.fake import FakeProvider
from finecorpus.index.adapter import alias_name
from finecorpus.retrieval.metrics import get_filtered_to_zero_count, reset_filtered_to_zero_count

# Import shared helpers from the retrieval test suite
sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent / "retrieval"))
from helpers import (  # type: ignore[import-not-found]
    FakeAdapter,
    FakeAliasRecord,
    make_provenance_payload,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

KB_A = "kb-explain-a"
KB_B = "kb-explain-b"
WS_A = "ws-explain-a"
WS_B = "ws-explain-b"
MODEL_ID = "fake-embed-v1"
DIMENSIONS = 64
COLL_A = f"rtfc_{KB_A.replace('-', '').lower()}_00000001"
COLL_B = f"rtfc_{KB_B.replace('-', '').lower()}_00000001"


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _make_alias_record(kb_id: str, ws_id: str, coll: str) -> FakeAliasRecord:
    return FakeAliasRecord(
        alias=alias_name(kb_id),
        kb_id=kb_id,
        workspace_id=ws_id,
        collection=coll,
        embedding_provider="fake",
        embedding_model=MODEL_ID,
        embedding_dimensions=DIMENSIONS,
        config_version="cfgv1",
    )


def _make_chunk(
    chunk_id: str,
    kb_id: str,
    score: float = 0.9,
    permission_resolved_at: str | None = None,
) -> dict[str, Any]:
    """Build a point dict with full provenance and optional permission_resolved_at."""
    prov = make_provenance_payload(
        source_document_id=f"doc-{chunk_id}",
        source_document_version="v1",
    )
    tenancy: dict[str, Any] = {
        "kb_id": kb_id,
        "workspace_id": WS_A if kb_id == KB_A else WS_B,
        "permission_mode": "public_to_kb",
        "permission_principals": [],
        "permission_source": "platform",
        "permission_fidelity": "authoritative",
    }
    if permission_resolved_at is not None:
        tenancy["permission_resolved_at"] = permission_resolved_at

    return {
        "id": chunk_id,
        "score": score,
        "payload": {
            "chunk_id": chunk_id,
            "text": f"Text from {chunk_id}",
            "tenancy": tenancy,
            "provenance": prov,
        },
    }


def _make_provider() -> FakeProvider:
    return FakeProvider(model_id=MODEL_ID, dimensions=DIMENSIONS)


def _fake_session_factory(alias_repo: Any) -> Any:
    """Return a context manager that yields a fake session with the given alias repo."""

    @contextmanager
    def _factory():
        class _FakeSession:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

        yield _FakeSession()

    # Patch AliasRepository at query time
    return _factory


def _run_query(
    kb_id: str,
    adapter: FakeAdapter,
    alias_records: dict[str, FakeAliasRecord],
    *,
    score_threshold: float | None = None,
    explain: bool = True,
) -> Any:
    """Run query() with faked session/repo returning the given alias records."""
    from finecorpus.retrieval.service import query

    provider = _make_provider()

    @contextmanager
    def _fake_session():
        class _FakeSession:
            pass

        yield _FakeSession()

    with patch("finecorpus.retrieval.service.AliasRepository") as mock_repo_cls:
        mock_repo = mock_repo_cls.return_value
        mock_repo.get.side_effect = lambda alias: alias_records.get(alias)

        with _fake_session() as session:
            return query(
                kb_id=kb_id,
                query_text="what is the policy",
                provider=provider,
                adapter=adapter,
                session=session,
                top_k=10,
                score_threshold=score_threshold,
                explain=explain,
                auth_enabled=False,
            )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestExplainCrossTenantParity:
    """M-063: Tenancy parity — explain is NOT a bypass (§11.5)."""

    def test_explain_cross_tenant_returns_no_foreign_candidates(self) -> None:
        """Explain on KB-A shows zero KB-B chunk_ids in candidates AND exclusions.

        This is the M-063 structural test: the tenancy filter is applied at
        the vector-search layer (inside _execute_search), so KB-B chunks are
        never in the over-fetched result set.  They cannot appear in candidates
        or exclusions in the explain block.
        """
        adapter = FakeAdapter()

        # Seed KB-A with 2 chunks and KB-B with 2 chunks
        adapter.seed_collection(
            alias_name(KB_A),
            COLL_A,
            [
                _make_chunk("chk-a-001", KB_A, score=0.9),
                _make_chunk("chk-a-002", KB_A, score=0.8),
            ],
        )
        adapter.seed_collection(
            alias_name(KB_B),
            COLL_B,
            [
                _make_chunk("chk-b-001", KB_B, score=0.95),
                _make_chunk("chk-b-002", KB_B, score=0.85),
            ],
        )

        alias_records = {
            alias_name(KB_A): _make_alias_record(KB_A, WS_A, COLL_A),
            alias_name(KB_B): _make_alias_record(KB_B, WS_B, COLL_B),
        }

        response = _run_query(KB_A, adapter, alias_records, explain=True)

        assert response.result_status == ResultStatus.matches
        assert response.explain is not None

        explain = response.explain
        all_chunk_ids_in_explain = {c.chunk_id for c in explain.candidates} | {
            e.chunk_id for e in explain.exclusions
        }

        # No KB-B chunk IDs should appear
        kb_b_chunk_ids = {"chk-b-001", "chk-b-002"}
        assert all_chunk_ids_in_explain.isdisjoint(kb_b_chunk_ids), (
            f"KB-B chunks appeared in explain for KB-A query: "
            f"{all_chunk_ids_in_explain & kb_b_chunk_ids}"
        )

        # KB-A chunks should appear
        assert "chk-a-001" in all_chunk_ids_in_explain
        assert "chk-a-002" in all_chunk_ids_in_explain


class TestExplainExclusionAttribution:
    """Each excluded candidate names the exact AppliedFilter that removed it."""

    def test_explain_exclusion_attribution(self) -> None:
        """Candidates excluded by score_threshold each name the threshold filter."""
        adapter = FakeAdapter()
        adapter.seed_collection(
            alias_name(KB_A),
            COLL_A,
            [
                _make_chunk("chk-a-high", KB_A, score=0.9),
                _make_chunk("chk-a-low", KB_A, score=0.3),
            ],
        )

        alias_records = {alias_name(KB_A): _make_alias_record(KB_A, WS_A, COLL_A)}

        response = _run_query(KB_A, adapter, alias_records, score_threshold=0.5, explain=True)

        assert response.explain is not None
        exclusions = response.explain.exclusions

        # The low-score chunk should be in exclusions
        excluded_ids = {e.chunk_id for e in exclusions}
        assert "chk-a-low" in excluded_ids

        # Each exclusion names the score filter with request origin
        for excl in exclusions:
            assert excl.removed_by.origin == FilterOrigin.request
            assert "score" in excl.removed_by.expression.lower()

    def test_each_exclusion_has_applied_filter(self) -> None:
        """Every exclusion record has a non-null removed_by AppliedFilter."""
        adapter = FakeAdapter()
        adapter.seed_collection(
            alias_name(KB_A),
            COLL_A,
            [_make_chunk(f"chk-{i}", KB_A, score=0.1 * i) for i in range(1, 6)],
        )

        alias_records = {alias_name(KB_A): _make_alias_record(KB_A, WS_A, COLL_A)}
        response = _run_query(KB_A, adapter, alias_records, score_threshold=0.4, explain=True)

        assert response.explain is not None
        for excl in response.explain.exclusions:
            assert excl.removed_by is not None
            assert excl.removed_by.origin is not None
            assert excl.removed_by.expression


class TestExplainFiltersEchoOrigins:
    """Filters in explain response have correct origins."""

    def test_explain_filters_echo_origins(self) -> None:
        """The filters_applied list contains at least the tenancy filter with correct origin."""
        adapter = FakeAdapter()
        adapter.seed_collection(
            alias_name(KB_A),
            COLL_A,
            [
                _make_chunk("chk-a-001", KB_A, score=0.9),
            ],
        )

        alias_records = {alias_name(KB_A): _make_alias_record(KB_A, WS_A, COLL_A)}
        response = _run_query(KB_A, adapter, alias_records, explain=True)

        assert response.request_echo is not None
        filters = response.request_echo.filters_applied

        # At least one tenancy filter must be present
        tenancy_filters = [f for f in filters if f.origin == FilterOrigin.tenancy]
        assert len(tenancy_filters) >= 1, "Expected at least one tenancy filter in explain response"

        # All filter origins must be valid FilterOrigin values
        for f in filters:
            assert f.origin in (
                FilterOrigin.tenancy,
                FilterOrigin.request,
                FilterOrigin.kb_config,
            )

    def test_score_threshold_filter_has_request_origin(self) -> None:
        """When score_threshold is set, the threshold filter has origin=request."""
        adapter = FakeAdapter()
        adapter.seed_collection(
            alias_name(KB_A),
            COLL_A,
            [
                _make_chunk("chk-a-001", KB_A, score=0.9),
            ],
        )

        alias_records = {alias_name(KB_A): _make_alias_record(KB_A, WS_A, COLL_A)}
        response = _run_query(KB_A, adapter, alias_records, score_threshold=0.5, explain=True)

        request_filters = [
            f for f in response.request_echo.filters_applied if f.origin == FilterOrigin.request
        ]
        assert any("score" in f.expression.lower() for f in request_filters), (
            "Expected a score filter with origin=request in filters_applied"
        )


class TestExplainCandidateProvenanceComplete:
    """Every explain candidate has provenance + scores (§11.5 requirement)."""

    def test_explain_candidate_provenance_complete(self) -> None:
        """All explain candidates have non-null provenance and scores."""
        resolved_ts = "2026-09-01T12:00:00+00:00"
        adapter = FakeAdapter()
        adapter.seed_collection(
            alias_name(KB_A),
            COLL_A,
            [
                _make_chunk("chk-a-001", KB_A, score=0.9, permission_resolved_at=resolved_ts),
                _make_chunk("chk-a-002", KB_A, score=0.8),
            ],
        )

        alias_records = {alias_name(KB_A): _make_alias_record(KB_A, WS_A, COLL_A)}
        response = _run_query(KB_A, adapter, alias_records, explain=True)

        assert response.explain is not None
        assert len(response.explain.candidates) >= 1

        for cand in response.explain.candidates:
            assert cand.provenance is not None
            assert cand.provenance.source_document_id
            assert cand.provenance.source_document_version
            assert cand.scores is not None
            assert cand.scores.raw is not None

        # D-17: permission_resolved_at is surfaced when present in the payload
        cand_with_ts = next(
            (c for c in response.explain.candidates if c.chunk_id == "chk-a-001"), None
        )
        if cand_with_ts is not None:
            assert cand_with_ts.permission_resolved_at is not None

    def test_explain_strategy_is_dense(self) -> None:
        """The explain block strategy is 'dense' (Phase 4 is dense-only)."""
        adapter = FakeAdapter()
        adapter.seed_collection(
            alias_name(KB_A),
            COLL_A,
            [
                _make_chunk("chk-a-001", KB_A, score=0.9),
            ],
        )

        alias_records = {alias_name(KB_A): _make_alias_record(KB_A, WS_A, COLL_A)}
        response = _run_query(KB_A, adapter, alias_records, explain=True)

        assert response.explain is not None
        assert response.explain.strategy == "dense"

    def test_explain_parsed_query_echoes_query_text(self) -> None:
        """The explain block parsed_query is the original query text."""
        from finecorpus.retrieval.service import query

        adapter = FakeAdapter()
        adapter.seed_collection(
            alias_name(KB_A),
            COLL_A,
            [
                _make_chunk("chk-a-001", KB_A, score=0.9),
            ],
        )

        alias_records = {alias_name(KB_A): _make_alias_record(KB_A, WS_A, COLL_A)}
        provider = _make_provider()
        query_text = "specific policy query text 12345"

        with patch("finecorpus.retrieval.service.AliasRepository") as mock_repo_cls:
            mock_repo = mock_repo_cls.return_value
            mock_repo.get.side_effect = lambda alias: alias_records.get(alias)

            @contextmanager
            def _fake_session():
                class _FakeSession:
                    pass

                yield _FakeSession()

            with _fake_session() as session:
                response = query(
                    kb_id=KB_A,
                    query_text=query_text,
                    provider=provider,
                    adapter=adapter,
                    session=session,
                    explain=True,
                    auth_enabled=False,
                )

        assert response.explain is not None
        assert response.explain.parsed_query == query_text


class TestFilteredToZeroCounterD24:
    """D-24: filtered_to_zero counter increments when score filter alone empties survivor set."""

    def setup_method(self) -> None:
        """Reset counter before each test."""
        reset_filtered_to_zero_count()

    def test_filtered_to_zero_counter_d24(self) -> None:
        """Counter increments when score threshold eliminates all candidates."""
        adapter = FakeAdapter()
        adapter.seed_collection(
            alias_name(KB_A),
            COLL_A,
            [
                _make_chunk("chk-a-001", KB_A, score=0.3),
                _make_chunk("chk-a-002", KB_A, score=0.2),
            ],
        )

        alias_records = {alias_name(KB_A): _make_alias_record(KB_A, WS_A, COLL_A)}

        before = get_filtered_to_zero_count()
        response = _run_query(KB_A, adapter, alias_records, score_threshold=0.9, explain=False)

        assert response.result_status == ResultStatus.filtered_to_zero
        assert get_filtered_to_zero_count() == before + 1

    def test_counter_not_incremented_on_matches(self) -> None:
        """Counter does NOT increment when results survive the threshold."""
        adapter = FakeAdapter()
        adapter.seed_collection(
            alias_name(KB_A),
            COLL_A,
            [
                _make_chunk("chk-a-001", KB_A, score=0.95),
            ],
        )

        alias_records = {alias_name(KB_A): _make_alias_record(KB_A, WS_A, COLL_A)}

        before = get_filtered_to_zero_count()
        response = _run_query(KB_A, adapter, alias_records, score_threshold=0.5)

        assert response.result_status == ResultStatus.matches
        assert get_filtered_to_zero_count() == before

    def test_counter_not_incremented_on_no_matches(self) -> None:
        """Counter does NOT increment when the vector search returns 0 results."""
        adapter = FakeAdapter()
        # Empty collection — no matches
        adapter.seed_collection(alias_name(KB_A), COLL_A, [])

        alias_records = {alias_name(KB_A): _make_alias_record(KB_A, WS_A, COLL_A)}

        before = get_filtered_to_zero_count()
        response = _run_query(KB_A, adapter, alias_records, score_threshold=0.5)

        assert response.result_status == ResultStatus.no_matches
        assert get_filtered_to_zero_count() == before

    def test_counter_accumulates_across_multiple_filtered_queries(self) -> None:
        """Counter accumulates across multiple filtered-to-zero events."""
        adapter = FakeAdapter()
        adapter.seed_collection(
            alias_name(KB_A),
            COLL_A,
            [
                _make_chunk("chk-a-001", KB_A, score=0.2),
            ],
        )

        alias_records = {alias_name(KB_A): _make_alias_record(KB_A, WS_A, COLL_A)}

        before = get_filtered_to_zero_count()
        _run_query(KB_A, adapter, alias_records, score_threshold=0.9)
        _run_query(KB_A, adapter, alias_records, score_threshold=0.9)
        _run_query(KB_A, adapter, alias_records, score_threshold=0.9)

        assert get_filtered_to_zero_count() == before + 3


class TestExplainNoMatchesBlock:
    """Explain block is populated even when result is no_matches or filtered_to_zero."""

    def test_explain_block_on_no_matches(self) -> None:
        """When explain=True and no results, explain block has empty candidates and exclusions."""
        adapter = FakeAdapter()
        adapter.seed_collection(alias_name(KB_A), COLL_A, [])  # empty

        alias_records = {alias_name(KB_A): _make_alias_record(KB_A, WS_A, COLL_A)}
        response = _run_query(KB_A, adapter, alias_records, explain=True)

        assert response.result_status == ResultStatus.no_matches
        assert response.explain is not None
        assert response.explain.candidates == []
        assert response.explain.exclusions == []
        assert response.explain.strategy == "dense"

    def test_explain_block_on_filtered_to_zero(self) -> None:
        """When explain=True and filtered_to_zero, explain block has candidates and exclusions."""
        reset_filtered_to_zero_count()
        adapter = FakeAdapter()
        adapter.seed_collection(
            alias_name(KB_A),
            COLL_A,
            [
                _make_chunk("chk-a-001", KB_A, score=0.2),
            ],
        )

        alias_records = {alias_name(KB_A): _make_alias_record(KB_A, WS_A, COLL_A)}
        response = _run_query(KB_A, adapter, alias_records, score_threshold=0.9, explain=True)

        assert response.result_status == ResultStatus.filtered_to_zero
        assert response.explain is not None
        assert len(response.explain.candidates) >= 1
        assert len(response.explain.exclusions) >= 1
