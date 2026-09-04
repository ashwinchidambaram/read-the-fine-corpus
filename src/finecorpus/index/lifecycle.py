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
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy.orm import Session

from finecorpus.control.metadata import AliasRepository
from finecorpus.index.adapter import (
    AliasSwapError,
    CollectionNotFoundError,
    IndexAdapter,
    ModelIdentity,
    alias_name,
    collection_name,
)

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


# ---------------------------------------------------------------------------
# Validation gate
# ---------------------------------------------------------------------------


@dataclass
class ValidationResult:
    """Outcome of the pre-promotion validation pass.

    Attributes:
        passed: True iff all gates passed.
        gate: Name of the first failing gate (if any).
        detail: Human-readable explanation.
    """

    passed: bool
    gate: str = ""
    detail: str = ""


def validate_shadow(
    adapter: IndexAdapter,
    shadow_collection: str,
    expected_min_chunks: int = 1,
    expected_max_chunks: int | None = None,
    declared_empty: bool = False,
) -> ValidationResult:
    """Run Phase 1 validation gates on a shadow collection (§5).

    Phase 1 gates:
    1. Chunk count ≥ ``expected_min_chunks`` (unless ``declared_empty=True``).
    2. Chunk count ≤ ``expected_max_chunks`` (if supplied).
    3. Non-empty unless declared empty (fail-safe default).

    Gates 3 and 4 (eval baseline, regression threshold) are out of scope for
    Phase 1 and pass vacuously here.

    Args:
        adapter: IndexAdapter instance.
        shadow_collection: Raw shadow collection name.
        expected_min_chunks: Lower bound on acceptable chunk count. Default 1.
        expected_max_chunks: Upper bound; unlimited if None.
        declared_empty: If True, a zero-chunk collection is accepted (Gate 3
            pass-through). Use for knowledge bases with no documents.

    Returns:
        ValidationResult with ``passed=True`` or the failing gate details.
    """
    try:
        actual_count = adapter.count_points(shadow_collection)
    except CollectionNotFoundError:
        return ValidationResult(
            passed=False,
            gate="collection_exists",
            detail=f"Shadow collection '{shadow_collection}' does not exist",
        )

    # Gate 1/2: non-empty check
    if actual_count == 0 and not declared_empty:
        return ValidationResult(
            passed=False,
            gate="non_empty",
            detail=(
                f"Shadow collection '{shadow_collection}' has 0 chunks and "
                f"declared_empty=False. Either the ingestion produced no chunks "
                f"(systematic failure) or the caller must set declared_empty=True."
            ),
        )

    # Gate 1: lower bound
    if actual_count < expected_min_chunks and not declared_empty:
        return ValidationResult(
            passed=False,
            gate="chunk_count_bounds",
            detail=(
                f"Shadow collection '{shadow_collection}' has {actual_count} chunks, "
                f"below expected minimum {expected_min_chunks}."
            ),
        )

    # Gate 2: upper bound
    if expected_max_chunks is not None and actual_count > expected_max_chunks:
        return ValidationResult(
            passed=False,
            gate="chunk_count_bounds",
            detail=(
                f"Shadow collection '{shadow_collection}' has {actual_count} chunks, "
                f"above expected maximum {expected_max_chunks}."
            ),
        )

    # Gates 3 and 4 (eval baseline, regression threshold) — vacuously pass in Phase 1.
    return ValidationResult(passed=True)


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
        # Discover all rtfc_ aliases from Qdrant
        try:
            all_aliases_response = adapter._client.get_aliases()  # type: ignore[attr-defined]
            aliases_to_check = [
                a.alias_name
                for a in all_aliases_response.aliases
                if a.alias_name.startswith("rtfc_")
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


__all__ = [
    "BuildContext",
    "BuildState",
    "LifecycleError",
    "NoNMinusOneError",
    "PromotionError",
    "ReconcileResult",
    "RollbackError",
    "ValidationFailedError",
    "ValidationResult",
    "create_shadow",
    "promote",
    "retire_previous_collection",
    "rollback",
    "startup_reconcile",
    "validate_shadow",
]
