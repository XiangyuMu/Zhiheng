"""Reversible maintenance batches with leases and dry-run accounting."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision = "0021_maintenance_batches"
down_revision: str | Sequence[str] | None = "0020_evolution_learning_loop"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "maintenance_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("idempotency_key", sa.String(128), nullable=False, unique=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="planned"),
        sa.Column("dry_run", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("lease_owner", sa.String(128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("batch_size", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("stats_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "status in ('planned', 'leased', 'running', 'completed', 'failed')",
            name="ck_maintenance_runs_status",
        ),
        sa.CheckConstraint("batch_size > 0", name="ck_maintenance_runs_batch_size"),
    )
    op.create_index(
        "ix_maintenance_runs_status_created",
        "maintenance_runs",
        ["status", "created_at"],
    )
    op.create_table(
        "maintenance_actions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("target_type", sa.String(32), nullable=False),
        sa.Column("target_id", sa.String(36), nullable=False),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("previous_state_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("state", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["run_id"], ["maintenance_runs.id"]),
        sa.UniqueConstraint("run_id", "target_type", "target_id", "action", name="uq_maintenance_action"),
        sa.CheckConstraint(
            "target_type in ('memory_candidate', 'derived_index', 'evolution_artifact')",
            name="ck_maintenance_action_target",
        ),
        sa.CheckConstraint(
            "state in ('pending', 'applied', 'skipped', 'reverted')",
            name="ck_maintenance_action_state",
        ),
    )
    op.create_index(
        "ix_maintenance_actions_run_state",
        "maintenance_actions",
        ["run_id", "state"],
    )


def downgrade() -> None:
    op.drop_index("ix_maintenance_actions_run_state", table_name="maintenance_actions")
    op.drop_table("maintenance_actions")
    op.drop_index("ix_maintenance_runs_status_created", table_name="maintenance_runs")
    op.drop_table("maintenance_runs")
