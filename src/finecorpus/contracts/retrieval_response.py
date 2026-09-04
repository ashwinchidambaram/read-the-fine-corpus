"""Contract 7 — Retrieval response.

Stage boundary: Serve → caller.
See docs/contracts/retrieval-response.md for the authoritative spec
(§11, §11.5, §14.1, §15, §8/§12).

This is the Serve→caller envelope: the shape the retrieval API (REST, MCP, Python client)
returns for a query. Exists because §15 requires the response to distinguish "no matches"
from "filtered to nothing" from "error".
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

from finecorpus.contracts.shared.blocks import Provenance, TrustLevel

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class ResultStatus(StrEnum):
    """The §15 four-way distinction — required, never inferred from an empty results list.

    See docs/contracts/retrieval-response.md RetrievalResponse.result_status.

    - matches: At least one chunk matched and survived all filters.
    - no_matches: The vector search itself returned nothing (corpus has no relevant content).
    - filtered_to_zero: Search found candidates but every one was removed by a filter.
    - error: A fail-closed condition occurred (embedding-model mismatch, DB unavailable, etc.).
    """

    matches = "matches"
    no_matches = "no_matches"
    filtered_to_zero = "filtered_to_zero"
    error = "error"


class FilterOrigin(StrEnum):
    """Where a filter came from (§11.5).

    See docs/contracts/retrieval-response.md AppliedFilter.origin.
    """

    request = "request"
    """Caller-supplied filter."""
    kb_config = "kb_config"
    """KB-level default filter."""
    tenancy = "tenancy"
    """Mandatory tenancy/permission clause — always present (§11.4)."""


class ErrorCode(StrEnum):
    """Error taxonomy for fail-closed conditions (§15).

    See docs/contracts/retrieval-response.md ErrorCode.
    EMBEDDING_MODEL_MISMATCH and VECTOR_DB_UNAVAILABLE are the two §15 fail-closed
    paths most likely to be mishandled as "empty result"; they MUST surface as
    result_status=error, never as no_matches.

    Schema version: 1.1 (CONTROL_PLANE_UNAVAILABLE and PAYLOAD_CORRUPT added).
    """

    EMBEDDING_MODEL_MISMATCH = "EMBEDDING_MODEL_MISMATCH"
    """Query embedded with a model != the alias's model identity — fail closed (§15)."""
    VECTOR_DB_UNAVAILABLE = "VECTOR_DB_UNAVAILABLE"
    """Qdrant unreachable — fail closed, do not serve stale/unscoped (§15)."""
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    """Embedding provider down at query time — fail closed, no model fallback (§15)."""
    KB_NOT_READY = "KB_NOT_READY"
    """Alias points at nothing yet (index-lifecycle §2.2)."""
    PERMISSION_DENIED = "PERMISSION_DENIED"
    """Authenticated principal has no access to the requested scope."""
    INVALID_QUERY = "INVALID_QUERY"
    """Malformed request."""
    CONTROL_PLANE_UNAVAILABLE = "CONTROL_PLANE_UNAVAILABLE"
    """Control-plane database (Postgres) unreachable at query time — fail closed, retriable."""
    PAYLOAD_CORRUPT = "PAYLOAD_CORRUPT"
    """A Qdrant point payload is missing required provenance fields (source_document_id /
    source_document_version).  The point is corrupt and cannot be served safely."""


# ---------------------------------------------------------------------------
# Sub-models
# ---------------------------------------------------------------------------


class AppliedFilter(BaseModel):
    """One filter in effect, with its origin.

    See docs/contracts/retrieval-response.md AppliedFilter.
    Tenancy-origin filters are the mandatory must clauses of the tenant-isolation mechanism
    and are always present.
    """

    expression: str = Field(description="Human-readable rendering of the filter predicate.")
    origin: FilterOrigin = Field(
        description=(
            "Where the filter came from (§11.5): a caller-supplied request filter, "
            "a kb_config default, or a mandatory tenancy clause."
        )
    )


class RequestEcho(BaseModel):
    """The query as received and the filters actually applied, with each filter's origin (§11.5).

    See docs/contracts/retrieval-response.md RequestEcho.
    """

    query: str = Field(description="The query text as received.")
    filters_applied: list[AppliedFilter] = Field(
        description=(
            "Every filter in effect, with its origin — "
            "so the caller can see what narrowed the search (§11.5)."
        )
    )


class Scores(BaseModel):
    """Component scores (raw / reranked).

    See docs/contracts/retrieval-response.md Scores.
    Always populated in explain mode.
    """

    raw: float = Field(description="Pre-rerank retrieval score.")
    reranked: float | None = Field(
        default=None,
        description="Post-rerank score when a reranker ran.",
    )


class RetrievalResult(BaseModel):
    """One returned chunk with score and full provenance.

    See docs/contracts/retrieval-response.md RetrievalResult.

    Invariants:
    - Provenance complete (§8, §12) — the full block the chunk carries.
    - trust_level always surfaced (§14.1).
    """

    chunk_id: str = Field(description="The chunk's deterministic ID (chunk.md).")
    text: str = Field(
        description=("The served chunk text (the Tier-1-normalized canonical text; chunk.md).")
    )
    provenance: Provenance = Field(
        description=(
            "The full §8 provenance block (README.md#provenance-block-provenance), "
            "including transformation history, confidence, salience, and the salience signal trace."
        )
    )
    score: float = Field(description="Final relevance score for this result.")
    scores: Scores | None = Field(
        default=None,
        description=(
            "Component scores (raw / reranked) when available; always populated in explain mode."
        ),
    )
    trust_level: TrustLevel = Field(
        description=(
            "The §14.1 trust label — carried through from the chunk; "
            "all ingested content is untrusted_ingested. "
            "Surfaced so a caller/agent knows the material is untrusted."
        )
    )


class ErrorEnvelope(BaseModel):
    """Error detail; present only when result_status=error.

    See docs/contracts/retrieval-response.md ErrorEnvelope.
    Errors fail closed (§15): the platform returns an error rather than degrading.
    """

    code: ErrorCode = Field(description="The error taxonomy.")
    message: str = Field(description="Human-readable detail; never leaks a secret (§14.2).")
    retriable: bool = Field(
        description="Whether the caller may retry (e.g. transient vector-DB unavailability)."
    )


class ExplainCandidate(BaseModel):
    """One candidate with raw and reranked scores, for explain mode (§11.5).

    See docs/contracts/retrieval-response.md ExplainCandidate.
    Explain mode respects the same tenancy and permission rules as ordinary retrieval — it is
    not a bypass (§11.5); it never contains cross-tenant candidates, exclusions, or scores.
    """

    chunk_id: str = Field(description="Candidate chunk.")
    scores: Scores = Field(description="Raw and reranked scores.")
    provenance: Provenance = Field(
        description=(
            "Full provenance for the candidate (§11.5 requires provenance for every candidate)."
        )
    )


class ExplainExclusion(BaseModel):
    """One excluded chunk with the specific filter that removed it (§11.5).

    See docs/contracts/retrieval-response.md ExplainExclusion.
    So a KB editor can tell a filter problem from a chunking problem (§11.5).
    """

    chunk_id: str = Field(description="The excluded chunk.")
    removed_by: AppliedFilter = Field(
        description=(
            "The specific filter (with origin) that removed this chunk — "
            "so a KB editor can tell a filter problem from a chunking problem (§11.5)."
        )
    )


class ExplainBlock(BaseModel):
    """Explain-mode extension (§11.5); present only for explain-mode queries.

    See docs/contracts/retrieval-response.md ExplainBlock.
    Respects the same tenancy and permission rules as ordinary retrieval — explain is
    not a bypass (§11.5).
    """

    parsed_query: str = Field(description="The parsed query and any expansion applied.")
    candidates: list[ExplainCandidate] = Field(
        description="The candidate set considered, each with raw and reranked scores."
    )
    exclusions: list[ExplainExclusion] = Field(
        description="Chunks that were excluded, each paired with the filter that removed it."
    )
    strategy: str = Field(
        description="The retrieval strategy used (dense/sparse/hybrid, rerank, §11.5)."
    )


# ---------------------------------------------------------------------------
# RetrievalResponse (root)
# ---------------------------------------------------------------------------


class RetrievalResponse(BaseModel):
    """Contract 7 root model — produced by Serve, returned to the caller.

    See docs/contracts/retrieval-response.md RetrievalResponse (root).

    Invariants:
    - result_status is required and authoritative. The caller never infers status from
      an empty results list; the four-way enum is always set (§15).
    - Provenance complete on every returned result and every explain candidate (§8, §12).
    - Trust label always surfaced on every RetrievalResult (§14.1).
    - Fail closed: EMBEDDING_MODEL_MISMATCH and VECTOR_DB_UNAVAILABLE surface as
      result_status=error, never as an empty success (§15).
    - No cross-tenant data, ever. The response — including explain candidates, exclusions,
      and scores — contains only content within the authenticated principal's scope (§11.5).
    """

    schema_version: str = Field(description="Contract version (semver).")
    request_echo: RequestEcho = Field(
        description=(
            "The query as received and the filters actually applied, "
            "with each filter's origin (§11.5)."
        )
    )
    result_status: ResultStatus = Field(
        description=(
            "Required. The §15 distinction (matches/no_matches/filtered_to_zero/error). "
            "Never inferred by the caller from an empty list — it is explicit."
        )
    )
    results: list[RetrievalResult] = Field(
        description=(
            "The returned chunks with scores and full provenance. "
            "Empty when result_status is no_matches, filtered_to_zero, or error."
        )
    )
    error: ErrorEnvelope | None = Field(
        default=None,
        description="Present iff result_status=error. Carries the error code taxonomy (§15).",
    )
    explain: ExplainBlock | None = Field(
        default=None,
        description="Present only when the query requested explain mode (§11.5).",
    )


__all__ = [
    "ResultStatus",
    "FilterOrigin",
    "ErrorCode",
    "AppliedFilter",
    "RequestEcho",
    "Scores",
    "RetrievalResult",
    "ErrorEnvelope",
    "ExplainCandidate",
    "ExplainExclusion",
    "ExplainBlock",
    "RetrievalResponse",
]
