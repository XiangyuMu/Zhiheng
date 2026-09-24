"""Durable event memory candidates and formal evidence links."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision = "0023_event_memory"
down_revision: str | Sequence[str] | None = "0022_model_config_management"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "event_memories",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_user_id", sa.String(36), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="candidate"),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("occurred_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("timezone", sa.String(64), nullable=True),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("entities_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("source_conversation_id", sa.String(36), nullable=False),
        sa.Column("source_history_id", sa.String(36), nullable=False),
        sa.Column("confirmation_generation", sa.Integer(), nullable=True),
        sa.Column("sensitivity_level", sa.String(32), nullable=False, server_default="private"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "status in ('candidate','formal_current','rejected','soft_deleted','expired')",
            name="ck_event_memories_status",
        ),
        sa.UniqueConstraint("source_history_id", "title", name="uq_event_memory_source_title"),
    )
    op.create_index(
        "ix_event_memories_owner_status_time",
        "event_memories",
        ["owner_user_id", "status", "occurred_at"],
    )
    op.create_table(
        "event_memory_versions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("event_memory_id", sa.String(36), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["event_memory_id"], ["event_memories.id"]),
        sa.UniqueConstraint("event_memory_id", "version_no"),
    )
    op.create_table(
        "event_memory_evidence",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("event_memory_id", sa.String(36), nullable=False),
        sa.Column("event_version_id", sa.String(36), nullable=False),
        sa.Column("conversation_id", sa.String(36), nullable=False),
        sa.Column("history_id", sa.String(36), nullable=False),
        sa.Column("excerpt", sa.Text(), nullable=False),
        sa.Column("start_offset", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("end_offset", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("support_type", sa.String(32), nullable=False, server_default="origin"),
        sa.Column("quote_hash", sa.String(64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["event_memory_id"], ["event_memories.id"]),
        sa.ForeignKeyConstraint(["event_version_id"], ["event_memory_versions.id"]),
        sa.CheckConstraint(
            "support_type in ('origin','supporting','contradicting')",
            name="ck_event_memory_evidence_support",
        ),
    )
    op.create_index("ix_event_memory_evidence_history", "event_memory_evidence", ["history_id"])


def downgrade() -> None:
    op.drop_index("ix_event_memory_evidence_history", table_name="event_memory_evidence")
    op.drop_table("event_memory_evidence")
    op.drop_table("event_memory_versions")
    op.drop_index("ix_event_memories_owner_status_time", table_name="event_memories")
    op.drop_table("event_memories")
