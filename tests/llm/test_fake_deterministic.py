"""Tests: FakeLLMProvider determinism.

Requirement: same input → byte-identical output across calls and processes.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from finecorpus.llm.fake import FakeLLMProvider
from finecorpus.llm.operations import AugmentationOutput, ClassificationOutput


class TestFakeDeterminism:
    """Same input → same output, every time."""

    def test_same_classification_call_produces_identical_json(self) -> None:
        provider = FakeLLMProvider()
        system = "Classify this segment."
        user = "Some text."

        r1 = provider.generate_json(system, user, ClassificationOutput, 0.0, 1024)
        r2 = provider.generate_json(system, user, ClassificationOutput, 0.0, 1024)

        assert r1.raw_json == r2.raw_json

    def test_same_augmentation_call_produces_identical_json(self) -> None:
        provider = FakeLLMProvider()
        system = "Augment this."
        user = "Some corpus text."

        r1 = provider.generate_json(system, user, AugmentationOutput, 0.3, 512)
        r2 = provider.generate_json(system, user, AugmentationOutput, 0.3, 512)

        assert r1.raw_json == r2.raw_json

    def test_different_inputs_produce_different_json(self) -> None:
        provider = FakeLLMProvider()
        system = "Classify."
        user_a = "Input A"
        user_b = "Input B"

        r_a = provider.generate_json(system, user_a, ClassificationOutput, 0.0, 1024)
        r_b = provider.generate_json(system, user_b, ClassificationOutput, 0.0, 1024)

        assert r_a.raw_json != r_b.raw_json

    def test_different_schemas_produce_different_json(self) -> None:
        provider = FakeLLMProvider()
        system = "Do something."
        user = "Some text."

        r_cls = provider.generate_json(system, user, ClassificationOutput, 0.0, 1024)
        r_aug = provider.generate_json(system, user, AugmentationOutput, 0.0, 1024)

        assert r_cls.raw_json != r_aug.raw_json

    def test_classification_output_is_valid_json(self) -> None:
        provider = FakeLLMProvider()
        r = provider.generate_json("sys", "user", ClassificationOutput, 0.0, 1024)
        parsed = json.loads(r.raw_json)
        assert isinstance(parsed, dict)

    def test_classification_output_passes_schema_validation(self) -> None:
        provider = FakeLLMProvider()
        r = provider.generate_json("sys", "user", ClassificationOutput, 0.0, 1024)
        # Should not raise
        output = ClassificationOutput.model_validate_json(r.raw_json)
        assert output.segment_type is not None
        assert 0.0 <= output.confidence <= 1.0

    def test_augmentation_output_passes_schema_validation(self) -> None:
        provider = FakeLLMProvider()
        r = provider.generate_json("sys", "user", AugmentationOutput, 0.3, 512)
        output = AugmentationOutput.model_validate_json(r.raw_json)
        # breadcrumb_blurb is always non-null in fake output
        assert output.breadcrumb_blurb is not None

    def test_fake_tag_present_in_output(self) -> None:
        """Fake output fields include [fake:<digest>] tag for traceability."""
        provider = FakeLLMProvider()
        r = provider.generate_json("sys", "user", ClassificationOutput, 0.0, 1024)
        parsed = json.loads(r.raw_json)
        assert "[fake:" in parsed.get("reasoning", "")

    def test_model_id_echoed(self) -> None:
        provider = FakeLLMProvider(model_id="test-model-v1")
        r = provider.generate_json("sys", "user", ClassificationOutput, 0.0, 1024)
        assert r.model_id == "test-model-v1"

    def test_provider_id_echoed(self) -> None:
        provider = FakeLLMProvider(provider_id="test-fake")
        r = provider.generate_json("sys", "user", ClassificationOutput, 0.0, 1024)
        assert r.provider_id == "test-fake"

    def test_token_counts_are_positive(self) -> None:
        provider = FakeLLMProvider()
        r = provider.generate_json(
            "system message", "user message", ClassificationOutput, 0.0, 1024
        )
        assert r.input_tokens_used > 0
        assert r.output_tokens_used > 0

    def test_fail_on_generate_raises(self) -> None:
        from finecorpus.llm.base import LLMProviderUnavailableError

        provider = FakeLLMProvider(fail_on_generate=True)
        with pytest.raises(LLMProviderUnavailableError):
            provider.generate_json("sys", "user", ClassificationOutput, 0.0, 1024)

    def test_health_check_healthy(self) -> None:
        provider = FakeLLMProvider()
        result = provider.health_check()
        assert result.reachable is True
        assert result.model_available is True
        assert result.error is None

    def test_fail_on_health_returns_unhealthy(self) -> None:
        provider = FakeLLMProvider(fail_on_health=True)
        result = provider.health_check()
        assert result.reachable is False
        assert result.model_available is False
        assert result.error is not None


class TestFakeDeterminismAcrossProcesses:
    """Verify determinism across interpreter restarts using subprocess."""

    def test_cross_process_determinism_classification(self) -> None:
        """Two separate Python processes must produce byte-identical output."""
        script = """
import json, sys
from finecorpus.llm.fake import FakeLLMProvider
from finecorpus.llm.operations import ClassificationOutput

p = FakeLLMProvider()
r = p.generate_json("Classify this.", "Some corpus text.", ClassificationOutput, 0.0, 1024)
print(r.raw_json)
"""
        result1 = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            check=True,
            cwd="/Users/ashwinchidambaram/dev/projects/rtfc-worktrees/p3-llm",
        )
        result2 = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            check=True,
            cwd="/Users/ashwinchidambaram/dev/projects/rtfc-worktrees/p3-llm",
        )

        assert result1.stdout.strip() == result2.stdout.strip(), (
            "Cross-process output mismatch — FakeLLMProvider is not deterministic."
        )

    def test_cross_process_determinism_augmentation(self) -> None:
        script = """
import json, sys
from finecorpus.llm.fake import FakeLLMProvider
from finecorpus.llm.operations import AugmentationOutput

p = FakeLLMProvider()
r = p.generate_json("Augment this.", "Some corpus text.", AugmentationOutput, 0.3, 512)
print(r.raw_json)
"""
        result1 = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            check=True,
            cwd="/Users/ashwinchidambaram/dev/projects/rtfc-worktrees/p3-llm",
        )
        result2 = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            check=True,
            cwd="/Users/ashwinchidambaram/dev/projects/rtfc-worktrees/p3-llm",
        )

        assert result1.stdout.strip() == result2.stdout.strip()
