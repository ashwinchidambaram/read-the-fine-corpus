"""Create alias_records table (control-plane index lifecycle §2.3).

Revision ID: 0001
Revises: (initial)
Create Date: 2026-09-03

This is the minimal control-plane schema for Phase 1.  The ``alias_records``
table is the authority for:
  - The current Qdrant collection name an alias points to.
  - The denormalized model identity (provider/model/dimensions/config_version)
    used by the retrieval service for the mismatch-fails-closed check (§15).
  - The N-1 collection retained for instant rollback (§10.3).

The full control-plane schema (jobs, tombstone log, inventory, audit log,
cost ledger) will be added in subsequent phases.

Spec references: index-lifecycle.md §2.3, §4.2, §10.3, §15.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Create the alias_records table."""
    # NOTE: index=True is intentionally absent from all sa.Column() calls below.
    # Indexes are created exclusively via the explicit op.create_index() calls that
    # follow.  Using index=True AND op.create_index() for the same column emits
    # duplicate CREATE INDEX statements which fail on re-runs (F-02).
    op.create_table(
        "alias_records",
        sa.Column("alias", sa.String(255), primary_key=True, nullable=False),
        sa.Column("kb_id", sa.String(64), nullable=False),
        sa.Column("workspace_id", sa.String(64), nullable=False),
        # Current live collection (null until first promotion)
        sa.Column("collection_name", sa.String(255), nullable=True),
        # Build ID of the current collection
        sa.Column("build_id", sa.Integer(), nullable=True),
        # Denormalized model identity (for mismatch-fails-closed check §15)
        sa.Column("embedding_provider", sa.String(128), nullable=True),
        sa.Column("embedding_model", sa.String(255), nullable=True),
        sa.Column("embedding_dimensions", sa.Integer(), nullable=True),
        sa.Column("config_version", sa.Text(), nullable=True),
        # Promotion tracking
        sa.Column("promoted_at", sa.DateTime(timezone=True), nullable=True),
        # N-1 collection + denormalized model identity (written at promote time, §10.3)
        # Stored alongside previous_collection so rollback is a pure in-record swap
        # with no external reads needed (F-01).
        sa.Column("previous_collection", sa.String(255), nullable=True),
        sa.Column("previous_embedding_provider", sa.String(128), nullable=True),
        sa.Column("previous_embedding_model", sa.String(255), nullable=True),
        sa.Column("previous_embedding_dimensions", sa.Integer(), nullable=True),
        sa.Column("previous_config_version", sa.Text(), nullable=True),
    )
    # Index on kb_id for fast KB-based lookups
    op.create_index("ix_alias_records_kb_id", "alias_records", ["kb_id"])
    # Index on workspace_id for workspace-scoped queries
    op.create_index("ix_alias_records_workspace_id", "alias_records", ["workspace_id"])


def downgrade() -> None:
    """Drop the alias_records table."""
    op.drop_index("ix_alias_records_workspace_id", "alias_records")
    op.drop_index("ix_alias_records_kb_id", "alias_records")
    op.drop_table("alias_records")
