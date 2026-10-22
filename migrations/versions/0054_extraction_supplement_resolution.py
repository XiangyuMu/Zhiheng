"""Record when an extraction needing a supplement was supplemented (#80).

``conversation_extraction_runs.supplement_resolved_at`` is set when the user
creates a draft from the run, which takes it out of 待处理. A re-extraction
with a different result clears it again, together with any 延后.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision = "0054_extraction_supplement_resolution"
down_revision: str | Sequence[str] | None = "0053_pending_inbox"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {
        str(row[1])
        for row in op.get_bind().execute(sa.text("PRAGMA table_info(conversation_extraction_runs)"))
    }
    if "supplement_resolved_at" not in columns:
        op.execute(
            "ALTER TABLE conversation_extraction_runs ADD COLUMN supplement_resolved_at TEXT"
        )


def downgrade() -> None:
    # Like 0053, the nullable column stays: SQLite's DROP COLUMN re-validates
    # every view and older revisions' views do not survive that. Upgrading
    # again detects the existing column.
    pass
