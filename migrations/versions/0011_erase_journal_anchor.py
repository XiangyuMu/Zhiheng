"""Anchor external erase journal existence to durable database state."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_erase_journal_anchor"
down_revision: str | None = "0010_maintenance_job_receipts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "privacy_erase_journal_anchor",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("genesis_digest", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint("id = 1", name="ck_privacy_erase_anchor_singleton"),
    )
    op.execute(
        "INSERT INTO privacy_erase_journal_anchor (id, genesis_digest) "
        "VALUES (1, 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855')"
    )


def downgrade() -> None:
    op.drop_table("privacy_erase_journal_anchor")
