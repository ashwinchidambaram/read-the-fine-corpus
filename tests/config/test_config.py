"""Tests for finecorpus.config — loader, models, secrets, and preflight.

Coverage mandated by the implementation brief:
  - defaults load from empty file
  - env override beats file value
  - nested env path (FINECORPUS_PROVIDERS__EMBEDDING__CLOUD__MODEL) works
  - unknown key in YAML is an error naming the key
  - plaintext secret in YAML is rejected
  - Config.export() contains no secret values
  - preflight marks unreachable Postgres FAIL with actionable text
  - SKIPPED stubs report SKIPPED (never silently passed)
"""

from __future__ import annotations

import os
import textwrap
from decimal import Decimal
from pathlib import Path

import pytest

from finecorpus.config import Config, load_config, run_preflight
from finecorpus.config.models import LogLevel, ObjectStoreBackend, RetrievalStrategy
from finecorpus.config.preflight import CheckStatus

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def write_yaml(tmp_path: Path, content: str) -> Path:
    """Write *content* to ``corpus.yaml`` inside *tmp_path* and return the path."""
    p = tmp_path / "corpus.yaml"
    p.write_text(textwrap.dedent(content), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# 1. Defaults load from an empty file
# ---------------------------------------------------------------------------


class TestDefaults:
    def test_empty_yaml_loads_built_in_defaults(self, tmp_path: Path) -> None:
        cfg_file = write_yaml(tmp_path, "{}\n")
        cfg = load_config(cfg_file)

        # Platform
        assert cfg.platform.instance_name == "rtfc"
        assert cfg.platform.first_run_complete is False
        assert cfg.platform.airgap is False
        assert cfg.platform.log_level == LogLevel.info
        assert cfg.platform.config_distribution_poll_interval_seconds == 30

        # Storage
        assert cfg.storage.postgres.pool_min == 2
        assert cfg.storage.postgres.pool_max == 10
        assert cfg.storage.qdrant.url == "http://qdrant:6333"
        assert cfg.storage.object_store.backend == ObjectStoreBackend.minio
        assert cfg.storage.object_store.bucket == "rtfc-snapshots"
        assert cfg.storage.object_store.region == "us-east-1"

        # Embedding providers
        assert cfg.providers.embedding.cloud.provider_id == "openai"
        assert cfg.providers.embedding.cloud.model == "text-embedding-3-small"
        assert cfg.providers.embedding.cloud.dimensions == 1536
        assert cfg.providers.embedding.local.model == "nomic-embed-text"
        assert cfg.providers.embedding.local.dimensions == 768
        assert cfg.providers.embedding.local.batch_mode is False

        # Internal LLM
        assert cfg.internal_llm.default.temperature == pytest.approx(0.2)
        assert cfg.internal_llm.max_retries == 3
        assert cfg.internal_llm.operations.classification.temperature == pytest.approx(0.0)
        assert cfg.internal_llm.operations.augmentation.temperature == pytest.approx(0.3)
        assert cfg.internal_llm.operations.question_generation.temperature == pytest.approx(0.7)
        assert cfg.internal_llm.operations.rewriting.temperature == pytest.approx(0.2)

        # Assessment
        assert cfg.assessment.ocr_confidence_exclude_floor == pytest.approx(0.60)
        assert cfg.assessment.ocr_confidence_warn_level == pytest.approx(0.80)
        assert cfg.assessment.boilerplate_corpus_proportion == pytest.approx(0.30)
        assert cfg.assessment.boilerplate_small_corpus_proportion == pytest.approx(0.50)
        assert cfg.assessment.boilerplate_small_corpus_doc_count == 10
        assert cfg.assessment.inline_split_min_lines == 3
        assert cfg.assessment.inline_split_min_chars == 200
        assert cfg.assessment.ocr_sub_decompose_confidence_floor == pytest.approx(0.85)
        assert cfg.assessment.chunk_count_tolerance_pct == pytest.approx(0.20)
        assert cfg.assessment.eval_regression_threshold == pytest.approx(0.05)

        # Index lifecycle
        assert cfg.index_lifecycle.hot_retention_count == 1
        assert cfg.index_lifecycle.cold_retention_count == 2
        assert cfg.index_lifecycle.validation_failed_shadow_retention_days == 7
        assert cfg.index_lifecycle.worker_heartbeat_timeout_seconds == 60
        assert cfg.index_lifecycle.service_principal_key_expiry_days == 90
        assert cfg.index_lifecycle.pricing_stale_warn_days == 90

        # Cache
        assert cfg.cache.query_embedding.enabled is True
        assert cfg.cache.query_embedding.ttl_seconds == 3600

        # Retrieval
        assert cfg.retrieval.default_top_k == 10
        assert cfg.retrieval.max_top_k == 100
        assert cfg.retrieval.default_strategy == RetrievalStrategy.hybrid
        assert cfg.retrieval.supporting_tier_weight == pytest.approx(0.7)

        # Budgets
        assert cfg.budgets.ingestion_confirmation_threshold_usd == Decimal("10.00")
        assert cfg.budgets.sweep_confirmation_threshold_usd == Decimal("5.00")
        assert cfg.budgets.scheduled_reindex_cap_hit_alert_count == 3

        # Observability
        assert cfg.observability.metrics_port == 9090
        assert cfg.observability.otel_service_name == "rtfc"
        assert cfg.observability.log_injection_suspicion_observations is True

    def test_missing_corpus_yaml_loads_defaults(self, tmp_path: Path) -> None:
        """When corpus.yaml is absent (and no explicit path given), defaults apply."""
        original_cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            cfg = load_config()
            assert cfg.platform.instance_name == "rtfc"
        finally:
            os.chdir(original_cwd)


# ---------------------------------------------------------------------------
# 2. File values override built-in defaults
# ---------------------------------------------------------------------------


class TestFileOverride:
    def test_file_value_overrides_default(self, tmp_path: Path) -> None:
        cfg_file = write_yaml(
            tmp_path,
            """
            platform:
              instance_name: "my-corpus"
              log_level: "debug"
            storage:
              postgres:
                pool_max: 20
            retrieval:
              default_top_k: 25
            """,
        )
        cfg = load_config(cfg_file)
        assert cfg.platform.instance_name == "my-corpus"
        assert cfg.platform.log_level == LogLevel.debug
        assert cfg.storage.postgres.pool_max == 20
        assert cfg.retrieval.default_top_k == 25
        # Unmentioned keys keep their defaults
        assert cfg.storage.postgres.pool_min == 2


# ---------------------------------------------------------------------------
# 3. Env overrides beat file values
# ---------------------------------------------------------------------------


class TestEnvOverride:
    def test_simple_env_override_beats_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg_file = write_yaml(tmp_path, "platform:\n  instance_name: 'from-file'\n")
        monkeypatch.setenv("FINECORPUS_PLATFORM__INSTANCE_NAME", "from-env")
        cfg = load_config(cfg_file)
        assert cfg.platform.instance_name == "from-env"

    def test_env_beats_file_for_integer_field(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg_file = write_yaml(tmp_path, "retrieval:\n  default_top_k: 5\n")
        monkeypatch.setenv("FINECORPUS_RETRIEVAL__DEFAULT_TOP_K", "99")
        cfg = load_config(cfg_file)
        assert cfg.retrieval.default_top_k == 99

    def test_env_beats_file_boolean(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg_file = write_yaml(tmp_path, "platform:\n  airgap: false\n")
        monkeypatch.setenv("FINECORPUS_PLATFORM__AIRGAP", "true")
        cfg = load_config(cfg_file)
        assert cfg.platform.airgap is True


# ---------------------------------------------------------------------------
# 4. Nested env path (FINECORPUS_PROVIDERS__EMBEDDING__CLOUD__MODEL)
# ---------------------------------------------------------------------------


class TestNestedEnvPath:
    def test_deep_nested_env_path_works(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg_file = write_yaml(tmp_path, "{}\n")
        monkeypatch.setenv(
            "FINECORPUS_PROVIDERS__EMBEDDING__CLOUD__MODEL",
            "text-embedding-3-large",
        )
        cfg = load_config(cfg_file)
        assert cfg.providers.embedding.cloud.model == "text-embedding-3-large"

    def test_nested_env_path_for_qdrant_url(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg_file = write_yaml(tmp_path, "{}\n")
        monkeypatch.setenv("FINECORPUS_STORAGE__QDRANT__URL", "http://myqdrant:7333")
        cfg = load_config(cfg_file)
        assert cfg.storage.qdrant.url == "http://myqdrant:7333"

    def test_nested_env_path_for_budget(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg_file = write_yaml(tmp_path, "{}\n")
        monkeypatch.setenv("FINECORPUS_BUDGETS__PER_KB_CAP_USD", "50.00")
        cfg = load_config(cfg_file)
        assert cfg.budgets.per_kb_cap_usd == Decimal("50.00")

    def test_nested_env_path_index_lifecycle(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg_file = write_yaml(tmp_path, "{}\n")
        monkeypatch.setenv("FINECORPUS_INDEX_LIFECYCLE__HOT_RETENTION_COUNT", "3")
        cfg = load_config(cfg_file)
        assert cfg.index_lifecycle.hot_retention_count == 3


# ---------------------------------------------------------------------------
# 5. Unknown key in YAML → hard error naming the key
# ---------------------------------------------------------------------------


class TestUnknownKeys:
    def test_unknown_top_level_key_errors(self, tmp_path: Path) -> None:
        cfg_file = write_yaml(tmp_path, "typo_section:\n  foo: bar\n")
        with pytest.raises(ValueError, match="Unknown configuration key: 'typo_section'"):
            load_config(cfg_file)

    def test_unknown_nested_key_errors_with_full_path(self, tmp_path: Path) -> None:
        cfg_file = write_yaml(
            tmp_path,
            """
            platform:
              nonexistent_key: "oops"
            """,
        )
        with pytest.raises(
            ValueError, match="Unknown configuration key: 'platform.nonexistent_key'"
        ):
            load_config(cfg_file)

    def test_unknown_storage_nested_key_errors(self, tmp_path: Path) -> None:
        cfg_file = write_yaml(
            tmp_path,
            """
            storage:
              postgres:
                not_a_real_field: 999
            """,
        )
        with pytest.raises(
            ValueError,
            match="Unknown configuration key: 'storage.postgres.not_a_real_field'",
        ):
            load_config(cfg_file)


# ---------------------------------------------------------------------------
# 6. Plaintext secret in YAML → rejected
# ---------------------------------------------------------------------------


class TestPlaintextSecretRejection:
    def test_plaintext_postgres_url_with_password_rejected(self, tmp_path: Path) -> None:
        cfg_file = write_yaml(
            tmp_path,
            """
            storage:
              postgres:
                url: "postgresql://myuser:mysecretpassword@localhost/mydb"
            """,
        )
        with pytest.raises(ValueError, match="plaintext credential"):
            load_config(cfg_file)

    def test_plaintext_openai_key_in_yaml_rejected(self, tmp_path: Path) -> None:
        """An sk-... key embedded in any YAML string must be rejected."""
        cfg_file = write_yaml(
            tmp_path,
            """
            storage:
              qdrant:
                api_key: "sk-abcdefghijklmnopqrstuvwx12345678"
            """,
        )
        with pytest.raises(ValueError, match="plaintext credential"):
            load_config(cfg_file)

    def test_valid_url_without_password_accepted(self, tmp_path: Path) -> None:
        """A URL without embedded credentials must not be rejected."""
        cfg_file = write_yaml(
            tmp_path,
            """
            storage:
              qdrant:
                url: "http://qdrant:6333"
            """,
        )
        # Should not raise
        cfg = load_config(cfg_file)
        assert cfg.storage.qdrant.url == "http://qdrant:6333"

    def test_env_var_secret_not_checked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Secrets supplied via env var must NOT trigger the secret validator."""
        cfg_file = write_yaml(tmp_path, "{}\n")
        monkeypatch.setenv(
            "FINECORPUS_STORAGE__POSTGRES__URL",
            "postgresql://user:secret@host/db",
        )
        # Should not raise — env vars bypass the YAML secret check
        cfg = load_config(cfg_file)
        assert cfg.storage.postgres.url == "postgresql://user:secret@host/db"


# ---------------------------------------------------------------------------
# 7. Config.export() contains no secret values
# ---------------------------------------------------------------------------


class TestExport:
    def test_export_redacts_postgres_url(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg_file = write_yaml(tmp_path, "{}\n")
        monkeypatch.setenv(
            "FINECORPUS_STORAGE__POSTGRES__URL",
            "postgresql://user:secret@host/db",
        )
        cfg = load_config(cfg_file)
        exported = cfg.export()
        pg_url = exported["storage"]["postgres"]["url"]
        assert pg_url == "<redacted>"
        assert "secret" not in str(pg_url)

    def test_export_redacts_qdrant_api_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg_file = write_yaml(tmp_path, "{}\n")
        monkeypatch.setenv("FINECORPUS_STORAGE__QDRANT__API_KEY", "super-secret-key")
        cfg = load_config(cfg_file)
        exported = cfg.export()
        assert exported["storage"]["qdrant"]["api_key"] == "<redacted>"

    def test_export_redacts_cache_url(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg_file = write_yaml(tmp_path, "{}\n")
        monkeypatch.setenv("FINECORPUS_STORAGE__CACHE__URL", "redis://user:pass@redis:6379")
        cfg = load_config(cfg_file)
        exported = cfg.export()
        assert exported["storage"]["cache"]["url"] == "<redacted>"

    def test_export_non_secret_fields_present(self, tmp_path: Path) -> None:
        cfg_file = write_yaml(tmp_path, "{}\n")
        cfg = load_config(cfg_file)
        exported = cfg.export()
        # Non-secret fields must survive the export
        assert exported["platform"]["instance_name"] == "rtfc"
        assert exported["storage"]["qdrant"]["url"] == "http://qdrant:6333"
        assert exported["retrieval"]["default_top_k"] == 10

    def test_export_no_secrets_when_all_set(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """export() must contain no credential values regardless of how many are set."""
        cfg_file = write_yaml(tmp_path, "{}\n")
        monkeypatch.setenv(
            "FINECORPUS_STORAGE__POSTGRES__URL",
            "postgresql://u:p@h/db",
        )
        monkeypatch.setenv("FINECORPUS_STORAGE__QDRANT__API_KEY", "qdrant-key-xyz")
        monkeypatch.setenv("FINECORPUS_STORAGE__CACHE__URL", "redis://:cachepass@r:6379")
        cfg = load_config(cfg_file)
        exported = cfg.export()
        flat = str(exported)
        # None of the secret values should appear in the serialised export
        for secret in ("postgresql://u:p@h/db", "qdrant-key-xyz", "redis://:cachepass@r:6379"):
            assert secret not in flat


# ---------------------------------------------------------------------------
# 8. Preflight — unreachable Postgres → FAIL with actionable text
# ---------------------------------------------------------------------------


class TestPreflightPostgres:
    def _config_with_closed_port(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
        """Build a Config pointing postgres at a port that is guaranteed closed."""
        cfg_file = write_yaml(tmp_path, "{}\n")
        # Port 19999 is almost certainly not in use; this is a "closed port" test
        monkeypatch.setenv(
            "FINECORPUS_STORAGE__POSTGRES__URL",
            "postgresql://user:pass@127.0.0.1:19999/testdb",
        )
        return load_config(cfg_file)

    def test_unreachable_postgres_is_fail(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = self._config_with_closed_port(tmp_path, monkeypatch)
        report = run_preflight(cfg)
        pg_result = next(r for r in report.results if r.name == "postgres")
        assert pg_result.status == CheckStatus.FAIL

    def test_unreachable_postgres_message_is_actionable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = self._config_with_closed_port(tmp_path, monkeypatch)
        report = run_preflight(cfg)
        pg_result = next(r for r in report.results if r.name == "postgres")
        msg = pg_result.message.lower()
        # Must name the host/port
        assert "127.0.0.1" in msg or "19999" in msg
        # Must suggest a remediation action
        assert any(hint in msg for hint in ("docker", "verify", "running", "storage.postgres.url"))

    def test_preflight_not_passed_when_postgres_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = self._config_with_closed_port(tmp_path, monkeypatch)
        report = run_preflight(cfg)
        assert not report.passed

    def test_postgres_url_missing_is_fail(self, tmp_path: Path) -> None:
        cfg_file = write_yaml(tmp_path, "{}\n")
        cfg = load_config(cfg_file)
        report = run_preflight(cfg)
        pg_result = next(r for r in report.results if r.name == "postgres")
        assert pg_result.status == CheckStatus.FAIL
        assert "storage.postgres.url" in pg_result.message.lower()


# ---------------------------------------------------------------------------
# 9. SKIPPED stubs report SKIPPED (never silently passed as OK)
# ---------------------------------------------------------------------------


class TestSkippedStubs:
    def _basic_config(self, tmp_path: Path) -> Config:
        cfg_file = write_yaml(tmp_path, "{}\n")
        return load_config(cfg_file)

    def test_embedding_provider_check_is_skipped(self, tmp_path: Path) -> None:
        cfg = self._basic_config(tmp_path)
        report = run_preflight(cfg)
        emb = next(r for r in report.results if r.name == "embedding_provider")
        assert emb.status == CheckStatus.SKIPPED

    def test_resource_headroom_check_is_skipped(self, tmp_path: Path) -> None:
        cfg = self._basic_config(tmp_path)
        report = run_preflight(cfg)
        rh = next(r for r in report.results if r.name == "resource_headroom")
        assert rh.status == CheckStatus.SKIPPED

    def test_skipped_checks_appear_in_report(self, tmp_path: Path) -> None:
        """SKIPPED checks must be in the report, not silently omitted."""
        cfg = self._basic_config(tmp_path)
        report = run_preflight(cfg)
        skipped_names = {r.name for r in report.results if r.status == CheckStatus.SKIPPED}
        assert "embedding_provider" in skipped_names
        assert "resource_headroom" in skipped_names

    def test_skipped_checks_have_informative_message(self, tmp_path: Path) -> None:
        cfg = self._basic_config(tmp_path)
        report = run_preflight(cfg)
        for result in report.results:
            if result.status == CheckStatus.SKIPPED:
                assert len(result.message) > 10, (
                    f"SKIPPED check '{result.name}' must have an informative message, "
                    f"got: {result.message!r}"
                )

    def test_config_parse_check_is_ok(self, tmp_path: Path) -> None:
        """The config_parse check must always be OK when Config is loaded successfully."""
        cfg = self._basic_config(tmp_path)
        report = run_preflight(cfg)
        cp = next(r for r in report.results if r.name == "config_parse")
        assert cp.status == CheckStatus.OK


# ---------------------------------------------------------------------------
# 10. Additional edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_explicit_path_not_found_raises(self) -> None:
        with pytest.raises(FileNotFoundError, match="not_a_real_file.yaml"):
            load_config("/tmp/not_a_real_file.yaml")

    def test_corpus_example_yaml_parses(self) -> None:
        """corpus.example.yaml must parse without errors."""
        example = Path(__file__).parents[2] / "corpus.example.yaml"
        assert example.exists(), "corpus.example.yaml not found at worktree root"
        cfg = load_config(example)
        assert cfg.platform.instance_name == "rtfc"

    def test_env_config_path_overrides_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg_file = write_yaml(tmp_path, "platform:\n  instance_name: 'env-path-test'\n")
        monkeypatch.setenv("FINECORPUS_CONFIG_PATH", str(cfg_file))
        cfg = load_config()  # no explicit path
        assert cfg.platform.instance_name == "env-path-test"

    def test_ingestion_dedup_field_via_alias(self, tmp_path: Path) -> None:
        """ingestion.'dedup.index_superseded_versions' can be loaded from YAML."""
        cfg_file = write_yaml(
            tmp_path,
            """
            ingestion:
              "dedup.index_superseded_versions": true
            """,
        )
        cfg = load_config(cfg_file)
        assert cfg.ingestion.dedup_index_superseded_versions is True
