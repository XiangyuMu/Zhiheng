"""Expose formally published events through the shared serving index."""

from collections.abc import Sequence

from alembic import op

revision = "0024_event_serving_chunks"
down_revision: str | Sequence[str] | None = "0023_event_memory"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP VIEW IF EXISTS serving_chunks")
    op.execute("""
      CREATE VIEW serving_chunks AS
      SELECT c.* FROM chunks c
      JOIN current_formal_knowledge k ON k.id=c.source_id AND c.source_type='knowledge_object'
       AND k.current_version_id=c.source_version_id AND k.confirmation_generation=c.confirmation_generation
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
        AND h.owner_user_id=e.owner_user_id
        AND ev.history_id=h.id
    """)


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS serving_chunks")
    op.execute(
        """CREATE VIEW serving_chunks AS SELECT c.* FROM chunks c JOIN current_formal_knowledge k ON k.id=c.source_id AND k.current_version_id=c.source_version_id AND k.confirmation_generation=c.confirmation_generation WHERE c.source_type='knowledge_object' AND c.status='ready' AND c.visibility_scope='formal'"""
    )
