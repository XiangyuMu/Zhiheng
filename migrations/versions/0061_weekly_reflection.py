"""Weekly reflection into 综合结论草稿 (#116, spec decision 23).

- ``reflection_runs`` records when each owner's knowledge was last reflected
  on, so reflection happens once a week inside the idle-time consolidation run.
- ``reflection_syntheses`` keys each synthesized draft by the versions of the
  formal knowledge it was drawn from. Whatever became of the draft, the same
  basis is never drafted again; a new version of a basis entry, or new formal
  knowledge joining the group, is a different basis.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision = "0061_weekly_reflection"
down_revision: str | Sequence[str] | None = "0060_knowledge_health_reports"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE reflection_runs (
          id TEXT PRIMARY KEY,
          owner_user_id TEXT NOT NULL,
          started_at TEXT NOT NULL,
          drafts_created INTEGER NOT NULL
        )
        """
    )
    op.execute("CREATE INDEX reflection_runs_owner ON reflection_runs (owner_user_id, started_at)")
    op.execute(
        """
        CREATE TABLE reflection_syntheses (
          owner_user_id TEXT NOT NULL,
          basis_key TEXT NOT NULL,
          entry_id TEXT NOT NULL REFERENCES conclusion_entries(id) ON DELETE CASCADE,
          basis_json TEXT NOT NULL,
          created_at TEXT NOT NULL,
          PRIMARY KEY (owner_user_id, basis_key)
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE reflection_syntheses")
    op.execute("DROP INDEX reflection_runs_owner")
    op.execute("DROP TABLE reflection_runs")
