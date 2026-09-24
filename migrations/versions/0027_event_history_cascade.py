"""Invalidate event evidence atomically when its source history is deleted."""

from collections.abc import Sequence

from alembic import op

revision = "0027_event_history_cascade"
down_revision: str | Sequence[str] | None = "0026_event_raw_evidence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
      CREATE TRIGGER trg_answer_history_event_erase
      BEFORE DELETE ON answer_history
      FOR EACH ROW
      BEGIN
        UPDATE chunks SET status='privacy_erased'
          WHERE source_type='event_memory'
            AND source_id IN (SELECT id FROM event_memories WHERE source_history_id=OLD.id);
        UPDATE event_memories SET status='soft_deleted', title='', summary='', entities_json='{}', updated_at=CURRENT_TIMESTAMP
          WHERE source_history_id=OLD.id AND status <> 'soft_deleted';
        DELETE FROM event_memory_evidence WHERE history_id=OLD.id;
        UPDATE event_memory_versions SET title='', summary='', payload_json='{}'
          WHERE event_memory_id IN (SELECT id FROM event_memories WHERE source_history_id=OLD.id);
        UPDATE event_confirmation_requests SET status='superseded'
          WHERE event_memory_id IN (SELECT id FROM event_memories WHERE source_history_id=OLD.id);
      END
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_answer_history_event_erase")
