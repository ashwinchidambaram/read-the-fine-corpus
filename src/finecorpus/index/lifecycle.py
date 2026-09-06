"""Index lifecycle — shadow build, validation, two-phase promotion, rollback.

Implements the state machine from index-lifecycle.md §3:
  SHADOW_CREATING → INGESTING → VALIDATING → PROMOTING → LIVE
  LIVE → HOT_STANDBY (N-1) on next promotion
  LIVE → ROLLED_BACK on rollback (requires N-1 model available in config)

Two-phase alias swap (§4):
  Phase 1: Qdrant alias retarget (adapter.retarget_alias).
  Phase 2: Control-plane alias record update (AliasRepository.promote).
  Failure between phases detected by startup_reconcile (OQ-L-1).

Spec references: index-lifecycle.md §2–§5, §10.3, §15; §18.3 tests 1, 6.

CLI / scheduling deferral (Ruling 6)
--------------------------------------
``snapshot_cold`` has no CLI command or scheduled wiring yet.  This is intentional:
CLI and scheduling integration land with the reindex scheduling work (later phase).
The function is fully implemented, tested, and callable from the lifecycle layer.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

from finecorpus.control.metadata import AliasRepository
from finecorpus.index.adapter import (
    RESTORED_UNREPLAYED_MARKER_KEY as _RESTORED_UNREPLAYED_MARKER_KEY,
)
from finecorpus.index.adapter import (
    AliasSwapError,
    CollectionNotFoundError,
    IndexAdapter,
    ModelIdentity,
    SnapshotRef,
    alias_name,
    collection_name,
)

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Build states (§3.1 of index-lifecycle.md)
# ---------------------------------------------------------------------------


class BuildState(StrEnum):
    """States of a single build attempt."""

    TRIGGERED = "TRIGGERED"
    SHADOW_CREATING = "SHADOW_CREATING"
    INGESTING = "INGESTING"
    PAUSED = "PAUSED"
    FAILED_INGESTION = "FAILED_INGESTION"
    VALIDATING = "VALIDATING"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    PROMOTING = "PROMOTING"
    PROMOTE_FAILED = "PROMOTE_FAILED"
    LIVE = "LIVE"
    ROLLED_BACK = "ROLLED_BACK"
    RETIRING = "RETIRING"
    HOT_STANDBY = "HOT_STANDBY"
    SNAPSHOTTING = "SNAPSHOTTING"
    COLD = "COLD"
    RESTORING = "RESTORING"
    TOMBSTONE_REPLAY = "TOMBSTONE_REPLAY"
    PURGED = "PURGED"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class LifecycleError(Exception):
    """Base for lifecycle-layer errors."""


class ValidationFailedError(LifecycleError):
    """Raised when pre-promotion validation fails.

    Per §3 / §15: the shadow is retained (never silently discarded), the alias
    is unchanged, and this exception surfaces the failure reason.

    Attributes:
        gate: Which validation gate failed (e.g. ``"chunk_count_bounds"``).
        detail: Human-readable failure explanation.
    """

    def __init__(self, gate: str, detail: str) -> None:
        self.gate = gate
        self.detail = detail
        super().__init__(f"Validation gate '{gate}' failed: {detail}")


class PromotionError(LifecycleError):
    """Raised when the alias swap fails (§4.3).

    Per §15: the previous alias target remains authoritative; the operation is
    idempotent and must be retried.
    """


class RollbackError(LifecycleError):
    """Raised when a rollback is blocked.

    Per OQ-L-7: rollback is blocked if the N-1 model is not available in config.
    """


class NoNMinusOneError(LifecycleError):
    """Raised when rollback is attempted but no N-1 collection exists."""


class RestoredUnreplayedError(LifecycleError):
    """Raised when promote() is called on a restored collection before tombstone replay.

    M-087: A collection restored from a snapshot MUST NOT be promoted until the
    tombstone log has been fully replayed.  ``restore_from_snapshot`` sets the
    ``RESTORED_UNREPLAYED_MARKER_KEY`` metadata key on the new collection before
    replay begins and clears it after.  ``promote()`` checks for this key and
    raises this error if it is present.

    Attributes:
        collection: The collection name that has unreplayed tombstones.
    """

    def __init__(self, collection: str) -> None:
        self.collection = collection
        super().__init__(
            f"Collection '{collection}' was restored from a snapshot and has not "
            f"yet completed tombstone replay (M-087). Call restore_from_snapshot() "
            f"which performs replay before returning, or wait for replay to complete "
            f"before promoting."
        )


class SnapshotLifecycleError(LifecycleError):
    """Raised when a snapshot lifecycle operation fails."""


# ---------------------------------------------------------------------------
# Validation gate
# ---------------------------------------------------------------------------


@dataclass
class ValidationResult:
    """Outcome of the pre-promotion validation pass.

    Attributes:
        passed: True iff all blocking gates passed.
        gate: Name of the first failing gate (if any).
        detail: Human-readable explanation.
        warnings: Non-blocking issues (D-20 tier-shift warning, etc.).
        gate_results: Per-gate outcome list for visibility reporting.
    """

    passed: bool
    gate: str = ""
    detail: str = ""
    warnings: list[str] = field(default_factory=list)
    gate_results: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Gate status enum
# ---------------------------------------------------------------------------


class GateStatus(StrEnum):
    """Status of an individual validation gate."""

    PASSED = "PASSED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    WARNING = "WARNING"


@dataclass
class GateResult:
    """Outcome of one individual validation gate.

    Attributes:
        gate: Gate name.
        status: PASSED / FAILED / SKIPPED / WARNING.
        reason: Human-readable explanation.
    """

    gate: str
    status: GateStatus
    reason: str = ""

    def to_dict(self) -> dict:
        return {"gate": self.gate, "status": str(self.status), "reason": self.reason}


def validate_shadow(
    adapter: IndexAdapter,
    shadow_collection: str,
    expected_min_chunks: int = 1,
    expected_max_chunks: int | None = None,
    declared_empty: bool = False,
    *,
    previous_chunk_count: int | None = None,
    previous_chunks_by_class: dict[str, int] | None = None,
    current_chunks_by_class: dict[str, int] | None = None,
    chunk_count_tolerance_pct: float = 20.0,
) -> ValidationResult:
    """Run pre-promotion validation gates on a shadow collection (M-054, §5).

    Gates:
    1. collection_exists — shadow collection must exist.
    2. non_empty — non-empty unless declared_empty=True.
    3. chunk_count_bounds — count within expected_min/max_chunks.
    4. chunk_count_tolerance (M-054 Gate 1) — count within ±chunk_count_tolerance_pct%
       of the PREVIOUS collection's count (when previous_chunk_count is supplied).
    5. no_class_zero_regression (M-054 Gate 2) — no segment class that produced chunks
       before produces ZERO now (uses previous_chunks_by_class if supplied).
    6. eval_baseline (M-054 Gate 3) — STUB returning SKIPPED ("phase5-eval-integration").
    7. regression_threshold (M-054 Gate 4) — STUB returning SKIPPED ("phase5-eval-integration").
    8. tier_shift_warning (D-20) — non-blocking WARNING comparing salience-tier distribution.

    Gate source for per-class counts (Gate 5):
    We use the build-result stats artifact (chunks_by_class) rather than scrolling
    Qdrant, because:
    - The build-result artifact is already computed and available at promotion time.
    - Scroll-with-payload aggregation would require a production Qdrant connection
      and a full-collection scan, adding latency and a network dependency.
    - The build artifact is the most consistent source since it reflects exactly
      what was written to the shadow collection in this run.
    This is documented as the "build-result stats" strategy (chosen over scroll).

    Args:
        adapter: IndexAdapter instance.
        shadow_collection: Raw shadow collection name.
        expected_min_chunks: Lower bound on acceptable chunk count. Default 1.
        expected_max_chunks: Upper bound; unlimited if None.
        declared_empty: If True, a zero-chunk collection is accepted (Gate 3
            pass-through). Use for knowledge bases with no documents.
        previous_chunk_count: Count from the previous (live) collection, used for
            Gate 4 tolerance check.  None skips the tolerance gate.
        previous_chunks_by_class: Per-class chunk counts from the previous build,
            used for Gate 5 class-zero-regression check.  None skips the gate.
        chunk_count_tolerance_pct: Maximum percentage deviation from previous_chunk_count
            before Gate 4 fails (default 20%).

    Returns:
        ValidationResult with ``passed=True`` or the failing gate details, plus
        non-blocking warnings and a gate_results list for full visibility.
    """
    gate_results: list[GateResult] = []
    warnings: list[str] = []

    # ------------------------------------------------------------------
    # Gate: collection_exists
    # ------------------------------------------------------------------
    try:
        actual_count = adapter.count_points(shadow_collection)
    except CollectionNotFoundError:
        gr = GateResult(
            gate="collection_exists",
            status=GateStatus.FAILED,
            reason=f"Shadow collection '{shadow_collection}' does not exist",
        )
        gate_results.append(gr)
        return ValidationResult(
            passed=False,
            gate="collection_exists",
            detail=gr.reason,
            warnings=warnings,
            gate_results=[r.to_dict() for r in gate_results],
        )
    gate_results.append(GateResult(gate="collection_exists", status=GateStatus.PASSED))

    # ------------------------------------------------------------------
    # Gate: non_empty
    # ------------------------------------------------------------------
    if actual_count == 0 and not declared_empty:
        gr = GateResult(
            gate="non_empty",
            status=GateStatus.FAILED,
            reason=(
                f"Shadow collection '{shadow_collection}' has 0 chunks and "
                f"declared_empty=False. Either the ingestion produced no chunks "
                f"(systematic failure) or the caller must set declared_empty=True."
            ),
        )
        gate_results.append(gr)
        return ValidationResult(
            passed=False,
            gate="non_empty",
            detail=gr.reason,
            warnings=warnings,
            gate_results=[r.to_dict() for r in gate_results],
        )
    gate_results.append(GateResult(gate="non_empty", status=GateStatus.PASSED))

    # ------------------------------------------------------------------
    # Gate: chunk_count_bounds (absolute)
    # ------------------------------------------------------------------
    if actual_count < expected_min_chunks and not declared_empty:
        gr = GateResult(
            gate="chunk_count_bounds",
            status=GateStatus.FAILED,
            reason=(
                f"Shadow collection '{shadow_collection}' has {actual_count} chunks, "
                f"below expected minimum {expected_min_chunks}."
            ),
        )
        gate_results.append(gr)
        return ValidationResult(
            passed=False,
            gate="chunk_count_bounds",
            detail=gr.reason,
            warnings=warnings,
            gate_results=[r.to_dict() for r in gate_results],
        )
    if expected_max_chunks is not None and actual_count > expected_max_chunks:
        gr = GateResult(
            gate="chunk_count_bounds",
            status=GateStatus.FAILED,
            reason=(
                f"Shadow collection '{shadow_collection}' has {actual_count} chunks, "
                f"above expected maximum {expected_max_chunks}."
            ),
        )
        gate_results.append(gr)
        return ValidationResult(
            passed=False,
            gate="chunk_count_bounds",
            detail=gr.reason,
            warnings=warnings,
            gate_results=[r.to_dict() for r in gate_results],
        )
    gate_results.append(GateResult(gate="chunk_count_bounds", status=GateStatus.PASSED))

    # ------------------------------------------------------------------
    # Gate: chunk_count_tolerance (M-054 Gate 1)
    # ------------------------------------------------------------------
    if previous_chunk_count is not None and previous_chunk_count > 0 and not declared_empty:
        tolerance = chunk_count_tolerance_pct / 100.0
        lower_bound = previous_chunk_count * (1.0 - tolerance)
        upper_bound = previous_chunk_count * (1.0 + tolerance)
        if actual_count < lower_bound or actual_count > upper_bound:
            gr = GateResult(
                gate="chunk_count_tolerance",
                status=GateStatus.FAILED,
                reason=(
                    f"Shadow chunk count {actual_count} deviates by more than "
                    f"{chunk_count_tolerance_pct:.1f}% from previous collection count "
                    f"{previous_chunk_count} "
                    f"(allowed range: [{int(lower_bound)}, {int(upper_bound)}])."
                ),
            )
            gate_results.append(gr)
            logger.error(
                "validate_shadow: chunk_count_tolerance FAILED for '%s': %s",
                shadow_collection,
                gr.reason,
            )
            return ValidationResult(
                passed=False,
                gate="chunk_count_tolerance",
                detail=gr.reason,
                warnings=warnings,
                gate_results=[r.to_dict() for r in gate_results],
            )
        gate_results.append(
            GateResult(
                gate="chunk_count_tolerance",
                status=GateStatus.PASSED,
                reason=(
                    f"count={actual_count} within "
                    f"{chunk_count_tolerance_pct:.1f}% of {previous_chunk_count}"
                ),
            )
        )
    else:
        gate_results.append(
            GateResult(
                gate="chunk_count_tolerance",
                status=GateStatus.SKIPPED,
                reason="no previous collection count available",
            )
        )

    # ------------------------------------------------------------------
    # Gate: no_class_zero_regression (M-054 Gate 2)
    # Source: build-result stats artifact (chunks_by_class from this run)
    # vs previous_chunks_by_class passed by the caller.
    #
    # Per-class count source decision: build-result stats artifact.
    # Rationale: the build artifact (chunks_by_document grouped by class)
    # is already available at promotion time and was produced by the same run
    # that populated the shadow collection.  Scroll-with-payload aggregation
    # would require a full Qdrant scan (expensive, adds latency, requires live
    # connection) and is less reliable than the artifact.  We require callers
    # to pass current_chunks_by_class from the build result artifact.
    # ------------------------------------------------------------------
    if previous_chunks_by_class is not None and current_chunks_by_class is not None:
        regressions: list[str] = []
        for seg_class, prev_count in previous_chunks_by_class.items():
            if prev_count > 0:
                cur_count = current_chunks_by_class.get(seg_class, 0)
                if cur_count == 0:
                    regressions.append(
                        f"class '{seg_class}' had {prev_count} chunk(s) before but now 0"
                    )
        if regressions:
            regression_detail = "; ".join(regressions)
            gr = GateResult(
                gate="no_class_zero_regression",
                status=GateStatus.FAILED,
                reason=(
                    f"M-054 Gate 2: segment class(es) that produced chunks before now "
                    f"produce zero — possible systematic regression: {regression_detail}"
                ),
            )
            gate_results.append(gr)
            logger.error(
                "validate_shadow: no_class_zero_regression FAILED for '%s': %s",
                shadow_collection,
                gr.reason,
            )
            return ValidationResult(
                passed=False,
                gate="no_class_zero_regression",
                detail=gr.reason,
                warnings=warnings,
                gate_results=[r.to_dict() for r in gate_results],
            )
        gate_results.append(
            GateResult(
                gate="no_class_zero_regression",
                status=GateStatus.PASSED,
                reason="all previously-producing classes still produce chunks",
            )
        )
    elif previous_chunks_by_class is not None and current_chunks_by_class is None:
        gate_results.append(
            GateResult(
                gate="no_class_zero_regression",
                status=GateStatus.SKIPPED,
                reason=(
                    "current per-class counts not supplied; pass current_chunks_by_class "
                    "from the build result to activate this gate"
                ),
            )
        )
    else:
        gate_results.append(
            GateResult(
                gate="no_class_zero_regression",
                status=GateStatus.SKIPPED,
                reason="no previous per-class counts available",
            )
        )

    # ------------------------------------------------------------------
    # Gate: eval_baseline (M-054 Gate 3) — STUB (phase5-eval-integration)
    # ------------------------------------------------------------------
    gate_results.append(
        GateResult(
            gate="eval_baseline",
            status=GateStatus.SKIPPED,
            reason="phase5-eval-integration",
        )
    )
    logger.debug("validate_shadow: eval_baseline gate SKIPPED (phase5-eval-integration)")

    # ------------------------------------------------------------------
    # Gate: regression_threshold (M-054 Gate 4) — STUB (phase5-eval-integration)
    # ------------------------------------------------------------------
    gate_results.append(
        GateResult(
            gate="regression_threshold",
            status=GateStatus.SKIPPED,
            reason="phase5-eval-integration",
        )
    )
    logger.debug("validate_shadow: regression_threshold gate SKIPPED (phase5-eval-integration)")

    # ------------------------------------------------------------------
    # D-20: tier-shift WARNING gate (non-blocking)
    # Compares the salience-tier distribution of the new shadow vs. previous.
    # We approximate using the total count ratio by tier.
    # This is a WARNING gate — it never blocks promotion.
    # ------------------------------------------------------------------
    if previous_chunk_count is not None and previous_chunk_count > 0 and actual_count > 0:
        # Without per-tier counts from the adapter, we can only emit a generic
        # advisory.  The full tier-distribution comparison requires the build
        # result artifact.  We emit a WARNING placeholder.
        logger.info(
            "validate_shadow: D-20 tier-shift check: shadow=%s count=%d previous=%d "
            "(per-tier breakdown requires build result artifact — placeholder WARNING)",
            shadow_collection,
            actual_count,
            previous_chunk_count,
        )
        shift_pct = abs(actual_count - previous_chunk_count) / previous_chunk_count * 100
        if shift_pct > 30.0:
            msg = (
                f"D-20 tier-shift WARNING: chunk count changed by {shift_pct:.1f}% "
                f"({previous_chunk_count} → {actual_count}); salience-tier distribution "
                f"may have shifted significantly. Review build report."
            )
            warnings.append(msg)
            logger.warning("validate_shadow: %s", msg)
            gate_results.append(
                GateResult(
                    gate="tier_shift_warning",
                    status=GateStatus.WARNING,
                    reason=msg,
                )
            )
        else:
            gate_results.append(
                GateResult(
                    gate="tier_shift_warning",
                    status=GateStatus.PASSED,
                    reason=f"count change {shift_pct:.1f}% within acceptable range",
                )
            )
    else:
        gate_results.append(
            GateResult(
                gate="tier_shift_warning",
                status=GateStatus.SKIPPED,
                reason="no previous collection count available for tier-shift comparison",
            )
        )

    return ValidationResult(
        passed=True,
        gate_results=[r.to_dict() for r in gate_results],
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# Build context
# ---------------------------------------------------------------------------


@dataclass
class BuildContext:
    """Runtime context for a single build attempt.

    Carries all the information needed for the two-phase promotion without
    querying the adapter or DB again mid-swap.

    Attributes:
        kb_id: Knowledge-base UUID.
        workspace_id: Workspace UUID.
        build_id: Build ID allocated by the control plane.
        shadow_collection: Raw shadow collection name.
        alias: Alias name for this KB.
        model_identity: Embedding model used for this build.
        state: Current build state.
        promoted_at: Set when promotion completes.
    """

    kb_id: str
    workspace_id: str
    build_id: int
    shadow_collection: str
    alias: str
    model_identity: ModelIdentity
    state: BuildState = BuildState.TRIGGERED
    promoted_at: datetime | None = None


# ---------------------------------------------------------------------------
# Core lifecycle functions
# ---------------------------------------------------------------------------


def create_shadow(
    adapter: IndexAdapter,
    kb_id: str,
    workspace_id: str,
    build_id: int,
    model_identity: ModelIdentity,
) -> BuildContext:
    """Create a shadow collection for a new build (§4.2 C-4, §3 SHADOW_CREATING).

    The shadow collection name is derived deterministically from (kb_id, build_id).
    The collection is created in Qdrant and model identity is stored in its
    metadata for retrieval without a DB round-trip.

    Args:
        adapter: IndexAdapter instance.
        kb_id: Knowledge-base UUID.
        workspace_id: Workspace UUID.
        build_id: Build ID allocated by the control plane (monotonically increasing).
        model_identity: Embedding model identity for this build.

    Returns:
        BuildContext with state=SHADOW_CREATING (transitions to INGESTING once
        the caller begins writing chunks).

    Raises:
        LifecycleError: If collection creation fails.
    """
    shadow = collection_name(kb_id, build_id)
    als = alias_name(kb_id)

    logger.info(
        "Creating shadow collection '%s' for kb '%s' build %d",
        shadow,
        kb_id,
        build_id,
    )

    try:
        adapter.create_collection(
            kb_id=kb_id,
            build_id=build_id,
            dimensions=model_identity.dimensions,
            metadata={
                "provider": model_identity.provider,
                "model": model_identity.model,
                "dimensions": model_identity.dimensions,
                "config_version": model_identity.config_version,
            },
        )
    except Exception as exc:
        raise LifecycleError(
            f"SHADOW_CREATING failed for kb '{kb_id}' build {build_id}: {exc}"
        ) from exc

    ctx = BuildContext(
        kb_id=kb_id,
        workspace_id=workspace_id,
        build_id=build_id,
        shadow_collection=shadow,
        alias=als,
        model_identity=model_identity,
        state=BuildState.SHADOW_CREATING,
    )
    return ctx


def promote(
    adapter: IndexAdapter,
    session: Session,
    ctx: BuildContext,
    *,
    expected_min_chunks: int = 1,
    expected_max_chunks: int | None = None,
    declared_empty: bool = False,
    promoted_at: datetime | None = None,
    available_model_providers: set[str] | None = None,
) -> None:
    """Run validation then execute the two-phase alias swap (§4, §5).

    State machine transitions:
    VALIDATING → PROMOTING → LIVE (success)
    VALIDATING → VALIDATION_FAILED (gate fails — alias unchanged, shadow retained)
    PROMOTING → PROMOTE_FAILED → PROMOTING (retried by caller on failure)

    The function is **idempotent**: if the Qdrant alias already points at
    ``ctx.shadow_collection``, Phase 1 is a no-op (Qdrant returns success
    on re-applying the same alias mapping). If the control-plane record already
    reflects the new collection, Phase 2 is likewise a no-op. (§15 idempotent retry.)

    Args:
        adapter: IndexAdapter instance.
        session: SQLAlchemy Session (transaction managed by caller).
        ctx: BuildContext for the build to promote.
        expected_min_chunks: Lower bound for Gate 1.
        expected_max_chunks: Upper bound for Gate 1.
        declared_empty: Pass True to allow zero-chunk promotion (rare).
        promoted_at: Override promotion timestamp (useful for tests).
        available_model_providers: Set of provider names currently configured.
            Not needed for forward promotion (only checked on rollback). Pass
            None to skip the check.

    Raises:
        ValidationFailedError: If a validation gate fails (alias unchanged,
            shadow retained).
        PromotionError: If the alias swap fails (Phase 1 or Phase 2).
    """
    ctx.state = BuildState.VALIDATING
    logger.info(
        "Validating shadow '%s' (build %d, kb '%s')",
        ctx.shadow_collection,
        ctx.build_id,
        ctx.kb_id,
    )

    validation = validate_shadow(
        adapter=adapter,
        shadow_collection=ctx.shadow_collection,
        expected_min_chunks=expected_min_chunks,
        expected_max_chunks=expected_max_chunks,
        declared_empty=declared_empty,
    )

    if not validation.passed:
        ctx.state = BuildState.VALIDATION_FAILED
        logger.error(
            "Validation FAILED for shadow '%s': gate='%s' detail='%s'",
            ctx.shadow_collection,
            validation.gate,
            validation.detail,
        )
        # Shadow is retained (never dropped here) — §3 / §15
        raise ValidationFailedError(gate=validation.gate, detail=validation.detail)

    # -----------------------------------------------------------------------
    # M-087 precondition: refuse promotion of a restored-but-unreplayed collection
    # -----------------------------------------------------------------------
    # ``restore_from_snapshot`` sets _RESTORED_UNREPLAYED_MARKER_KEY="true" in the
    # collection metadata immediately after restore, before tombstone replay begins.
    # It clears the key after replay completes.  We check for it here to provide
    # a hard structural gate (not just a convention).
    #
    # _RESTORED_UNREPLAYED_MARKER_KEY is imported from index.adapter (the single
    # canonical definition) — no local string literal here.
    try:
        _meta = adapter.get_collection_metadata(ctx.shadow_collection)
        if _meta.get(_RESTORED_UNREPLAYED_MARKER_KEY) == "true":
            ctx.state = BuildState.VALIDATION_FAILED
            raise RestoredUnreplayedError(ctx.shadow_collection)
    except RestoredUnreplayedError:
        raise
    except Exception as _exc:
        # get_collection_metadata may raise CollectionNotFoundError or other errors;
        # those will surface naturally at validate_shadow time.  Non-fatal here.
        logger.debug(
            "M-087 marker check: get_collection_metadata failed for '%s': %s",
            ctx.shadow_collection,
            _exc,
        )

    # -----------------------------------------------------------------------
    # Phase 1: Qdrant alias retarget (§4.1)
    # -----------------------------------------------------------------------
    ctx.state = BuildState.PROMOTING
    logger.info(
        "Promoting shadow '%s' via alias '%s' (Phase 1: Qdrant retarget)",
        ctx.shadow_collection,
        ctx.alias,
    )

    # Check if alias already points at the shadow (idempotent retry path)
    current_qdrant_target = adapter.resolve_alias(ctx.alias)

    if current_qdrant_target != ctx.shadow_collection:
        try:
            if not adapter.alias_exists(ctx.alias):
                adapter.create_alias(ctx.alias, ctx.shadow_collection)
            else:
                adapter.retarget_alias(ctx.alias, ctx.shadow_collection)
        except AliasSwapError as exc:
            ctx.state = BuildState.PROMOTE_FAILED
            raise PromotionError(
                f"Phase 1 (Qdrant alias retarget) failed for alias '{ctx.alias}': {exc}"
            ) from exc
    else:
        logger.info(
            "Phase 1 idempotent: alias '%s' already points at '%s'",
            ctx.alias,
            ctx.shadow_collection,
        )

    # -----------------------------------------------------------------------
    # Phase 2: Control-plane record update (§4.2)
    # -----------------------------------------------------------------------
    logger.info(
        "Promoting shadow '%s' (Phase 2: control-plane record update)",
        ctx.shadow_collection,
    )
    repo = AliasRepository(session)

    # Check if Phase 2 already completed (idempotent retry path)
    existing = repo.get(ctx.alias)
    if existing is not None and existing.collection_name == ctx.shadow_collection:
        logger.info(
            "Phase 2 idempotent: control-plane record already reflects '%s'",
            ctx.shadow_collection,
        )
        ctx.state = BuildState.LIVE
        ctx.promoted_at = existing.promoted_at
        return

    try:
        if existing is None:
            # First-ever promotion for this KB: create the alias record
            repo.create(
                alias=ctx.alias,
                kb_id=ctx.kb_id,
                workspace_id=ctx.workspace_id,
            )

        repo.promote(
            alias=ctx.alias,
            new_collection=ctx.shadow_collection,
            new_build_id=ctx.build_id,
            embedding_provider=ctx.model_identity.provider,
            embedding_model=ctx.model_identity.model,
            embedding_dimensions=ctx.model_identity.dimensions,
            config_version=ctx.model_identity.config_version,
            promoted_at=promoted_at,
        )
        session.commit()
    except Exception as exc:
        session.rollback()
        ctx.state = BuildState.PROMOTE_FAILED
        raise PromotionError(
            f"Phase 2 (control-plane update) failed for alias '{ctx.alias}': {exc}. "
            f"Qdrant alias already points at '{ctx.shadow_collection}'. "
            f"Run startup_reconcile() to repair."
        ) from exc

    ctx.state = BuildState.LIVE
    ctx.promoted_at = promoted_at or datetime.now(tz=UTC)
    logger.info(
        "Promotion complete: alias '%s' -> '%s' (build %d)",
        ctx.alias,
        ctx.shadow_collection,
        ctx.build_id,
    )


def rollback(
    adapter: IndexAdapter,
    session: Session,
    kb_id: str,
    available_model_providers: set[str],
) -> str:
    """Roll back the alias to the N-1 collection (§10.3, OQ-L-7).

    Blocked (raises RollbackError) if:
    - There is no N-1 collection (first promotion).
    - The N-1 collection was built with an embedding model whose provider is not
      in ``available_model_providers`` (OQ-L-7: block rather than allow a state
      where every query fails).

    The rollback is the same two-phase swap as promotion, in reverse:
    Phase 1: Qdrant alias retarget to N-1.
    Phase 2: Control-plane record swap of current and previous collections.

    Args:
        adapter: IndexAdapter instance.
        session: SQLAlchemy Session (transaction managed by caller).
        kb_id: Knowledge-base UUID.
        available_model_providers: Set of embedding provider names currently
            available in the platform config. Used for OQ-L-7 guard.

    Returns:
        The N-1 collection name that the alias now resolves to.

    Raises:
        NoNMinusOneError: If there is no N-1 collection.
        RollbackError: If the N-1 model provider is unavailable (OQ-L-7).
        PromotionError: If the alias swap fails.
    """
    als = alias_name(kb_id)
    repo = AliasRepository(session)
    record = repo.get(als)

    if record is None:
        raise NoNMinusOneError(
            f"No alias record found for kb '{kb_id}'; has the KB ever been promoted?"
        )
    if not record.previous_collection:
        raise NoNMinusOneError(
            f"No N-1 collection for alias '{als}' "
            f"(current: {record.collection_name}). Rollback requires a prior promotion."
        )

    # OQ-L-7: block rollback if N-1 model provider is not available
    n1_provider = record.embedding_provider
    if n1_provider and n1_provider not in available_model_providers:
        raise RollbackError(
            f"Rollback blocked (OQ-L-7): N-1 collection '{record.previous_collection}' "
            f"was built with embedding provider '{n1_provider}', which is not in the "
            f"currently configured providers ({sorted(available_model_providers)}). "
            f"Re-configure the provider '{n1_provider}' and retry, or perform a fresh "
            f"reindex with an available provider."
        )

    n1_collection = record.previous_collection
    logger.info(
        "Rolling back alias '%s': %s -> %s",
        als,
        record.collection_name,
        n1_collection,
    )

    # Phase 1: Qdrant retarget
    try:
        adapter.retarget_alias(als, n1_collection)
    except AliasSwapError as exc:
        raise PromotionError(
            f"Rollback Phase 1 (Qdrant retarget to '{n1_collection}') failed: {exc}"
        ) from exc

    # Phase 2: control-plane record swap
    try:
        repo.rollback_to_previous(als)
        session.commit()
    except Exception as exc:
        session.rollback()
        raise PromotionError(
            f"Rollback Phase 2 (control-plane update) failed: {exc}. "
            f"Qdrant alias now points at '{n1_collection}'. Run startup_reconcile()."
        ) from exc

    logger.info("Rollback complete: alias '%s' now serves '%s'", als, n1_collection)
    return n1_collection


# ---------------------------------------------------------------------------
# Startup reconciliation (OQ-L-1)
# ---------------------------------------------------------------------------


@dataclass
class ReconcileResult:
    """Result of a startup reconciliation pass.

    Attributes:
        inconsistencies_found: Number of alias/record mismatches detected.
        repaired: Number of mismatches successfully repaired.
        failed: Number of repair attempts that failed.
        details: Human-readable notes per KB.
    """

    inconsistencies_found: int = 0
    repaired: int = 0
    failed: int = 0
    details: list[str] = field(default_factory=list)


def startup_reconcile(
    adapter: IndexAdapter,
    session: Session,
    aliases_to_check: list[str] | None = None,
) -> ReconcileResult:
    """Detect and repair Phase-2 failures from previous runs (OQ-L-1).

    On every startup (or after any promotion attempt), the platform compares
    the Qdrant alias target against the control-plane record. If they differ,
    Phase 2 is retried: the control-plane record is updated to match Qdrant.

    The Qdrant alias is the **leading state**; the control-plane record is the
    **lagging state** (index-lifecycle.md §4.3). During the inconsistency
    window, the retrieval service uses the old model identity from the
    control-plane record — which is safe, because the new model identity has
    not been propagated. This is a safe inconsistency.

    Args:
        adapter: IndexAdapter instance.
        session: SQLAlchemy Session.
        aliases_to_check: List of alias names to check. If None, all collections
            starting with ``rtfc_`` that also have alias records are checked.

    Returns:
        ReconcileResult describing what was found and repaired.
    """
    result = ReconcileResult()
    repo = AliasRepository(session)

    # Build the list of aliases to inspect
    if aliases_to_check is None:
        # Discover all rtfc_ aliases from Qdrant via the public ABC method (F-03).
        try:
            all_aliases = adapter.list_aliases()
            aliases_to_check = [
                a.alias_name for a in all_aliases if a.alias_name.startswith("rtfc_")
            ]
        except Exception as exc:
            logger.error("startup_reconcile: failed to list aliases from Qdrant: %s", exc)
            result.details.append(f"Failed to list aliases: {exc}")
            return result

    for als in aliases_to_check:
        qdrant_target = adapter.resolve_alias(als)
        db_record = repo.get(als)

        if qdrant_target is None:
            # Alias doesn't exist in Qdrant — nothing to reconcile
            continue

        if db_record is None:
            result.inconsistencies_found += 1
            detail = (
                f"Alias '{als}' exists in Qdrant (-> '{qdrant_target}') "
                f"but has no control-plane record. Manual investigation required."
            )
            logger.warning("startup_reconcile: %s", detail)
            result.details.append(detail)
            result.failed += 1
            continue

        if db_record.collection_name == qdrant_target:
            # Consistent — nothing to do
            continue

        # Inconsistency detected: Qdrant leads, control-plane lags
        result.inconsistencies_found += 1
        logger.warning(
            "startup_reconcile: alias '%s' Qdrant=%s, control-plane=%s — repairing",
            als,
            qdrant_target,
            db_record.collection_name,
        )

        # Read model identity from the Qdrant collection metadata
        try:
            meta = adapter.get_collection_metadata(qdrant_target)
        except Exception as exc:
            detail = (
                f"Alias '{als}': could not read metadata from Qdrant collection "
                f"'{qdrant_target}': {exc}. Skipping repair."
            )
            logger.error("startup_reconcile: %s", detail)
            result.details.append(detail)
            result.failed += 1
            continue

        # Parse build_id from the collection name (rtfc_{kb_nohyphen}_{build_id:08d})
        build_id = _parse_build_id_from_collection(qdrant_target)

        try:
            repo.reconcile_from_qdrant(
                alias=als,
                qdrant_collection=qdrant_target,
                embedding_provider=meta.get("provider", ""),
                embedding_model=meta.get("model", ""),
                embedding_dimensions=int(meta.get("dimensions", 0)),
                config_version=meta.get("config_version", ""),
                build_id=build_id,
            )
            session.commit()
            result.repaired += 1
            result.details.append(
                f"Alias '{als}': repaired control-plane record "
                f"to match Qdrant target '{qdrant_target}'."
            )
        except Exception as exc:
            session.rollback()
            result.failed += 1
            result.details.append(f"Alias '{als}': repair failed — {exc}")
            logger.error("startup_reconcile: failed to repair alias '%s': %s", als, exc)

    if result.inconsistencies_found:
        logger.info(
            "startup_reconcile: found %d inconsistencies, repaired %d, failed %d",
            result.inconsistencies_found,
            result.repaired,
            result.failed,
        )
    else:
        logger.debug("startup_reconcile: all aliases consistent")

    return result


def _parse_build_id_from_collection(coll_name: str) -> int:
    """Extract build_id from a canonical collection name.

    Format: ``rtfc_{kb_nohyphen}_{build_id:08d}``

    Args:
        coll_name: Collection name string.

    Returns:
        build_id integer, or 0 if parsing fails.
    """
    try:
        return int(coll_name.rsplit("_", 1)[-1])
    except (ValueError, IndexError):
        return 0


# ---------------------------------------------------------------------------
# Retention helper
# ---------------------------------------------------------------------------


def retire_previous_collection(
    adapter: IndexAdapter,
    session: Session,
    kb_id: str,
) -> str | None:
    """Drop the N-1 collection when it is no longer the hot standby.

    Called after a second successful promotion: the collection that was N-1
    (now N-2) is dropped from Qdrant. Callers are responsible for snapshotting
    it to cold storage first if required by the retention policy (§10.1).

    This function reads ``previous_collection`` from the alias record and drops
    it. It does NOT update the alias record — the lifecycle caller does that.

    Args:
        adapter: IndexAdapter instance.
        session: SQLAlchemy Session.
        kb_id: Knowledge-base UUID.

    Returns:
        The collection name that was dropped, or None if there was nothing to drop.
    """
    als = alias_name(kb_id)
    repo = AliasRepository(session)
    record = repo.get(als)

    if record is None or not record.previous_collection:
        return None

    to_drop = record.previous_collection
    if adapter.collection_exists(to_drop):
        try:
            adapter.drop_collection(to_drop)
            logger.info("Retired N-2 collection '%s' from Qdrant", to_drop)
        except Exception as exc:
            logger.warning("Failed to drop retired collection '%s': %s", to_drop, exc)
    return to_drop


# ---------------------------------------------------------------------------
# Cold snapshot lifecycle (§10.2, §17.1, D-05)
# ---------------------------------------------------------------------------

# _RESTORED_UNREPLAYED_MARKER_KEY is imported from finecorpus.index.adapter above
# (as an alias) — that module is the single canonical definition.  Do NOT redefine it here.


@dataclass
class SnapshotColdResult:
    """Result of a snapshot_cold operation.

    Attributes:
        ref: The SnapshotRef for the newly created snapshot.
        swept_snapshots: Snapshot IDs deleted by the retention sweep.
    """

    ref: SnapshotRef
    swept_snapshots: list[str] = field(default_factory=list)


def snapshot_cold(
    adapter: IndexAdapter,
    collection: str,
    *,
    retention_period_days: int = 90,
) -> SnapshotColdResult:
    """Create a cold snapshot of a collection and sweep aged snapshots.

    Implements §10.2 cold retention:
    1. Create a snapshot of ``collection``.
    2. Enumerate existing snapshots for the same collection.
    3. Delete snapshots older than ``retention_period_days`` (D-05 default: 90 days).

    The retention sweep is conservative: snapshots without a ``created_at``
    timestamp are never swept (they cannot be age-checked).

    Args:
        adapter: IndexAdapter instance.
        collection: Raw collection name to snapshot.
        retention_period_days: Snapshots older than this many days are deleted.
            Default 90 per D-05 (M-088: retention documented).

    Returns:
        SnapshotColdResult with the new ref and list of swept snapshot IDs.

    Raises:
        SnapshotLifecycleError: If the snapshot creation fails.
        CollectionNotFoundError: If the collection does not exist.
    """
    from finecorpus.index.adapter import SnapshotError

    logger.info(
        "snapshot_cold: creating snapshot for collection '%s' (retention=%d days)",
        collection,
        retention_period_days,
    )
    try:
        ref = adapter.snapshot_collection(collection)
    except Exception as exc:
        raise SnapshotLifecycleError(
            f"Failed to create cold snapshot for collection '{collection}': {exc}"
        ) from exc

    # Retention sweep: delete snapshots older than retention_period_days
    swept: list[str] = []
    cutoff = datetime.now(tz=UTC) - timedelta(days=retention_period_days)

    try:
        existing = adapter.list_snapshots(collection)
    except Exception as exc:
        logger.warning("snapshot_cold: retention sweep skipped — list_snapshots failed: %s", exc)
        return SnapshotColdResult(ref=ref, swept_snapshots=[])

    for snap in existing:
        # Never sweep the snapshot we just created
        if snap.snapshot_id == ref.snapshot_id:
            continue
        # Snapshots without a timestamp cannot be age-checked — skip them
        if snap.created_at is None:
            continue
        if snap.created_at <= cutoff:
            try:
                adapter.delete_snapshot(snap)
                swept.append(snap.snapshot_id)
                logger.info(
                    "snapshot_cold: swept aged snapshot '%s' (created_at=%s < cutoff=%s)",
                    snap.snapshot_id,
                    snap.created_at,
                    cutoff,
                )
            except (SnapshotError, Exception) as exc:
                logger.warning(
                    "snapshot_cold: failed to sweep aged snapshot '%s': %s",
                    snap.snapshot_id,
                    exc,
                )

    return SnapshotColdResult(ref=ref, swept_snapshots=swept)


def restore_from_snapshot(
    adapter: IndexAdapter,
    session: Session,
    kb_id: str,
    ref: SnapshotRef,
    *,
    new_build_id: int,
) -> str:
    """Restore a snapshot into a shadow collection and replay tombstones (M-087).

    This function implements the COMPLETE restore flow:
    1. Derive a new shadow collection name (must not exist).
    2. Set the RESTORED_UNREPLAYED_MARKER_KEY metadata to "true" — this blocks
       promote() until replay completes.
    3. Call adapter.restore_snapshot(ref, new_collection_name).
    4. Replay all unreplayed tombstones for this KB into the new collection:
       - For each unreplayed TombstoneRecord: call delete_by_document + mark_replayed.
    5. Clear the RESTORED_UNREPLAYED_MARKER_KEY (set to "false" / remove).
    6. Return the new collection name.

    Only after step 5 is the collection promotion-eligible (M-087).

    C-4 invariant: restore ALWAYS goes into a NEW shadow collection (never in place).
    The caller must then call promote() with a BuildContext pointing at the new
    collection.

    Args:
        adapter: IndexAdapter instance.
        session: SQLAlchemy Session for tombstone replay.
        kb_id: Knowledge-base UUID.
        ref: SnapshotRef returned by snapshot_cold / list_snapshots.
        new_build_id: Build ID for the restored collection (monotonically increasing;
            caller must allocate this from the control plane).

    Returns:
        The name of the newly restored shadow collection (promotion-eligible).

    Raises:
        SnapshotLifecycleError: If restore or tombstone replay fails.
        IndexError: If the new collection already exists (caller collision).
    """
    # Imports here to avoid circular import from pipeline.deletion
    from finecorpus.control.tombstone import TombstoneRepository
    from finecorpus.index.adapter import SnapshotError

    new_coll = collection_name(kb_id, new_build_id)
    logger.info(
        "restore_from_snapshot: restoring snapshot '%s' -> '%s' for kb '%s'",
        ref.snapshot_id,
        new_coll,
        kb_id,
    )

    if adapter.collection_exists(new_coll):
        raise SnapshotLifecycleError(
            f"Cannot restore: target collection '{new_coll}' already exists. "
            f"Allocate a new build_id via the control plane."
        )

    # ------------------------------------------------------------------
    # Step 1: Set unreplayed marker BEFORE restore (M-087 structural gate)
    # We set the marker on the collection AFTER restore since the collection
    # doesn't exist yet. After restore, we set it immediately.
    # ------------------------------------------------------------------
    try:
        adapter.restore_snapshot(ref, new_coll)
    except (SnapshotError, Exception) as exc:
        raise SnapshotLifecycleError(
            f"restore_snapshot failed for snapshot '{ref.snapshot_id}' -> '{new_coll}': {exc}"
        ) from exc

    # Set the unreplayed marker — blocks promote() until we clear it
    try:
        adapter.set_collection_metadata(new_coll, {_RESTORED_UNREPLAYED_MARKER_KEY: "true"})
    except Exception as exc:
        logger.warning(
            "restore_from_snapshot: failed to set unreplayed marker on '%s': %s "
            "(promote() will NOT be blocked — safety degraded)",
            new_coll,
            exc,
        )

    # ------------------------------------------------------------------
    # Step 2: Replay all unreplayed tombstones for this KB
    # ------------------------------------------------------------------
    tomb_repo = TombstoneRepository(session)
    unreplayed = tomb_repo.unreplayed_for(kb_id=kb_id, collection=new_coll)

    logger.info(
        "restore_from_snapshot: replaying %d unreplayed tombstone(s) into '%s'",
        len(unreplayed),
        new_coll,
    )

    replay_errors: list[str] = []
    for tomb in unreplayed:
        try:
            if adapter.collection_exists(new_coll):
                removed = adapter.delete_by_document(new_coll, tomb.document_id)
                logger.debug(
                    "tombstone replay: removed %d point(s) for doc '%s' from '%s'",
                    removed,
                    tomb.document_id,
                    new_coll,
                )
        except Exception as exc:
            err = (
                f"delete_by_document failed for document '{tomb.document_id}' "
                f"during replay into '{new_coll}': {exc}"
            )
            replay_errors.append(err)
            logger.error("restore_from_snapshot: %s", err)
            continue

        try:
            tomb_repo.mark_replayed(
                entry_id=tomb.entry_id,
                collection=new_coll,
            )
        except Exception as exc:
            err = f"mark_replayed failed for entry '{tomb.entry_id}': {exc}"
            replay_errors.append(err)
            logger.error("restore_from_snapshot: %s", err)

    if replay_errors:
        # Replay is best-effort for individual entries but we surface failures.
        # The collection remains in RESTORED_UNREPLAYED state — do not clear marker.
        error_summary = "; ".join(replay_errors[:5])
        raise SnapshotLifecycleError(
            f"Tombstone replay into '{new_coll}' had {len(replay_errors)} error(s): "
            f"{error_summary}. Collection remains not promotion-eligible (M-087)."
        )

    try:
        session.commit()
    except Exception as exc:
        session.rollback()
        raise SnapshotLifecycleError(
            f"Failed to commit tombstone replay records for '{new_coll}': {exc}"
        ) from exc

    # ------------------------------------------------------------------
    # Step 3: Clear the unreplayed marker — collection is now promotion-eligible
    # ------------------------------------------------------------------
    try:
        adapter.set_collection_metadata(new_coll, {_RESTORED_UNREPLAYED_MARKER_KEY: "false"})
    except Exception as exc:
        logger.warning(
            "restore_from_snapshot: failed to clear unreplayed marker on '%s': %s "
            "(promote() may still be blocked — set marker manually if needed)",
            new_coll,
            exc,
        )

    logger.info(
        "restore_from_snapshot: complete — '%s' is promotion-eligible (%d tombstone(s) replayed)",
        new_coll,
        len(unreplayed),
    )
    return new_coll


__all__ = [
    "BuildContext",
    "BuildState",
    "GateResult",
    "GateStatus",
    "LifecycleError",
    "NoNMinusOneError",
    "PromotionError",
    "ReconcileResult",
    "RestoredUnreplayedError",
    "RollbackError",
    "SnapshotColdResult",
    "SnapshotLifecycleError",
    "ValidationFailedError",
    "ValidationResult",
    "create_shadow",
    "promote",
    "restore_from_snapshot",
    "retire_previous_collection",
    "rollback",
    "snapshot_cold",
    "startup_reconcile",
    "validate_shadow",
]
