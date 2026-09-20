"""Preserve every conclusion relation proposal and user decision."""

from alembic import op

revision = "0033_conclusion_relation_history"
down_revision = "0032_topic_taxonomy"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE conclusion_relation_events (
          id TEXT PRIMARY KEY,
          relation_id TEXT NOT NULL REFERENCES conclusion_relations(id) ON DELETE CASCADE,
          owner_user_id TEXT NOT NULL,
          from_status TEXT,
          to_status TEXT NOT NULL,
          actor_user_id TEXT NOT NULL,
          left_version INTEGER NOT NULL,
          right_version INTEGER NOT NULL,
          left_source_id TEXT NOT NULL,
          right_source_id TEXT NOT NULL,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE conclusion_relation_events")
