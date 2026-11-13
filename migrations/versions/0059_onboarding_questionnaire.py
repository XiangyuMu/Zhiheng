"""The fixed five-question first-use questionnaire (#114, spec decision 25).

One row per user once the questionnaire was skipped or completed; no row means
it has not been offered an answer yet and is shown on first use. A skipped
questionnaire can still be completed later from 个人上下文, but is never shown
again on its own. ``operation_key`` and ``result_json`` let a retried
submission return the original outcome.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision = "0059_onboarding_questionnaire"
down_revision: str | Sequence[str] | None = "0058_background_consolidation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE onboarding_questionnaire (
          owner_user_id TEXT PRIMARY KEY,
          status TEXT NOT NULL CHECK (status IN ('skipped', 'completed')),
          operation_key TEXT,
          result_json TEXT,
          updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE onboarding_questionnaire")
