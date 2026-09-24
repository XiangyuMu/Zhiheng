"""Complete memory-center metadata, conflict and expiry projections."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision = "0019_memory_center_capabilities"
down_revision: str | Sequence[str] | None = "0018_retrieval_conversations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "memory_candidates", sa.Column("confidence_explanation", sa.Text(), nullable=True)
    )
    op.add_column(
        "memory_candidates", sa.Column("valid_from", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "memory_candidates", sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "memory_candidates",
        sa.Column("time_sensitivity", sa.String(32), nullable=False, server_default="persistent"),
    )
    op.add_column(
        "memory_candidates", sa.Column("extracted_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("formal_memories", sa.Column("confidence_explanation", sa.Text(), nullable=True))
    op.add_column(
        "formal_memories",
        sa.Column("time_sensitivity", sa.String(32), nullable=False, server_default="persistent"),
    )

    op.create_table(
        "memory_candidate_evidence",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("candidate_id", sa.String(36), nullable=False),
        sa.Column("candidate_version_id", sa.String(36), nullable=False),
        sa.Column("conversation_id", sa.String(36), nullable=True),
        sa.Column("message_id", sa.String(36), nullable=True),
        sa.Column("message_start", sa.Integer(), nullable=True),
        sa.Column("message_end", sa.Integer(), nullable=True),
        sa.Column("excerpt", sa.Text(), nullable=True),
        sa.Column("support_type", sa.String(32), nullable=False, server_default="supporting"),
        sa.Column(
            "extracted_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["candidate_id"], ["memory_candidates.id"]),
        sa.ForeignKeyConstraint(["candidate_version_id"], ["memory_candidate_versions.id"]),
        sa.CheckConstraint(
            "message_start IS NULL OR message_start >= 0", name="ck_memory_candidate_evidence_start"
        ),
        sa.CheckConstraint(
            "message_end IS NULL OR message_end >= 0", name="ck_memory_candidate_evidence_end"
        ),
        sa.CheckConstraint(
            "support_type in ('supporting', 'contradicting', 'context')",
            name="ck_memory_candidate_evidence_support",
        ),
    )
    op.create_index(
        "ix_memory_candidate_evidence_candidate",
        "memory_candidate_evidence",
        ["candidate_id", "candidate_version_id"],
    )
    op.create_index(
        "ix_memory_candidate_evidence_conversation",
        "memory_candidate_evidence",
        ["conversation_id", "message_id"],
    )

    op.create_table(
        "memory_conflicts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("state_key", sa.String(128), nullable=False),
        sa.Column("memory_type", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("candidate_id", sa.String(36), nullable=True),
        sa.Column("candidate_version_id", sa.String(36), nullable=True),
        sa.Column("formal_memory_id", sa.String(36), nullable=True),
        sa.Column("formal_version_id", sa.String(36), nullable=True),
        sa.Column("candidate_value_json", sa.Text(), nullable=True),
        sa.Column("formal_value_json", sa.Text(), nullable=True),
        sa.Column("candidate_confidence", sa.Float(), nullable=True),
        sa.Column("formal_confidence", sa.Float(), nullable=True),
        sa.Column("resolution", sa.Text(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["candidate_id"], ["memory_candidates.id"]),
        sa.ForeignKeyConstraint(["candidate_version_id"], ["memory_candidate_versions.id"]),
        sa.ForeignKeyConstraint(["formal_memory_id"], ["formal_memories.id"]),
        sa.ForeignKeyConstraint(["formal_version_id"], ["formal_memory_versions.id"]),
        sa.CheckConstraint(
            "status in ('pending', 'resolved', 'dismissed')",
            name="ck_memory_conflicts_status",
        ),
    )
    op.create_index(
        "ix_memory_conflicts_state_status",
        "memory_conflicts",
        ["state_key", "status"],
    )
    op.create_index(
        "ix_memory_conflicts_candidate",
        "memory_conflicts",
        ["candidate_id", "status"],
    )

    op.create_table(
        "memory_expiry_reminders",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("target_type", sa.String(32), nullable=False),
        sa.Column("target_id", sa.String(36), nullable=False),
        sa.Column("remind_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("kind", sa.String(32), nullable=False, server_default="expiring"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "target_type in ('memory_candidate', 'formal_memory')",
            name="ck_memory_expiry_reminders_target",
        ),
        sa.CheckConstraint(
            "status in ('pending', 'sent', 'dismissed')",
            name="ck_memory_expiry_reminders_status",
        ),
        sa.UniqueConstraint(
            "target_type",
            "target_id",
            "kind",
            name="uq_memory_expiry_reminders_target_kind",
        ),
    )
    op.create_index(
        "ix_memory_expiry_reminders_due",
        "memory_expiry_reminders",
        ["status", "remind_at"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    for table_name in (
        "memory_candidates",
        "memory_candidate_versions",
        "formal_memories",
        "formal_memory_versions",
        "memory_candidate_evidence",
        "memory_conflicts",
        "memory_expiry_reminders",
    ):
        if int(bind.execute(sa.text(f"SELECT count(*) FROM {table_name}")).scalar_one()):
            raise RuntimeError("empty G004 memory tables required before downgrade")
    # SQLite validates dependent views while rebuilding tables for DROP COLUMN.
    # Remove the retrieval projections first, then restore them against the
    # remaining G004 columns so a later downgrade can continue safely.
    op.execute("DROP VIEW IF EXISTS serving_formal_goals")
    op.execute("DROP VIEW IF EXISTS current_formal_memory")
    op.drop_index("ix_memory_expiry_reminders_due", table_name="memory_expiry_reminders")
    op.drop_table("memory_expiry_reminders")
    op.drop_index("ix_memory_conflicts_candidate", table_name="memory_conflicts")
    op.drop_index("ix_memory_conflicts_state_status", table_name="memory_conflicts")
    op.drop_table("memory_conflicts")
    op.drop_index(
        "ix_memory_candidate_evidence_conversation", table_name="memory_candidate_evidence"
    )
    op.drop_index("ix_memory_candidate_evidence_candidate", table_name="memory_candidate_evidence")
    op.drop_table("memory_candidate_evidence")
    op.drop_column("formal_memories", "time_sensitivity")
    op.drop_column("formal_memories", "confidence_explanation")

    op.execute(
        """
        CREATE VIEW current_formal_memory AS
        SELECT fm.*, fmv.value_json, mcs.effective_generation
        FROM memory_current_state mcs
        JOIN formal_memories fm ON fm.id = mcs.formal_memory_id
        JOIN formal_memory_versions fmv ON fmv.id = mcs.formal_version_id
        WHERE fm.status = 'formal_current'
          AND fm.current_version_id = mcs.formal_version_id
          AND fm.current_generation = mcs.effective_generation
        """
    )
    op.execute(
        """
        CREATE VIEW serving_formal_goals AS
        SELECT * FROM current_formal_memory
        WHERE state_key LIKE 'goal.%'
          AND status = 'formal_current'
          AND current_generation = effective_generation
        """
    )
    op.drop_column("memory_candidates", "extracted_at")
    op.drop_column("memory_candidates", "time_sensitivity")
    op.drop_column("memory_candidates", "valid_to")
    op.drop_column("memory_candidates", "valid_from")
    op.drop_column("memory_candidates", "confidence_explanation")
