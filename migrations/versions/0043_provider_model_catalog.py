"""Track Provider model catalog refresh outcomes."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision = "0043_provider_model_catalog"
down_revision: str | Sequence[str] | None = "0042_provider_model_records"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("model_provider_configs", sa.Column("catalog_status", sa.String(32), nullable=False, server_default="unknown"))
    op.add_column("model_provider_configs", sa.Column("catalog_error", sa.Text()))
    op.add_column("model_provider_configs", sa.Column("catalog_refreshed_at", sa.DateTime(timezone=True)))


def downgrade() -> None:
    op.drop_column("model_provider_configs", "catalog_refreshed_at")
    op.drop_column("model_provider_configs", "catalog_error")
    op.drop_column("model_provider_configs", "catalog_status")
