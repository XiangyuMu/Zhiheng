"""Store immutable raw conversation payload references for event evidence."""
from collections.abc import Sequence
import sqlalchemy as sa
from alembic import op

revision = "0026_event_raw_evidence"
down_revision: str | Sequence[str] | None = "0025_event_confirmation"
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.add_column("event_memory_evidence", sa.Column("query_text", sa.Text(), nullable=True))
    op.add_column("event_memory_evidence", sa.Column("response_json", sa.Text(), nullable=True))
    op.add_column("event_memory_evidence", sa.Column("raw_sha256", sa.String(64), nullable=True))

def downgrade() -> None:
    # SQLite rebuilds the table for DROP COLUMN; remove dependent views first.
    op.execute("DROP VIEW IF EXISTS serving_chunks")
    op.execute("DROP VIEW IF EXISTS serving_formal_goals")
    op.drop_column("event_memory_evidence", "raw_sha256")
    op.drop_column("event_memory_evidence", "response_json")
    op.drop_column("event_memory_evidence", "query_text")
    op.execute("""CREATE VIEW serving_chunks AS SELECT c.* FROM chunks c JOIN current_formal_knowledge k ON k.id=c.source_id AND k.current_version_id=c.source_version_id AND k.confirmation_generation=c.confirmation_generation WHERE c.source_type='knowledge_object' AND c.status='ready' AND c.visibility_scope='formal'""")
    op.execute("""CREATE VIEW serving_formal_goals AS SELECT * FROM current_formal_memory WHERE state_key LIKE 'goal.%' AND status='formal_current'""")
