"""Phase 4 control-plane schema — seven new tables.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-05

Adds the full Phase 4 control-plane schema:
  - service_principal_keys  (§14.2)
  - audit_log               (§13.2, APPEND-ONLY)
  - break_glass_grants      (§2.3, M-001..M-004)
  - tombstone_log           (§17.1, APPEND-ONLY)
  - tombstone_replays       (mutable replay-tracking companion to tombstone_log)
  - job_queue               (§6.6)
  - reindex_triggers        (§10.3)
  - cost_ledger             (§16, M-088)

Immutability:
  - audit_log and tombstone_log have a PG BEFORE UPDATE OR DELETE trigger that
    raises an exception.  The trigger DDL is guarded to PG dialect and is also
    attached via SQLAlchemy event.listen so that create_tables() (used in
    integration tests) fires the same triggers.
  - App-level immutability is ALWAYS enforced by the repository layer
    (AuditLogRepository, TombstoneRepository) regardless of dialect.

Tombstone replay design:
  - tombstone_replays is a separate mutable table keyed (entry_id, collection)
    to keep tombstone_log strictly append-only.  This is the PREFERRED design
    per the Phase 4 spec: replay tracking is mutable state (a new row is added
    when a collection acknowledges the tombstone) while the tombstone_log row
    itself must never be updated or deleted.

Spec references: §2.3, §6.6, §10.3, §13.2, §14.2, §16, §17.1.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# ---------------------------------------------------------------------------
# revision identifiers
# ---------------------------------------------------------------------------

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | None = None
depends_on: str | None = None

# ---------------------------------------------------------------------------
# Immutability trigger SQL (PostgreSQL only)
# ---------------------------------------------------------------------------

_AUDIT_LOG_TRIGGER_FN = """
CREATE OR REPLACE FUNCTION _rtfc_audit_log_immutable()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'audit_log is append-only (entry_id=%)', OLD.entry_id;
END;
$$;
"""

_AUDIT_LOG_TRIGGER = """
CREATE TRIGGER trg_audit_log_immutable
BEFORE UPDATE OR DELETE ON audit_log
FOR EACH ROW EXECUTE FUNCTION _rtfc_audit_log_immutable();
"""

_TOMBSTONE_LOG_TRIGGER_FN = """
CREATE OR REPLACE FUNCTION _rtfc_tombstone_log_immutable()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'tombstone_log is append-only (entry_id=%)', OLD.entry_id;
END;
$$;
"""

_TOMBSTONE_LOG_TRIGGER = """
CREATE TRIGGER trg_tombstone_log_immutable
BEFORE UPDATE OR DELETE ON tombstone_log
FOR EACH ROW EXECUTE FUNCTION _rtfc_tombstone_log_immutable();
"""


def upgrade() -> None:
    """Create Phase 4 control-plane tables."""
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    # JSON type: JSONB on PG, JSON on SQLite/others
    json_type = postgresql.JSONB() if is_pg else sa.JSON()

    # ------------------------------------------------------------------
    # 1. service_principal_keys  (§14.2)
    # ------------------------------------------------------------------
    op.create_table(
        "service_principal_keys",
        sa.Column("key_id", sa.String(64), primary_key=True, nullable=False),
        sa.Column("key_prefix", sa.String(32), nullable=False),
        sa.Column("key_hash", sa.String(64), nullable=False),  # sha256 hex
        sa.Column("principal_name", sa.String(255), nullable=False),
        sa.Column("role", sa.String(64), nullable=False),
        sa.Column("scope_kind", sa.String(32), nullable=False),
        sa.Column("workspace_id", sa.String(64), nullable=True),
        sa.Column("kb_id", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.String(255), nullable=False),
    )
    op.create_index(
        "ix_spk_key_prefix",
        "service_principal_keys",
        ["key_prefix"],
        unique=True,
    )

    # ------------------------------------------------------------------
    # 2. audit_log  (§13.2, APPEND-ONLY)
    # ------------------------------------------------------------------
    op.create_table(
        "audit_log",
        sa.Column("entry_id", sa.String(64), primary_key=True, nullable=False),
        sa.Column("entry_type", sa.String(64), nullable=False),
        sa.Column("actor_id", sa.String(255), nullable=False),
        sa.Column("target_kb_id", sa.String(64), nullable=True),
        sa.Column("target_workspace_id", sa.String(64), nullable=True),
        sa.Column("details", json_type, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_audit_log_target_kb_id", "audit_log", ["target_kb_id"])
    op.create_index("ix_audit_log_target_workspace_id", "audit_log", ["target_workspace_id"])
    op.create_index("ix_audit_log_created_at", "audit_log", ["created_at"])

    # ------------------------------------------------------------------
    # 3. break_glass_grants  (§2.3, M-001..M-004)
    # ------------------------------------------------------------------
    op.create_table(
        "break_glass_grants",
        sa.Column("grant_id", sa.String(64), primary_key=True, nullable=False),
        sa.Column("target_kb_id", sa.String(64), nullable=False),
        sa.Column("granting_admin_id", sa.String(255), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("granted_at", sa.DateTime(timezone=True), nullable=False),
        # expires_at NOT NULL — D-04 ruling: window must be finite
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("notified_principals", json_type, nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("audit_entry_id", sa.String(64), nullable=False),
        sa.ForeignKeyConstraint(
            ["audit_entry_id"],
            ["audit_log.entry_id"],
            name="fk_break_glass_audit_entry",
        ),
    )
    op.create_index("ix_bgg_target_kb_id", "break_glass_grants", ["target_kb_id"])

    # ------------------------------------------------------------------
    # 4. tombstone_log  (§17.1, APPEND-ONLY)
    # ------------------------------------------------------------------
    op.create_table(
        "tombstone_log",
        sa.Column("entry_id", sa.String(64), primary_key=True, nullable=False),
        sa.Column("kb_id", sa.String(64), nullable=False),
        sa.Column("document_id", sa.String(255), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),  # 'delete' | 'purge'
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("deleted_by", sa.String(255), nullable=False),
        sa.Column("snapshots_destroyed", json_type, nullable=True),
    )
    op.create_index("ix_tombstone_log_kb_id", "tombstone_log", ["kb_id"])
    op.create_index("ix_tombstone_log_document_id", "tombstone_log", ["document_id"])

    # ------------------------------------------------------------------
    # 5. tombstone_replays  (mutable replay-tracking; keeps tombstone_log
    #    strictly append-only — preferred design from spec)
    # ------------------------------------------------------------------
    op.create_table(
        "tombstone_replays",
        sa.Column("entry_id", sa.String(64), nullable=False),
        sa.Column("collection", sa.String(255), nullable=False),
        sa.Column("replayed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["entry_id"],
            ["tombstone_log.entry_id"],
            name="fk_tombstone_replay_entry",
        ),
        sa.PrimaryKeyConstraint("entry_id", "collection", name="pk_tombstone_replays"),
    )

    # ------------------------------------------------------------------
    # 6. job_queue  (§6.6)
    # ------------------------------------------------------------------
    op.create_table(
        "job_queue",
        sa.Column("job_id", sa.String(32), primary_key=True, nullable=False),  # ULID
        sa.Column("kb_id", sa.String(64), nullable=False),
        sa.Column("workspace_id", sa.String(64), nullable=False),
        sa.Column("job_type", sa.String(64), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("payload", json_type, nullable=False),
        sa.Column("checkpoint", json_type, nullable=True),
        sa.Column("progress", json_type, nullable=True),
        sa.Column(
            "cost_accrued_usd",
            sa.Numeric(precision=18, scale=8),
            nullable=False,
            server_default="0",
        ),
        sa.Column("claimed_by", sa.String(255), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("dedupe_key", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_msg", sa.Text(), nullable=True),
    )
    op.create_index("ix_job_queue_kb_id", "job_queue", ["kb_id"])
    # Partial unique index: dedupe active jobs per (kb_id, dedupe_key)
    # Active states: queued, claimed, running, paused_budget
    if is_pg:
        op.execute(
            "CREATE UNIQUE INDEX ix_job_queue_dedupe "
            "ON job_queue (kb_id, dedupe_key) "
            "WHERE state IN ('queued', 'claimed', 'running', 'paused_budget')"
        )
    else:
        # SQLite: plain unique index (no WHERE support); acceptable for unit tests
        op.create_index(
            "ix_job_queue_dedupe",
            "job_queue",
            ["kb_id", "dedupe_key"],
            unique=True,
        )

    # ------------------------------------------------------------------
    # 7. reindex_triggers  (§10.3)
    # ------------------------------------------------------------------
    op.create_table(
        "reindex_triggers",
        sa.Column("trigger_id", sa.String(64), primary_key=True, nullable=False),
        sa.Column("kb_id", sa.String(64), nullable=False),
        sa.Column("trigger_type", sa.String(64), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("cron_expr", sa.String(128), nullable=True),
        sa.Column("last_fired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_config_version", sa.Text(), nullable=True),
        sa.Column("consecutive_cap_hits", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index("ix_reindex_triggers_kb_id", "reindex_triggers", ["kb_id"])

    # ------------------------------------------------------------------
    # 8. cost_ledger  (§16, M-088)
    # ------------------------------------------------------------------
    op.create_table(
        "cost_ledger",
        sa.Column("ledger_id", sa.String(64), primary_key=True, nullable=False),
        sa.Column("kb_id", sa.String(64), nullable=False),
        sa.Column("workspace_id", sa.String(64), nullable=False),
        sa.Column("operation_type", sa.String(64), nullable=False),
        sa.Column("cost_usd", sa.Numeric(precision=18, scale=8), nullable=False),
        sa.Column("tokens_consumed", sa.Integer(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_cost_ledger_kb_id", "cost_ledger", ["kb_id"])
    op.create_index("ix_cost_ledger_workspace_id", "cost_ledger", ["workspace_id"])
    op.create_index("ix_cost_ledger_recorded_at", "cost_ledger", ["recorded_at"])

    # ------------------------------------------------------------------
    # PG immutability triggers (audit_log, tombstone_log)
    # ------------------------------------------------------------------
    if is_pg:
        op.execute(_AUDIT_LOG_TRIGGER_FN)
        op.execute(_AUDIT_LOG_TRIGGER)
        op.execute(_TOMBSTONE_LOG_TRIGGER_FN)
        op.execute(_TOMBSTONE_LOG_TRIGGER)


def downgrade() -> None:
    """Drop Phase 4 control-plane tables (reverse order)."""
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    if is_pg:
        op.execute("DROP TRIGGER IF EXISTS trg_tombstone_log_immutable ON tombstone_log")
        op.execute("DROP FUNCTION IF EXISTS _rtfc_tombstone_log_immutable() CASCADE")
        op.execute("DROP TRIGGER IF EXISTS trg_audit_log_immutable ON audit_log")
        op.execute("DROP FUNCTION IF EXISTS _rtfc_audit_log_immutable() CASCADE")

    op.drop_index("ix_cost_ledger_recorded_at", "cost_ledger")
    op.drop_index("ix_cost_ledger_workspace_id", "cost_ledger")
    op.drop_index("ix_cost_ledger_kb_id", "cost_ledger")
    op.drop_table("cost_ledger")

    op.drop_index("ix_reindex_triggers_kb_id", "reindex_triggers")
    op.drop_table("reindex_triggers")

    op.drop_index("ix_job_queue_dedupe", "job_queue")
    op.drop_index("ix_job_queue_kb_id", "job_queue")
    op.drop_table("job_queue")

    op.drop_table("tombstone_replays")

    op.drop_index("ix_tombstone_log_document_id", "tombstone_log")
    op.drop_index("ix_tombstone_log_kb_id", "tombstone_log")
    op.drop_table("tombstone_log")

    op.drop_index("ix_bgg_target_kb_id", "break_glass_grants")
    op.drop_table("break_glass_grants")

    op.drop_index("ix_audit_log_created_at", "audit_log")
    op.drop_index("ix_audit_log_target_workspace_id", "audit_log")
    op.drop_index("ix_audit_log_target_kb_id", "audit_log")
    op.drop_table("audit_log")

    op.drop_index("ix_spk_key_prefix", "service_principal_keys")
    op.drop_table("service_principal_keys")
