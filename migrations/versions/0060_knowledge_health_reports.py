"""Weekly knowledge health reports (#115, spec decision 24).

- ``knowledge_health_reports`` keeps every weekly report as a snapshot. The
  snapshot holds ids, counts and kinds, never titles or values, so privacy
  erasure never has to reach into it; triggers refuse any change to a row or
  its removal, so every report is kept as it was generated.
- ``pending_resolutions`` records when each pending item was first decided,
  with when it became pending, for the share of items handled within 7 days.
  延后 and restoring are not decisions and are not recorded.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision = "0060_knowledge_health_reports"
down_revision: str | Sequence[str] | None = "0059_onboarding_questionnaire"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE knowledge_health_reports (
          id TEXT PRIMARY KEY,
          owner_user_id TEXT NOT NULL,
          period_start TEXT NOT NULL,
          period_end TEXT NOT NULL,
          generated_at TEXT NOT NULL,
          previous_report_id TEXT REFERENCES knowledge_health_reports(id),
          report_json TEXT NOT NULL,
          UNIQUE (owner_user_id, period_end)
        )
        """
    )
    op.execute(
        """
        CREATE TRIGGER knowledge_health_reports_immutable
        BEFORE UPDATE ON knowledge_health_reports
        BEGIN
          SELECT RAISE(ABORT, 'knowledge health reports are immutable');
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER knowledge_health_reports_kept
        BEFORE DELETE ON knowledge_health_reports
        BEGIN
          SELECT RAISE(ABORT, 'knowledge health reports are kept');
        END
        """
    )
    op.execute(
        """
        CREATE TABLE pending_resolutions (
          owner_user_id TEXT NOT NULL,
          item_type TEXT NOT NULL,
          item_id TEXT NOT NULL,
          pending_since TEXT NOT NULL,
          resolved_at TEXT NOT NULL,
          PRIMARY KEY (owner_user_id, item_type, item_id)
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE pending_resolutions")
    op.execute("DROP TRIGGER knowledge_health_reports_kept")
    op.execute("DROP TRIGGER knowledge_health_reports_immutable")
    op.execute("DROP TABLE knowledge_health_reports")
