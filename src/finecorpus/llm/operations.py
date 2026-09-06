"""M-067 enforcement point: operation input/output schemas and run_operation.

Implements provider-abstraction.md §4.3 — each operation has a fixed input
schema and a fixed output schema.  Outputs are validated against the schema
before use.  A response that does not conform is treated as a provider error;
the operation is retried up to the configured limit (§15).

This module owns schema validation — the providers (openai_provider,
ollama_provider, fake) return raw JSON; this module calls
``Output.model_validate_json()`` to enforce the schema.

Operations implemented in Phase 3 (Phase 3 = augmentation + classification):
- ``augmentation`` — AugmentationInput / AugmentationOutput
- ``classification`` — ClassificationInput / ClassificationOutput

Operations deferred to later phases:
- ``question_generation`` — Phase 5 — raises ``OperationNotImplementedError``
- ``rewriting`` — Phase 7 — raises ``OperationNotImplementedError``

Prompt injection observations
------------------------------
Free-text output fields (``reasoning``, ``diff_summary``) are checked by
``prompts.check_for_injection_suspicion``.  Suspected injection is LOGGED
(never acted on).  The field content is stored for provenance and not
re-fed to any model (§14.1, provider-abstraction.md §4.4).
"""

from __future__ import annotations

import logging
from enum import StrEnum
from typing import Any, Literal, TypeVar

from pydantic import BaseModel, Field

from finecorpus.llm.base import (
    LLMProvider,
    LLMProviderError,
    LLMProviderUnavailableError,
)
from finecorpus.llm.prompts import (
    build_augmentation_prompt,
    build_classification_prompt,
    build_question_generation_prompt,
    check_for_injection_suspicion,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class SegmentType(StrEnum):
    """Segment types (see docs/architecture/segment-taxonomy.md)."""

    prose = "prose"
    table = "table"
    code = "code"
    figure_caption = "figure_caption"
    list = "list"
    heading = "heading"
    footnote = "footnote"
    metadata = "metadata"
    scanned_region = "scanned_region"
    other = "other"


class SalienceTier(StrEnum):
    """Salience tier values."""

    primary = "primary"
    supporting = "supporting"
    boilerplate = "boilerplate"
    excluded = "excluded"


class ContentType(StrEnum):
    """Content type for augmentation input."""

    table = "table"
    prose_paragraph = "prose_paragraph"
    list = "list"
    code = "code"
    figure_caption = "figure_caption"
    heading = "heading"
    footnote = "footnote"
    metadata = "metadata"
    scanned_region = "scanned_region"
    other = "other"


class QuestionType(StrEnum):
    """Question types for eval set generation."""

    factual_lookup = "factual_lookup"
    interpretive = "interpretive"
    multi_document_synthesis = "multi_document_synthesis"


# ---------------------------------------------------------------------------
# Classification schemas
# ---------------------------------------------------------------------------


class ClassificationInput(BaseModel):
    """Input schema for the classification operation (§4.3)."""

    segment_text: str = Field(description="Raw text of the segment.")
    document_context: dict[str, Any] = Field(
        description="Structural path, surrounding heading context, document type."
    )
    class_description: str | None = Field(
        default=None,
        description="User-supplied class description (§6.5), if available.",
    )


class ClassificationOutput(BaseModel):
    """Output schema for the classification operation (§4.3).

    Schema-validated before any downstream code touches it.
    """

    segment_type: SegmentType
    salience_tier: SalienceTier
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str = Field(description="One sentence. Retained in provenance.")


# ---------------------------------------------------------------------------
# Augmentation schemas
# ---------------------------------------------------------------------------


class AugmentationInput(BaseModel):
    """Input schema for the augmentation operation (§4.3)."""

    content: str = Field(description="Verbatim segment text.")
    structural_path: list[str] = Field(
        description="Ordered heading breadcrumb.",
        default_factory=list,
    )
    class_description: str | None = Field(default=None)
    content_type: ContentType = Field(description="Segment content type.")


class AugmentationOutput(BaseModel):
    """Output schema for the augmentation operation (§4.3).

    None of these fields replace or modify ``content`` (Tier 2 behaviour, §7.2).
    """

    natural_language_description: str | None = Field(
        default=None,
        description=(
            "For tables: description of subject, columns, key findings. For other types: null."
        ),
    )
    breadcrumb_blurb: str | None = Field(
        default=None,
        description=(
            "Sentence contextualising this chunk within the heading structure, "
            "for use in augmented embedding."
        ),
    )
    class_context_blurb: str | None = Field(
        default=None,
        description=(
            "Derived from the class description; placed in the embedding input "
            "but not returned to callers."
        ),
    )


# ---------------------------------------------------------------------------
# Question generation schemas (Phase 5 — operations raise OperationNotImplementedError)
# ---------------------------------------------------------------------------


class QuestionGenSegment(BaseModel):
    """A single segment passed to the question-generation operation (§4.3).

    Typed replacement for ``dict[str, Any]`` — field names match the segment
    contract defined in provider-abstraction.md §4.3.  The operation remains
    a loud Phase-5 stub; this model exists so the interface is fully typed.
    """

    segment_text: str = Field(description="Raw text of the segment.")
    segment_type: SegmentType | None = Field(
        default=None,
        description="Segment type from the classification operation.",
    )
    structural_path: list[str] = Field(
        default_factory=list,
        description="Ordered heading breadcrumb (§4.3).",
    )
    source_document_id: str = Field(description="Stable document ID for provenance.")
    segment_id: str | None = Field(
        default=None,
        description=(
            "Stable segment-level ID for provenance.  When set, "
            "``_segment_id()`` in generation.py uses this value for "
            "``source_segment_ids`` / ``expected_segment_ids`` on each "
            "EvalQuestion; without it the fallback is ``source_document_id``, "
            "which makes eval scoring semantically meaningless (§19 acceptance "
            "criteria require segment-level granularity)."
        ),
    )


class _QuestionItem(BaseModel):
    question_text: str
    question_type: QuestionType
    source_segment_ids: list[str]
    generation_method: Literal["llm_generated"]
    review_status: Literal["provisional"]


class QuestionGenInput(BaseModel):
    """Input schema for the question generation operation (§4.3)."""

    segments: list[QuestionGenSegment]
    class_description: str | None = None
    question_types: list[QuestionType]
    count_per_type: int


class QuestionGenOutput(BaseModel):
    """Output schema for the question generation operation (§4.3)."""

    questions: list[_QuestionItem]


# ---------------------------------------------------------------------------
# Rewriting schemas (Phase 7 — operations raise OperationNotImplementedError)
# ---------------------------------------------------------------------------


class RewriteInput(BaseModel):
    """Input schema for the Tier 3 rewriting operation (§4.3)."""

    original_text: str
    rewrite_instructions: str = Field(description="Platform-generated text; NOT user free-text.")
    class_description: str | None = None


class RewriteOutput(BaseModel):
    """Output schema for the Tier 3 rewriting operation (§4.3)."""

    rewritten_text: str
    diff_summary: str


# ---------------------------------------------------------------------------
# Deferred-operation sentinel
# ---------------------------------------------------------------------------


class OperationNotImplementedError(NotImplementedError):
    """Raised by deferred operations (Phase 5 / Phase 7).

    The caller MUST log this and treat it as a non-fatal skip for the
    current document / segment — not a class-wide halt.
    """

    def __init__(self, operation: str, available_in: str) -> None:
        super().__init__(
            f"Operation '{operation}' is not implemented in Phase 3. "
            f"It will be available in {available_in}.  "
            f"Raise a GitHub issue if you need this operation sooner."
        )
        self.operation = operation
        self.available_in = available_in


# ---------------------------------------------------------------------------
# Op-config type (minimal; registry resolves full config before calling here)
# ---------------------------------------------------------------------------


class ResolvedOpConfig(BaseModel):
    """Fully-resolved per-operation config (provider, model, temperature, etc.).

    Constructed by the registry after applying operation-specific overrides
    over the internal_llm default block.
    """

    provider_id: str
    model_id: str
    temperature: float
    max_output_tokens: int
    max_retries: int = 3


# ---------------------------------------------------------------------------
# run_operation — the M-067 enforcement entry point
# ---------------------------------------------------------------------------


def run_operation(
    provider: LLMProvider,
    op_config: ResolvedOpConfig,
    input_model: ClassificationInput | AugmentationInput,
) -> ClassificationOutput | AugmentationOutput:
    """Execute an LLM operation with schema validation and bounded retry.

    This is the ONLY path through which the platform makes internal LLM calls
    for classification and augmentation.  It enforces M-067: corpus content is
    treated as data (via prompts.py), and outputs are schema-validated before
    any downstream code touches them.

    Parameters
    ----------
    provider:
        A concrete ``LLMProvider`` — typically built by ``registry.py``.
    op_config:
        Fully-resolved operation config (temperature, max_output_tokens, etc.).
    input_model:
        One of ``ClassificationInput`` or ``AugmentationInput``.

    Returns
    -------
    ClassificationOutput | AugmentationOutput
        Schema-validated output.

    Raises
    ------
    LLMProviderUnavailableError
        After ``op_config.max_retries`` failed attempts.
    LLMProviderError
        Non-retryable provider error.
    TypeError
        If ``input_model`` is not a recognised operation type.
    """
    if isinstance(input_model, ClassificationInput):
        return _run_classification(provider, op_config, input_model)
    elif isinstance(input_model, AugmentationInput):
        return _run_augmentation(provider, op_config, input_model)
    else:
        raise TypeError(
            f"run_operation received an unsupported input type: {type(input_model).__name__}. "
            f"Expected ClassificationInput or AugmentationInput."
        )


def run_question_generation(
    provider: LLMProvider,
    op_config: ResolvedOpConfig,
    input_model: QuestionGenInput,
) -> QuestionGenOutput:
    """Execute the question-generation operation (Phase 5 — M-067 enforcement point).

    Builds prompts via ``prompts.build_question_generation_prompt``, dispatches
    to the provider, and validates the response schema.  Segment text is framed
    as data (M-067): placed inside ``<document_content>`` delimiters in the
    user message and never in the system message instruction area.

    Parameters
    ----------
    provider:
        A concrete ``LLMProvider``.
    op_config:
        Fully-resolved operation config (temperature, max_output_tokens, etc.).
    input_model:
        ``QuestionGenInput`` containing segments, class_description,
        question_types, and count_per_type.

    Returns
    -------
    QuestionGenOutput
        Schema-validated output; every question has
        ``generation_method="llm_generated"`` and ``review_status="provisional"``
        (enforced by the ``_QuestionItem`` schema).

    Raises
    ------
    LLMProviderUnavailableError
        After ``op_config.max_retries`` failed attempts (schema-validation
        failure counts as a provider error per §4.3).
    LLMProviderError
        Non-retryable provider error.
    """
    system, user = build_question_generation_prompt(
        segments=input_model.segments,
        class_description=input_model.class_description,
        question_types=[qt.value for qt in input_model.question_types],
        count_per_type=input_model.count_per_type,
    )

    output = _call_with_retry(
        provider=provider,
        op_config=op_config,
        system=system,
        user=user,
        schema=QuestionGenOutput,
        operation_name="question_generation",
    )

    # §14.1: check free-text output fields for injection-shaped content; log only.
    for item in output.questions:
        if check_for_injection_suspicion(item.question_text):
            logger.warning(
                "LLM security observation: question_generation 'question_text' field "
                "contains injection-shaped content. Stored for provenance; "
                "NOT re-fed to model. provider=%s model=%s",
                op_config.provider_id,
                op_config.model_id,
            )

    return output


def run_rewriting(
    provider: LLMProvider,
    op_config: ResolvedOpConfig,
    input_model: RewriteInput,
) -> RewriteOutput:
    """Tier 3 rewriting — raises OperationNotImplementedError (Phase 7).

    Loud stub: callers that attempt this in Phase 3 get a clear error message
    with the target phase, not a silent no-op.
    """
    raise OperationNotImplementedError("rewriting", available_in="Phase 7")


# ---------------------------------------------------------------------------
# Internal: classification
# ---------------------------------------------------------------------------


def _run_classification(
    provider: LLMProvider,
    op_config: ResolvedOpConfig,
    inp: ClassificationInput,
) -> ClassificationOutput:
    system, user = build_classification_prompt(
        segment_text=inp.segment_text,
        document_context=inp.document_context,
        class_description=inp.class_description,
    )
    # _call_with_retry validates exactly once and returns the validated model.
    output = _call_with_retry(
        provider=provider,
        op_config=op_config,
        system=system,
        user=user,
        schema=ClassificationOutput,
        operation_name="classification",
    )

    # §14.1: check reasoning for injection-shaped content; log but never act.
    if check_for_injection_suspicion(output.reasoning):
        logger.warning(
            "LLM security observation: classification 'reasoning' field contains "
            "injection-shaped content. Stored for provenance; NOT re-fed to model. "
            "provider=%s model=%s",
            op_config.provider_id,
            op_config.model_id,
        )

    return output


# ---------------------------------------------------------------------------
# Internal: augmentation
# ---------------------------------------------------------------------------


def _run_augmentation(
    provider: LLMProvider,
    op_config: ResolvedOpConfig,
    inp: AugmentationInput,
) -> AugmentationOutput:
    system, user = build_augmentation_prompt(
        content=inp.content,
        structural_path=inp.structural_path,
        class_description=inp.class_description,
        content_type=inp.content_type.value,
    )
    # _call_with_retry validates exactly once and returns the validated model.
    output = _call_with_retry(
        provider=provider,
        op_config=op_config,
        system=system,
        user=user,
        schema=AugmentationOutput,
        operation_name="augmentation",
    )

    # §14.1: check free-text fields for injection suspicion; log only.
    for field_name, field_val in [
        ("natural_language_description", output.natural_language_description),
        ("breadcrumb_blurb", output.breadcrumb_blurb),
        ("class_context_blurb", output.class_context_blurb),
    ]:
        if field_val and check_for_injection_suspicion(field_val):
            logger.warning(
                "LLM security observation: augmentation '%s' field contains "
                "injection-shaped content. Stored for provenance; NOT re-fed to model. "
                "provider=%s model=%s",
                field_name,
                op_config.provider_id,
                op_config.model_id,
            )

    return output


# ---------------------------------------------------------------------------
# Internal: retry wrapper (schema-validation failure = provider error)
# ---------------------------------------------------------------------------


_M = TypeVar("_M", bound=BaseModel)  # noqa: PYI018


def _call_with_retry(  # noqa: UP047
    provider: LLMProvider,
    op_config: ResolvedOpConfig,
    system: str,
    user: str,
    schema: type[_M],
    operation_name: str,
) -> _M:
    """Call provider.generate_json with bounded retry, returning the validated model.

    Validation happens exactly once per successful call — here, inside this
    function.  The validated model instance is returned directly to callers;
    callers MUST NOT re-validate (RULING 3).

    Schema-validation failure is treated as a provider error (§4.3): "a response
    that does not conform to the output schema is treated as a provider error;
    the operation is retried up to the configured limit."

    After ``op_config.max_retries`` failures, raises ``LLMProviderUnavailableError``
    so the pipeline can follow §15 single-document-failure semantics.
    """
    last_error: Exception | None = None

    for attempt in range(op_config.max_retries):
        try:
            result = provider.generate_json(
                system=system,
                user=user,
                schema=schema,
                temperature=op_config.temperature,
                max_output_tokens=op_config.max_output_tokens,
            )
            # Validate schema exactly once — failure counts as provider error per §4.3.
            try:
                validated = schema.model_validate_json(result.raw_json)
            except Exception as validation_err:
                logger.warning(
                    "LLM output schema-validation failure on attempt %d/%d "
                    "for operation '%s' (provider=%s model=%s): %s",
                    attempt + 1,
                    op_config.max_retries,
                    operation_name,
                    op_config.provider_id,
                    op_config.model_id,
                    type(validation_err).__name__,
                )
                last_error = LLMProviderError(
                    f"Output schema-validation failed for operation '{operation_name}' "
                    f"(attempt {attempt + 1}/{op_config.max_retries}): "
                    f"{type(validation_err).__name__}",
                    provider_id=op_config.provider_id,
                    model_id=op_config.model_id,
                )
                continue  # retry

            return validated

        except (LLMProviderError, LLMProviderUnavailableError) as exc:
            last_error = exc
            logger.warning(
                "LLM provider error on attempt %d/%d for operation '%s' (provider=%s model=%s): %s",
                attempt + 1,
                op_config.max_retries,
                operation_name,
                op_config.provider_id,
                op_config.model_id,
                type(exc).__name__,
            )
            if isinstance(exc, LLMProviderUnavailableError):
                # Provider is down — no point retrying further
                raise

    # All retries exhausted
    raise LLMProviderUnavailableError(
        f"Operation '{operation_name}' failed after {op_config.max_retries} attempt(s). "
        f"Last error: {type(last_error).__name__ if last_error else 'unknown'}.",
        provider_id=op_config.provider_id,
        model_id=op_config.model_id,
        attempts=op_config.max_retries,
    )
