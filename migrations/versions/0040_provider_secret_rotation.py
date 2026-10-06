"""Record Provider secret version in connectivity audits."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision = "0040_provider_secret_rotation"
down_revision: str | Sequence[str] | None = "0039_provider_secret_records"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "model_connectivity_audits",
        sa.Column("secret_version", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("model_connectivity_audits", "secret_version")
