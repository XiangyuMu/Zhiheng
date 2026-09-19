"""Track automatic personal-information updates and unresolved prompts."""
from alembic import op

revision = "0031_personal_updates"
down_revision = "0030_knowledge_conclusions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE personal_updates (
      id TEXT PRIMARY KEY, owner_user_id TEXT NOT NULL, state_key TEXT NOT NULL,
      value_json TEXT NOT NULL, source_text TEXT NOT NULL, source_kind TEXT NOT NULL,
      valid_from TEXT, valid_to TEXT, status TEXT NOT NULL DEFAULT 'formal',
      created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
    op.execute("""CREATE TABLE personal_conflicts (
      id TEXT PRIMARY KEY, owner_user_id TEXT NOT NULL, state_key TEXT NOT NULL,
      candidate_json TEXT NOT NULL, existing_json TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
      resolved_at TEXT)""")
    op.execute("""CREATE TABLE personal_prompts (
      id TEXT PRIMARY KEY, owner_user_id TEXT NOT NULL, prompt_kind TEXT NOT NULL,
      state_key TEXT, reason TEXT NOT NULL, payload_json TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
      resolved_at TEXT)""")


def downgrade() -> None:
    for table in ("personal_prompts", "personal_conflicts", "personal_updates"):
        op.execute(f"DROP TABLE {table}")
