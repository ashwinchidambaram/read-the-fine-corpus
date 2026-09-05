"""Tests for RetrievalResponse schema_version 1.2.0 additions (PR-B).

Covers:
- ErrorCode.RATE_LIMITED present and maps to the right string.
- RetrievalResponse.break_glass_read_ref: defaults to None, accepts str.
- ExplainCandidate.permission_resolved_at: defaults to None, accepts datetime.
- 1.1-shaped payloads still validate (backward compat — all new fields nullable/defaulted).
- Producer stamp RETRIEVAL_RESPONSE_SCHEMA_VERSION == "1.2.0" in versions.py.
- Consumer SpecRange SUPPORTED_RETRIEVAL_RESPONSE still accepts 1.2.0 (min_minor=1).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from finecorpus.contracts.retrieval_response import (
    ErrorCode,
    ExplainBlock,
    ExplainCandidate,
    ResultStatus,
    RetrievalResponse,
)
from finecorpus.contracts.versions import (
    RETRIEVAL_RESPONSE_SCHEMA_VERSION,
    SUPPORTED_RETRIEVAL_RESPONSE,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BASE_PROVENANCE = {
    "source_document_id": "doc-001",
    "source_document_version": "v1",
    "source_location": {
        "locator_kind": "char_range",
        "char_start": 0,
        "char_end": 100,
        "page_start": None,
        "page_end": None,
        "byte_start": None,
        "byte_end": None,
        "cell_range": None,
        "dom_path": None,
        "bbox": None,
        "coordinate_note": None,
    },
    "structural_path": ["Section 1"],
    "transformations": [],
    "confidence": 0.95,
    "ocr_confidence": None,
    "segment_type": "prose",
    "salience_tier": "primary",
    "salience_basis": "default",
    "salience_signals": [],
    "language": "en",
    "injection_suspicion": 0.0,
    "invisible_content_flags": [],
    "sensitivity_flags": [],
    "trust_level": "untrusted_ingested",
}


def _make_minimal_response(**overrides) -> dict:
    """Build a minimal valid 1.2.0 RetrievalResponse dict."""
    base = {
        "schema_version": "1.2.0",
        "request_echo": {
            "query": "what is retrieval?",
            "filters_applied": [],
        },
        "result_status": "no_matches",
        "results": [],
        "error": None,
        "explain": None,
    }
    base.update(overrides)
    return base


def _make_result() -> dict:
    return {
        "chunk_id": "chk_abc123",
        "text": "Some chunk text.",
        "provenance": _BASE_PROVENANCE,
        "score": 0.85,
        "scores": None,
        "trust_level": "untrusted_ingested",
    }


# ---------------------------------------------------------------------------
# 1. Producer stamp
# ---------------------------------------------------------------------------


class TestProducerStamp:
    def test_stamp_value(self) -> None:
        """RETRIEVAL_RESPONSE_SCHEMA_VERSION must be 1.2.0."""
        assert RETRIEVAL_RESPONSE_SCHEMA_VERSION == "1.2.0"

    def test_consumer_range_accepts_120(self) -> None:
        """SpecRange(major=1, min_minor=1) must accept 1.2.0."""
        SUPPORTED_RETRIEVAL_RESPONSE.check("retrieval_response", "1.2.0")

    def test_consumer_range_accepts_110(self) -> None:
        """SpecRange must still accept 1.1.0 (min_minor=1)."""
        SUPPORTED_RETRIEVAL_RESPONSE.check("retrieval_response", "1.1.0")

    def test_consumer_range_rejects_100(self) -> None:
        """SpecRange(min_minor=1) must reject 1.0.0."""
        from finecorpus.contracts.versions import ContractVersionError

        with pytest.raises(ContractVersionError):
            SUPPORTED_RETRIEVAL_RESPONSE.check("retrieval_response", "1.0.0")


# ---------------------------------------------------------------------------
# 2. ErrorCode.RATE_LIMITED
# ---------------------------------------------------------------------------


class TestRateLimitedErrorCode:
    def test_enum_member_exists(self) -> None:
        assert ErrorCode.RATE_LIMITED == "RATE_LIMITED"

    def test_enum_member_string_value(self) -> None:
        assert str(ErrorCode.RATE_LIMITED) == "RATE_LIMITED"

    def test_all_error_codes_present(self) -> None:
        """Regression: existing codes must not have been removed."""
        names = {e.name for e in ErrorCode}
        assert "EMBEDDING_MODEL_MISMATCH" in names
        assert "VECTOR_DB_UNAVAILABLE" in names
        assert "PROVIDER_UNAVAILABLE" in names
        assert "KB_NOT_READY" in names
        assert "PERMISSION_DENIED" in names
        assert "INVALID_QUERY" in names
        assert "CONTROL_PLANE_UNAVAILABLE" in names
        assert "PAYLOAD_CORRUPT" in names
        assert "RATE_LIMITED" in names


# ---------------------------------------------------------------------------
# 3. RetrievalResponse.break_glass_read_ref
# ---------------------------------------------------------------------------


class TestBreakGlassReadRef:
    def test_defaults_to_none(self) -> None:
        """break_glass_read_ref defaults to None when omitted."""
        resp = RetrievalResponse(**_make_minimal_response())
        assert resp.break_glass_read_ref is None

    def test_accepts_string(self) -> None:
        """break_glass_read_ref accepts a non-empty audit-log entry ID."""
        resp = RetrievalResponse(**_make_minimal_response(break_glass_read_ref="audit-entry-001"))
        assert resp.break_glass_read_ref == "audit-entry-001"

    def test_accepts_explicit_none(self) -> None:
        resp = RetrievalResponse(**_make_minimal_response(break_glass_read_ref=None))
        assert resp.break_glass_read_ref is None

    def test_roundtrip_json(self) -> None:
        """break_glass_read_ref survives model_dump/model_validate round trip."""
        resp = RetrievalResponse(**_make_minimal_response(break_glass_read_ref="bg-audit-123"))
        data = resp.model_dump()
        resp2 = RetrievalResponse.model_validate(data)
        assert resp2.break_glass_read_ref == "bg-audit-123"


# ---------------------------------------------------------------------------
# 4. ExplainCandidate.permission_resolved_at
# ---------------------------------------------------------------------------


class TestPermissionResolvedAt:
    def _make_candidate(self, **overrides) -> dict:
        base = {
            "chunk_id": "chk_cand_001",
            "scores": {"raw": 0.9, "reranked": None},
            "provenance": _BASE_PROVENANCE,
        }
        base.update(overrides)
        return base

    def test_defaults_to_none(self) -> None:
        """permission_resolved_at defaults to None when omitted."""
        cand = ExplainCandidate(**self._make_candidate())
        assert cand.permission_resolved_at is None

    def test_accepts_datetime(self) -> None:
        ts = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        cand = ExplainCandidate(**self._make_candidate(permission_resolved_at=ts))
        assert cand.permission_resolved_at == ts

    def test_accepts_explicit_none(self) -> None:
        cand = ExplainCandidate(**self._make_candidate(permission_resolved_at=None))
        assert cand.permission_resolved_at is None

    def test_roundtrip_json(self) -> None:
        ts = datetime(2026, 6, 15, 8, 30, 0, tzinfo=UTC)
        cand = ExplainCandidate(**self._make_candidate(permission_resolved_at=ts))
        data = cand.model_dump()
        cand2 = ExplainCandidate.model_validate(data)
        assert cand2.permission_resolved_at == ts


# ---------------------------------------------------------------------------
# 5. Backward compatibility — 1.1-shaped payloads still validate
# ---------------------------------------------------------------------------


class TestBackwardCompatV11:
    """Payloads missing the new 1.2.0 fields MUST still validate.

    All three new fields are nullable with defaults=None, so a 1.1-era
    consumer can send or receive a 1.1-shaped dict and it will parse
    correctly into a 1.2 model.
    """

    def test_response_without_break_glass_field(self) -> None:
        """A 1.1-era response dict (no break_glass_read_ref key) validates."""
        d = {
            "schema_version": "1.1.0",
            "request_echo": {"query": "q", "filters_applied": []},
            "result_status": "no_matches",
            "results": [],
        }
        resp = RetrievalResponse.model_validate(d)
        assert resp.break_glass_read_ref is None
        assert resp.schema_version == "1.1.0"

    def test_explain_candidate_without_permission_resolved_at(self) -> None:
        """A 1.1-era ExplainCandidate dict (no permission_resolved_at key) validates."""
        d = {
            "chunk_id": "chk_old",
            "scores": {"raw": 0.7, "reranked": None},
            "provenance": _BASE_PROVENANCE,
        }
        cand = ExplainCandidate.model_validate(d)
        assert cand.permission_resolved_at is None

    def test_full_1_1_response_roundtrip(self) -> None:
        """A complete 1.1-shaped RetrievalResponse validates with 1.2 model."""
        result_dict = _make_result()
        d = {
            "schema_version": "1.1.0",
            "request_echo": {
                "query": "what is the policy?",
                "filters_applied": [
                    {"expression": "kb_id == 'kb-001'", "origin": "tenancy"},
                ],
            },
            "result_status": "matches",
            "results": [result_dict],
            "error": None,
            "explain": None,
        }
        resp = RetrievalResponse.model_validate(d)
        assert resp.result_status == ResultStatus.matches
        assert len(resp.results) == 1
        assert resp.break_glass_read_ref is None

    def test_explain_block_without_new_fields(self) -> None:
        """An ExplainBlock built from 1.1-era candidates (no permission_resolved_at) validates."""
        cand_dict = {
            "chunk_id": "chk_001",
            "scores": {"raw": 0.8, "reranked": None},
            "provenance": _BASE_PROVENANCE,
        }
        excl_dict = {
            "chunk_id": "chk_excl_001",
            "removed_by": {"expression": "kb_id != 'kb-001'", "origin": "tenancy"},
        }
        explain_dict = {
            "parsed_query": "policy",
            "candidates": [cand_dict],
            "exclusions": [excl_dict],
            "strategy": "dense",
        }
        explain = ExplainBlock.model_validate(explain_dict)
        assert explain.candidates[0].permission_resolved_at is None


# ---------------------------------------------------------------------------
# 6. Version history docstring present
# ---------------------------------------------------------------------------


class TestVersionHistoryDocstring:
    def test_module_docstring_mentions_120(self) -> None:
        """The module docstring must mention 1.2.0 in the version history."""
        import finecorpus.contracts.retrieval_response as mod

        doc = mod.__doc__ or ""
        assert "1.2.0" in doc, "Module docstring must mention 1.2.0 in version history"

    def test_module_docstring_mentions_break_glass_read_ref(self) -> None:
        import finecorpus.contracts.retrieval_response as mod

        doc = mod.__doc__ or ""
        assert "break_glass_read_ref" in doc

    def test_module_docstring_mentions_rate_limited(self) -> None:
        import finecorpus.contracts.retrieval_response as mod

        doc = mod.__doc__ or ""
        assert "RATE_LIMITED" in doc
