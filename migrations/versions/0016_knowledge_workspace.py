"""Add duplicate merge provenance for the knowledge workspace."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision = "0016_knowledge_workspace"
down_revision: str | None = "0015_classification_productization"
branch_labels: str | Sequence[str] | None = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "knowledge_merge_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("primary_knowledge_object_id", sa.String(36), nullable=False),
        sa.Column("requested_by_user_id", sa.String(36), nullable=False),
        sa.Column("source_object_ids_json", sa.Text(), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["primary_knowledge_object_id"], ["knowledge_objects.id"]),
        sa.ForeignKeyConstraint(["requested_by_user_id"], ["auth_users.id"]),
    )
    op.create_index(
        "ix_knowledge_merge_events_primary_created",
        "knowledge_merge_events",
        ["primary_knowledge_object_id", "created_at"],
    )
    op.create_table(
        "knowledge_merge_sources",
        sa.Column("merge_event_id", sa.String(36), nullable=False),
        sa.Column("source_knowledge_object_id", sa.String(36), nullable=False),
        sa.Column("primary_knowledge_object_id", sa.String(36), nullable=False),
        sa.Column("source_version_id", sa.String(36), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["merge_event_id"], ["knowledge_merge_events.id"]),
        sa.ForeignKeyConstraint(["source_knowledge_object_id"], ["knowledge_objects.id"]),
        sa.ForeignKeyConstraint(["primary_knowledge_object_id"], ["knowledge_objects.id"]),
        sa.PrimaryKeyConstraint("merge_event_id", "source_knowledge_object_id"),
    )
    op.create_index(
        "ix_knowledge_merge_sources_source",
        "knowledge_merge_sources",
        ["source_knowledge_object_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_knowledge_merge_sources_source", table_name="knowledge_merge_sources")
    op.drop_table("knowledge_merge_sources")
    op.drop_index(
        "ix_knowledge_merge_events_primary_created",
        table_name="knowledge_merge_events",
    )
    op.drop_table("knowledge_merge_events")
