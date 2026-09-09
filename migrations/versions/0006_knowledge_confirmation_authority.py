"""knowledge confirmation authority

Revision ID: 0006_knowledge_confirmation
Revises: 0005_g006_evolution
Create Date: 2026-09-08
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_knowledge_confirmation"
down_revision: str | None = "0005_g006_evolution"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamps() -> list[sa.Column[sa.DateTime]]:
    return [
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),  # type: ignore[arg-type]
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),  # type: ignore[arg-type]
            server_default=sa.func.now(),
            nullable=False,
        ),
    ]


def upgrade() -> None:
    op.create_table(
        "knowledge_operation_receipts",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("operation_key", sa.String(length=128), nullable=False),
        sa.Column("operation_type", sa.String(length=64), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("result_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.UniqueConstraint("operation_key", name="uq_knowledge_operation_receipts_key"),
        sa.CheckConstraint(
            "status IN ('started', 'ok', 'failed')",
            name="ck_knowledge_operation_receipts_status",
        ),
    )

    op.create_table(
        "knowledge_confirmation_requests",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("target_type", sa.String(length=64), nullable=False),
        sa.Column("target_id", sa.String(length=36), nullable=False),
        sa.Column("risk_level", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("proposed_value_json", sa.JSON(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "target_type = 'knowledge_object'",
            name="ck_knowledge_confirmation_requests_target_type",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'confirmed', 'edited', 'rejected', 'expired')",
            name="ck_knowledge_confirmation_requests_status",
        ),
        sa.CheckConstraint(
            "risk_level IN ('low', 'medium', 'high')",
            name="ck_knowledge_confirmation_requests_risk",
        ),
    )
    op.create_index(
        "ix_knowledge_confirmation_requests_target_status",
        "knowledge_confirmation_requests",
        ["target_type", "target_id", "status"],
    )
    op.create_index(
        "uq_knowledge_confirmation_one_pending",
        "knowledge_confirmation_requests",
        ["target_type", "target_id"],
        unique=True,
        sqlite_where=sa.text("status = 'pending'"),
    )

    op.create_table(
        "knowledge_confirmation_decisions",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("request_id", sa.String(length=36), nullable=False),
        sa.Column("decision", sa.String(length=32), nullable=False),
        sa.Column("final_value_json", sa.JSON(), nullable=True),
        sa.Column("decided_by_user_id", sa.String(length=36), nullable=False),
        sa.Column(
            "decided_at",
            sa.DateTime(timezone=True),  # type: ignore[arg-type]
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["request_id"], ["knowledge_confirmation_requests.id"]),
        sa.UniqueConstraint("request_id", name="uq_knowledge_confirmation_decisions_request"),
        sa.CheckConstraint(
            "decision IN ('confirmed', 'edited', 'rejected')",
            name="ck_knowledge_confirmation_decisions_decision",
        ),
    )

    op.execute(
        """
        CREATE TRIGGER trg_knowledge_confirmation_decisions_append_only_update
        BEFORE UPDATE ON knowledge_confirmation_decisions
        FOR EACH ROW
        BEGIN
          SELECT RAISE(ABORT, 'knowledge_confirmation_decisions is append-only');
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_knowledge_confirmation_decisions_append_only_delete
        BEFORE DELETE ON knowledge_confirmation_decisions
        FOR EACH ROW
        BEGIN
          SELECT RAISE(ABORT, 'knowledge_confirmation_decisions is append-only');
        END
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_knowledge_confirmation_decisions_append_only_delete")
    op.execute("DROP TRIGGER IF EXISTS trg_knowledge_confirmation_decisions_append_only_update")
    op.drop_table("knowledge_confirmation_decisions")
    op.drop_index(
        "uq_knowledge_confirmation_one_pending",
        table_name="knowledge_confirmation_requests",
    )
    op.drop_index(
        "ix_knowledge_confirmation_requests_target_status",
        table_name="knowledge_confirmation_requests",
    )
    op.drop_table("knowledge_confirmation_requests")
    op.drop_table("knowledge_operation_receipts")
