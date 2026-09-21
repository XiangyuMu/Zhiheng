"""Store chunk integrity hashes and fence superseded event versions."""

import sqlalchemy as sa
from alembic import op

revision = "0029_event_chunk_hash"
down_revision = "0028_event_serving_authority"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("chunks", sa.Column("quote_hash", sa.String(64), nullable=True))
    op.execute("DROP VIEW serving_chunks")
    op.execute(
        """CREATE VIEW serving_chunks AS
      SELECT c.* FROM chunks c
      JOIN current_formal_knowledge k ON k.id=c.source_id AND c.source_type='knowledge_object'
       AND k.current_version_id=c.source_version_id
       AND k.confirmation_generation=c.confirmation_generation
      WHERE c.status='ready' AND c.visibility_scope='formal'
      UNION ALL
      SELECT c.* FROM chunks c
      JOIN event_memories e ON e.id=c.source_id AND c.source_type='event_memory'
       AND e.status='formal_current' AND e.confirmation_generation=c.confirmation_generation
      JOIN event_memory_versions v ON v.id=c.source_version_id AND v.event_memory_id=e.id
      JOIN event_memory_evidence ev ON ev.event_memory_id=e.id AND ev.event_version_id=v.id
      JOIN answer_history h ON h.id=e.source_history_id
        AND h.conversation_id=e.source_conversation_id
      WHERE c.status='ready' AND c.visibility_scope='formal'
        AND h.owner_user_id=e.owner_user_id AND ev.history_id=h.id
        AND ev.conversation_id=h.conversation_id
        AND v.version_no=(SELECT max(version_no) FROM event_memory_versions
                          WHERE event_memory_id=e.id)
    """
    )


def downgrade():
    goals_view = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT sql FROM sqlite_master WHERE type='view' AND name='serving_formal_goals'"
            )
        )
        .scalar_one_or_none()
    )
    op.execute("DROP VIEW IF EXISTS serving_formal_goals")
    op.execute("DROP VIEW serving_chunks")
    op.drop_column("chunks", "quote_hash")
    op.execute(
        """CREATE VIEW serving_chunks AS
      SELECT c.* FROM chunks c
      JOIN current_formal_knowledge k ON k.id=c.source_id AND c.source_type='knowledge_object'
       AND k.current_version_id=c.source_version_id
       AND k.confirmation_generation=c.confirmation_generation
      WHERE c.status='ready' AND c.visibility_scope='formal'
      UNION ALL
      SELECT c.* FROM chunks c
      JOIN event_memories e ON e.id=c.source_id AND c.source_type='event_memory'
       AND e.status='formal_current' AND e.confirmation_generation=c.confirmation_generation
      JOIN event_memory_versions v ON v.id=c.source_version_id AND v.event_memory_id=e.id
      JOIN event_memory_evidence ev ON ev.event_memory_id=e.id AND ev.event_version_id=v.id
      JOIN answer_history h ON h.id=e.source_history_id
        AND h.conversation_id=e.source_conversation_id
      WHERE c.status='ready' AND c.visibility_scope='formal'
        AND h.owner_user_id=e.owner_user_id AND ev.history_id=h.id
    """
    )

    if goals_view:
        op.execute(goals_view)
