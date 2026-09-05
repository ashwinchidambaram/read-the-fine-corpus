"""Observability metrics for Read The Fine Corpus (M-102, §13.2).

Five telemetry domains exposed as prometheus-client collectors:

1. **ingestion** — docs processed/failed/excluded, stage durations, token+cost accrual.
2. **index** — collection sizes, hot/cold inventory, time since reindex, promote/rollback counts.
3. **retrieval** — QPS, latency, errors by cause, cache-hit rate, zero-result rate,
   per-tenant volume.
4. **quality** — NAMED STUBS registered at zero; populated Phase 5 (see docstrings).
5. **governance** — break-glass grants/reads, config changes, promotions/rollbacks,
   permission changes.

Layer placement: this module lives at the top level of ``finecorpus`` — the same layer as
``finecorpus.control`` and ``finecorpus.contracts``.  It must be importable from ALL layers
(cli, services, retrieval, pipeline) without creating a forbidden import cycle.

Import-linter analysis:
- The ``layers`` contract places ``cli | services`` above ``retrieval | pipeline`` above
  ``config | index`` above ``embedding | llm`` above ``control | contracts``.
- A module that imports from ``finecorpus.control`` would be forbidden from importing
  into ``retrieval`` or ``pipeline`` (they sit above control in the layering).
- The ONLY safe placement for a module that is importable everywhere is at the TOP of the
  package (``finecorpus.telemetry``) WITHOUT importing from any finecorpus sub-package at
  module-scope.  The ``exhaustive = false`` setting means unlisted top-level modules are
  not forced into a layer, so ``finecorpus.telemetry`` is effectively opt-in for all layers.
- Governance hook callables (``incr_*`` functions) are plain functions with no imports from
  finecorpus submodules, ensuring zero cycle risk.

Usage:
    from finecorpus.telemetry import (
        # ingestion
        INGESTION_DOCS_PROCESSED, INGESTION_DOCS_FAILED, INGESTION_DOCS_EXCLUDED,
        INGESTION_STAGE_DURATION, INGESTION_TOKENS_TOTAL, INGESTION_COST_USD,
        # index
        INDEX_COLLECTION_SIZE, INDEX_HOT_COUNT, INDEX_COLD_COUNT,
        INDEX_TIME_SINCE_REINDEX, INDEX_PROMOTIONS_TOTAL, INDEX_ROLLBACKS_TOTAL,
        # retrieval
        RETRIEVAL_REQUESTS_TOTAL, RETRIEVAL_LATENCY, RETRIEVAL_ERRORS_TOTAL,
        RETRIEVAL_CACHE_HITS_TOTAL, RETRIEVAL_ZERO_RESULTS_TOTAL, RETRIEVAL_TENANT_VOLUME,
        # governance hook helpers
        incr_break_glass_grant, incr_break_glass_read, incr_config_change,
        incr_promotion, incr_rollback, incr_permission_change,
    )
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

# ---------------------------------------------------------------------------
# 1. INGESTION domain
# ---------------------------------------------------------------------------

INGESTION_DOCS_PROCESSED: Counter = Counter(
    "rtfc_ingestion_docs_processed_total",
    "Total documents processed by the ingestion pipeline (all outcomes).",
    ["kb_id", "workspace_id"],
)

INGESTION_DOCS_FAILED: Counter = Counter(
    "rtfc_ingestion_docs_failed_total",
    "Total documents that failed processing with an error.",
    ["kb_id", "workspace_id", "stage"],
)

INGESTION_DOCS_EXCLUDED: Counter = Counter(
    "rtfc_ingestion_docs_excluded_total",
    "Total documents excluded by plan-stage rules (not errors).",
    ["kb_id", "workspace_id", "reason"],
)

INGESTION_STAGE_DURATION: Histogram = Histogram(
    "rtfc_ingestion_stage_duration_seconds",
    "Wall-clock duration of each ingestion pipeline stage.",
    ["kb_id", "stage"],
    buckets=[0.1, 0.5, 1.0, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0],
)

INGESTION_TOKENS_TOTAL: Counter = Counter(
    "rtfc_ingestion_tokens_total",
    "Total embedding + LLM tokens consumed during ingestion.",
    ["kb_id", "workspace_id", "provider", "token_type"],
)

INGESTION_COST_USD: Counter = Counter(
    "rtfc_ingestion_cost_usd_total",
    "Total estimated USD cost accrued during ingestion.",
    ["kb_id", "workspace_id", "operation_type"],
)

# ---------------------------------------------------------------------------
# 2. INDEX domain
# ---------------------------------------------------------------------------

INDEX_COLLECTION_SIZE: Gauge = Gauge(
    "rtfc_index_collection_size_points",
    "Number of vector points in the live (alias-targeted) collection.",
    ["kb_id", "collection"],
)

INDEX_HOT_COUNT: Gauge = Gauge(
    "rtfc_index_hot_copies",
    "Number of hot (in-memory) collection copies for a KB (should equal hot_retention_count).",
    ["kb_id"],
)

INDEX_COLD_COUNT: Gauge = Gauge(
    "rtfc_index_cold_snapshots",
    "Number of cold snapshots for a KB's collections.",
    ["kb_id"],
)

INDEX_TIME_SINCE_REINDEX: Gauge = Gauge(
    "rtfc_index_time_since_reindex_seconds",
    "Seconds since the last successful promotion for a KB.",
    ["kb_id"],
)

INDEX_PROMOTIONS_TOTAL: Counter = Counter(
    "rtfc_index_promotions_total",
    "Total successful collection promotions (alias retarget).",
    ["kb_id"],
)

INDEX_ROLLBACKS_TOTAL: Counter = Counter(
    "rtfc_index_rollbacks_total",
    "Total rollbacks (alias retarget to previous collection).",
    ["kb_id"],
)

# ---------------------------------------------------------------------------
# 3. RETRIEVAL domain
# ---------------------------------------------------------------------------

RETRIEVAL_REQUESTS_TOTAL: Counter = Counter(
    "rtfc_retrieval_requests_total",
    "Total retrieval query requests received.",
    ["kb_id", "result_status"],
)

RETRIEVAL_LATENCY: Histogram = Histogram(
    "rtfc_retrieval_latency_seconds",
    "End-to-end latency for retrieval queries (embedding + vector search).",
    ["kb_id"],
    buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0],
)

RETRIEVAL_ERRORS_TOTAL: Counter = Counter(
    "rtfc_retrieval_errors_total",
    "Total retrieval errors, by error code.",
    ["kb_id", "error_code"],
)

RETRIEVAL_CACHE_HITS_TOTAL: Counter = Counter(
    "rtfc_retrieval_cache_hits_total",
    "Total query-embedding cache hits (avoided provider round-trips).",
    ["kb_id"],
)

RETRIEVAL_ZERO_RESULTS_TOTAL: Counter = Counter(
    "rtfc_retrieval_zero_results_total",
    "Total queries that returned no results (no_matches or filtered_to_zero).",
    ["kb_id", "reason"],
)

RETRIEVAL_TENANT_VOLUME: Counter = Counter(
    "rtfc_retrieval_tenant_volume_total",
    "Total retrieval requests per tenant (principal_id), for per-tenant billing/quota tracking.",
    ["kb_id", "principal_id"],
)

# ---------------------------------------------------------------------------
# 4. QUALITY domain — NAMED STUBS (populated Phase 5)
# ---------------------------------------------------------------------------
# All quality metrics are registered here with zero initial values and
# comprehensive docstrings so that Phase 5 can increment them without
# touching the registry.  The ``_INFO`` suffix comments explain what each
# metric will track when implementation arrives.

QUALITY_RETRIEVAL_PRECISION_AT_K: Gauge = Gauge(
    "rtfc_quality_retrieval_precision_at_k",
    (
        "[STUB — Phase 5] Mean precision@k measured against curated eval sets. "
        "Populated by the offline evaluation harness; zero until Phase 5 ships eval."
    ),
    ["kb_id"],
)

QUALITY_RETRIEVAL_RECALL_AT_K: Gauge = Gauge(
    "rtfc_quality_retrieval_recall_at_k",
    ("[STUB — Phase 5] Mean recall@k measured against curated eval sets. Zero until Phase 5."),
    ["kb_id"],
)

QUALITY_INJECTION_FLAGGED_DOCS: Counter = Counter(
    "rtfc_quality_injection_flagged_docs_total",
    (
        "[STUB — Phase 5] Documents flagged with non-zero injection_suspicion score "
        "during ingestion. Will be incremented by the assess stage when Phase 5 "
        "lands the injection-pattern detector."
    ),
    ["kb_id"],
)

QUALITY_PII_FLAGGED_DOCS: Counter = Counter(
    "rtfc_quality_pii_flagged_docs_total",
    (
        "[STUB — Phase 5] Documents where PII detection found at least one sensitive field. "
        "Zero until Phase 5 PII detection is implemented."
    ),
    ["kb_id"],
)

QUALITY_EVAL_SET_SIZE: Gauge = Gauge(
    "rtfc_quality_eval_set_size",
    (
        "[STUB — Phase 5] Number of curated (query, expected-chunk) pairs in the eval set "
        "for a KB. Populated by 'corpus eval import'; zero until Phase 5."
    ),
    ["kb_id"],
)

# ---------------------------------------------------------------------------
# 5. GOVERNANCE domain
# ---------------------------------------------------------------------------

GOVERNANCE_BREAK_GLASS_GRANTS_TOTAL: Counter = Counter(
    "rtfc_governance_break_glass_grants_total",
    "Total break-glass access grants (§2.3). Each grant is an audited emergency escalation.",
    ["kb_id", "actor_id"],
)

GOVERNANCE_BREAK_GLASS_READS_TOTAL: Counter = Counter(
    "rtfc_governance_break_glass_reads_total",
    "Total content reads performed under an active break-glass grant.",
    ["kb_id", "actor_id"],
)

GOVERNANCE_CONFIG_CHANGES_TOTAL: Counter = Counter(
    "rtfc_governance_config_changes_total",
    "Total ingestion-config changes committed (M-026/M-071).",
    ["kb_id"],
)

GOVERNANCE_PROMOTIONS_TOTAL: Counter = Counter(
    "rtfc_governance_promotions_total",
    "Total collection promotions from the governance perspective (mirrors INDEX_PROMOTIONS_TOTAL; "
    "kept separate so the governance dashboard does not depend on the index domain).",
    ["kb_id", "actor_id"],
)

GOVERNANCE_ROLLBACKS_TOTAL: Counter = Counter(
    "rtfc_governance_rollbacks_total",
    "Total collection rollbacks from the governance perspective.",
    ["kb_id", "actor_id"],
)

GOVERNANCE_PERMISSION_CHANGES_TOTAL: Counter = Counter(
    "rtfc_governance_permission_changes_total",
    "Total RBAC permission changes (key issue/revoke/rotate, scope changes).",
    ["kb_id", "change_type"],
)

# ---------------------------------------------------------------------------
# Governance hook helpers — callable from control repos and services
# ---------------------------------------------------------------------------
# These thin wrappers provide a stable calling convention so that callers
# do not need to know label ordering or counter names.


def incr_break_glass_grant(*, kb_id: str, actor_id: str) -> None:
    """Increment GOVERNANCE_BREAK_GLASS_GRANTS_TOTAL for a grant event."""
    GOVERNANCE_BREAK_GLASS_GRANTS_TOTAL.labels(kb_id=kb_id, actor_id=actor_id).inc()


def incr_break_glass_read(*, kb_id: str, actor_id: str) -> None:
    """Increment GOVERNANCE_BREAK_GLASS_READS_TOTAL for a content read under break-glass."""
    GOVERNANCE_BREAK_GLASS_READS_TOTAL.labels(kb_id=kb_id, actor_id=actor_id).inc()


def incr_config_change(*, kb_id: str) -> None:
    """Increment GOVERNANCE_CONFIG_CHANGES_TOTAL for a config commit."""
    GOVERNANCE_CONFIG_CHANGES_TOTAL.labels(kb_id=kb_id).inc()


def incr_promotion(*, kb_id: str, actor_id: str) -> None:
    """Increment GOVERNANCE_PROMOTIONS_TOTAL and INDEX_PROMOTIONS_TOTAL for a promotion."""
    GOVERNANCE_PROMOTIONS_TOTAL.labels(kb_id=kb_id, actor_id=actor_id).inc()
    INDEX_PROMOTIONS_TOTAL.labels(kb_id=kb_id).inc()


def incr_rollback(*, kb_id: str, actor_id: str) -> None:
    """Increment GOVERNANCE_ROLLBACKS_TOTAL and INDEX_ROLLBACKS_TOTAL for a rollback."""
    GOVERNANCE_ROLLBACKS_TOTAL.labels(kb_id=kb_id, actor_id=actor_id).inc()
    INDEX_ROLLBACKS_TOTAL.labels(kb_id=kb_id).inc()


def incr_permission_change(*, kb_id: str, change_type: str) -> None:
    """Increment GOVERNANCE_PERMISSION_CHANGES_TOTAL for a key/RBAC change.

    Args:
        kb_id: KB whose permission changed (or "global" for platform-scoped changes).
        change_type: One of "key_issued", "key_revoked", "key_rotated", "scope_change".
    """
    GOVERNANCE_PERMISSION_CHANGES_TOTAL.labels(kb_id=kb_id, change_type=change_type).inc()


__all__ = [
    # ingestion
    "INGESTION_COST_USD",
    "INGESTION_DOCS_EXCLUDED",
    "INGESTION_DOCS_FAILED",
    "INGESTION_DOCS_PROCESSED",
    "INGESTION_STAGE_DURATION",
    "INGESTION_TOKENS_TOTAL",
    # index
    "INDEX_COLD_COUNT",
    "INDEX_COLLECTION_SIZE",
    "INDEX_HOT_COUNT",
    "INDEX_PROMOTIONS_TOTAL",
    "INDEX_ROLLBACKS_TOTAL",
    "INDEX_TIME_SINCE_REINDEX",
    # retrieval
    "RETRIEVAL_CACHE_HITS_TOTAL",
    "RETRIEVAL_ERRORS_TOTAL",
    "RETRIEVAL_LATENCY",
    "RETRIEVAL_REQUESTS_TOTAL",
    "RETRIEVAL_TENANT_VOLUME",
    "RETRIEVAL_ZERO_RESULTS_TOTAL",
    # quality stubs
    "QUALITY_EVAL_SET_SIZE",
    "QUALITY_INJECTION_FLAGGED_DOCS",
    "QUALITY_PII_FLAGGED_DOCS",
    "QUALITY_RETRIEVAL_PRECISION_AT_K",
    "QUALITY_RETRIEVAL_RECALL_AT_K",
    # governance
    "GOVERNANCE_BREAK_GLASS_GRANTS_TOTAL",
    "GOVERNANCE_BREAK_GLASS_READS_TOTAL",
    "GOVERNANCE_CONFIG_CHANGES_TOTAL",
    "GOVERNANCE_PERMISSION_CHANGES_TOTAL",
    "GOVERNANCE_PROMOTIONS_TOTAL",
    "GOVERNANCE_ROLLBACKS_TOTAL",
    # governance hooks
    "incr_break_glass_grant",
    "incr_break_glass_read",
    "incr_config_change",
    "incr_permission_change",
    "incr_promotion",
    "incr_rollback",
]
