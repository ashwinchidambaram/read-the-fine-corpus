"""Tests for M-041: language warning before ingestion for unsupported languages.

- Batch with segments in a language the provider doesn't declare → warned_proceed
  with language named.
- All-supported → proceed.
- Universal coverage ("*") → proceed regardless of languages.
- Languages with share below threshold → proceed (not warned).
"""

from __future__ import annotations

from datetime import UTC, datetime

from finecorpus.contracts.ingestion_config import (
    EmbeddingConfig,
    LanguageDecision,
)
from finecorpus.pipeline.plan.corpus_stats import ClassStats, CorpusStats, TokenLengthStats
from finecorpus.pipeline.plan.stage import _compute_language_support

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_embedding(supports: list[str] | str | None = None) -> EmbeddingConfig:
    return EmbeddingConfig(
        provider="ollama",
        model="nomic-embed-text",
        dimensions=768,
        normalize=True,
        supports_languages=supports if isinstance(supports, (list, type(None))) else [supports],
    )


def _make_corpus_with_languages(lang_counts: dict[str, int]) -> CorpusStats:
    """Build a minimal CorpusStats with the given language → count mapping."""
    total = sum(lang_counts.values())
    by_class: dict[str, ClassStats] = {
        "prose": ClassStats(
            segment_type="prose",
            segment_count=total,
            token_lengths=TokenLengthStats(
                count=total,
                min=10,
                max=500,
                mean=200.0,
                median=180.0,
                p75=280.0,
                p90=320.0,
                p95=400.0,
            ),
            has_heading_structure=False,
            table_shapes=[],
            ocr=None,
            language_counts=dict(lang_counts),
            salience_tier_counts={"primary": total},
        )
    }
    return CorpusStats(
        by_class=by_class,
        total_segments=total,
        detected_languages=dict(lang_counts),
    )


def _make_fake_provider_caps(supported: list[str] | str):
    """Create a minimal ProviderCapabilities-like object for testing."""

    from finecorpus.embedding.base import ProviderCapabilities

    return ProviderCapabilities(
        provider_id="fake",
        model_id="fake-embed",
        vector_dimensions=128,
        max_input_tokens=512,
        max_batch_size=64,
        supported_languages=supported,
        cross_lingual=False,
        is_local=True,
        cost_per_1k_tokens=None,
        pricing_as_of=None,
        api_version="test",
    )


# ---------------------------------------------------------------------------
# Core M-041 tests
# ---------------------------------------------------------------------------


class TestLanguageWarning:
    """M-041: warn before ingestion when embedding model lacks a detected language."""

    def test_unsupported_language_with_meaningful_share_warns(self):
        """Corpus with meaningful French content, model only supports English → warned_proceed."""
        corpus = _make_corpus_with_languages({"en": 80, "fr": 20})
        embedding = _make_embedding(["en"])
        result = _compute_language_support(corpus, embedding, None)
        assert result.decision == LanguageDecision.warned_proceed
        assert "fr" in result.unsupported_languages

    def test_unsupported_language_named_in_decision(self):
        """The unsupported language must be named explicitly (M-041)."""
        corpus = _make_corpus_with_languages({"en": 60, "de": 40})
        embedding = _make_embedding(["en"])
        result = _compute_language_support(corpus, embedding, None)
        assert "de" in result.unsupported_languages

    def test_multiple_unsupported_languages_all_named(self):
        """All unsupported languages with meaningful share must be named."""
        corpus = _make_corpus_with_languages({"en": 50, "fr": 30, "de": 20})
        embedding = _make_embedding(["en"])
        result = _compute_language_support(corpus, embedding, None)
        assert result.decision == LanguageDecision.warned_proceed
        assert "fr" in result.unsupported_languages
        assert "de" in result.unsupported_languages

    def test_all_supported_languages_proceed(self):
        """All detected languages are supported → proceed without warning."""
        corpus = _make_corpus_with_languages({"en": 80, "fr": 20})
        embedding = _make_embedding(["en", "fr"])
        result = _compute_language_support(corpus, embedding, None)
        assert result.decision == LanguageDecision.proceed
        assert result.unsupported_languages == []

    def test_universal_coverage_star_proceeds(self):
        """Model with supported_languages='*' → all languages supported, no warning."""
        corpus = _make_corpus_with_languages({"en": 50, "zh": 50})
        embedding = _make_embedding("*")
        # Need to pass a list for the EmbeddingConfig
        embedding = EmbeddingConfig(
            provider="fake",
            model="multilingual",
            dimensions=768,
            normalize=True,
            supports_languages=None,  # None means unknown
        )
        # Use provider caps with "*" support
        caps = _make_fake_provider_caps("*")
        result = _compute_language_support(corpus, embedding, caps)
        assert result.decision == LanguageDecision.proceed

    def test_provider_caps_star_proceeds(self):
        """ProviderCapabilities with supported_languages='*' → no warning."""
        corpus = _make_corpus_with_languages({"en": 30, "ja": 70})
        embedding = _make_embedding(["en"])  # embedding says en-only
        caps = _make_fake_provider_caps("*")
        result = _compute_language_support(corpus, embedding, caps)
        assert result.decision == LanguageDecision.proceed

    def test_provider_caps_used_over_embedding_config(self):
        """When provider_capabilities is provided, it overrides embedding.supports_languages."""
        corpus = _make_corpus_with_languages({"en": 50, "fr": 50})
        embedding = _make_embedding(["en"])  # embedding says en-only
        # Provider caps say both languages supported
        caps = _make_fake_provider_caps(["en", "fr"])
        result = _compute_language_support(corpus, embedding, caps)
        assert result.decision == LanguageDecision.proceed

    def test_und_language_not_warned(self):
        """'und' (undetermined) segments are not included in the language support check."""
        corpus = _make_corpus_with_languages({"en": 90, "und": 10})
        embedding = _make_embedding(["en"])
        result = _compute_language_support(corpus, embedding, None)
        assert result.decision == LanguageDecision.proceed
        assert "und" not in result.unsupported_languages

    def test_unsupported_language_below_threshold_does_not_warn(self):
        """Unsupported language with < 2% share does not trigger warning."""
        # 1% share → below the 2% threshold
        corpus = _make_corpus_with_languages({"en": 99, "fr": 1})
        embedding = _make_embedding(["en"])
        result = _compute_language_support(corpus, embedding, None)
        # fr is at 1/100 = 1%, below the 2% threshold
        assert result.decision == LanguageDecision.proceed

    def test_empty_corpus_proceeds(self):
        """Empty corpus → no language data → proceed."""
        corpus = CorpusStats(by_class={}, total_segments=0, detected_languages={})
        embedding = _make_embedding(["en"])
        result = _compute_language_support(corpus, embedding, None)
        assert result.decision == LanguageDecision.proceed

    def test_detected_languages_list_populated(self):
        """detected_languages list is populated with actual corpus language shares."""
        corpus = _make_corpus_with_languages({"en": 80, "fr": 20})
        embedding = _make_embedding(["en", "fr"])
        result = _compute_language_support(corpus, embedding, None)
        langs = {ls.language for ls in result.detected_languages}
        assert "en" in langs
        assert "fr" in langs


# ---------------------------------------------------------------------------
# RULING 2 (M-041 fails-open): capabilities with None supported_languages
# ---------------------------------------------------------------------------


class TestLanguageWarningNoneCapabilities:
    """RULING 2: capabilities.supported_languages=None must NOT silently proceed.

    Spec §7.6: when capabilities is provided but its supported_languages is None,
    fall back to embedding.supports_languages. If that fallback also gives no
    language list (None/empty), emit warned_proceed with a client-readable message
    stating the provider declares no supported languages so support could not be
    verified.

    When the fallback list covers all detected languages, proceed via fallback.

    The tests that use a ProviderCapabilities object with supported_languages=None
    must fail before the Ruling 2 fix (current code hits `if declared is None: pass`
    → silently proceeds despite having non-English content).
    """

    def _make_caps_with_none_languages(self):
        """Build a ProviderCapabilities that reports supported_languages=None.

        ProviderCapabilities is a frozen dataclass typed list[str]|str (non-optional).
        We use object.__setattr__ to inject None to simulate a provider that omits
        this field or sets it to None at runtime.
        """
        from finecorpus.embedding.base import ProviderCapabilities

        caps = ProviderCapabilities(
            provider_id="fake-none",
            model_id="none-embed",
            vector_dimensions=128,
            max_input_tokens=512,
            max_batch_size=64,
            supported_languages=[],  # placeholder; overwritten below
            cross_lingual=False,
            is_local=True,
            cost_per_1k_tokens=None,
            pricing_as_of=None,
            api_version="test",
        )
        # Inject None to simulate a provider that does not declare supported languages
        object.__setattr__(caps, "supported_languages", None)
        return caps

    def test_caps_with_none_languages_and_no_embedding_fallback_warns(self):
        """capabilities.supported_languages=None + embedding.supports_languages=None
        + significant non-English → warned_proceed (must NOT silently proceed).

        This test FAILS against current code (hits `if declared is None: pass`).
        """
        corpus = _make_corpus_with_languages({"en": 60, "fr": 40})
        embedding = _make_embedding(None)  # embedding also declares no languages
        caps = self._make_caps_with_none_languages()

        result = _compute_language_support(corpus, embedding, caps)
        assert result.decision == LanguageDecision.warned_proceed, (
            "When provider_capabilities.supported_languages is None and "
            "embedding.supports_languages is also None/empty, the system must emit "
            "warned_proceed (not silently proceed). "
            f"Got: {result.decision}"
        )

    def test_caps_with_none_languages_fallback_list_covers_all_proceeds(self):
        """capabilities.supported_languages=None but embedding.supports_languages covers
        all detected languages → proceed via fallback (no warning).

        This test FAILS against current code (silently proceeds regardless of fallback
        because `if declared is None: pass` → no unsupported → proceed is correct by
        accident, but for the wrong reason — it ignores fr entirely).
        """
        corpus = _make_corpus_with_languages({"en": 70, "fr": 30})
        # embedding declares both languages → fallback covers corpus
        embedding = _make_embedding(["en", "fr"])
        caps = self._make_caps_with_none_languages()

        result = _compute_language_support(corpus, embedding, caps)
        assert result.decision == LanguageDecision.proceed, (
            "When caps.supported_languages is None but embedding.supports_languages "
            "covers all detected languages, proceed via fallback. "
            f"Got: {result.decision}"
        )

    def test_caps_with_none_languages_warns_names_unsupported(self):
        """When warned_proceed via None-capabilities path, unsupported_languages is populated."""
        corpus = _make_corpus_with_languages({"en": 50, "de": 50})
        embedding = _make_embedding(None)
        caps = self._make_caps_with_none_languages()

        result = _compute_language_support(corpus, embedding, caps)
        assert result.decision == LanguageDecision.warned_proceed
        assert len(result.unsupported_languages) > 0, (
            "warned_proceed must name the languages that could not be verified"
        )
        # 'de' must be named since neither caps nor embedding declares German support
        assert "de" in result.unsupported_languages


# ---------------------------------------------------------------------------
# Integration: PlanStage uses language decision correctly
# ---------------------------------------------------------------------------


class TestPlanStageLanguageDecision:
    """Integration test: PlanStage builds LanguageSupportDecision correctly."""

    def test_plan_stage_warned_proceed_when_unsupported_language(self):
        """Full PlanStage integration: unsupported language → warned_proceed in output."""
        from finecorpus.pipeline.plan.stage import PlanStage

        # Build a minimal SegmentSetBatch with French segments
        batch = {
            "schema_version": "1.0.0",
            "contract": "segment_set_batch",
            "run_id": "test-lang-warning",
            "produced_at": datetime.now(UTC).isoformat(),
            "segment_sets": [
                {
                    "schema_version": "1.1.0",
                    "document_id": "doc-001",
                    "content_hash": "abc123",
                    "tenancy": {
                        "workspace_id": "ws-test",
                        "kb_id": "kb-test",
                        "permission_mode": "public_to_kb",
                        "permission_principals": [],
                        "permission_source": "platform",
                        "permission_fidelity": "authoritative",
                    },
                    "segments": [
                        {
                            "segment_id": f"seg-{i:03d}",
                            "document_order": i,
                            "segment_type": "prose",
                            "salience_tier": "primary",
                            "structural_path": [],
                            "segment_path": f"prose/{i}",
                            "location": {
                                "locator_kind": "char_range",
                                "char_start": i * 100,
                                "char_end": i * 100 + 100,
                            },
                            "source_region_ids": [f"region-{i}"],
                            "language": "en" if i < 80 else "fr",  # 80% en, 20% fr
                            "ocr_confidence": None,
                            "injection_suspicion": 0.0,
                            "invisible_content_flags": [],
                            "sensitivity_flags": [],
                            "salience_signals": [
                                {
                                    "kind": "segment_type_prior",
                                    "implied_tier": "primary",
                                    "won": True,
                                    "detail": None,
                                }
                            ],
                            "salience_basis": "segment_type_prior",
                            "text": f"Text in {'English' if i < 80 else 'French'} segment {i}.",
                        }
                        for i in range(100)
                    ],
                    "reassembly": {
                        "method": "document_order_concat",
                        "covered_region_ids": [f"region-{i}" for i in range(100)],
                        "reassembly_digest": "fakedigest",
                    },
                    "exclusions": [],
                    "cross_references": [],
                    "decomposed_at": datetime.now(UTC).isoformat(),
                }
            ],
        }

        # PlanStage with default embedding (en-only nomic-embed-text)
        plan = PlanStage()
        result = plan._produce(batch)

        lang_support = result.get("language_support", {})
        assert lang_support.get("decision") == "warned_proceed", (
            "Expected warned_proceed when corpus has French but model only supports English"
        )
        assert "fr" in lang_support.get("unsupported_languages", []), (
            "French must be named in unsupported_languages"
        )
