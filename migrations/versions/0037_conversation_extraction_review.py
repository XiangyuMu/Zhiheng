"""Expose conversation extraction outcomes for review."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0037_conversation_extraction_review"
down_revision: str | None = "0036_receipt_source_lineage"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "conversation_extraction_runs",
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column("owner_user_id", sa.String(255), nullable=False),
        sa.Column("conversation_id", sa.String(255), nullable=False),
        sa.Column("history_id", sa.String(255), nullable=False),
        sa.Column("source_id", sa.String(36), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("extracted_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("unrecognized_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failure_code", sa.String(128), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column(
            "extractor_version",
            sa.String(128),
            nullable=False,
            server_default="heuristic-conclusion-v1",
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["source_id"], ["conclusion_sources.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("history_id", name="uq_conversation_extraction_runs_history"),
        sa.CheckConstraint(
            "status IN ('succeeded','failed')", name="ck_conversation_extraction_runs_status"
        ),
        sa.CheckConstraint(
            "extracted_count >= 0", name="ck_conversation_extraction_runs_extracted"
        ),
        sa.CheckConstraint(
            "unrecognized_count >= 0", name="ck_conversation_extraction_runs_unrecognized"
        ),
    )
    op.create_index(
        "ix_conversation_extraction_runs_owner_status",
        "conversation_extraction_runs",
        ["owner_user_id", "status", "updated_at"],
    )
    op.create_table(
        "conversation_extraction_review_items",
        sa.Column("id", sa.String(160), primary_key=True),
        sa.Column("run_id", sa.String(128), nullable=False),
        sa.Column("owner_user_id", sa.String(255), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("excerpt", sa.Text(), nullable=False),
        sa.Column("start_offset", sa.Integer(), nullable=False),
        sa.Column("end_offset", sa.Integer(), nullable=False),
        sa.Column("reason_code", sa.String(128), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["run_id"], ["conversation_extraction_runs.id"], ondelete="CASCADE"),
        sa.CheckConstraint(
            "kind IN ('manual_supplement','failure')",
            name="ck_conversation_extraction_review_items_kind",
        ),
        sa.CheckConstraint(
            "start_offset >= 0 AND end_offset >= start_offset",
            name="ck_conversation_extraction_review_items_offsets",
        ),
    )
    op.create_index(
        "ix_conversation_extraction_review_items_run",
        "conversation_extraction_review_items",
        ["run_id", "kind"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_conversation_extraction_review_items_run",
        table_name="conversation_extraction_review_items",
    )
    op.drop_table("conversation_extraction_review_items")
    op.drop_index(
        "ix_conversation_extraction_runs_owner_status",
        table_name="conversation_extraction_runs",
    )
    op.drop_table("conversation_extraction_runs")
