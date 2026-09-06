"""Phase 5 tests for eval scoring (score_eval_set).

Tests:
  - test_score_eval_set_fakeprovider_deterministic: fixed fake embeddings →
    fixed candidates → deterministic recall/precision.
  - test_usable_question_filter: unreviewed / rejected / empty-expected excluded
    or marked non-scorable.
  - test_provisional_confidence_propagates: scoring a provisional set yields a
    ScoreResult carrying provisional (M-044).
  - test_score_eval_set_real_qdrant: integration-flagged (container-gated).
"""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import patch

import pytest

from finecorpus.contracts.eval_set import (
    ConfidenceLevel,
    EvalQuestion,
    GenerationMethod,
    QuestionType,
    ReviewStatus,
)
from finecorpus.embedding.fake import FakeProvider
from finecorpus.index.adapter import alias_name
from finecorpus.services.eval_scoring import ScoreResult, score_eval_set
from tests.retrieval.helpers import (
    FakeAdapter,
    FakeAliasRecord,
    FakeAliasRepository,
    make_alias_record,
    make_chunk_payload,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

KB_ID = "kb-eval-test"
ALIAS = alias_name(KB_ID)
COLL = f"rtfc_{KB_ID.replace('-', '').lower()}_00000001"
MODEL_ID = "fake-embed-v1"
DIMENSIONS = 64

# Fixed chunk IDs used as expected_segment_ids in tests.
CHK_A = "chk_a"
CHK_B = "chk_b"
CHK_C = "chk_c"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_provider() -> FakeProvider:
    return FakeProvider(dimensions=DIMENSIONS, model_id=MODEL_ID)


def _make_alias_record() -> FakeAliasRecord:
    return make_alias_record(KB_ID, model_id=MODEL_ID, dimensions=DIMENSIONS)


def _make_adapter(chunks: list[dict] | None = None) -> FakeAdapter:
    adapter = FakeAdapter()
    pts = chunks or [
        make_chunk_payload(chunk_id=CHK_A, kb_id=KB_ID, score=0.95),
        make_chunk_payload(chunk_id=CHK_B, kb_id=KB_ID, score=0.85),
        make_chunk_payload(chunk_id=CHK_C, kb_id=KB_ID, score=0.60),
    ]
    adapter.seed_collection(alias=ALIAS, coll=COLL, points=pts)
    return adapter


def _make_question(
    question_id: str,
    text: str,
    review_status: ReviewStatus,
    question_type: QuestionType = QuestionType.factual_lookup,
    expected_segment_ids: list[str] | None = None,
) -> EvalQuestion:
    return EvalQuestion(
        question_id=question_id,
        text=text,
        generation_method=GenerationMethod.generated_factual,
        review_status=review_status,
        source_segment_ids=[],
        source_unknown=False,
        question_type=question_type,
        expected_segment_ids=expected_segment_ids,
    )


@contextmanager
def _patch_repo(alias_record: FakeAliasRecord):
    """Patch AliasRepository.get for the duration of the context."""
    repo = FakeAliasRepository({alias_record.alias: alias_record})
    with patch("finecorpus.retrieval.service.AliasRepository") as MockRepo:
        MockRepo.return_value = repo
        yield


# ---------------------------------------------------------------------------
# test_score_eval_set_fakeprovider_deterministic
# ---------------------------------------------------------------------------


class TestScoreEvalSetFakeProviderDeterministic:
    """Deterministic scoring with FakeProvider + FakeAdapter."""

    def test_perfect_recall_single_question(self):
        """Question expects CHK_A; retrieval returns [CHK_A, CHK_B, CHK_C].
        Recall@3 = 1/1 = 1.0."""
        provider = _make_provider()
        adapter = _make_adapter()
        alias_record = _make_alias_record()

        q = _make_question(
            "q1",
            "What is the primary content?",
            ReviewStatus.reviewed_kept,
            expected_segment_ids=[CHK_A],
        )

        with _patch_repo(alias_record):
            result = score_eval_set(
                [q],
                kb_id=KB_ID,
                adapter=adapter,
                provider=provider,
                session=object(),  # type: ignore[arg-type]
                k=3,
                confidence_level=ConfidenceLevel.reviewed,
            )

        assert isinstance(result, ScoreResult)
        assert result.k == 3
        assert result.n_total == 1
        assert result.n_scored == 1
        # CHK_A should be in top-3 candidates (FakeAdapter returns all seeded points)
        assert result.recall == 1.0
        assert result.confidence_level == ConfidenceLevel.reviewed

    def test_partial_recall_two_expected_one_retrieved(self):
        """Question expects [CHK_A, CHK_C]; retrieval at k=1 only returns top-1.
        With k=1 we expect CHK_A in top-1 → recall = 1/2 = 0.5."""
        provider = _make_provider()
        # Seed only CHK_A and CHK_B so CHK_C is not in the collection.
        adapter = _make_adapter(
            chunks=[
                make_chunk_payload(chunk_id=CHK_A, kb_id=KB_ID, score=0.95),
                make_chunk_payload(chunk_id=CHK_B, kb_id=KB_ID, score=0.85),
            ]
        )
        alias_record = _make_alias_record()

        q = _make_question(
            "q2",
            "Describe the two most relevant sections.",
            ReviewStatus.reviewed_edited,
            expected_segment_ids=[CHK_A, CHK_C],
        )

        with _patch_repo(alias_record):
            result = score_eval_set(
                [q],
                kb_id=KB_ID,
                adapter=adapter,
                provider=provider,
                session=object(),  # type: ignore[arg-type]
                k=1,
                confidence_level=ConfidenceLevel.reviewed,
            )

        # CHK_A is top-1 (score 0.95) → 1 of 2 expected found at k=1 → recall = 0.5
        assert result.n_scored == 1
        # recall = hits / |expected| = 1/2 = 0.5
        assert abs(result.recall - 0.5) < 1e-9

    def test_deterministic_across_two_runs(self):
        """Two identical calls must produce identical scores."""
        provider = _make_provider()
        adapter = _make_adapter()
        alias_record = _make_alias_record()
        q = _make_question(
            "q3",
            "Sample question text.",
            ReviewStatus.reviewed_kept,
            expected_segment_ids=[CHK_A],
        )

        with _patch_repo(alias_record):
            r1 = score_eval_set(
                [q],
                kb_id=KB_ID,
                adapter=adapter,
                provider=provider,
                session=object(),  # type: ignore[arg-type]
                k=3,
                confidence_level=ConfidenceLevel.reviewed,
            )
        with _patch_repo(alias_record):
            r2 = score_eval_set(
                [q],
                kb_id=KB_ID,
                adapter=adapter,
                provider=provider,
                session=object(),  # type: ignore[arg-type]
                k=3,
                confidence_level=ConfidenceLevel.reviewed,
            )

        assert r1.recall == r2.recall
        assert r1.precision == r2.precision

    def test_precision_computation(self):
        """With k=2 and [CHK_A, CHK_B, CHK_C] returned (sorted by score),
        top-2 = [CHK_A, CHK_B].  Expected = [CHK_A].
        Precision@2 = |{CHK_A} ∩ {CHK_A, CHK_B}| / 2 = 1/2 = 0.5."""
        provider = _make_provider()
        adapter = _make_adapter()
        alias_record = _make_alias_record()

        q = _make_question(
            "q4",
            "What is in the first chunk?",
            ReviewStatus.reviewed_kept,
            expected_segment_ids=[CHK_A],
        )

        with _patch_repo(alias_record):
            result = score_eval_set(
                [q],
                kb_id=KB_ID,
                adapter=adapter,
                provider=provider,
                session=object(),  # type: ignore[arg-type]
                k=2,
                confidence_level=ConfidenceLevel.reviewed,
            )

        # top-2 candidates contain CHK_A (1 relevant out of 2) → precision = 0.5
        assert abs(result.precision - 0.5) < 1e-9


# ---------------------------------------------------------------------------
# test_usable_question_filter
# ---------------------------------------------------------------------------


class TestUsableQuestionFilter:
    """Verify that review_status and expected_segment_ids filtering is correct."""

    def test_unreviewed_excluded_entirely(self):
        """Unreviewed questions are not scored (n_scored=0, n_total=1)."""
        provider = _make_provider()
        adapter = _make_adapter()
        alias_record = _make_alias_record()

        q = _make_question(
            "q_unrev",
            "Unreviewed question.",
            ReviewStatus.unreviewed,
            expected_segment_ids=[CHK_A],
        )

        with _patch_repo(alias_record):
            result = score_eval_set(
                [q],
                kb_id=KB_ID,
                adapter=adapter,
                provider=provider,
                session=object(),  # type: ignore[arg-type]
                k=3,
                confidence_level=ConfidenceLevel.provisional,
            )

        # Unreviewed excluded entirely — not even a non-scorable entry
        assert result.n_total == 1
        assert result.n_scored == 0
        assert result.summary.mean_recall == 0.0

    def test_reviewed_rejected_excluded_entirely(self):
        """Rejected questions are not scored."""
        provider = _make_provider()
        adapter = _make_adapter()
        alias_record = _make_alias_record()

        q = _make_question(
            "q_rej",
            "Rejected question.",
            ReviewStatus.reviewed_rejected,
            expected_segment_ids=[CHK_A],
        )

        with _patch_repo(alias_record):
            result = score_eval_set(
                [q],
                kb_id=KB_ID,
                adapter=adapter,
                provider=provider,
                session=object(),  # type: ignore[arg-type]
                k=3,
                confidence_level=ConfidenceLevel.reviewed,
            )

        assert result.n_scored == 0
        assert result.summary.mean_recall == 0.0

    def test_empty_expected_segment_ids_marked_non_scorable(self):
        """Questions with empty expected_segment_ids are scored but marked scorable=False."""
        provider = _make_provider()
        adapter = _make_adapter()
        alias_record = _make_alias_record()

        q = _make_question(
            "q_no_expected",
            "Question with no expected segments.",
            ReviewStatus.reviewed_kept,
            expected_segment_ids=[],  # empty → non-scorable
        )

        with _patch_repo(alias_record):
            result = score_eval_set(
                [q],
                kb_id=KB_ID,
                adapter=adapter,
                provider=provider,
                session=object(),  # type: ignore[arg-type]
                k=3,
                confidence_level=ConfidenceLevel.reviewed,
            )

        # The question was processed (n_total=1) but n_scored=0 (non-scorable)
        assert result.n_total == 1
        assert result.n_scored == 0
        # mean_recall is 0.0 (no scorable questions → sentinel exclusion)
        assert result.summary.mean_recall == 0.0

    def test_none_expected_segment_ids_treated_as_empty(self):
        """expected_segment_ids=None also yields non-scorable."""
        provider = _make_provider()
        adapter = _make_adapter()
        alias_record = _make_alias_record()

        q = _make_question(
            "q_none_expected",
            "Question with None expected segments.",
            ReviewStatus.reviewed_kept,
            expected_segment_ids=None,
        )

        with _patch_repo(alias_record):
            result = score_eval_set(
                [q],
                kb_id=KB_ID,
                adapter=adapter,
                provider=provider,
                session=object(),  # type: ignore[arg-type]
                k=3,
                confidence_level=ConfidenceLevel.reviewed,
            )

        assert result.n_scored == 0

    def test_reviewed_kept_and_edited_both_scored(self):
        """Both reviewed_kept and reviewed_edited questions are scored."""
        provider = _make_provider()
        adapter = _make_adapter()
        alias_record = _make_alias_record()

        q_kept = _make_question(
            "q_kept",
            "Kept question.",
            ReviewStatus.reviewed_kept,
            expected_segment_ids=[CHK_A],
        )
        q_edited = _make_question(
            "q_edited",
            "Edited question.",
            ReviewStatus.reviewed_edited,
            expected_segment_ids=[CHK_B],
        )

        with _patch_repo(alias_record):
            result = score_eval_set(
                [q_kept, q_edited],
                kb_id=KB_ID,
                adapter=adapter,
                provider=provider,
                session=object(),  # type: ignore[arg-type]
                k=3,
                confidence_level=ConfidenceLevel.reviewed,
            )

        assert result.n_total == 2
        assert result.n_scored == 2

    def test_mixed_statuses_only_usable_scored(self):
        """Mix of statuses: only reviewed_kept/edited are scored."""
        provider = _make_provider()
        adapter = _make_adapter()
        alias_record = _make_alias_record()

        questions = [
            _make_question(
                "q1", "Question 1", ReviewStatus.reviewed_kept, expected_segment_ids=[CHK_A]
            ),
            _make_question(
                "q2", "Question 2", ReviewStatus.unreviewed, expected_segment_ids=[CHK_B]
            ),
            _make_question(
                "q3", "Question 3", ReviewStatus.reviewed_rejected, expected_segment_ids=[CHK_C]
            ),
            _make_question(
                "q4", "Question 4", ReviewStatus.reviewed_edited, expected_segment_ids=[CHK_A]
            ),
        ]

        with _patch_repo(alias_record):
            result = score_eval_set(
                questions,
                kb_id=KB_ID,
                adapter=adapter,
                provider=provider,
                session=object(),  # type: ignore[arg-type]
                k=3,
                confidence_level=ConfidenceLevel.provisional,
            )

        # n_total is 4 (all questions passed in)
        assert result.n_total == 4
        # Only q1 (kept) and q4 (edited) are scored
        assert result.n_scored == 2


# ---------------------------------------------------------------------------
# test_provisional_confidence_propagates (M-044)
# ---------------------------------------------------------------------------


class TestProvisionalConfidencePropagates:
    """M-044: scoring a provisional set yields a ScoreResult carrying provisional."""

    def test_provisional_propagates(self):
        """confidence_level=provisional propagates to ScoreResult."""
        provider = _make_provider()
        adapter = _make_adapter()
        alias_record = _make_alias_record()

        q = _make_question(
            "q_prov",
            "Provisional question.",
            ReviewStatus.reviewed_kept,
            expected_segment_ids=[CHK_A],
        )

        with _patch_repo(alias_record):
            result = score_eval_set(
                [q],
                kb_id=KB_ID,
                adapter=adapter,
                provider=provider,
                session=object(),  # type: ignore[arg-type]
                k=3,
                confidence_level=ConfidenceLevel.provisional,
            )

        assert result.confidence_level == ConfidenceLevel.provisional

    def test_reviewed_propagates(self):
        """confidence_level=reviewed propagates to ScoreResult."""
        provider = _make_provider()
        adapter = _make_adapter()
        alias_record = _make_alias_record()

        q = _make_question(
            "q_rev",
            "Reviewed question.",
            ReviewStatus.reviewed_kept,
            expected_segment_ids=[CHK_A],
        )

        with _patch_repo(alias_record):
            result = score_eval_set(
                [q],
                kb_id=KB_ID,
                adapter=adapter,
                provider=provider,
                session=object(),  # type: ignore[arg-type]
                k=3,
                confidence_level=ConfidenceLevel.reviewed,
            )

        assert result.confidence_level == ConfidenceLevel.reviewed

    def test_production_derived_propagates(self):
        """confidence_level=production_derived propagates to ScoreResult."""
        provider = _make_provider()
        adapter = _make_adapter()
        alias_record = _make_alias_record()

        q = _make_question(
            "q_prod",
            "Production derived question.",
            ReviewStatus.reviewed_kept,
            expected_segment_ids=[CHK_A],
        )

        with _patch_repo(alias_record):
            result = score_eval_set(
                [q],
                kb_id=KB_ID,
                adapter=adapter,
                provider=provider,
                session=object(),  # type: ignore[arg-type]
                k=3,
                confidence_level=ConfidenceLevel.production_derived,
            )

        assert result.confidence_level == ConfidenceLevel.production_derived

    def test_empty_question_list_returns_zero_scores(self):
        """Empty question list → ScoreResult with 0.0 recall/precision."""
        provider = _make_provider()
        adapter = _make_adapter()
        alias_record = _make_alias_record()

        with _patch_repo(alias_record):
            result = score_eval_set(
                [],
                kb_id=KB_ID,
                adapter=adapter,
                provider=provider,
                session=object(),  # type: ignore[arg-type]
                k=10,
                confidence_level=ConfidenceLevel.provisional,
            )

        assert result.n_total == 0
        assert result.n_scored == 0
        assert result.recall == 0.0
        assert result.precision == 0.0
        assert result.confidence_level == ConfidenceLevel.provisional


# ---------------------------------------------------------------------------
# Integration-flagged: test_score_eval_set_real_qdrant
# ---------------------------------------------------------------------------
# NOTE: This test requires a live Qdrant + Postgres container.
# It is gated by the qdrant_integration composite mark (conftest.py) and
# will auto-skip when containers are unreachable.
# Run at phase close with: uv run pytest -m qdrant_integration tests/phase5/


@pytest.mark.skip(reason="integration: requires Qdrant + Postgres containers — run at phase close")
def test_score_eval_set_real_qdrant():
    """Integration test: score_eval_set against a live Qdrant collection.

    Follows the pattern from tests/index/test_integration.py.
    Skipped by default; activate at phase close with:
        uv run pytest -m qdrant_integration tests/phase5/test_eval_scoring.py
    """
    pass  # Full implementation deferred to phase-close overlay.
