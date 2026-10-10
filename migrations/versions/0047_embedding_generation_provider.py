"""Persist embedding generation provider provenance."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision = "0047_embedding_generation_provider"
down_revision: str | Sequence[str] | None = "0046_provider_registry_kinds"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "embedding_generations",
        sa.Column("provider_id", sa.String(length=36), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("embedding_generations", "provider_id")
