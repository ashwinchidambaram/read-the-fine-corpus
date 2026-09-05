"""Pydantic v2 configuration model tree for Read The Fine Corpus.

Each nested model mirrors a top-level section of ``corpus.yaml``.  Default
values are the **(proposed)** values from ``docs/configuration/reference.md``;
every field carries a docstring quoting the governing spec section.

Secret-bearing fields (API keys, database passwords) are intentionally typed
``Optional[str] = None``.  A ``model_validator`` in ``Config`` rejects any
field whose value looks like a plaintext credential — secrets must arrive via
environment variables, never the YAML file.

The ``Config`` model carries a ``@model_validator(mode="after")`` that sweeps
the dumped dict and rejects plaintext credentials regardless of how the model
was constructed.  The known env-only credential fields
(``storage.postgres.url``, ``storage.qdrant.api_key``, ``storage.cache.url``)
are **exempt** from the model-validator sweep because the validator cannot
distinguish an env-supplied secret from a YAML-supplied one — the file-level
scan in ``load_config()`` (which runs *before* env-var merging) is the
authoritative gate for YAML-sourced secrets on those fields.  All other string
fields are checked by both the model validator and ``load_config()``.
"""

from __future__ import annotations

import contextvars
import re
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, model_validator

# Context variable used by load_config() to suppress the model-validator secret
# sweep (load_config already ran the file-level scan before env-var merging).
_SKIP_MODEL_SECRET_CHECK: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "_rtfc_skip_model_secret_check", default=False
)

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class LogLevel(StrEnum):
    """Valid log levels (§13)."""

    debug = "debug"
    info = "info"
    warn = "warn"
    error = "error"


class ObjectStoreBackend(StrEnum):
    """Supported object-storage backends (§10.2)."""

    s3 = "s3"
    minio = "minio"
    gcs = "gcs"
    azure_blob = "azure_blob"


class CacheBackend(StrEnum):
    """Supported cache backends (§11.3, OQ-C-1 unresolved)."""

    redis = "redis"
    postgres = "postgres"


class RetrievalStrategy(StrEnum):
    """Retrieval strategy options (§11.2)."""

    dense = "dense"
    sparse = "sparse"
    hybrid = "hybrid"


# ---------------------------------------------------------------------------
# 2.1 Platform
# ---------------------------------------------------------------------------


class PlatformConfig(BaseModel):
    """Platform identity and global behaviour (§4.6)."""

    instance_name: str = Field(
        default="rtfc",
        description=("§4.6 — Platform identity; used in collection naming prefix ``rtfc_``."),
    )
    first_run_complete: bool = Field(
        default=False,
        description=(
            "§4.6 — Set to ``true`` by the first-run prompt after provider validation. "
            "Do not set manually."
        ),
    )
    airgap: bool = Field(
        default=False,
        description=(
            "§4.3, §7.2 — Equivalent to ``RTFC_AIRGAP=true``. Blocks all outbound HTTP "
            "before any provider call. All providers must have ``is_local: true``. "
            "In airgap mode, pricing-fetch is unconditionally skipped and cost estimates "
            "use declared ``cost_per_1k_tokens`` (null for local providers)."
        ),
    )
    log_level: LogLevel = Field(
        default=LogLevel.info,
        description="§13 — Structured log level for all services.",
    )
    config_distribution_poll_interval_seconds: int = Field(
        default=30,
        description=(
            "§4.6, ADR-0005 — How often services poll the control-plane database for "
            "config changes. Setting to 0 disables polling (restart required for config "
            "changes). **(proposed)**"
        ),
    )


# ---------------------------------------------------------------------------
# 2.2 Storage backends
# ---------------------------------------------------------------------------


class PostgresConfig(BaseModel):
    """PostgreSQL connection settings (§4.1)."""

    url: str | None = Field(
        default=None,
        description=(
            "§4.1 — PostgreSQL connection URL. Supply via env var; never write the password here."
        ),
    )
    pool_min: int = Field(
        default=2,
        description="§4.1 — Minimum connection pool size. **(proposed)**",
    )
    pool_max: int = Field(
        default=10,
        description="§4.1 — Maximum connection pool size. **(proposed)**",
    )


class QdrantConfig(BaseModel):
    """Qdrant vector-database connection settings (§4.4)."""

    url: str = Field(
        default="http://qdrant:6333",
        description="§4.4 — Qdrant gRPC/HTTP endpoint.",
    )
    api_key: str | None = Field(
        default=None,
        description=(
            "§4.4 — Qdrant API key if Qdrant is configured with authentication. Supply via env var."
        ),
    )


class ObjectStoreConfig(BaseModel):
    """Object-storage settings for cold snapshots and source-document retention (§10.2)."""

    backend: ObjectStoreBackend = Field(
        default=ObjectStoreBackend.minio,
        description="§10.2 — Object storage backend. **(proposed)**",
    )
    bucket: str = Field(
        default="rtfc-snapshots",
        description="§10.2 — Bucket / container name. **(proposed)**",
    )
    prefix: str = Field(
        default="",
        description="§10.2 — Key prefix within the bucket.",
    )
    endpoint_url: str | None = Field(
        default=None,
        description=(
            "§10.2 — Override endpoint (required for MinIO; leave unset for AWS S3). "
            "Supply via env var."
        ),
    )
    region: str = Field(
        default="us-east-1",
        description="§10.2 — Region for S3/GCS/Azure backends. **(proposed)**",
    )


class CacheStorageConfig(BaseModel):
    """Cache-backend connection settings (§11.3, OQ-C-1 unresolved)."""

    backend: CacheBackend | None = Field(
        default=None,
        description=(
            "§11.3, OQ-C-1 — Backing store for the query-embedding cache. "
            "Decision (Redis vs Postgres vs in-process) is unresolved before Phase 4."
        ),
    )
    url: str | None = Field(
        default=None,
        description=("§11.3 — Connection URL for the cache backend. Supply via env var."),
    )


class StorageConfig(BaseModel):
    """All storage-backend settings (§4.1, §4.4, §10.2, §11.3)."""

    postgres: PostgresConfig = Field(default_factory=PostgresConfig)
    qdrant: QdrantConfig = Field(default_factory=QdrantConfig)
    object_store: ObjectStoreConfig = Field(default_factory=ObjectStoreConfig)
    cache: CacheStorageConfig = Field(default_factory=CacheStorageConfig)


# ---------------------------------------------------------------------------
# 2.3 Providers — Embedding
# ---------------------------------------------------------------------------


class EmbeddingCloudConfig(BaseModel):
    """Cloud (OpenAI) embedding provider settings (§7.1)."""

    provider_id: str = Field(
        default="openai",
        description="§7.1 — Provider identifier.",
    )
    model: str = Field(
        default="text-embedding-3-small",
        description=(
            "§7.1, provider-abstraction.md §7.1 — Model identifier passed to the OpenAI API."
        ),
    )
    dimensions: int = Field(
        default=1536,
        description=("provider-abstraction.md §7.1 — Must match the model's output dimensions."),
    )
    max_batch_size: int = Field(
        default=2048,
        description="provider-abstraction.md §7.1 — OpenAI API batch limit.",
    )
    cost_per_1k_tokens: Decimal | None = Field(
        default=None,
        description=(
            "§16, provider-abstraction.md §2.1 — Current price; updated by "
            "``corpus provider update-pricing``."
        ),
    )
    pricing_as_of: str | None = Field(
        default=None,
        description=(
            "provider-abstraction.md OQ-P-5 — Date (ISO 8601) the cost figure was "
            "last verified. Platform warns if more than "
            "``index_lifecycle.pricing_stale_warn_days`` days old. **(proposed)**"
        ),
    )


class EmbeddingLocalConfig(BaseModel):
    """Local (Ollama) embedding provider settings (§7.2)."""

    provider_id: str = Field(
        default="ollama",
        description="§7.2, provider-abstraction.md §7.2 — Local provider identifier.",
    )
    endpoint: str = Field(
        default="http://localhost:11434",
        description="provider-abstraction.md §7.2 — Ollama HTTP endpoint.",
    )
    model: str = Field(
        default="nomic-embed-text",
        description=(
            "provider-abstraction.md §7.2 — Ollama model name "
            "(``nomic-embed-text`` or ``bge-m3``). **(proposed)**"
        ),
    )
    dimensions: int = Field(
        default=768,
        description=(
            "provider-abstraction.md §7.2 — Must match the model. "
            "``nomic-embed-text``=768; ``bge-m3``=1024. **(proposed)**"
        ),
    )
    batch_mode: bool = Field(
        default=False,
        description=(
            "provider-abstraction.md OQ-P-3 — Opt-in batch mode for Ollama "
            "(requires minimum Ollama version documented per OQ-P-3). **(proposed)**"
        ),
    )


class EmbeddingProvidersConfig(BaseModel):
    """Embedding provider settings (§7.1, §7.2)."""

    default: str | None = Field(
        default=None,
        description=(
            "§4.6 — Name of the default embedding provider (``openai`` or ``ollama``). "
            "Set by the first-run prompt."
        ),
    )
    cloud: EmbeddingCloudConfig = Field(default_factory=EmbeddingCloudConfig)
    local: EmbeddingLocalConfig = Field(default_factory=EmbeddingLocalConfig)


class ProvidersConfig(BaseModel):
    """All provider configuration (§7)."""

    embedding: EmbeddingProvidersConfig = Field(default_factory=EmbeddingProvidersConfig)


# ---------------------------------------------------------------------------
# 2.4 Providers — Internal LLM
# ---------------------------------------------------------------------------


class LLMOperationConfig(BaseModel):
    """Per-operation LLM overrides (provider-abstraction.md §4.2)."""

    provider: str | None = Field(
        default=None,
        description=(
            "provider-abstraction.md §4.2 — Provider for this operation. "
            "Inherits ``internal_llm.default.provider`` when ``null``."
        ),
    )
    model: str | None = Field(
        default=None,
        description=(
            "provider-abstraction.md §4.2 — Model identifier for this operation. "
            "Inherits ``internal_llm.default.model`` when ``null``."
        ),
    )
    temperature: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description=(
            "provider-abstraction.md §4.2 — Temperature for this operation. "
            "Inherits operation-specific default when ``null``."
        ),
    )


class LLMOperationsConfig(BaseModel):
    """Per-operation LLM configuration overrides (provider-abstraction.md §4.2)."""

    classification: LLMOperationConfig = Field(
        default_factory=lambda: LLMOperationConfig(temperature=0.0),
        description=(
            "provider-abstraction.md §4.2 — Classification: determinism strongly "
            "preferred (temperature 0.0). **(proposed)**"
        ),
    )
    augmentation: LLMOperationConfig = Field(
        default_factory=lambda: LLMOperationConfig(temperature=0.3),
        description=(
            "provider-abstraction.md §4.2 — Table description and breadcrumb blurb "
            "generation. **(proposed)**"
        ),
    )
    question_generation: LLMOperationConfig = Field(
        default_factory=lambda: LLMOperationConfig(temperature=0.7),
        description=("provider-abstraction.md §4.2 — Eval question generation. **(proposed)**"),
    )
    rewriting: LLMOperationConfig = Field(
        default_factory=lambda: LLMOperationConfig(temperature=0.2),
        description=(
            "provider-abstraction.md §4.2 — Tier 3 rewriting. Local providers strongly "
            "preferred for sensitive corpora. **(proposed)**"
        ),
    )


class InternalLLMDefaultConfig(BaseModel):
    """Default internal LLM provider and model (§7.3)."""

    provider: str | None = Field(
        default=None,
        description=(
            "§7.3, provider-abstraction.md §4.2 — Default internal LLM provider "
            "(``openai`` or ``ollama``)."
        ),
    )
    model: str | None = Field(
        default=None,
        description=("§7.3, provider-abstraction.md §4.2 — Exact model identifier."),
    )
    endpoint: str | None = Field(
        default=None,
        description=(
            "§7.3 — Ollama HTTP endpoint for internal LLM calls.  "
            "Used when ``provider='ollama'``.  Defaults to ``http://localhost:11434`` "
            "when ``null``.  Not a secret — a plain URL.  "
            "The registry falls back to this field via ``getattr(llm_cfg, 'endpoint', None)``."
        ),
    )
    temperature: float = Field(
        default=0.2,
        ge=0.0,
        le=1.0,
        description=(
            "§7.3 — Temperature for internal model calls. The only place temperature "
            "appears in the product. **(proposed)**"
        ),
    )
    max_output_tokens: int = Field(
        default=1024,
        gt=0,
        description=(
            "provider-abstraction.md §4.3, D-21(b) — Hard ceiling on output tokens per "
            "internal LLM call.  Prevents runaway completions.  Budget enforcement "
            "(§16 cost cap) is deferred to Phase 4; this ceiling is the Phase 3 guard. "
            "**(proposed)**"
        ),
    )


class InternalLLMConfig(BaseModel):
    """Internal LLM configuration (§7.3, provider-abstraction.md §4.2)."""

    default: InternalLLMDefaultConfig = Field(default_factory=InternalLLMDefaultConfig)
    operations: LLMOperationsConfig = Field(default_factory=LLMOperationsConfig)
    max_retries: int = Field(
        default=3,
        description=(
            "§15, provider-abstraction.md §4.3 — Retry limit before the operation is "
            "recorded as a failure. **(proposed)**"
        ),
    )


# ---------------------------------------------------------------------------
# 2.5 Ingestion and assessment thresholds
# ---------------------------------------------------------------------------


class AssessmentConfig(BaseModel):
    """Platform-level assessment and quality thresholds (§6.2, §6.3, §6.4, §10.4)."""

    ocr_confidence_exclude_floor: float = Field(
        default=0.60,
        ge=0.0,
        le=1.0,
        description=(
            "§6.2, §6.4 — ``scanned_region`` segments below this page-level OCR "
            "confidence are assigned tier ``excluded``. **(proposed)**"
        ),
    )
    ocr_confidence_warn_level: float = Field(
        default=0.80,
        ge=0.0,
        le=1.0,
        description=(
            "§6.2, §6.4 — Segments between ``ocr_confidence_exclude_floor`` and this "
            "level are assigned tier ``supporting`` with a low-confidence flag. "
            "Above this level: normal tier assignment. **(proposed)**"
        ),
    )
    boilerplate_corpus_proportion: float = Field(
        default=0.30,
        ge=0.0,
        le=1.0,
        description=(
            "§6.2 — A text block appearing in more than this proportion of corpus "
            "documents is classified as ``boilerplate``. **(proposed)**"
        ),
    )
    boilerplate_small_corpus_proportion: float = Field(
        default=0.50,
        ge=0.0,
        le=1.0,
        description=(
            "§6.2 — Boilerplate threshold for corpora smaller than "
            "``boilerplate_small_corpus_doc_count``. Raises the bar to avoid false "
            "positives in small corpora. **(proposed)**"
        ),
    )
    boilerplate_small_corpus_doc_count: int = Field(
        default=10,
        description=(
            "§6.2 — Corpus size (document count) below which the small-corpus "
            "boilerplate proportion applies. **(proposed)**"
        ),
    )
    boilerplate_abs_floor_count: int = Field(
        default=3,
        description=(
            "§6.2, D-32 — Branch (b) absolute-floor: minimum total corpus occurrences "
            "(counting within-document repetitions) for the absolute-floor boilerplate "
            "branch. A block appearing at least this many times triggers branch (b) when "
            "``boilerplate_abs_floor_fraction`` is also met. **(proposed)**"
        ),
    )
    boilerplate_abs_floor_fraction: float = Field(
        default=0.05,
        ge=0.0,
        le=1.0,
        description=(
            "§6.2, D-32 — Branch (b) absolute-floor: minimum unique-document fraction "
            "for the absolute-floor boilerplate branch. Prevents a block appearing 3+ "
            "times within a single document in a large corpus from being classified as "
            "corpus-wide boilerplate. **(proposed)**"
        ),
    )
    inline_split_min_lines: int = Field(
        default=3,
        description=(
            "§6.3 — Inline minority content shorter than this line count is absorbed "
            "into the parent segment type. **(proposed)**"
        ),
    )
    inline_split_min_chars: int = Field(
        default=200,
        description=(
            "§6.3 — Inline minority content shorter than this character count is "
            "absorbed into the parent segment type. Both ``inline_split_min_lines`` "
            "and ``inline_split_min_chars`` must be exceeded for a split to occur. "
            "**(proposed)**"
        ),
    )
    ocr_sub_decompose_confidence_floor: float = Field(
        default=0.85,
        ge=0.0,
        le=1.0,
        description=(
            "§6.3 — Page-level OCR confidence above which the platform attempts "
            "sub-decomposition of a ``scanned_region`` into typed sub-segments. "
            "**(proposed)**"
        ),
    )
    chunk_count_tolerance_pct: float = Field(
        default=0.20,
        ge=0.0,
        le=1.0,
        description=(
            "§10.4 — Validation gate 1: the shadow collection's chunk count must be "
            "within this fraction of the expected count (±20% by default). **(proposed)**"
        ),
    )
    eval_regression_threshold: float = Field(
        default=0.05,
        ge=0.0,
        le=1.0,
        description=(
            "§10.4, §9.4 — Validation gate 4: the shadow collection's eval baseline "
            "score must not fall more than this amount below the prior live baseline. "
            "**(proposed)**"
        ),
    )
    sweep_min_corpus_docs: int | None = Field(
        default=None,
        description=(
            "§9.3 — Below this document count, the platform declines to run a "
            "configuration sweep. Executor-proposed; must be documented before Phase 5. "
            "**(proposed — pending executor design)**"
        ),
    )


class DedupSettings(BaseModel):
    """Deduplication sub-settings for the ingestion section (§6.1, §6.3)."""

    index_superseded_versions: bool = Field(
        default=False,
        description=(
            "§6.1, §6.3 — When ``false`` (default), superseded near-duplicate documents "
            "are inventoried but produce no segments and no chunks. When ``true``, "
            "superseded documents are indexed at salience tier ``excluded``. "
            "Source: owner ruling D-25, spec §6.1/§6.3."
        ),
    )


class IngestionConfig(BaseModel):
    """Platform-level ingestion settings (§6.1)."""

    dedup: DedupSettings = Field(default_factory=DedupSettings)


# ---------------------------------------------------------------------------
# 2.6 Index lifecycle and retention
# ---------------------------------------------------------------------------


class IndexLifecycleConfig(BaseModel):
    """Index lifecycle and retention settings (§10.2, §14.2, §15)."""

    hot_retention_count: int = Field(
        default=1,
        description=(
            "§10.2 — Number of previous collections to retain hot in Qdrant memory "
            "(N-1 count). Default 1 = instant rollback. Set to 0 to disable hot standby "
            "(saves RAM; loses instant rollback — UI warns). **(proposed)**"
        ),
    )
    cold_retention_count: int = Field(
        default=2,
        description=(
            "§10.2 — Number of cold snapshots to retain in object storage before "
            "purging the oldest. **(proposed)**"
        ),
    )
    validation_failed_shadow_retention_days: int = Field(
        default=7,
        description=(
            "§15 — How long a ``VALIDATION_FAILED`` shadow collection is retained in "
            "Qdrant before being automatically cold-snapshotted and deleted. Operator "
            "is notified before automatic purge. **(proposed)**"
        ),
    )
    validation_failed_shadow_notify_hours_before: int = Field(
        default=24,
        description=(
            "§15 — Hours before automatic purge of a VALIDATION_FAILED shadow at "
            "which the operator is notified. **(proposed)**"
        ),
    )
    worker_heartbeat_timeout_seconds: int = Field(
        default=60,
        description=(
            "§15 — If an ingestion worker misses a heartbeat for this duration, it is "
            "declared dead and the job is eligible for resumption by another worker. "
            "**(proposed)**"
        ),
    )
    service_principal_key_expiry_days: int = Field(
        default=90,
        description=(
            "§14.2 — Default expiry for newly issued service principal keys. **(proposed)**"
        ),
    )
    service_principal_key_expiry_warn_days: int = Field(
        default=7,
        description=(
            "§14.2 — Days before service principal key expiry at which a warning is "
            "issued. **(proposed)**"
        ),
    )
    pricing_stale_warn_days: int = Field(
        default=90,
        description=(
            "§16 — Platform warns if ``providers.embedding.cloud.pricing_as_of`` is "
            "older than this many days. **(proposed)**"
        ),
    )
    scheduled_reindex_cron: str | None = Field(
        default=None,
        description=(
            "§10.3 — Default cron expression for scheduled reindex trigger. "
            "``null`` / empty disables scheduled reindex at platform level."
        ),
    )
    config_distribution_poll_interval_seconds: int = Field(
        default=30,
        description=(
            "ADR-0005 — Aliases ``platform.config_distribution_poll_interval_seconds`` "
            "— same setting. **(proposed)**"
        ),
    )
    break_glass_grant_window_hours: int | None = Field(
        default=None,
        description=(
            "§2.3 — Default time-bound window for a Platform Admin break-glass "
            "content-read grant. Must be finite. "
            "**(pending Open Decision #4 — OQ-C-2)**"
        ),
    )
    snapshot_retention_period_days: int | None = Field(
        default=None,
        description=(
            "§17.1 — Maximum age of a cold snapshot before it is eligible for "
            "automatic purge (bounds deleted-content persistence). "
            "**(pending Open Decision #5 — OQ-C-3)**"
        ),
    )


# ---------------------------------------------------------------------------
# 2.7 Query embedding cache
# ---------------------------------------------------------------------------


class QueryEmbeddingCacheConfig(BaseModel):
    """Query-embedding cache settings (§11.3)."""

    enabled: bool = Field(
        default=True,
        description=("§11.3 — Enable the query-embedding cache. Disable only for debugging."),
    )
    ttl_seconds: int = Field(
        default=3600,
        description=(
            "§11.3, provider-abstraction.md §5.3 — Cache entry TTL in seconds. **(proposed)**"
        ),
    )
    max_entries: int | None = Field(
        default=None,
        description=(
            "§11.3 — Maximum number of cache entries before LRU eviction. "
            "Executor-proposed value pending Phase 4 sizing. "
            "**(proposed — pending executor design — OQ-C-4)**"
        ),
    )


class CacheConfig(BaseModel):
    """Cache configuration (§11.3)."""

    query_embedding: QueryEmbeddingCacheConfig = Field(default_factory=QueryEmbeddingCacheConfig)


# ---------------------------------------------------------------------------
# 2.8 Retrieval
# ---------------------------------------------------------------------------


class RetrievalRateLimitConfig(BaseModel):
    """Retrieval rate-limiting settings (§11.3)."""

    queries_per_second_per_tenant: int | None = Field(
        default=None,
        description=(
            "§11.3 — Per-tenant query rate limit. Executor-proposed value pending "
            "Phase 4 design. **(proposed — pending executor design — OQ-C-4)**"
        ),
    )


class RetrievalConfig(BaseModel):
    """Retrieval behaviour settings (§11.2)."""

    default_top_k: int = Field(
        default=10,
        description=(
            "§11.2 — Default number of chunks returned per query when not specified "
            "by the caller. **(proposed)**"
        ),
    )
    max_top_k: int = Field(
        default=100,
        description=("§11.2 — Maximum top-k a caller may request. **(proposed)**"),
    )
    default_strategy: RetrievalStrategy = Field(
        default=RetrievalStrategy.hybrid,
        description=(
            "§11.2 — Default retrieval strategy at KB level. Per-class and per-request "
            "overrides available. **(proposed)**"
        ),
    )
    supporting_tier_weight: float = Field(
        default=0.7,
        ge=0.0,
        le=1.0,
        description=(
            "§11.2, segment-taxonomy.md §3.1 — Score weighting applied to "
            "``supporting``-tier chunks relative to ``primary`` chunks. Configurable "
            "at KB level; caller may override per query. **(proposed)**"
        ),
    )
    rate_limit: RetrievalRateLimitConfig = Field(default_factory=RetrievalRateLimitConfig)


# ---------------------------------------------------------------------------
# 2.9 Budgets and cost governance
# ---------------------------------------------------------------------------


class BudgetsConfig(BaseModel):
    """Budget and cost-governance settings (§16)."""

    per_kb_cap_usd: Decimal | None = Field(
        default=None,
        description=(
            "§16 — Per-knowledge-base budget cap in USD. Ingestion, reindex, and "
            "sweep operations pause when this cap is reached. ``null`` = no cap."
        ),
    )
    per_workspace_cap_usd: Decimal | None = Field(
        default=None,
        description=("§16 — Per-workspace budget cap in USD. ``null`` = no cap."),
    )
    ingestion_confirmation_threshold_usd: Decimal = Field(
        default=Decimal("10.00"),
        description=(
            "§16 — Operations estimated to cost more than this amount require explicit "
            "operator confirmation before starting. **(proposed)**"
        ),
    )
    sweep_confirmation_threshold_usd: Decimal = Field(
        default=Decimal("5.00"),
        description=(
            "§16, §9.3 — Configuration sweeps estimated to cost more than this amount "
            "require explicit confirmation. **(proposed)**"
        ),
    )
    scheduled_reindex_cap_hit_alert_count: int = Field(
        default=3,
        description=(
            "§16 — After this many consecutive budget-cap hits on scheduled reindexes, "
            "the Workspace Owner receives a repeated-cap-hit alert. **(proposed)**"
        ),
    )


# ---------------------------------------------------------------------------
# 2.10 Observability
# ---------------------------------------------------------------------------


class ObservabilityConfig(BaseModel):
    """Observability and telemetry settings (§13.3)."""

    metrics_port: int = Field(
        default=9090,
        description=("§13.3 — Port for the Prometheus-compatible metrics endpoint. **(proposed)**"),
    )
    otel_endpoint: str | None = Field(
        default=None,
        description=(
            "§13.3 — OpenTelemetry collector endpoint. Leave unset to disable trace export."
        ),
    )
    otel_service_name: str = Field(
        default="rtfc",
        description="§13.3 — Service name reported to the OTEL collector.",
    )
    drift_detection_cron: str | None = Field(
        default=None,
        description=(
            "§9.4 — Cron expression for the scheduled drift-detection eval run. "
            "Complements the per-promotion eval gate."
        ),
    )
    log_injection_suspicion_observations: bool = Field(
        default=True,
        description=(
            "§14.1 — Log security observations when augmentation or rewriting outputs "
            "contain injection-shaped content in reasoning/diff_summary fields. "
            "Does not affect pipeline behaviour; audit only."
        ),
    )


# ---------------------------------------------------------------------------
# Secret-detection helpers
# ---------------------------------------------------------------------------

# Heuristic patterns that look like plaintext credentials.
# A match triggers a hard validation error.
_SECRET_PATTERNS: list[re.Pattern[str]] = [
    # postgres DSN with embedded password  postgresql://user:pass@host/db
    re.compile(r"postgres(?:ql)?://[^:]+:[^@]+@", re.IGNORECASE),
    # mysql / mariadb DSN
    re.compile(r"mysql://[^:]+:[^@]+@", re.IGNORECASE),
    # generic URI with password segment  scheme://user:password@
    re.compile(r"\w+://[^:@/\s]+:[^@/\s]+@", re.IGNORECASE),
    # sk- style OpenAI / Anthropic API keys
    re.compile(r"\bsk-[A-Za-z0-9\-_]{20,}\b"),
    # AWS access-key-like tokens
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    # generic high-entropy token (32+ hex chars) — only when field name signals secret
    re.compile(r"\b[0-9a-fA-F]{32,}\b"),
]

# Field names that use the high-entropy pattern as a signal (more aggressive check)
_HIGH_ENTROPY_SECRET_FIELDS: frozenset[str] = frozenset(
    {"api_key", "access_key", "secret_key", "api_token"}
)


def _is_secret_value(field_name: str, value: str) -> bool:
    """Return ``True`` if *value* looks like a plaintext credential.

    Applies heuristic regex patterns.  For fields named in
    ``_HIGH_ENTROPY_SECRET_FIELDS`` the high-entropy hex pattern is also tested.
    For ``url`` fields only URI-with-password patterns are checked (a plain
    hostname URL such as ``http://qdrant:6333`` must not be rejected).
    """
    is_key_field = field_name in _HIGH_ENTROPY_SECRET_FIELDS

    for pattern in _SECRET_PATTERNS:
        # Skip the high-entropy hex pattern for non-key fields to avoid
        # flagging legitimate values like hex colours or UUIDs.
        if pattern.pattern.startswith(r"\b[0-9a-fA-F]") and not is_key_field:
            continue
        if pattern.search(value):
            return True
    return False


# ---------------------------------------------------------------------------
# Root config model
# ---------------------------------------------------------------------------


class Config(BaseModel):
    """Root configuration model for Read The Fine Corpus.

    Loaded from ``corpus.yaml`` with environment variable overrides
    (prefix ``FINECORPUS_``, separator ``__``).  Precedence: env > file > defaults.

    Secret-bearing fields (API keys, DB passwords with credentials embedded in
    the URL) MUST be supplied via environment variables.  A model validator
    rejects plaintext credentials in the YAML file (§14.2).
    """

    platform: PlatformConfig = Field(default_factory=PlatformConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    providers: ProvidersConfig = Field(default_factory=ProvidersConfig)
    internal_llm: InternalLLMConfig = Field(default_factory=InternalLLMConfig)
    assessment: AssessmentConfig = Field(default_factory=AssessmentConfig)
    ingestion: IngestionConfig = Field(default_factory=IngestionConfig)
    index_lifecycle: IndexLifecycleConfig = Field(default_factory=IndexLifecycleConfig)
    cache: CacheConfig = Field(default_factory=CacheConfig)
    retrieval: RetrievalConfig = Field(default_factory=RetrievalConfig)
    budgets: BudgetsConfig = Field(default_factory=BudgetsConfig)
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)

    @model_validator(mode="after")
    def _reject_plaintext_secrets(self) -> Config:
        """Sweep the dumped config dict for plaintext credentials (§14.2).

        This catches direct ``Config(...)`` construction with plaintext secrets
        as well as any code path that bypasses ``load_config()``.

        **Exempted paths**: ``storage.postgres.url``, ``storage.qdrant.api_key``,
        and ``storage.cache.url`` are skipped by this validator.  Those three
        fields are the only documented env-only credential paths; the validator
        cannot distinguish a value that arrived from the YAML file versus one
        injected by the OS environment.  ``load_config()`` runs the authoritative
        YAML-only secret scan *before* env-var merging, so YAML-sourced secrets
        on those paths are caught there with a clearer "key came from YAML" error.
        All other string fields are swept here.

        ``load_config()`` sets ``_SKIP_MODEL_SECRET_CHECK`` (a
        ``contextvars.ContextVar``) to ``True`` before calling
        ``model_validate()``, so the file-level scan is not duplicated for the
        env-merge path.  Direct callers do not set the flag, so the validator
        fires normally.
        """
        if _SKIP_MODEL_SECRET_CHECK.get():
            return self
        dumped = self.model_dump(mode="python")
        _check_secrets_in_dict_skip_paths(dumped, path="", skip=_ENV_ONLY_SECRET_PATHS)
        return self

    def export(self) -> dict[str, Any]:
        """Return a secret-free representation of this configuration.

        All credential-bearing fields are replaced with the placeholder string
        ``"<redacted>"`` so the result can be logged, exported, or committed to
        version control (§14.2).

        Redaction uses two complementary strategies (spec §14.2
        "secret-free by construction"):

        1. **Pattern sweep** — every string value in the exported dict is
           tested against ``_is_secret_value``; any match is redacted
           regardless of field path.  This is the primary backstop that catches
           future credential fields automatically.

        2. **Known-path redaction** — the three known credential paths
           (``storage.postgres.url``, ``storage.qdrant.api_key``,
           ``storage.cache.url``) are unconditionally redacted when non-null,
           covering short/low-entropy secrets (e.g. a plain API token) that the
           pattern sweep might not catch.
        """
        raw = self.model_dump(mode="python")
        # Apply known-path redaction first (covers low-entropy secrets)
        _redact_known_paths(raw)
        # Then sweep every string value for secret patterns (SEC-1 backstop)
        _redact_secret_values(raw)
        return raw


def _check_secrets_in_dict(data: dict[str, Any], path: str) -> None:
    """Recursively walk *data* and raise if any value looks like a plaintext secret.

    Handles nested dicts and lists.  List elements are iterated; dicts within
    lists are recursed into, and strings within lists are checked using the
    parent key as the field-name hint (SEC-2).
    """
    for key, value in data.items():
        current_path = f"{path}.{key}" if path else key
        if isinstance(value, dict):
            _check_secrets_in_dict(value, current_path)
        elif isinstance(value, list):
            _check_secrets_in_list(value, current_path, key)
        elif isinstance(value, str) and value:
            if _is_secret_value(key, value):
                raise ValueError(
                    f"Config key '{current_path}' appears to contain a plaintext "
                    f"credential. Supply secrets via environment variables "
                    f"(FINECORPUS_<PATH>) or a secrets-manager reference, "
                    f"never in corpus.yaml (§14.2)."
                )


def _check_secrets_in_list(items: list[Any], path: str, parent_key: str) -> None:
    """Recursively check list elements for plaintext secrets (SEC-2).

    Dicts within the list are recursed into; plain strings are checked using
    *parent_key* as the field-name hint.
    """
    for i, item in enumerate(items):
        element_path = f"{path}[{i}]"
        if isinstance(item, dict):
            _check_secrets_in_dict(item, element_path)
        elif isinstance(item, list):
            _check_secrets_in_list(item, element_path, parent_key)
        elif isinstance(item, str) and item:
            if _is_secret_value(parent_key, item):
                raise ValueError(
                    f"Config key '{element_path}' appears to contain a plaintext "
                    f"credential. Supply secrets via environment variables "
                    f"(FINECORPUS_<PATH>) or a secrets-manager reference, "
                    f"never in corpus.yaml (§14.2)."
                )


# Known credential paths: documented env-only fields that export() always redacts
# and that the model-validator exempts (it cannot distinguish env vs YAML origin).
_ENV_ONLY_SECRET_PATHS: frozenset[tuple[str, ...]] = frozenset(
    {
        ("storage", "postgres", "url"),
        ("storage", "qdrant", "api_key"),
        ("storage", "cache", "url"),
    }
)


def _redact_known_paths(data: dict[str, Any]) -> None:
    """In-place redact known credential paths in *data* (first pass in export)."""
    for path in _ENV_ONLY_SECRET_PATHS:
        node = data
        for part in path[:-1]:
            if not isinstance(node, dict) or part not in node:
                break
            node = node[part]
        else:
            leaf = path[-1]
            if isinstance(node, dict) and node.get(leaf) is not None:
                node[leaf] = "<redacted>"


def _redact_secret_values(data: dict[str, Any]) -> None:
    """In-place sweep *data* and redact any string value matching secret patterns (SEC-1).

    Walks the entire dict recursively so that future credential fields are
    automatically covered without updating a denylist.
    """
    for key, value in data.items():
        if isinstance(value, dict):
            _redact_secret_values(value)
        elif isinstance(value, list):
            _redact_secret_values_in_list(data, key, value)
        elif isinstance(value, str) and value != "<redacted>" and _is_secret_value(key, value):
            data[key] = "<redacted>"


def _redact_secret_values_in_list(parent: dict[str, Any], key: str, items: list[Any]) -> None:
    """Walk list elements and redact secret strings in-place."""
    for i, item in enumerate(items):
        if isinstance(item, dict):
            _redact_secret_values(item)
        elif isinstance(item, list):
            _redact_secret_values_in_list(parent, key, item)
        elif isinstance(item, str) and item != "<redacted>" and _is_secret_value(key, item):
            items[i] = "<redacted>"


def _check_secrets_in_dict_skip_paths(
    data: dict[str, Any],
    path: str,
    skip: frozenset[tuple[str, ...]],
) -> None:
    """Like ``_check_secrets_in_dict`` but skips field paths listed in *skip*.

    Used by the ``Config`` model validator to exempt documented env-only
    credential paths from the sweep (SEC-4).
    """
    for key, value in data.items():
        current_path = f"{path}.{key}" if path else key
        current_tuple = tuple(current_path.split("."))
        if current_tuple in skip:
            continue
        if isinstance(value, dict):
            _check_secrets_in_dict_skip_paths(value, current_path, skip)
        elif isinstance(value, list):
            _check_secrets_in_list(value, current_path, key)
        elif isinstance(value, str) and value:
            if _is_secret_value(key, value):
                raise ValueError(
                    f"Config key '{current_path}' appears to contain a plaintext "
                    f"credential. Supply secrets via environment variables "
                    f"(FINECORPUS_<PATH>) or a secrets-manager reference, "
                    f"never in corpus.yaml (§14.2)."
                )
