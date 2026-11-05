"""Primary domains whose questions are answered without personal memory (#111).

One row per excluded primary domain. Like formal memory, the setting belongs to
the single local user. Excluding a domain only stops answers from reading
personal memory; personal statements are still remembered as before.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision = "0056_personalization_exclusions"
down_revision: str | Sequence[str] | None = "0055_single_unclassified_deferral"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE personalization_excluded_domains (
          domain_id TEXT PRIMARY KEY,
          excluded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE personalization_excluded_domains")
