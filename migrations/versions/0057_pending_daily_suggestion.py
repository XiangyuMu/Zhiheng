"""每日建议处理量 for 待处理 (#112, spec decision 22).

- ``pending_daily_settings`` holds the user's suggested amount; no row means
  the default of 10, and 0 turns the suggestion off.
- ``pending_daily_handled`` records each item the user handled on a local
  day, once per item, so the page can say when today's amount is reached.
  It only feeds that note and never changes any item.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision = "0057_pending_daily_suggestion"
down_revision: str | Sequence[str] | None = "0056_personalization_exclusions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE pending_daily_settings (
          owner_user_id TEXT PRIMARY KEY,
          suggested_amount INTEGER NOT NULL CHECK (suggested_amount >= 0),
          updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    op.execute(
        """
        CREATE TABLE pending_daily_handled (
          owner_user_id TEXT NOT NULL,
          handled_on TEXT NOT NULL,
          item_type TEXT NOT NULL,
          item_id TEXT NOT NULL,
          handled_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          PRIMARY KEY (owner_user_id, handled_on, item_type, item_id)
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE pending_daily_handled")
    op.execute("DROP TABLE pending_daily_settings")
