"""Persist authoritative source lineage for derived operation receipts."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0036_receipt_source_lineage"
down_revision: str | None = "0035_conclusion_conflict_serving"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "memory_operation_receipt_sources",
        sa.Column("receipt_id", sa.String(36), nullable=False),
        sa.Column("source_type", sa.String(64), nullable=False),
        sa.Column("source_id", sa.String(255), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["receipt_id"], ["memory_operation_receipts.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("receipt_id", "source_type", "source_id"),
    )
    op.create_index(
        "ix_memory_operation_receipt_sources_source",
        "memory_operation_receipt_sources",
        ["source_type", "source_id"],
    )
    op.create_table(
        "decision_run_sources",
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("source_type", sa.String(64), nullable=False),
        sa.Column("source_id", sa.String(255), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["run_id"], ["decision_support_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("run_id", "source_type", "source_id"),
    )
    op.create_index(
        "ix_decision_run_sources_source",
        "decision_run_sources",
        ["source_type", "source_id"],
    )
    op.create_table(
        "privacy_erase_unresolved_derived",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("erase_request_id", sa.String(36), nullable=False),
        sa.Column("derived_type", sa.String(64), nullable=False),
        sa.Column("derived_id", sa.String(255), nullable=False),
        sa.Column("target_type", sa.String(64), nullable=False),
        sa.Column("target_id", sa.String(255), nullable=False),
        sa.Column("reason", sa.String(255), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["erase_request_id"], ["privacy_erase_requests.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint(
            "erase_request_id",
            "derived_type",
            "derived_id",
            name="uq_privacy_erase_unresolved_derived",
        ),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_decision_run_sources_source",
        table_name="decision_run_sources",
    )
    op.drop_table("decision_run_sources")
    op.drop_index(
        "ix_memory_operation_receipt_sources_source",
        table_name="memory_operation_receipt_sources",
    )
    op.drop_table("privacy_erase_unresolved_derived")
    op.drop_table("memory_operation_receipt_sources")
