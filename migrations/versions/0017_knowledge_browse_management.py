"""Add knowledge browsing flags used by search and workspace management."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision = "0017_knowledge_browse_management"
down_revision: str | None = "0016_knowledge_workspace"
branch_labels: str | Sequence[str] | None = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "knowledge_objects",
        sa.Column("is_favorite", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "knowledge_objects",
        sa.Column("is_pinned", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("knowledge_objects", sa.Column("pinned_at", sa.DateTime(), nullable=True))
    op.create_index(
        "ix_knowledge_objects_owner_flags",
        "knowledge_objects",
        ["owner_user_id", "is_favorite", "is_pinned"],
    )


def downgrade() -> None:
    op.drop_index("ix_knowledge_objects_owner_flags", table_name="knowledge_objects")
    op.drop_column("knowledge_objects", "pinned_at")
    op.drop_column("knowledge_objects", "is_pinned")
    op.drop_column("knowledge_objects", "is_favorite")
