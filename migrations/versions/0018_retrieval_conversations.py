"""Persist retrieval conversations, turns, and user favorites."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision = "0018_retrieval_conversations"
down_revision: str | Sequence[str] | None = "0017_knowledge_browse_management"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "answer_conversations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_user_id", sa.String(36), nullable=False),
        sa.Column("title", sa.String(200), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("archived_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["owner_user_id"], ["auth_users.id"]),
    )
    op.create_index(
        "ix_answer_conversations_owner_updated",
        "answer_conversations",
        ["owner_user_id", "updated_at"],
    )
    op.create_table(
        "answer_history",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("conversation_id", sa.String(36), nullable=False),
        sa.Column("owner_user_id", sa.String(36), nullable=False),
        sa.Column("turn_index", sa.Integer(), nullable=False),
        sa.Column("query", sa.Text(), nullable=False),
        sa.Column("response_json", sa.Text(), nullable=False),
        sa.Column("route", sa.String(32), nullable=False),
        sa.Column("stop_reason", sa.String(64), nullable=False),
        sa.Column("is_favorite", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.ForeignKeyConstraint(["conversation_id"], ["answer_conversations.id"]),
        sa.ForeignKeyConstraint(["owner_user_id"], ["auth_users.id"]),
        sa.UniqueConstraint("conversation_id", "turn_index"),
    )
    op.create_index(
        "ix_answer_history_owner_created",
        "answer_history",
        ["owner_user_id", "created_at"],
    )
    op.create_index(
        "ix_answer_history_owner_favorite",
        "answer_history",
        ["owner_user_id", "is_favorite"],
    )


def downgrade() -> None:
    op.drop_index("ix_answer_history_owner_favorite", table_name="answer_history")
    op.drop_index("ix_answer_history_owner_created", table_name="answer_history")
    op.drop_table("answer_history")
    op.drop_index("ix_answer_conversations_owner_updated", table_name="answer_conversations")
    op.drop_table("answer_conversations")
