"""Support the single pending inbox (ADR 0011).

- ``pending_deferrals`` holds 延后 for pending types whose own tables have no
  deferred state (extraction results needing a supplement, unclassified
  knowledge). Deleting the row restores the item.
- ``conclusion_relations.created_at`` records when a relation suggestion became
  pending; existing rows are backfilled from their first history event.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision = "0051_pending_inbox"
down_revision: str | Sequence[str] | None = "0050_unify_domain_catalog"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE pending_deferrals (
          owner_user_id TEXT NOT NULL,
          item_type TEXT NOT NULL,
          item_id TEXT NOT NULL,
          deferred_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          PRIMARY KEY (owner_user_id, item_type, item_id)
        )
        """
    )
    columns = {
        str(row[1])
        for row in op.get_bind().execute(sa.text("PRAGMA table_info(conclusion_relations)"))
    }
    if "created_at" not in columns:
        op.execute("ALTER TABLE conclusion_relations ADD COLUMN created_at TEXT")
    op.execute(
        """
        UPDATE conclusion_relations
        SET created_at = COALESCE(
          (SELECT ev.created_at FROM conclusion_relation_events ev
           WHERE ev.relation_id = conclusion_relations.id
           ORDER BY ev.created_at ASC, ev.id ASC LIMIT 1),
          CURRENT_TIMESTAMP
        )
        WHERE created_at IS NULL
        """
    )


def downgrade() -> None:
    # The nullable created_at column is left in place: SQLite's DROP COLUMN
    # re-validates every view, and older revisions' views do not survive that.
    # Upgrading again detects the existing column.
    op.execute("DROP TABLE pending_deferrals")
