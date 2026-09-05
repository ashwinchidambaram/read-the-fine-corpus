"""FakeLLMProvider — deterministic test-only internal LLM provider.

**FOR TESTS ONLY.**  Must never appear in production configuration.

Design
------
- Deterministic: outputs are pure functions of sha256(system + user + schema_name).
  Same input always produces the same output, across processes and interpreter
  restarts.  No randomness.
- Zero network: no HTTP calls, no filesystem access, no external dependencies.
- Schema-aware: produces structurally valid JSON for ClassificationOutput,
  AugmentationOutput, QuestionGenOutput, and RewriteOutput based on the
  requested schema name.
- Failure injection: ``fail_on_generate`` raises ``LLMProviderUnavailableError``
  on every generate_json call; ``fail_on_health`` similarly.
  Used by unit tests to exercise §15 failure-mode paths.
- Provenance: output fields include a ``[fake:{digest[:8]}]`` suffix so test
  assertions can distinguish real from fake provider output.

Why this is a hard requirement
-------------------------------
The FakeLLMProvider is what makes downstream T-04 / preview tests CI-runnable
without live API keys.  Determinism is required so that snapshot-based tests
are stable across runs.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Any

from pydantic import BaseModel

from finecorpus.llm.base import (
    LLMCostEstimate,
    LLMHealthCheckResult,
    LLMProvider,
    LLMProviderCapabilities,
    LLMProviderUnavailableError,
    LLMRawResult,
)

# Fixed probe string (health check only — carries no document content)
_PROBE = "finecorpus-llm-health-probe-v1"

# Approximate chars per token
_CHARS_PER_TOKEN_APPROX = 4


def _digest(payload: str) -> str:
    """Return sha256 hex digest of *payload* (UTF-8 encoded)."""
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _make_payload(system: str, user: str, schema_name: str) -> str:
    """Canonical payload for deterministic output derivation."""
    return f"{schema_name}|{system}|{user}"


class FakeLLMProvider(LLMProvider):
    """Deterministic test-only internal LLM provider.

    **FOR TESTS ONLY.**  Not suitable for production use.

    Parameters
    ----------
    model_id:
        Model identifier to declare (default: ``"fake-llm-v1"``).
    fail_on_generate:
        When ``True``, every ``generate_json`` call raises
        ``LLMProviderUnavailableError``.
    fail_on_health:
        When ``True``, ``health_check`` returns ``reachable=False``.
    provider_id:
        Provider identifier (default: ``"fake"``).
    """

    def __init__(
        self,
        *,
        model_id: str = "fake-llm-v1",
        fail_on_generate: bool = False,
        fail_on_health: bool = False,
        provider_id: str = "fake",
    ) -> None:
        self._model_id = model_id
        self._fail_on_generate = fail_on_generate
        self._fail_on_health = fail_on_health
        self._provider_id = provider_id
        self._caps = LLMProviderCapabilities(
            provider_id=provider_id,
            model_id=model_id,
            supports_json_schema=True,
            is_local=True,
            max_output_tokens=4096,
            cost_per_1k_input_tokens=None,
            cost_per_1k_output_tokens=None,
            pricing_as_of=None,
            api_version="fake-llm-v1",
        )

    @property
    def capabilities(self) -> LLMProviderCapabilities:
        return self._caps

    def generate_json(
        self,
        system: str,
        user: str,
        schema: type[BaseModel],
        temperature: float,
        max_output_tokens: int,
    ) -> LLMRawResult:
        """Return deterministic JSON derived from sha256 of the input payload.

        The output is structurally valid for the requested schema.  All string
        fields include a ``[fake:{digest[:8]}]`` tag so tests can identify
        fake output.

        Raises
        ------
        LLMProviderUnavailableError
            If ``fail_on_generate=True``.
        """
        if self._fail_on_generate:
            raise LLMProviderUnavailableError(
                f"FakeLLMProvider '{self._provider_id}' is configured to fail on generate.",
                provider_id=self._provider_id,
                model_id=self._model_id,
                attempts=1,
                http_status=503,
            )

        schema_name = schema.__name__
        payload = _make_payload(system, user, schema_name)
        digest = _digest(payload)
        tag = f"[fake:{digest[:8]}]"

        raw_json = _build_json_for_schema(schema_name, digest, tag)

        # Approximate token counts
        total_chars = len(system) + len(user)
        input_tokens = max(1, total_chars // _CHARS_PER_TOKEN_APPROX)
        output_tokens = max(1, len(raw_json) // _CHARS_PER_TOKEN_APPROX)

        return LLMRawResult(
            raw_json=raw_json,
            model_id=self._model_id,
            input_tokens_used=input_tokens,
            output_tokens_used=output_tokens,
            provider_id=self._provider_id,
        )

    def health_check(self) -> LLMHealthCheckResult:
        """Return a healthy result, or ``reachable=False`` if ``fail_on_health=True``."""
        if self._fail_on_health:
            return LLMHealthCheckResult(
                reachable=False,
                model_available=False,
                latency_ms=0.0,
                error=(
                    f"FakeLLMProvider '{self._provider_id}' is configured to fail on health check."
                ),
            )
        return LLMHealthCheckResult(
            reachable=True,
            model_available=True,
            latency_ms=0.1,
            error=None,
        )

    def estimate_cost(self, requests: list[dict[str, Any]]) -> LLMCostEstimate:
        """Estimate cost — always zero for a fake/local provider."""
        total_chars = sum(len(r.get("system", "")) + len(r.get("user", "")) for r in requests)
        tokens = max(0, total_chars // _CHARS_PER_TOKEN_APPROX)
        return LLMCostEstimate(
            estimated_input_tokens=tokens,
            estimated_output_tokens=tokens // 4,  # rough output estimate
            estimated_cost_usd=Decimal("0.0"),
            basis="FakeLLMProvider: local/test provider with no real cost.",
            is_exact=False,
        )


# ---------------------------------------------------------------------------
# Schema-specific JSON builders
# ---------------------------------------------------------------------------


def _build_json_for_schema(schema_name: str, digest: str, tag: str) -> str:
    """Build a structurally valid JSON response for the given schema name.

    Outputs are deterministic: all string fields are derived from *digest*
    and tagged with *tag*.  Numeric fields use fixed or digest-derived values.
    """
    # Use digest bytes as a stable source of "random" choices
    byte0 = int(digest[0:2], 16)
    byte1 = int(digest[2:4], 16)
    float_val = round((byte0 / 255.0) * 0.6 + 0.2, 2)  # 0.2–0.8 range

    if schema_name == "ClassificationOutput":
        segment_types = [
            "prose",
            "table",
            "code",
            "figure_caption",
            "list",
            "heading",
            "footnote",
            "metadata",
            "scanned_region",
            "other",
        ]
        salience_tiers = ["primary", "supporting", "boilerplate", "excluded"]
        data = {
            "segment_type": segment_types[byte0 % len(segment_types)],
            "salience_tier": salience_tiers[byte1 % len(salience_tiers)],
            "confidence": float_val,
            "reasoning": f"Fake classification reasoning {tag}",
        }

    elif schema_name == "AugmentationOutput":
        # Table descriptions are only generated for tables; use a conditional
        # based on digest to produce stable null / non-null output
        has_table_desc = (byte0 % 2) == 0
        data = {
            "natural_language_description": (
                f"Table describing columns and rows {tag}" if has_table_desc else None
            ),
            "breadcrumb_blurb": f"This segment appears under a section {tag}",
            "class_context_blurb": (
                f"Class context for this segment {tag}" if (byte1 % 2) == 0 else None
            ),
        }

    elif schema_name == "QuestionGenOutput":
        question_types = ["factual_lookup", "interpretive", "multi_document_synthesis"]
        data = {
            "questions": [
                {
                    "question_text": f"What is described in the segment {tag}?",
                    "question_type": question_types[byte0 % len(question_types)],
                    "source_segment_ids": [f"seg-{digest[:8]}"],
                    "generation_method": "llm_generated",
                    "review_status": "provisional",
                }
            ]
        }

    elif schema_name == "RewriteOutput":
        data = {
            "rewritten_text": f"Rewritten form of the segment {tag}",
            "diff_summary": f"Changed phrasing to improve retrievability {tag}",
        }

    else:
        # Fallback for unknown schemas: return an empty JSON object.
        # The caller's schema validation will fail, which is correct behaviour.
        data = {}

    return json.dumps(data)
