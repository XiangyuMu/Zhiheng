"""Persist embedding generation provider provenance."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

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
    bind = op.get_bind()
    views: list[str] = []
    if bind.dialect.name == "sqlite":
        views = [
            str(row[0])
            for row in bind.execute(
                sa.text("SELECT sql FROM sqlite_master WHERE type='view' AND sql IS NOT NULL")
            ).fetchall()
        ]
        for name in bind.execute(
            sa.text("SELECT name FROM sqlite_master WHERE type='view' AND sql IS NOT NULL")
        ).scalars():
            op.execute(f'DROP VIEW IF EXISTS "{name}"')
    op.drop_column("embedding_generations", "provider_id")
    for statement in views:
        op.execute(statement)
