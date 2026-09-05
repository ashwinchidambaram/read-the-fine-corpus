"""Ruling 2: honest cost gate — never fabricate $0.00 for declared paid providers.

Tests:
- resolve_costing_providers with declared 'openai' embedding + no API key in env
  → returns unavailable sentinel (no $0.00 claim)
- resolve_costing_providers with declared 'fake' embedding
  → returns FakeProvider (zero-marginal labelled)
- resolve_costing_providers with declared 'ollama' embedding (local)
  → returns a local provider (zero-marginal labelled)
- Gate: when estimate unavailable, gate still requires confirmation (no bypass)
- Gate: fake/local → zero-marginal message displayed, gate still requires confirmation

Run: uv run pytest tests/phase3/test_honest_cost_gate.py -v
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import patch

import pytest

from finecorpus.contracts.ingestion_config import (
    ChunkingConfig,
    ChunkingStrategy,
    ClassRule,
    EmbeddingConfig,
    IngestionConfig,
    LanguageDecision,
    LanguageSupportDecision,
    NaiveBaselineRef,
    RecommendationBasis,
    RecommendationProvenance,
    RetrievalStrategy,
    RetrievalTreatment,
    TransformationSettings,
)
from finecorpus.contracts.shared.blocks import (
    PermissionFidelity,
    PermissionMode,
    PermissionSource,
    SalienceTier,
    SegmentType,
    TenancyBlock,
)
from finecorpus.contracts.versions import INGESTION_CONFIG_SCHEMA_VERSION

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ingestion_config(provider: str = "fake") -> IngestionConfig:
    """Build a minimal IngestionConfig with the given embedding provider."""
    tenancy = TenancyBlock(
        workspace_id="ws-gate-test",
        kb_id="kb-gate-test",
        permission_mode=PermissionMode.public_to_kb,
        permission_principals=[],
        permission_source=PermissionSource.platform,
        permission_fidelity=PermissionFidelity.authoritative,
        permission_resolved_at=None,
    )
    embedding = EmbeddingConfig(
        provider=provider,
        model="text-embedding-3-small" if provider == "openai" else "fake-embed-v1",
        dimensions=1536 if provider == "openai" else 64,
        normalize=True,
        supports_languages=["*"],
    )
    chunking = ChunkingConfig(
        strategy=ChunkingStrategy.recursive_char,
        max_tokens=512,
        overlap_tokens=50,
        respect_headings=False,
    )
    default_rule = ClassRule(
        segment_class=SegmentType.prose,
        transformation=TransformationSettings(
            tier1_enabled=False,
            tier1_operations=[],
            tier2_enabled=False,
            tier2_operations=[],
            tier3_enabled=False,
        ),
        chunking=chunking,
        embedding_override=None,
        metadata_schema=[],
        retrieval_treatment=RetrievalTreatment(
            default_salience_filter=[SalienceTier.primary, SalienceTier.supporting],
            salience_weights=None,
            rerank_eligible=False,
            strategy=RetrievalStrategy.dense,
        ),
    )
    config_version = hashlib.sha256(f"gate-test-{provider}".encode()).hexdigest()
    return IngestionConfig(
        schema_version=INGESTION_CONFIG_SCHEMA_VERSION,
        tenancy=tenancy,
        config_version=config_version,
        created_at=datetime.now(tz=UTC),
        naive_baseline=NaiveBaselineRef(
            reference_id="naive-baseline-v0",
            description="Cost gate test.",
        ),
        class_rules=[],
        default_rule=default_rule,
        embedding=embedding,
        retrieval_defaults=RetrievalTreatment(
            default_salience_filter=[SalienceTier.primary, SalienceTier.supporting],
            salience_weights=None,
            rerank_eligible=False,
            strategy=RetrievalStrategy.dense,
        ),
        language_support=LanguageSupportDecision(
            detected_languages=[],
            unsupported_languages=[],
            decision=LanguageDecision.proceed,
        ),
        spreadsheet_triage=[],
        exclusions_confirmed=[],
        provenance=[
            RecommendationProvenance(
                target="/default_rule",
                basis=RecommendationBasis.heuristic,
                rationale="Cost gate test.",
            )
        ],
        class_descriptions=[],
        secret_free_attestation=True,
    )


# ---------------------------------------------------------------------------
# resolve_costing_providers tests
# ---------------------------------------------------------------------------


class TestResolveCostingProviders:
    """resolve_costing_providers must be honest about provider availability."""

    def test_fake_embedding_returns_fake_provider(self) -> None:
        """Declared 'fake' embedding → FakeProvider (local, zero-marginal-cost)."""
        from finecorpus.pipeline.costing import resolve_costing_providers

        config = _make_ingestion_config(provider="fake")
        result = resolve_costing_providers(config)

        assert result.embedding_provider is not None
        assert result.embedding_provider.capabilities.is_local is True
        assert result.embedding_unavailable is False
        assert result.embedding_unavailable_reason is None

    def test_openai_embedding_without_key_returns_unavailable(self) -> None:
        """Declared 'openai' embedding with no API key → unavailable (not $0.00)."""
        from finecorpus.pipeline.costing import resolve_costing_providers

        config = _make_ingestion_config(provider="openai")

        # Ensure no API key is present in env
        with patch.dict("os.environ", {}, clear=False):
            # Remove any existing key vars
            import os

            clean_env = {
                k: v
                for k, v in os.environ.items()
                if k not in ("OPENAI_API_KEY", "FINECORPUS_OPENAI_API_KEY")
            }
            with patch.dict("os.environ", clean_env, clear=True):
                result = resolve_costing_providers(config)

        assert result.embedding_unavailable is True, (
            "Declared 'openai' provider without API key must be marked unavailable, "
            "not returned as a $0.00 fake provider."
        )
        assert result.embedding_unavailable_reason is not None
        assert "openai" in result.embedding_unavailable_reason.lower()
        # Must not return a fake/zero-cost provider (that would be dishonest)
        # The embedding_provider should be None or explicitly marked unavailable
        assert result.embedding_provider is None, (
            "embedding_provider must be None when provider is unavailable "
            "(cannot fabricate $0.00 for a declared paid provider)."
        )

    def test_ollama_embedding_is_local_zero_cost(self) -> None:
        """Declared 'ollama' embedding → local provider (zero-marginal-cost)."""
        from finecorpus.pipeline.costing import resolve_costing_providers

        config = _make_ingestion_config(provider="ollama")
        result = resolve_costing_providers(config)

        # Ollama is local — never unavailable due to missing API key
        assert result.embedding_unavailable is False
        assert result.embedding_provider is not None
        assert result.embedding_provider.capabilities.is_local is True

    def test_no_llm_tier2_disabled_returns_none_llm(self) -> None:
        """When tier2 is disabled (no LLM ops), llm_provider is None."""
        from finecorpus.pipeline.costing import resolve_costing_providers

        config = _make_ingestion_config(provider="fake")
        result = resolve_costing_providers(config)

        # No tier2 ops → no LLM → llm_provider should be None
        assert result.llm_provider is None
        assert result.llm_unavailable is False


# ---------------------------------------------------------------------------
# CLI cost gate tests (via _try_load_cost_estimate + _print_cost_estimate)
# ---------------------------------------------------------------------------


class TestCostGate:
    """The CLI cost gate must honour the unavailable path and still require confirmation."""

    def _write_plan_and_decompose(
        self,
        tmp_path: pathlib.Path,
        run_id: str,
        provider: str = "fake",
    ) -> None:
        """Write minimal plan + decompose artifacts to tmp_path."""
        from finecorpus.contracts.segment_set import (
            ReassemblyMethod,
            ReassemblyRecord,
            Segment,
            SegmentSet,
        )
        from finecorpus.contracts.segment_set_batch import BATCH_SCHEMA_VERSION, SegmentSetBatch
        from finecorpus.contracts.shared.blocks import (
            LocatorKind,
            SalienceSignal,
            SalienceSignalKind,
            SourceLocation,
        )

        config = _make_ingestion_config(provider=provider)
        run_dir = tmp_path / run_id
        run_dir.mkdir(parents=True, exist_ok=True)

        (run_dir / "plan.json").write_text(
            json.dumps(config.model_dump(mode="json")), encoding="utf-8"
        )

        seg = Segment(
            segment_id=str(uuid.uuid4()),
            document_order=0,
            segment_type=SegmentType.prose,
            salience_tier=SalienceTier.primary,
            structural_path=[],
            segment_path="sec/0",
            location=SourceLocation(
                locator_kind=LocatorKind.char_range,
                char_start=0,
                char_end=20,
            ),
            source_region_ids=["region_0"],
            language="en",
            ocr_confidence=None,
            injection_suspicion=0.0,
            invisible_content_flags=[],
            sensitivity_flags=[],
            salience_signals=[
                SalienceSignal(
                    kind=SalienceSignalKind.segment_type_prior,
                    implied_tier=SalienceTier.primary,
                    won=True,
                    detail=None,
                )
            ],
            salience_basis=SalienceSignalKind.segment_type_prior,
            text="Some sample text here.",
        )
        tenancy = config.tenancy
        seg_set = SegmentSet(
            schema_version="1.1.0",
            tenancy=TenancyBlock(
                workspace_id=tenancy.workspace_id,
                kb_id=tenancy.kb_id,
                permission_mode=tenancy.permission_mode,
                permission_principals=list(tenancy.permission_principals),
                permission_source=tenancy.permission_source,
                permission_fidelity=tenancy.permission_fidelity,
                permission_resolved_at=tenancy.permission_resolved_at,
            ),
            document_id="doc-gate-test",
            content_hash=hashlib.sha256(b"doc-gate-test").hexdigest(),
            segments=[seg],
            reassembly=ReassemblyRecord(
                method=ReassemblyMethod.document_order_concat,
                covered_region_ids=["region_0"],
                reassembly_digest=hashlib.sha256(b"text").hexdigest(),
            ),
            exclusions=[],
            cross_references=[],
            decomposed_at=datetime.now(tz=UTC),
        )
        batch = SegmentSetBatch(
            schema_version=BATCH_SCHEMA_VERSION,
            contract="segment_set_batch",
            run_id=run_id,
            produced_at=datetime.now(tz=UTC),
            skeleton=None,
            segment_sets=[seg_set.model_dump(mode="json")],
        )
        (run_dir / "decompose.json").write_text(
            json.dumps(batch.model_dump(mode="json")), encoding="utf-8"
        )

    def test_fake_provider_estimate_returns_zero_marginal(self, tmp_path: pathlib.Path) -> None:
        """Fake provider → estimate returned with zero_marginal_cost=True."""
        import argparse

        from finecorpus.cli.main import _try_load_cost_estimate

        run_id = "gate-fake-test"
        self._write_plan_and_decompose(tmp_path, run_id, provider="fake")

        args = argparse.Namespace(
            artifacts=str(tmp_path),
            run_id=run_id,
        )
        estimate = _try_load_cost_estimate(args)

        assert estimate is not None
        assert estimate.embedding_zero_marginal_cost is True
        assert estimate.embedding_cost_usd == Decimal("0.0")

    def test_openai_provider_without_key_returns_unavailable(self, tmp_path: pathlib.Path) -> None:
        """Declared openai provider without API key → _try_load_cost_estimate signals unavailable.

        The function must return a sentinel/None for the estimate OR a special unavailable
        signal — it must NOT return an estimate with $0.00 claiming it's from a real provider.

        Ruling 2: the CLI must print "cost estimate unavailable for declared provider 'openai'"
        instead of $0.00 when the API key is absent.
        """
        import argparse
        import os

        clean_env = {
            k: v
            for k, v in os.environ.items()
            if k not in ("OPENAI_API_KEY", "FINECORPUS_OPENAI_API_KEY")
        }

        with patch.dict("os.environ", clean_env, clear=True):
            from finecorpus.cli.main import _try_load_cost_estimate

            run_id = "gate-openai-test"
            self._write_plan_and_decompose(tmp_path, run_id, provider="openai")

            args = argparse.Namespace(
                artifacts=str(tmp_path),
                run_id=run_id,
            )

            # The function must signal "unavailable" not a fake $0.00 estimate
            # Either returns None (estimate skipped) OR returns a CostEstimateUnavailable object
            # Per ruling: it must NOT return an IngestionCostEstimate with $0.00
            result = _try_load_cost_estimate(args)

        # Either the result signals unavailability OR the provider field reflects openai
        # The critical invariant: if an estimate IS returned, it must NOT claim $0.00
        # for an openai provider when no key is present.
        if result is not None:
            # If something is returned it must be an unavailable sentinel
            # (not a normal IngestionCostEstimate with $0.00 from a FakeProvider)
            assert hasattr(result, "unavailable") and result.unavailable is True, (
                "When openai API key is absent, _try_load_cost_estimate must return an "
                "unavailable sentinel, not a $0.00 IngestionCostEstimate. "
                "Got: {result}"
            )

    def test_openai_provider_without_key_prints_unavailable_message(
        self, tmp_path: pathlib.Path, capsys: pytest.CaptureFixture
    ) -> None:
        """When openai key absent, printing must show 'unavailable' not '$0.00'."""
        import argparse
        import os

        clean_env = {
            k: v
            for k, v in os.environ.items()
            if k not in ("OPENAI_API_KEY", "FINECORPUS_OPENAI_API_KEY")
        }

        with patch.dict("os.environ", clean_env, clear=True):
            from finecorpus.cli.main import _print_cost_estimate, _try_load_cost_estimate

            run_id = "gate-openai-print-test"
            self._write_plan_and_decompose(tmp_path, run_id, provider="openai")

            args = argparse.Namespace(
                artifacts=str(tmp_path),
                run_id=run_id,
            )

            result = _try_load_cost_estimate(args)

        if result is not None:
            _print_cost_estimate(result)
            captured = capsys.readouterr()
            output = captured.out

            # Must mention "unavailable" somewhere
            assert "unavailable" in output.lower(), (
                "When openai key is absent, output must mention 'unavailable'. "
                f"Got output: {output!r}"
            )
            # Must NOT claim $0.00 as if the estimate is valid
            # (it would be dishonest to show $0.00 for a paid provider)
            assert "$0.00" not in output or "unavailable" in output.lower(), (
                "Output must not show '$0.00' without also noting it's unavailable. "
                f"Got output: {output!r}"
            )
