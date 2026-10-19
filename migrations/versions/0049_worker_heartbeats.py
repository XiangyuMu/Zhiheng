"""Record each worker's declared deployment capabilities with a heartbeat.

The API reads these rows to learn which deployment capabilities a live worker
declares, so it can refuse PDF uploads when no live worker parses PDFs.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision = "0049_worker_heartbeats"
down_revision: str | Sequence[str] | None = "0048_backfill_embedding_generation_provider"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "worker_heartbeats",
        sa.Column("worker_id", sa.String(128), primary_key=True),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("capabilities_json", sa.JSON(), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("worker_heartbeats")
