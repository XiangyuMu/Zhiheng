"""Persist runtime learning signals and clustered knowledge gaps."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision = "0020_evolution_learning_loop"
down_revision: str | Sequence[str] | None = "0019_memory_center_capabilities"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "trajectory_learning_signals",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("trajectory_id", sa.String(length=36), nullable=False),
        sa.Column("evaluation_id", sa.String(length=36), nullable=False),
        sa.Column("signal_key", sa.String(length=64), nullable=False),
        sa.Column("signal_kind", sa.String(length=32), nullable=False),
        sa.Column("attribution_confidence", sa.Float(), nullable=False),
        sa.Column("learning_eligible", sa.Boolean(), nullable=False),
        sa.Column("evidence_refs_json", sa.JSON(), nullable=False),
        sa.Column("details_json", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["trajectory_id"], ["task_trajectories.id"]),
        sa.ForeignKeyConstraint(["evaluation_id"], ["task_evaluations.id"]),
        sa.UniqueConstraint("signal_key", name="uq_trajectory_learning_signals_key"),
        sa.CheckConstraint(
            "attribution_confidence >= 0 AND attribution_confidence <= 1",
            name="ck_trajectory_learning_signals_confidence",
        ),
    )
    op.create_index(
        "ix_trajectory_learning_signals_attribution",
        "trajectory_learning_signals",
        ["signal_kind", "learning_eligible", "created_at"],
    )
    op.create_index(
        "ix_trajectory_learning_signals_trajectory",
        "trajectory_learning_signals",
        ["trajectory_id"],
    )

    op.create_table(
        "knowledge_gap_clusters",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("cluster_key", sa.String(length=64), nullable=False),
        sa.Column("task_family", sa.String(length=128), nullable=True),
        sa.Column("attribution", sa.String(length=64), nullable=False),
        sa.Column("occurrence_count", sa.Integer(), nullable=False),
        sa.Column("evidence_refs_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="open"),
        sa.Column("proposal_id", sa.String(length=36), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("cluster_key", name="uq_knowledge_gap_clusters_key"),
        sa.CheckConstraint("occurrence_count >= 0", name="ck_knowledge_gap_clusters_count"),
        sa.CheckConstraint(
            "status IN ('open', 'proposed', 'resolved', 'dismissed')",
            name="ck_knowledge_gap_clusters_status",
        ),
        sa.ForeignKeyConstraint(["proposal_id"], ["evolution_artifacts.id"]),
    )
    op.create_index(
        "ix_knowledge_gap_clusters_status",
        "knowledge_gap_clusters",
        ["status", "occurrence_count"],
    )


def downgrade() -> None:
    op.drop_index("ix_knowledge_gap_clusters_status", table_name="knowledge_gap_clusters")
    op.drop_table("knowledge_gap_clusters")
    op.drop_index(
        "ix_trajectory_learning_signals_trajectory",
        table_name="trajectory_learning_signals",
    )
    op.drop_index(
        "ix_trajectory_learning_signals_attribution",
        table_name="trajectory_learning_signals",
    )
    op.drop_table("trajectory_learning_signals")
