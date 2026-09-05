"""Phase 4 retrieval service — library-level query flow.

Implements C-5: all business logic lives here, not in the FastAPI wiring.

Query flow:
1. Resolve KB alias to its control-plane alias record (AliasRepository).
2. Mismatch check: configured provider's model identity vs. alias record's.
   On mismatch: return EMBEDDING_MODEL_MISMATCH result fail-closed WITHOUT
   calling the provider (§15, provider-abstraction.md §3.3).
3. Embed query via provider WITH the query-embedding cache (§11.3, §5 of
   provider-abstraction.md). Cache miss → call provider; cache hit → skip
   provider entirely.
4. Search via index adapter using the ALIAS name (C-3).
5. Apply score threshold. Classify result into the §15 four-way taxonomy:
   - matches: ≥1 result survives threshold.
   - no_matches: vector search returned 0 candidates.
   - filtered_to_zero: search found candidates but threshold eliminated all.
   - error: fail-closed condition (mismatch / provider down / vector DB down).
6. Build and return RetrievalResponse per the contract.

Tenancy seam (Phase 4):
The query builder receives ``kb_id`` as a mandatory parameter and derives the
tenancy ``must`` clause from it.  For workspace- and kb-scoped principals the
filter also includes workspace_id and permission_principals.

Phase 4 additions:
- Explain mode (M-062/M-063, §11.5): ``explain=True`` populates ExplainBlock
  with parsed_query echo, filters in effect, every candidate with raw score
  + provenance + permission_resolved_at (D-17), exclusions with the exact
  AppliedFilter that removed each, and retrieval strategy.
  NOTE: the tenancy filter stays inside the vector search (§11.5: explain is
  NOT a bypass).  Tenancy exclusions are structurally invisible by design —
  chunks that the tenant cannot see are never surfaced even in explain mode.
  Only non-tenancy post-search filters (score_threshold, kb-config floors)
  produce per-chunk ExplainExclusion records.
- Break-glass read (M-001..M-004, T-05): ``break_glass_grant_id`` bypasses
  the permission_principals clause (but NOT kb/workspace tenancy) when a
  valid active grant exists for the querying admin and target KB.
- D-24: Increment filtered_to_zero counter when a score filter alone empties
  the survivor set.
- Admin fail-closed: admin principals querying content MUST provide a
  break-glass grant; without a grant, admin content reads are denied.

Spec references: §11.1, §11.2, §11.5, §15, §14.1, §8, §12, §2.3.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from finecorpus.contracts.retrieval_response import (
    AppliedFilter,
    ErrorCode,
    ErrorEnvelope,
    ExplainBlock,
    ExplainCandidate,
    ExplainExclusion,
    FilterOrigin,
    RequestEcho,
    ResultStatus,
    RetrievalResponse,
    RetrievalResult,
    Scores,
)
from finecorpus.contracts.shared.blocks import (
    AppliedBy,
    LocatorKind,
    Provenance,
    SalienceSignal,
    SalienceSignalKind,
    SalienceTier,
    SegmentType,
    SourceLocation,
    TransformationRecord,
    TransformationTier,
    TrustLevel,
)
from finecorpus.contracts.versions import RETRIEVAL_RESPONSE_SCHEMA_VERSION as _SCHEMA_VERSION
from finecorpus.control.auth import Principal
from finecorpus.control.metadata import AliasRecord, AliasRepository
from finecorpus.embedding.base import EmbeddingProvider, ProviderUnavailableError
from finecorpus.embedding.cache import QueryEmbeddingCache, get_query_cache
from finecorpus.index.adapter import (
    AliasNotFoundError,
    IndexAdapter,
    IndexError,
    SearchResult,
    alias_name,
)
from finecorpus.retrieval.metrics import increment_filtered_to_zero

logger = logging.getLogger(__name__)

_TOP_K_MAX = 100
_TOP_K_DEFAULT = 10

# Over-fetch multiplier for explain mode / score-threshold filtering.
_OVER_FETCH_MULTIPLIER = 10
_OVER_FETCH_CAP = 1000


# ---------------------------------------------------------------------------
# Tenancy filter construction seam (Phase 4 slot-in point)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TenancyScope:
    """Resolved tenancy scope for a single query.

    Phase 4: kb_id is mandatory (D-06: single-KB queries only).
    workspace_id and permission_principals are enforced when a principal is
    present (auth enabled).

    Attributes:
        kb_id: Knowledge-base ID derived from the URL path parameter.
        workspace_id: Resolved workspace from the authenticated principal.
            Empty string when auth is disabled (Phase 1–3 compat).
        permission_principals: Principal identifiers allowed to retrieve.
            The query filter checks that the authenticated principal's ID is
            present in each retrieved chunk's tenancy.permission_principals
            list — OR that the chunk is public_to_kb (permission_mode check
            is done by payload filter, not here).
            Empty tuple when auth is disabled.
    """

    kb_id: str
    workspace_id: str = ""
    permission_principals: tuple[str, ...] = ()


def _build_tenancy_filter(scope: TenancyScope) -> dict[str, Any]:
    """Build the mandatory tenancy must-clause for the vector-DB query.

    Phase 4: extends Phase 1 (kb_id only) with workspace_id and
    permission_principals when the scope carries a principal.

    The tenancy clause is ALWAYS present and is a mandatory must clause (§11.4,
    M-060: server-side only, never client-overridable). It can only narrow the
    result set; it cannot be expanded by the caller.

    Payload field mapping (from build_point_payload / TenancyBlock):
    - ``tenancy.kb_id``              — exact match on the KB ULID (D-06).
    - ``tenancy.workspace_id``       — exact match on the owning workspace (when
                                       a workspace-or-narrower-scoped principal is
                                       present).
    - ``tenancy.permission_principals`` — the authenticated principal_id must be
                                       present in this list (MatchValue on an array
                                       field: Qdrant checks if the value is one of
                                       the array elements; FakeAdapter handles this
                                       via contains semantics).

    Args:
        scope: The resolved tenancy scope.

    Returns:
        Qdrant-format payload filter dict keyed by dotted field path to match
        value.  For list-type fields (permission_principals) the value is a
        sentinel dict ``{"__contains__": value}`` understood by both
        ``_dict_to_qdrant_filter`` and ``FakeAdapter._matches_filter``.

        When permission_principals is empty (auth disabled or global scope),
        the permission_principals clause is omitted — all principals can read.
    """
    f: dict[str, Any] = {"tenancy.kb_id": scope.kb_id}

    if scope.workspace_id:
        f["tenancy.workspace_id"] = scope.workspace_id

    if scope.permission_principals:
        # Each principal in the tuple must be present in the chunk's
        # permission_principals list.  For Phase 4 the tuple always has exactly
        # one element (the authenticated principal_id).  The ``__contains__``
        # sentinel signals list-contains semantics to the adapter layer.
        for pid in scope.permission_principals:
            f["tenancy.permission_principals"] = {"__contains__": pid}
            break  # Phase 4: single principal; loop is forward-compat only.

    return f


def _build_break_glass_tenancy_filter(scope: TenancyScope) -> dict[str, Any]:
    """Build the tenancy filter for a break-glass read.

    Under break-glass the permission_principals clause is bypassed for this
    query — the admin can read content without being in the chunk's
    permission_principals list.  However, kb/workspace tenancy clauses REMAIN
    (break-glass is not a cross-KB bypass).

    Args:
        scope: The resolved tenancy scope (kb_id and workspace_id still apply).

    Returns:
        Qdrant-format payload filter dict without permission_principals clause.
    """
    f: dict[str, Any] = {"tenancy.kb_id": scope.kb_id}
    if scope.workspace_id:
        f["tenancy.workspace_id"] = scope.workspace_id
    # permission_principals deliberately omitted
    return f


def _tenancy_applied_filter(scope: TenancyScope) -> AppliedFilter:
    """Build the AppliedFilter that records the tenancy clause origin."""
    parts = [f"tenancy.kb_id == {scope.kb_id!r}"]
    if scope.workspace_id:
        parts.append(f"tenancy.workspace_id == {scope.workspace_id!r}")
    if scope.permission_principals:
        parts.append(f"tenancy.permission_principals contains {scope.permission_principals[0]!r}")
    return AppliedFilter(
        expression=" AND ".join(parts),
        origin=FilterOrigin.tenancy,
    )


# ---------------------------------------------------------------------------
# Provenance reconstruction from payload
# ---------------------------------------------------------------------------


class _PayloadCorruptError(Exception):
    """Raised by _provenance_from_payload when a point payload is corrupt.

    A corrupt point is one that is missing the mandatory provenance identity
    fields (source_document_id or source_document_version).  Rather than
    fabricating these values the service fails closed with PAYLOAD_CORRUPT.
    """

    def __init__(self, chunk_id: str, missing: str) -> None:
        self.chunk_id = chunk_id
        self.missing = missing
        super().__init__(f"Point '{chunk_id}' has corrupt payload: missing provenance.{missing}")


def _provenance_from_payload(payload: dict[str, Any], chunk_id: str) -> Provenance:
    """Reconstruct a Provenance model from a Qdrant point payload.

    The payload stores the full §8 provenance block as a plain dict (written
    by ``build_point_payload`` at ingest time). This function reconstructs it
    so the RetrievalResult carries the contract type.

    A payload that is missing the mandatory identity fields (source_document_id
    or source_document_version) is **corrupt** — these values MUST NOT be
    fabricated.  The function raises _PayloadCorruptError instead, and the
    caller maps that to result_status=error / PAYLOAD_CORRUPT.

    Args:
        payload: Full Qdrant point payload dict.
        chunk_id: The point/chunk identifier used for logging.

    Returns:
        Provenance instance.

    Raises:
        _PayloadCorruptError: If source_document_id or source_document_version
            are absent or empty in the provenance block.
    """
    prov = payload.get("provenance", {})
    if not isinstance(prov, dict):
        prov = {}

    # Mandatory identity fields — NEVER fabricate (F-01).
    source_document_id = prov.get("source_document_id")
    if not source_document_id:
        raise _PayloadCorruptError(chunk_id=chunk_id, missing="source_document_id")
    source_document_version = prov.get("source_document_version")
    if not source_document_version:
        raise _PayloadCorruptError(chunk_id=chunk_id, missing="source_document_version")

    # SourceLocation
    loc_data = prov.get("source_location", {})
    if isinstance(loc_data, dict):
        # Allow partial data — fill in defaults for required enum field
        locator_kind_str = loc_data.get("locator_kind", LocatorKind.char_range)
        try:
            locator_kind = LocatorKind(locator_kind_str)
        except ValueError:
            locator_kind = LocatorKind.char_range
        source_location = SourceLocation(
            locator_kind=locator_kind,
            page_start=loc_data.get("page_start"),
            page_end=loc_data.get("page_end"),
            byte_start=loc_data.get("byte_start"),
            byte_end=loc_data.get("byte_end"),
            char_start=loc_data.get("char_start"),
            char_end=loc_data.get("char_end"),
            cell_range=loc_data.get("cell_range"),
            dom_path=loc_data.get("dom_path"),
            bbox=loc_data.get("bbox"),
            coordinate_note=loc_data.get("coordinate_note"),
        )
    else:
        source_location = SourceLocation(locator_kind=LocatorKind.char_range)

    # TransformationRecords
    transformations: list[TransformationRecord] = []
    for tr in prov.get("transformations", []):
        if isinstance(tr, dict):
            try:
                transformations.append(
                    TransformationRecord(
                        tier=TransformationTier(tr.get("tier", 1)),
                        operation=tr.get("operation", "unknown"),
                        applied_by=AppliedBy(tr.get("applied_by", "deterministic")),
                        model_ref=tr.get("model_ref"),
                        changed_text=tr.get("changed_text", False),
                        note=tr.get("note"),
                    )
                )
            except (ValueError, KeyError):
                pass

    # SalienceSignals
    salience_signals: list[SalienceSignal] = []
    for ss in prov.get("salience_signals", []):
        if isinstance(ss, dict):
            try:
                salience_signals.append(
                    SalienceSignal(
                        kind=SalienceSignalKind(ss.get("kind", SalienceSignalKind.default)),
                        implied_tier=SalienceTier(ss.get("implied_tier", SalienceTier.supporting)),
                        won=ss.get("won", False),
                        detail=ss.get("detail"),
                    )
                )
            except (ValueError, KeyError):
                pass

    # Enums with defaults
    try:
        segment_type = SegmentType(prov.get("segment_type", SegmentType.prose))
    except ValueError:
        segment_type = SegmentType.prose

    try:
        salience_tier = SalienceTier(prov.get("salience_tier", SalienceTier.supporting))
    except ValueError:
        salience_tier = SalienceTier.supporting

    try:
        salience_basis = SalienceSignalKind(prov.get("salience_basis", SalienceSignalKind.default))
    except ValueError:
        salience_basis = SalienceSignalKind.default

    trust_level_str = prov.get("trust_level", TrustLevel.untrusted_ingested)
    try:
        trust_level = TrustLevel(trust_level_str)
    except ValueError:
        trust_level = TrustLevel.untrusted_ingested

    from finecorpus.contracts.shared.blocks import (
        InvisibleContentKind,
        SensitivityFlag,
    )

    invisible_content_flags: list[InvisibleContentKind] = []
    for flag in prov.get("invisible_content_flags", []):
        try:
            invisible_content_flags.append(InvisibleContentKind(flag))
        except ValueError:
            pass

    sensitivity_flags: list[SensitivityFlag] = []
    for flag in prov.get("sensitivity_flags", []):
        try:
            sensitivity_flags.append(SensitivityFlag(flag))
        except ValueError:
            pass

    return Provenance(
        source_document_id=source_document_id,
        source_document_version=source_document_version,
        source_location=source_location,
        structural_path=prov.get("structural_path", []),
        transformations=transformations,
        confidence=float(prov.get("confidence", 1.0)),
        ocr_confidence=prov.get("ocr_confidence"),
        segment_type=segment_type,
        salience_tier=salience_tier,
        salience_basis=salience_basis,
        salience_signals=salience_signals,
        language=prov.get("language") or "und",
        injection_suspicion=float(prov.get("injection_suspicion", 0.0)),
        invisible_content_flags=invisible_content_flags,
        sensitivity_flags=sensitivity_flags,
        trust_level=trust_level,
    )


def _permission_resolved_at_from_payload(payload: dict[str, Any]) -> datetime | None:
    """Extract permission_resolved_at from the tenancy block in the payload (D-17).

    Returns None if the field is absent or not parseable.
    """
    tenancy = payload.get("tenancy", {})
    if not isinstance(tenancy, dict):
        return None
    raw = tenancy.get("permission_resolved_at")
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw
    try:
        return datetime.fromisoformat(str(raw))
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Error result helpers
# ---------------------------------------------------------------------------


def _error_response(
    query: str,
    filters_applied: list[AppliedFilter],
    code: ErrorCode,
    message: str,
    retriable: bool,
) -> RetrievalResponse:
    """Build a fail-closed error RetrievalResponse."""
    return RetrievalResponse(
        schema_version=_SCHEMA_VERSION,
        request_echo=RequestEcho(
            query=query,
            filters_applied=filters_applied,
        ),
        result_status=ResultStatus.error,
        results=[],
        error=ErrorEnvelope(
            code=code,
            message=message,
            retriable=retriable,
        ),
    )


# ---------------------------------------------------------------------------
# Shared search execution (single call site for tenancy parity — M-063)
# ---------------------------------------------------------------------------


def _execute_search(
    *,
    alias_str: str,
    query_vector: list[float],
    tenancy_filter: dict[str, Any],
    search_top_k: int,
    adapter: IndexAdapter,
) -> list[SearchResult] | None:
    """Execute the vector search through the adapter.

    This is the single call site for all search paths (normal and explain).
    Tenancy filter is ALWAYS inside the vector-search filter — explain mode
    does not get a different filter path (§11.5: explain is not a bypass,
    M-063: tenancy parity is structural).

    Args:
        alias_str: The alias to search.
        query_vector: The embedded query vector.
        tenancy_filter: The mandatory tenancy filter dict (always present).
        search_top_k: Number of results to request from the adapter.
        adapter: The IndexAdapter instance.

    Returns:
        List of SearchResult, or None if a recoverable IndexError occurred.
        Callers must check for None and return VECTOR_DB_UNAVAILABLE.

    Raises:
        AliasNotFoundError, IndexError: Propagated to caller for error mapping.
    """
    return adapter.search(
        alias=alias_str,
        query_vector=query_vector,
        top_k=search_top_k,
        payload_filter=tenancy_filter,
    )


# ---------------------------------------------------------------------------
# Main query function (C-5 business logic)
# ---------------------------------------------------------------------------


def query(
    *,
    kb_id: str,
    query_text: str,
    provider: EmbeddingProvider,
    adapter: IndexAdapter,
    session: Session,
    top_k: int = _TOP_K_DEFAULT,
    score_threshold: float | None = None,
    cache: QueryEmbeddingCache | None = None,
    principal: Principal | None = None,
    auth_enabled: bool = False,
    explain: bool = False,
    break_glass_grant_id: str | None = None,
) -> RetrievalResponse:
    """Execute a dense retrieval query with optional auth enforcement.

    This is the library-level entry point (C-5). The FastAPI handler calls this
    and maps the result to HTTP status codes; all business logic lives here.

    Dense-only retrieval. Hybrid search, reranking, and per-request metadata
    filters are not supported and MUST NOT be passed.

    Fail-closed conditions (§15) returned as result_status=error:
    - PERMISSION_DENIED: auth is enabled and principal is None (fail closed,
      M-061/T-02: cross-tenant must fail closed before any index access).
    - EMBEDDING_MODEL_MISMATCH: provider model identity != alias record's.
      Provider is never called on mismatch (§15, provider-abstraction §3.3).
    - PROVIDER_UNAVAILABLE: embedding provider raised ProviderUnavailableError.
    - VECTOR_DB_UNAVAILABLE: adapter raised IndexError on search.
    - KB_NOT_READY: alias record missing or has no promoted collection.

    Zero-result distinction (§15):
    - no_matches: vector search returned 0 candidates before threshold.
    - filtered_to_zero: candidates existed but score_threshold eliminated all.

    Tenancy enforcement (Phase 4, M-060):
    The TenancyScope is built from the authenticated principal (workspace_id,
    permission_principals).  The resulting filter is injected server-side into
    every query and CANNOT be overridden by client-supplied request body fields.

    Explain mode (M-062/M-063, §11.5):
    When explain=True the response includes ExplainBlock with:
    - parsed_query: the query text as parsed (no expansion in Phase 4).
    - candidates: every candidate from the over-fetched result set with raw
      score, provenance, and permission_resolved_at (D-17).
    - exclusions: candidates removed by non-tenancy filters with the exact
      AppliedFilter that removed them. Tenancy exclusions are structurally
      invisible (§11.5: explain is not a bypass — see module docstring).
    - strategy: "dense" (Phase 4 is dense-only).
    Note: explain uses the SAME _execute_search call site as normal queries
    (M-063 tenancy parity is structural, not documented).

    Break-glass read (§2.3, M-001..M-004, T-05):
    When break_glass_grant_id is provided:
    - The grant is validated (active, unexpired, matches kb and admin principal).
    - The permission_principals clause is bypassed for this query (admin access).
    - kb/workspace tenancy clauses REMAIN (break-glass is not cross-KB).
    - An audit row (break_glass_read) is appended with chunk_ids + query context.
    - break_glass_read_ref is set on the response.

    Admin fail-closed:
    Admin-role principals querying content MUST provide a break_glass_grant_id.
    Without a valid grant, admin content reads return PERMISSION_DENIED.

    Args:
        kb_id: Knowledge-base ID (from the URL path).
        query_text: The query string.
        provider: Configured embedding provider.
        adapter: Index adapter (Qdrant or test fake).
        session: SQLAlchemy session bound to the control-plane DB.
        top_k: Maximum results to return (default 10, max 100).
        score_threshold: Minimum score to include a result (optional).
        cache: Query-embedding cache; uses module default if None.
        principal: Authenticated principal (Phase 4).  When ``auth_enabled``
            is True and ``principal`` is None the query returns PERMISSION_DENIED
            (fail closed, M-061).  Ignored when auth_enabled is False.
        auth_enabled: True when the auth subsystem is active.  When False,
            the principal is ignored and no tenancy filters beyond kb_id
            are applied (Phase 1–3 compatibility).
        explain: When True, populate ExplainBlock in the response (§11.5).
        break_glass_grant_id: Optional break-glass grant ID for admin content
            reads (§2.3).  When provided and valid, bypasses the
            permission_principals clause (but not kb/workspace tenancy).

    Returns:
        RetrievalResponse per the retrieval-response contract.
    """
    if cache is None:
        cache = get_query_cache()

    # Clamp top_k to valid range
    top_k = max(1, min(top_k, _TOP_K_MAX))

    # ------------------------------------------------------------------
    # Phase 4: fail closed when auth enabled and no principal (M-061/T-02)
    # ------------------------------------------------------------------
    if auth_enabled and principal is None:
        return _error_response(
            query_text,
            [],
            ErrorCode.PERMISSION_DENIED,
            "Authentication required.  Provide a valid API key.",
            retriable=False,
        )

    # ------------------------------------------------------------------
    # Phase 4 fail-closed checks (auth enabled, principal present):
    # All checks happen BEFORE alias resolution and BEFORE index access.
    # ------------------------------------------------------------------
    if auth_enabled and principal is not None:
        from finecorpus.control.auth import Action, AuthError, ScopeKind, authorize

        # R4c: workspace-scoped principal with null workspace_id is a corrupt
        # stored record — it would silently drop the workspace containment clause
        # and fail open.  Reject immediately.
        if principal.scope_kind == ScopeKind.workspace and not principal.workspace_id:
            return _error_response(
                query_text,
                [],
                ErrorCode.PERMISSION_DENIED,
                "workspace-scoped principal has no workspace_id — key record is corrupt.",
                retriable=False,
            )

        # R4c symmetric: kb-scoped principal with null kb_id is also corrupt.
        if principal.scope_kind == ScopeKind.kb and not principal.kb_id:
            return _error_response(
                query_text,
                [],
                ErrorCode.PERMISSION_DENIED,
                "kb-scoped principal has no kb_id — key record is corrupt.",
                retriable=False,
            )

        # Service-level scope containment (M-061/T-02, fail closed):
        # enforce that a KB-scoped principal can only query their own KB.
        # This is the innermost safety gate — even if the FastAPI dependency
        # layer already checked, the service re-checks so the library is
        # independently safe regardless of call site.
        try:
            authorize(principal, Action.query_kb, kb_id=kb_id)
        except AuthError:
            return _error_response(
                query_text,
                [],
                ErrorCode.PERMISSION_DENIED,
                f"Principal does not have access to knowledge base '{kb_id}'.",
                retriable=False,
            )

    # ------------------------------------------------------------------
    # R3: For workspace-scoped principals, resolve alias record FIRST so we
    # can verify KB→workspace containment before touching the index.
    # This pre-alias-resolution workspace check must fire BEFORE the tenancy
    # scope is built and BEFORE the adapter is called.
    # ------------------------------------------------------------------
    if auth_enabled and principal is not None:
        from finecorpus.control.auth import ScopeKind

        if principal.scope_kind == ScopeKind.workspace and principal.workspace_id:
            # Resolve alias record to check workspace containment.
            _pre_repo = AliasRepository(session)
            _pre_alias = alias_name(kb_id)
            try:
                _pre_record = _pre_repo.get(_pre_alias)
            except Exception:
                _pre_record = None

            if _pre_record is not None and hasattr(_pre_record, "workspace_id"):
                if _pre_record.workspace_id and _pre_record.workspace_id != principal.workspace_id:
                    # KB belongs to a different workspace — PERMISSION_DENIED, not no_matches.
                    return _error_response(
                        query_text,
                        [],
                        ErrorCode.PERMISSION_DENIED,
                        (
                            f"Knowledge base '{kb_id}' belongs to workspace "
                            f"'{_pre_record.workspace_id}', not the principal's "
                            f"workspace '{principal.workspace_id}'."
                        ),
                        retriable=False,
                    )

    # ------------------------------------------------------------------
    # Admin fail-closed: admin-role principals MUST supply a break-glass
    # grant to read content.  Without a valid grant, content access is denied.
    # This enforces §2.3: admin content reads require the grant, the grant
    # requires a reason, every read is audited.
    # ------------------------------------------------------------------
    _is_break_glass_read = False
    _bg_grant_record = None

    if auth_enabled and principal is not None:
        from finecorpus.control.auth import Role, ScopeKind

        if principal.role == Role.admin:
            # Admin must have a break-glass grant to read content.
            if not break_glass_grant_id:
                return _error_response(
                    query_text,
                    [],
                    ErrorCode.PERMISSION_DENIED,
                    (
                        "Admin principals must supply a break-glass grant to read content (§2.3). "
                        "Request a grant via POST /admin/break-glass/grant with a stated reason."
                    ),
                    retriable=False,
                )

            # Validate the grant.
            from finecorpus.control.break_glass import BreakGlassRepository

            bg_repo = BreakGlassRepository(session)
            grant = bg_repo.active_grant_for(kb_id, principal.principal_id)
            if grant is None or grant.grant_id != break_glass_grant_id:
                return _error_response(
                    query_text,
                    [],
                    ErrorCode.PERMISSION_DENIED,
                    (
                        f"Break-glass grant '{break_glass_grant_id}' is not active for "
                        f"KB '{kb_id}' and admin '{principal.principal_id}'. "
                        "The grant may have expired or been revoked."
                    ),
                    retriable=False,
                )

            _is_break_glass_read = True
            _bg_grant_record = grant

    # Resolve tenancy scope from the authenticated principal (M-060):
    # workspace_id and permission_principals come from the principal, never
    # from client-supplied request body fields.
    #
    # R2: global-scope principals must NOT add a permission_principals filter.
    # Global scope means platform-wide access — injecting the admin's own
    # principal_id into the filter would match zero chunks (chunks carry the
    # ingesting service key's ID, not the admin's ID).  The permission_principals
    # clause is only meaningful for workspace- and kb-scoped principals whose ID
    # was written into the chunk's tenancy block at ingest time.
    if auth_enabled and principal is not None:
        from finecorpus.control.auth import ScopeKind

        if principal.scope_kind == ScopeKind.global_:
            # Global scope: no permission_principals restriction (platform-wide admin access).
            scope = TenancyScope(
                kb_id=kb_id,
                workspace_id="",  # global scope does not filter by workspace
                permission_principals=(),  # omit — all content is readable
            )
        else:
            scope = TenancyScope(
                kb_id=kb_id,
                workspace_id=principal.workspace_id or "",
                permission_principals=(principal.principal_id,),
            )
    else:
        scope = TenancyScope(kb_id=kb_id)

    # For break-glass reads, bypass permission_principals in the filter.
    if _is_break_glass_read:
        tenancy_filter = _build_break_glass_tenancy_filter(scope)
    else:
        tenancy_filter = _build_tenancy_filter(scope)
    tenancy_af = _tenancy_applied_filter(scope)
    filters_applied = [tenancy_af]

    # -----------------------------------------------------------------------
    # Step 1: resolve alias record from control plane
    # -----------------------------------------------------------------------
    repo = AliasRepository(session)
    alias_str = alias_name(kb_id)

    try:
        record: AliasRecord | None = repo.get(alias_str)
    except Exception as exc:
        logger.error("Control-plane read failed for alias '%s': %s", alias_str, exc)
        return _error_response(
            query_text,
            filters_applied,
            ErrorCode.CONTROL_PLANE_UNAVAILABLE,
            "Control-plane database unavailable.",
            retriable=True,
        )

    if record is None or record.collection_name is None:
        return _error_response(
            query_text,
            filters_applied,
            ErrorCode.KB_NOT_READY,
            f"Knowledge base '{kb_id}' has no promoted collection yet. "
            "Run ingestion and promote before querying.",
            retriable=False,
        )

    # -----------------------------------------------------------------------
    # Step 2: mismatch check (§15, provider-abstraction §3.3)
    # Before embedding anything — no provider call on mismatch.
    # -----------------------------------------------------------------------
    caps = provider.capabilities
    record_model = record.embedding_model or ""
    record_dims = record.embedding_dimensions or 0

    if caps.model_id != record_model or caps.vector_dimensions != record_dims:
        return _error_response(
            query_text,
            filters_applied,
            ErrorCode.EMBEDDING_MODEL_MISMATCH,
            (
                f"Query provider model '{caps.model_id}' "
                f"(dimensions={caps.vector_dimensions}) does not match "
                f"alias '{alias_str}' model '{record_model}' "
                f"(dimensions={record_dims}). "
                "Reindex with the current provider or reconfigure to match."
            ),
            retriable=False,
        )

    # -----------------------------------------------------------------------
    # Step 3: embed query (with cache)
    # -----------------------------------------------------------------------
    query_vector: list[float] | None = cache.get(
        model_id=caps.model_id,
        vector_dimensions=caps.vector_dimensions,
        api_version=caps.api_version,
        query_text=query_text,
    )

    if query_vector is None:
        # Cache miss — call provider
        try:
            result = provider.embed_batch([query_text], model_id=caps.model_id)
        except ProviderUnavailableError as exc:
            logger.warning(
                "Embedding provider '%s' unavailable at query time: %s",
                caps.provider_id,
                exc,
            )
            return _error_response(
                query_text,
                filters_applied,
                ErrorCode.PROVIDER_UNAVAILABLE,
                f"Embedding provider '{caps.provider_id}' is unavailable. "
                "Please retry; if the problem persists, check provider health.",
                retriable=True,
            )
        query_vector = result.embeddings[0]
        # Store in cache (only on success — F-007)
        cache.put(
            model_id=caps.model_id,
            vector_dimensions=caps.vector_dimensions,
            api_version=caps.api_version,
            query_text=query_text,
            embedding=query_vector,
        )
    else:
        logger.debug("Query embedding cache hit for model '%s'", caps.model_id)

    # -----------------------------------------------------------------------
    # Step 4: search via alias (C-3) — single call site for tenancy parity
    # (M-063: tenancy ALWAYS inside the vector-search filter for ALL paths).
    # -----------------------------------------------------------------------
    # For explain mode we always over-fetch so we can show all candidates
    # that were considered after tenancy filtering, before Python-level filters.
    # For score_threshold we also over-fetch so filtered_to_zero is correct.
    if explain or score_threshold is not None:
        search_top_k = min(top_k * _OVER_FETCH_MULTIPLIER, _OVER_FETCH_CAP)
    else:
        search_top_k = top_k

    try:
        raw_results = _execute_search(
            alias_str=alias_str,
            query_vector=query_vector,
            tenancy_filter=tenancy_filter,
            search_top_k=search_top_k,
            adapter=adapter,
        )
    except (IndexError, AliasNotFoundError) as exc:
        logger.warning("Vector DB search failed for alias '%s': %s", alias_str, exc)
        return _error_response(
            query_text,
            filters_applied,
            ErrorCode.VECTOR_DB_UNAVAILABLE,
            "Vector database is unavailable or the alias could not be resolved. "
            "Please retry; if the problem persists, check Qdrant health.",
            retriable=True,
        )

    # -----------------------------------------------------------------------
    # Step 5: classify results into the §15 four-way taxonomy
    # -----------------------------------------------------------------------
    if not raw_results:
        # The vector search itself returned nothing — corpus has no relevant content.
        return RetrievalResponse(
            schema_version=_SCHEMA_VERSION,
            request_echo=RequestEcho(
                query=query_text,
                filters_applied=filters_applied,
            ),
            result_status=ResultStatus.no_matches,
            results=[],
            explain=_build_explain_block(
                query_text=query_text,
                raw_results=[],
                candidates=[],
                exclusions=[],
            )
            if explain
            else None,
        )

    # ------------------------------------------------------------------
    # Apply non-tenancy filters (score_threshold) in Python over the
    # over-fetched candidate set — so per-filter attribution is exact
    # (explain mode requirement).  The tenancy filter stays in the vector
    # search; its exclusions are structurally invisible (§11.5).
    # ------------------------------------------------------------------

    # Build ExplainCandidates for every tenancy-surviving candidate (before
    # Python-level filters) when explain=True.
    explain_candidates: list[ExplainCandidate] = []
    explain_exclusions: list[ExplainExclusion] = []

    if explain:
        for sr in raw_results:
            payload = sr.payload or {}
            try:
                prov = _provenance_from_payload(payload, chunk_id=sr.chunk_id)
            except _PayloadCorruptError:
                # Skip corrupt candidates in explain mode — don't abort the whole response.
                continue
            permission_resolved_at = _permission_resolved_at_from_payload(payload)
            explain_candidates.append(
                ExplainCandidate(
                    chunk_id=sr.chunk_id,
                    scores=Scores(raw=sr.score),
                    provenance=prov,
                    permission_resolved_at=permission_resolved_at,
                )
            )

    # Apply score threshold
    if score_threshold is not None:
        threshold_filter = AppliedFilter(
            expression=f"score >= {score_threshold}",
            origin=FilterOrigin.request,
        )
        above_threshold = [r for r in raw_results if r.score >= score_threshold]

        if explain:
            # Record which candidates were excluded by the score threshold.
            surviving_ids = {r.chunk_id for r in above_threshold}
            for sr in raw_results:
                if sr.chunk_id not in surviving_ids:
                    explain_exclusions.append(
                        ExplainExclusion(
                            chunk_id=sr.chunk_id,
                            removed_by=threshold_filter,
                        )
                    )

        if not above_threshold:
            # Search found candidates but threshold eliminated ALL of them.
            # This is filtered_to_zero, NOT no_matches.
            # D-24: increment counter when filter alone empties the set.
            increment_filtered_to_zero()
            effective_filters = [*filters_applied, threshold_filter]
            return RetrievalResponse(
                schema_version=_SCHEMA_VERSION,
                request_echo=RequestEcho(
                    query=query_text,
                    filters_applied=effective_filters,
                ),
                result_status=ResultStatus.filtered_to_zero,
                results=[],
                explain=_build_explain_block(
                    query_text=query_text,
                    raw_results=raw_results,
                    candidates=explain_candidates,
                    exclusions=explain_exclusions,
                )
                if explain
                else None,
            )
        # Keep only the first top_k from the survivors
        kept = above_threshold[:top_k]
        effective_filters = [*filters_applied, threshold_filter]
    else:
        kept = raw_results[:top_k]
        effective_filters = filters_applied

    # -----------------------------------------------------------------------
    # Step 6: build RetrievalResult list with full provenance + trust label
    # -----------------------------------------------------------------------
    retrieval_results: list[RetrievalResult] = []
    for sr in kept:
        payload = sr.payload or {}
        text = payload.get("text", "")
        try:
            provenance = _provenance_from_payload(payload, chunk_id=sr.chunk_id)
        except _PayloadCorruptError as exc:
            logger.error(
                "PAYLOAD_CORRUPT: point '%s' (chunk_id='%s') missing provenance.%s — "
                "skipping result and returning error",
                sr.point_id,
                sr.chunk_id,
                exc.missing,
            )
            return _error_response(
                query_text,
                effective_filters,
                ErrorCode.PAYLOAD_CORRUPT,
                f"A retrieved point has a corrupt payload: missing provenance.{exc.missing}. "
                f"Re-ingest the affected document to repair.",
                retriable=False,
            )

        retrieval_results.append(
            RetrievalResult(
                chunk_id=sr.chunk_id,
                text=text,
                provenance=provenance,
                score=sr.score,
                scores=Scores(raw=sr.score),
                trust_level=TrustLevel.untrusted_ingested,  # §14.1: always
            )
        )

    # -----------------------------------------------------------------------
    # Break-glass audit row (T-05, M-003): append BEFORE returning the response.
    # -----------------------------------------------------------------------
    break_glass_ref: str | None = None
    if _is_break_glass_read and _bg_grant_record is not None and principal is not None:
        from finecorpus.control.audit import AuditAction, AuditLogRepository

        audit_repo = AuditLogRepository(session)
        chunk_ids_returned = [r.chunk_id for r in retrieval_results]
        audit_record = audit_repo.append(
            entry_type=AuditAction.break_glass_read,
            actor_id=principal.principal_id,
            target_kb_id=kb_id,
            details={
                "grant_id": _bg_grant_record.grant_id,
                "query_text": query_text,
                "chunk_ids_returned": chunk_ids_returned,
                "top_k": top_k,
                "score_threshold": score_threshold,
            },
        )
        try:
            session.flush()  # ensure entry_id is available
        except Exception:
            pass
        break_glass_ref = audit_record.entry_id
        logger.info(
            "Break-glass read audited: grant=%s kb=%s admin=%s chunks=%d audit=%s",
            _bg_grant_record.grant_id,
            kb_id,
            principal.principal_id,
            len(chunk_ids_returned),
            break_glass_ref,
        )

    return RetrievalResponse(
        schema_version=_SCHEMA_VERSION,
        request_echo=RequestEcho(
            query=query_text,
            filters_applied=effective_filters,
        ),
        result_status=ResultStatus.matches,
        results=retrieval_results,
        explain=_build_explain_block(
            query_text=query_text,
            raw_results=raw_results,
            candidates=explain_candidates,
            exclusions=explain_exclusions,
        )
        if explain
        else None,
        break_glass_read_ref=break_glass_ref,
    )


def _build_explain_block(
    *,
    query_text: str,
    raw_results: list[SearchResult],
    candidates: list[ExplainCandidate],
    exclusions: list[ExplainExclusion],
) -> ExplainBlock:
    """Build the ExplainBlock for an explain-mode response.

    Args:
        query_text: The query text (used as parsed_query; no expansion in Phase 4).
        raw_results: Raw results from the adapter (unused here, kept for future).
        candidates: Pre-built ExplainCandidate list (all tenancy survivors).
        exclusions: Pre-built ExplainExclusion list (non-tenancy filter exclusions).

    Returns:
        ExplainBlock instance.
    """
    return ExplainBlock(
        parsed_query=query_text,
        candidates=candidates,
        exclusions=exclusions,
        strategy="dense",
    )


# ---------------------------------------------------------------------------
# KB status helper
# ---------------------------------------------------------------------------


@dataclass
class KBStatus:
    """Summary of a knowledge base's index state.

    Returned by get_kb_status(); surfaced via GET /v1/kb/{kb_id}/status.
    No secrets — model identity only (§14.2).

    Attributes:
        kb_id: The knowledge-base ID.
        alias: The alias name (e.g. rtfc_{kb_id}).
        collection_name: Current live collection name, or None if not promoted.
        embedding_provider: Embedding provider ID.
        embedding_model: Embedding model identifier.
        embedding_dimensions: Vector dimensionality.
        config_version: Ingestion config version hash.
        promoted_at: UTC timestamp of the last promotion, or None.
        ready: True iff a promoted collection exists.
    """

    kb_id: str
    alias: str
    collection_name: str | None
    embedding_provider: str | None
    embedding_model: str | None
    embedding_dimensions: int | None
    config_version: str | None
    promoted_at: datetime | None
    ready: bool


def get_kb_status(
    *,
    kb_id: str,
    session: Session,
) -> KBStatus | None:
    """Return the alias record summary for a knowledge base.

    Returns None if no alias record exists for this KB.
    No secrets are included (§14.2).

    Args:
        kb_id: Knowledge-base ID.
        session: SQLAlchemy session bound to the control-plane DB.

    Returns:
        KBStatus or None if the KB is not registered.
    """
    repo = AliasRepository(session)
    alias_str = alias_name(kb_id)
    record = repo.get(alias_str)
    if record is None:
        return None

    return KBStatus(
        kb_id=kb_id,
        alias=alias_str,
        collection_name=record.collection_name,
        embedding_provider=record.embedding_provider,
        embedding_model=record.embedding_model,
        embedding_dimensions=record.embedding_dimensions,
        config_version=record.config_version,
        promoted_at=record.promoted_at,
        ready=record.collection_name is not None,
    )


__all__ = [
    "TenancyScope",
    "KBStatus",
    "query",
    "get_kb_status",
    "_TOP_K_DEFAULT",
    "_TOP_K_MAX",
]
