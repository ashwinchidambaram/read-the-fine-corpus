"""Phase 5 eval persistence — five new tables.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-05

Adds the Phase 5 eval-store schema:
  - eval_sets              (EvalSetRecord — Contract 6 root)
  - eval_questions         (EvalQuestionRecord — per-question with M-086 deletion linkage)
  - eval_baselines         (EvalBaselineRecord — §9.3 naive baseline with M-049 marking)
  - sweep_runs             (SweepRunRecord — eval-sweep experiment run)
  - sweep_ranking_rows     (SweepRankingRow — per-config ranking within a sweep)

Key invariants implemented at the storage layer:
  - M-049: EvalBaselineRepository.upsert_current() marks prior current row with
    measured_against_reference_id when the reference_fingerprint changes.
  - M-086 (Deletion linkage §17.1): EvalSetRepository.remove_questions_for_segments()
    deletes questions whose source_segment_ids intersect the deleted segment set.

JSON columns: JSONB on PostgreSQL, JSON on SQLite (unit-test compat).

Spec references: eval-set.md §9.1, §9.2, §12, §17.1; M-049, M-086.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# ---------------------------------------------------------------------------
# revision identifiers
# ---------------------------------------------------------------------------

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Create Phase 5 eval tables."""
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    # JSON type: JSONB on PG, JSON on SQLite/others
    json_type = postgresql.JSONB() if is_pg else sa.JSON()

    # ------------------------------------------------------------------
    # 1. eval_sets
    # ------------------------------------------------------------------
    op.create_table(
        "eval_sets",
        sa.Column("eval_set_id", sa.String(64), primary_key=True, nullable=False),
        sa.Column("kb_id", sa.String(64), nullable=False),
        sa.Column("workspace_id", sa.String(64), nullable=False),
        sa.Column("schema_version", sa.String(32), nullable=False),
        sa.Column("origin", sa.String(64), nullable=False),
        sa.Column("confidence_level", sa.String(64), nullable=False),
        sa.Column("baseline_ref", json_type, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_eval_sets_kb_id", "eval_sets", ["kb_id"])
    op.create_index("ix_eval_sets_workspace_id", "eval_sets", ["workspace_id"])

    # ------------------------------------------------------------------
    # 2. eval_questions
    # ------------------------------------------------------------------
    op.create_table(
        "eval_questions",
        sa.Column("question_id", sa.String(64), primary_key=True, nullable=False),
        sa.Column(
            "eval_set_id",
            sa.String(64),
            sa.ForeignKey("eval_sets.eval_set_id", name="fk_eq_eval_set"),
            nullable=False,
        ),
        sa.Column("kb_id", sa.String(64), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("question_type", sa.String(64), nullable=False),
        sa.Column("generation_method", sa.String(64), nullable=False),
        # review_status has NO DEFAULT in the contract — stored nullable=False
        sa.Column("review_status", sa.String(64), nullable=False),
        sa.Column("source_segment_ids", json_type, nullable=False),
        sa.Column("expected_segment_ids", json_type, nullable=True),
        sa.Column("source_unknown", sa.Boolean(), nullable=False),
        sa.Column("injection_suspicion", sa.Float(), nullable=True),
        sa.Column("reviewed_by", sa.String(255), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("class_description_ref", sa.String(255), nullable=True),
    )
    op.create_index("ix_eval_questions_eval_set_id", "eval_questions", ["eval_set_id"])
    op.create_index("ix_eval_questions_kb_id", "eval_questions", ["kb_id"])

    # ------------------------------------------------------------------
    # 3. eval_baselines
    # ------------------------------------------------------------------
    op.create_table(
        "eval_baselines",
        sa.Column("id", sa.String(64), primary_key=True, nullable=False),
        sa.Column("kb_id", sa.String(64), nullable=False),
        sa.Column("reference_id", sa.String(255), nullable=False),
        sa.Column("reference_fingerprint", sa.String(255), nullable=False),
        sa.Column(
            "eval_set_id",
            sa.String(64),
            sa.ForeignKey("eval_sets.eval_set_id", name="fk_eb_eval_set"),
            nullable=False,
        ),
        sa.Column("recall", sa.Float(), nullable=False),
        sa.Column("precision", sa.Float(), nullable=False),
        sa.Column("scored_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_current", sa.Boolean(), nullable=False),
        # M-049: set to old reference_id when superseded by a new-fingerprint baseline
        sa.Column("measured_against_reference_id", sa.String(255), nullable=True),
    )
    op.create_index("ix_eval_baselines_kb_id", "eval_baselines", ["kb_id"])
    op.create_index(
        "ix_eval_baselines_reference_fingerprint",
        "eval_baselines",
        ["reference_fingerprint"],
    )
    op.create_index("ix_eval_baselines_eval_set_id", "eval_baselines", ["eval_set_id"])
    # Defense-in-depth: at most one is_current=True row per kb_id.
    # Prevents concurrent upsert_current races from leaving two current rows,
    # which would corrupt M-049 marking and M-100 drift-baseline selection.
    # Matches the partial-unique pattern used for job_queue dedupe in 0002.
    #
    # Migration path: op.execute() is PG-only here (SQLite WHERE syntax on
    # TRUE vs 1 differs; and Alembic's op.execute is not dialect-guarded for
    # SQLite).  The ORM __table_args__ declaration in eval_store.py uses both
    # postgresql_where and sqlite_where so that create_tables() (create_all)
    # emits the correct WHERE-guarded DDL on both backends.  The migration
    # path is only exercised against real PostgreSQL.
    if is_pg:
        op.execute(
            "CREATE UNIQUE INDEX uq_eval_baselines_kb_current "
            "ON eval_baselines (kb_id) WHERE is_current = TRUE"
        )

    # ------------------------------------------------------------------
    # 4. sweep_runs
    # ------------------------------------------------------------------
    op.create_table(
        "sweep_runs",
        sa.Column("id", sa.String(64), primary_key=True, nullable=False),  # ULID
        sa.Column("kb_id", sa.String(64), nullable=False),
        sa.Column("workspace_id", sa.String(64), nullable=False),
        sa.Column("status", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("total_cost_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("confirmed", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("declined_reason", sa.Text(), nullable=True),
    )
    op.create_index("ix_sweep_runs_kb_id", "sweep_runs", ["kb_id"])
    op.create_index("ix_sweep_runs_workspace_id", "sweep_runs", ["workspace_id"])

    # ------------------------------------------------------------------
    # 5. sweep_ranking_rows
    # ------------------------------------------------------------------
    op.create_table(
        "sweep_ranking_rows",
        sa.Column("id", sa.String(64), primary_key=True, nullable=False),
        sa.Column(
            "sweep_run_id",
            sa.String(64),
            sa.ForeignKey("sweep_runs.id", name="fk_srr_sweep_run"),
            nullable=False,
        ),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("config_json", json_type, nullable=False),
        sa.Column("recall", sa.Float(), nullable=False),
        sa.Column("precision", sa.Float(), nullable=False),
        sa.Column("recall_delta", sa.Float(), nullable=False),
        sa.Column("precision_delta", sa.Float(), nullable=False),
        sa.Column("est_cost_usd", sa.Float(), nullable=False),
        sa.Column("index_size", sa.Integer(), nullable=False),
        sa.Column("ingestion_seconds", sa.Float(), nullable=False),
    )
    op.create_index("ix_sweep_ranking_rows_sweep_run_id", "sweep_ranking_rows", ["sweep_run_id"])


def downgrade() -> None:
    """Drop Phase 5 eval tables (reverse order)."""
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    op.drop_index("ix_sweep_ranking_rows_sweep_run_id", "sweep_ranking_rows")
    op.drop_table("sweep_ranking_rows")

    op.drop_index("ix_sweep_runs_workspace_id", "sweep_runs")
    op.drop_index("ix_sweep_runs_kb_id", "sweep_runs")
    op.drop_table("sweep_runs")

    if is_pg:
        op.execute("DROP INDEX IF EXISTS uq_eval_baselines_kb_current")
    op.drop_index("ix_eval_baselines_eval_set_id", "eval_baselines")
    op.drop_index("ix_eval_baselines_reference_fingerprint", "eval_baselines")
    op.drop_index("ix_eval_baselines_kb_id", "eval_baselines")
    op.drop_table("eval_baselines")

    op.drop_index("ix_eval_questions_kb_id", "eval_questions")
    op.drop_index("ix_eval_questions_eval_set_id", "eval_questions")
    op.drop_table("eval_questions")

    op.drop_index("ix_eval_sets_workspace_id", "eval_sets")
    op.drop_index("ix_eval_sets_kb_id", "eval_sets")
    op.drop_table("eval_sets")
