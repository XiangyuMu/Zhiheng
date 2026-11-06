"""Idle-time background consolidation (#113, spec decision 23).

- ``conclusion_relations.origin`` tells a suggestion made while reviewing a
  draft (``review``) from one the background run made between two formal
  conclusions (``consolidation``).
- ``consolidation_activity`` keeps the last conversation or review activity;
  the run starts only after 30 idle minutes.
- ``consolidation_runs`` records each run, so it happens at most once a day.
- ``consolidation_findings`` holds 整理发现: observations that propose no
  change to formal knowledge and never count as pending. One row per kind,
  subject and subject version, so a dismissed finding stays dismissed until
  the subject has a new version.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision = "0058_background_consolidation"
down_revision: str | Sequence[str] | None = "0057_pending_daily_suggestion"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {
        str(row[1])
        for row in op.get_bind().execute(sa.text("PRAGMA table_info(conclusion_relations)"))
    }
    if "origin" not in columns:
        op.execute(
            "ALTER TABLE conclusion_relations ADD COLUMN origin TEXT NOT NULL DEFAULT 'review'"
        )
    op.execute(
        """
        CREATE TABLE consolidation_activity (
          scope TEXT PRIMARY KEY,
          last_active_at TEXT NOT NULL
        )
        """
    )
    op.execute(
        """
        CREATE TABLE consolidation_runs (
          id TEXT PRIMARY KEY,
          started_at TEXT NOT NULL,
          suggestions_created INTEGER NOT NULL DEFAULT 0,
          findings_created INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    op.execute(
        """
        CREATE TABLE consolidation_findings (
          id TEXT PRIMARY KEY,
          owner_user_id TEXT NOT NULL,
          kind TEXT NOT NULL,
          subject_type TEXT NOT NULL CHECK (subject_type IN ('conclusion', 'memory')),
          subject_id TEXT NOT NULL,
          subject_version TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'open'
            CHECK (status IN ('open', 'dismissed', 'resolved')),
          detail_json TEXT NOT NULL DEFAULT '{}',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE (owner_user_id, kind, subject_type, subject_id, subject_version)
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE consolidation_findings")
    op.execute("DROP TABLE consolidation_runs")
    op.execute("DROP TABLE consolidation_activity")
    # Like 0053 and 0054, the column stays: SQLite's DROP COLUMN re-validates
    # every view and older revisions' views do not survive that. Its default
    # keeps older code's inserts valid, and upgrading again detects it.
