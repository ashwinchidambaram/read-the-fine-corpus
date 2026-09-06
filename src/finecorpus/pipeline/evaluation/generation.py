"""Eval-set generation pipeline module (Phase 5 — M-043, D-22).

Design
------
``generate_eval_set`` is the single entry point for building an EvalSet from
corpus segments.  It stratifies question generation across all three
``QuestionType`` values (factual_lookup, interpretive, multi_document_synthesis)
and enforces:

M-043 — Provisional labelling is inescapable
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Every generated EvalSet has ``confidence_level=provisional``.  There is NO code
path in this module that produces a ``reviewed`` or ``production_derived``
confidence level.  The ``ConfidenceLevel.provisional`` assignment is
unconditional, not conditional on review_status of individual questions.

D-22 — Injection scan on generated question text
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Each generated ``question_text`` is scored for injection-shaped content using
``_compute_injection_score`` from ``pipeline.decompose.passes.injection``.
That scorer is chosen over ``llm.prompts.check_for_injection_suspicion`` because
it returns a numeric score (float in [0, 1]) that can be compared against the
configurable threshold ``assessment.eval_injection_suspicion_threshold``,
whereas the prompts helper returns only a bool.  The Phase-2 injection scorer is
therefore richer for D-22 purposes.

If a question's injection score exceeds the threshold:
- Its ``review_status`` is set to ``ReviewStatus.unreviewed`` (it already
  defaults to unreviewed for generated questions; the key point is it can NEVER
  be auto-upgraded to a reviewed status by generation code).
- The score is returned alongside the question as ``injection_suspicion_score``
  so PR-2's store can persist it in the control row.

Multi-document synthesis
~~~~~~~~~~~~~~~~~~~~~~~~
``multi_document_synthesis`` questions are generated from a segment sample that
spans at least two distinct ``source_document_id`` values.  If fewer than two
source documents are available in the corpus, synthesis generation is skipped
gracefully (no error) and a warning is emitted.

Provenance
~~~~~~~~~~
Each ``EvalQuestion`` records:
- ``source_segment_ids``: the segments that seeded the generation call.
- ``expected_segment_ids``: same as source_segment_ids for generated questions
  (the generating segments are the expected gold).  Not None.
- ``source_unknown=False``: provenance is always known for generated questions.
- ``class_description_ref``: the class description (§6.5) used, for auditability.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from finecorpus.config.models import Config
from finecorpus.contracts.eval_set import (
    ConfidenceLevel,
    EvalQuestion,
    EvalSet,
    EvalSetOrigin,
    GenerationMethod,
    QuestionType,
    ReviewStatus,
)
from finecorpus.contracts.shared.blocks import (
    PermissionFidelity,
    PermissionMode,
    PermissionSource,
    TenancyBlock,
)
from finecorpus.llm.base import LLMProvider
from finecorpus.llm.operations import (
    QuestionGenInput,
    QuestionGenSegment,
    ResolvedOpConfig,
    run_question_generation,
)
from finecorpus.pipeline.decompose.passes.injection import _compute_injection_score

logger = logging.getLogger(__name__)

# Contract version for generated eval sets.
_EVAL_SET_SCHEMA_VERSION = "1.0.0"

# Injection scorer choice for D-22:
# _compute_injection_score (pipeline.decompose.passes.injection) is used instead
# of check_for_injection_suspicion (llm.prompts) because it returns a numeric
# float in [0, 1] that can be compared against the configurable threshold
# assessment.eval_injection_suspicion_threshold.  The prompts helper returns only
# a bool, which is insufficient for threshold comparison.  The decompose scorer
# is the richer implementation for D-22 purposes.

# ---------------------------------------------------------------------------
# Dataclass for per-question output including suspicion score (D-22)
# ---------------------------------------------------------------------------


class GeneratedQuestionRecord:
    """Public holder for a generated question plus its D-22 suspicion score.

    Returned by ``generate_eval_set`` so that PR-2's eval_store can persist the
    ``injection_suspicion_score`` alongside the EvalQuestion control row.

    Previously named ``_GeneratedQuestion`` (private).  Renamed to a public name
    so downstream units (eval_store, PR-2) can import it without depending on an
    internal implementation detail.
    """

    __slots__ = ("question", "injection_suspicion_score")

    def __init__(self, question: EvalQuestion, injection_suspicion_score: float) -> None:
        self.question = question
        self.injection_suspicion_score = injection_suspicion_score


# Backward-compatible alias — remove once all call sites use GeneratedQuestionRecord.
_GeneratedQuestion = GeneratedQuestionRecord


# ---------------------------------------------------------------------------
# Generation method map — QuestionType → GenerationMethod
# ---------------------------------------------------------------------------

_TYPE_TO_GENERATION_METHOD: dict[QuestionType, GenerationMethod] = {
    QuestionType.factual_lookup: GenerationMethod.generated_factual,
    QuestionType.interpretive: GenerationMethod.generated_interpretive,
    QuestionType.multi_document_synthesis: GenerationMethod.generated_synthesis,
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def generate_eval_set(
    segments: list[Any],
    class_descriptions: dict[str, str] | str | None,
    *,
    llm_provider: LLMProvider,
    op_config: ResolvedOpConfig,
    config: Config,
    kb_id: str,
    workspace_id: str,
    count_per_type: int = 5,
) -> tuple[EvalSet, list[GeneratedQuestionRecord]]:
    """Generate an EvalSet from corpus segments, stratified by QuestionType.

    Parameters
    ----------
    segments:
        Corpus segments to generate questions from.  Each segment must expose
        at least ``segment_text``, ``source_document_id``, and optionally
        ``segment_id`` attributes (or dict keys via ``getattr``).
    class_descriptions:
        Class description(s) (§6.5) to contextualise generation.  Pass a single
        string for one class, or a mapping of ``{class_name: description}`` for
        multi-class corpora.  ``None`` is accepted (no class description).
    llm_provider:
        LLMProvider to dispatch question-generation calls.
    op_config:
        Fully-resolved per-operation config (model, temperature, retries).
    config:
        Platform config — used to read ``assessment.eval_injection_suspicion_threshold``.
    kb_id:
        Knowledge-base ULID for the TenancyBlock.
    workspace_id:
        Workspace ULID for the TenancyBlock.
    count_per_type:
        How many questions to generate per QuestionType.  Default 5.

    Returns
    -------
    (EvalSet, list[GeneratedQuestionRecord])
        The assembled EvalSet (confidence_level=provisional, origin=generated)
        plus a list of ``GeneratedQuestionRecord`` objects carrying the per-question
        injection_suspicion_score for PR-2's store.

    Notes
    -----
    M-043: The returned EvalSet has ``confidence_level=ConfidenceLevel.provisional``
    unconditionally.  There is NO code path in this function that returns a
    ``reviewed`` or ``production_derived`` confidence level for a freshly generated set.

    D-22: Questions whose ``question_text`` injection score exceeds
    ``assessment.eval_injection_suspicion_threshold`` keep ``review_status=unreviewed``.
    All generated questions start as unreviewed; this constraint prevents any
    generation-time auto-promotion to a reviewed status.

    Multi-class limitation: When ``class_descriptions`` is a ``dict``, only the
    FIRST value is forwarded to the LLM as the class description.  Phase 5 is
    scoped to single-class corpora; per-class question stratification is a future
    refinement.  Callers with multi-class corpora should invoke ``generate_eval_set``
    once per class, or accept that the first class description drives all questions.
    """
    threshold = config.assessment.eval_injection_suspicion_threshold
    now = datetime.now(tz=UTC)
    eval_set_id = str(uuid.uuid4())

    # Resolve class_description string for the LLM (use first value if mapping)
    if isinstance(class_descriptions, dict):
        # Use the first description if multiple classes present
        class_description: str | None = next(iter(class_descriptions.values()), None)
    else:
        class_description = class_descriptions

    # Build typed segment list for the operation
    typed_segments = _coerce_segments(segments)

    all_generated: list[GeneratedQuestionRecord] = []

    question_types = [
        QuestionType.factual_lookup,
        QuestionType.interpretive,
        QuestionType.multi_document_synthesis,
    ]

    for qtype in question_types:
        if qtype == QuestionType.multi_document_synthesis:
            segs_for_type = _select_multi_doc_segments(typed_segments)
            if segs_for_type is None:
                logger.warning(
                    "generate_eval_set: skipping multi_document_synthesis — "
                    "fewer than 2 distinct source_document_ids in corpus segments. "
                    "kb_id=%s workspace_id=%s",
                    kb_id,
                    workspace_id,
                )
                continue
        else:
            segs_for_type = typed_segments

        if not segs_for_type:
            logger.warning(
                "generate_eval_set: no segments available for question_type=%s, skipping. "
                "kb_id=%s workspace_id=%s",
                qtype.value,
                kb_id,
                workspace_id,
            )
            continue

        gen_input = QuestionGenInput(
            segments=segs_for_type,
            class_description=class_description,
            question_types=[qtype],
            count_per_type=count_per_type,
        )

        try:
            gen_output = run_question_generation(
                provider=llm_provider,
                op_config=op_config,
                input_model=gen_input,
            )
        except Exception:
            logger.exception(
                "generate_eval_set: run_question_generation failed for "
                "question_type=%s kb_id=%s — skipping this type",
                qtype.value,
                kb_id,
            )
            continue

        # Segment IDs used as source for this call
        source_ids = [_segment_id(s) for s in segs_for_type]

        generation_method = _TYPE_TO_GENERATION_METHOD[qtype]

        for item in gen_output.questions:
            question_id = str(uuid.uuid4())

            # D-22: score the generated question text for injection
            inj_score = _compute_injection_score(item.question_text)

            # review_status is always unreviewed for generated questions.
            # D-22: if the injection score exceeds the threshold, the question
            # MUST remain unreviewed — it cannot be auto-accepted.
            # (Generated questions are already unreviewed by default; this is
            # documented explicitly so it is clear there is no promotion path.)
            review_status = ReviewStatus.unreviewed

            if inj_score > threshold:
                logger.warning(
                    "generate_eval_set: D-22 — generated question has injection "
                    "suspicion score %.4f > threshold %.4f; question forced to "
                    "review_status=unreviewed. question_id=%s kb_id=%s",
                    inj_score,
                    threshold,
                    question_id,
                    kb_id,
                )

            eval_question = EvalQuestion(
                question_id=question_id,
                text=item.question_text,
                generation_method=generation_method,
                review_status=review_status,
                source_segment_ids=source_ids,
                source_unknown=False,
                question_type=qtype,
                expected_segment_ids=source_ids,
                reviewed_by=None,
                reviewed_at=None,
                class_description_ref=class_description,
            )

            all_generated.append(GeneratedQuestionRecord(eval_question, inj_score))

    tenancy = TenancyBlock(
        workspace_id=workspace_id,
        kb_id=kb_id,
        permission_mode=PermissionMode.public_to_kb,
        permission_principals=[],
        permission_source=PermissionSource.platform,
        permission_fidelity=PermissionFidelity.authoritative,
        permission_resolved_at=None,
    )

    questions = [gq.question for gq in all_generated]

    # M-043: confidence_level is ALWAYS provisional for a generated set.
    # There is no conditional logic here — this is unconditional.
    eval_set = EvalSet(
        schema_version=_EVAL_SET_SCHEMA_VERSION,
        tenancy=tenancy,
        eval_set_id=eval_set_id,
        origin=EvalSetOrigin.generated,
        provenance_note=None,
        created_at=now,
        confidence_level=ConfidenceLevel.provisional,
        questions=questions,
        baseline_ref=None,
    )

    return eval_set, all_generated


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _coerce_segments(segments: list[Any]) -> list[QuestionGenSegment]:
    """Convert raw segment objects/dicts into ``QuestionGenSegment`` instances.

    Accepts either attribute-bearing objects or dicts.  Falls back gracefully
    when optional fields (segment_type, structural_path) are missing.
    """
    result: list[QuestionGenSegment] = []
    for s in segments:
        if isinstance(s, QuestionGenSegment):
            result.append(s)
            continue
        result.append(_coerce_one_segment(s))
    return result


def _coerce_one_segment(s: Any) -> QuestionGenSegment:
    """Coerce a single raw segment (dict or attribute object) to QuestionGenSegment."""

    def _get(key: str, default: Any = None) -> Any:
        if isinstance(s, dict):
            return s.get(key, default)
        return getattr(s, key, default)

    text = str(_get("segment_text") or _get("text") or "")
    doc_id = str(_get("source_document_id") or _get("document_id") or "unknown")
    seg_type = _get("segment_type")
    structural_path = _get("structural_path") or []
    # Extract segment_id so that _segment_id() can return the real segment-level
    # ID rather than falling back to source_document_id (Ruling 1 fix).
    raw_seg_id = _get("segment_id")
    seg_id: str | None = str(raw_seg_id) if raw_seg_id is not None else None

    return QuestionGenSegment(
        segment_text=text,
        source_document_id=doc_id,
        segment_type=seg_type,
        structural_path=list(structural_path),
        segment_id=seg_id,
    )


def _segment_id(seg: QuestionGenSegment) -> str:
    """Return a stable string ID for a segment.

    Uses ``segment_id`` if available, otherwise falls back to
    ``source_document_id`` (which is always set on QuestionGenSegment).
    """
    sid = getattr(seg, "segment_id", None)
    if sid:
        return str(sid)
    return str(seg.source_document_id)


def _select_multi_doc_segments(
    segments: list[QuestionGenSegment],
) -> list[QuestionGenSegment] | None:
    """Return a subset of segments spanning at least 2 distinct source_document_ids.

    Returns ``None`` if fewer than 2 distinct document IDs are present.
    Otherwise returns all segments whose source_document_id appears in the
    two most frequent document groups (to keep the context window bounded while
    ensuring multi-document coverage).
    """
    # Group by source_document_id
    by_doc: dict[str, list[QuestionGenSegment]] = {}
    for seg in segments:
        doc_id = str(seg.source_document_id)
        by_doc.setdefault(doc_id, []).append(seg)

    if len(by_doc) < 2:
        return None

    # Take segments from the two largest document groups
    sorted_docs = sorted(by_doc.keys(), key=lambda d: len(by_doc[d]), reverse=True)
    selected_docs = sorted_docs[:2]

    result: list[QuestionGenSegment] = []
    for doc_id in selected_docs:
        result.extend(by_doc[doc_id])

    return result
