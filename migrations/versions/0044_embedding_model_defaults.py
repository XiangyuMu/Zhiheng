"""Add an explicit Embedding route to provider defaults."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision = "0044_embedding_model_defaults"
down_revision: str | Sequence[str] | None = "0043_provider_model_catalog"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("model_route_defaults", sa.Column("embedding_provider_id", sa.String(36)))
    op.add_column("model_route_defaults", sa.Column("embedding_model_id", sa.String(256)))


def downgrade() -> None:
    bind = op.get_bind()
    views: list[str] = []
    if bind.dialect.name == "sqlite":
        views = [
            str(row[1])
            for row in bind.execute(
                sa.text("SELECT name, sql FROM sqlite_master WHERE type='view' AND sql IS NOT NULL")
            ).fetchall()
        ]
        for name in bind.execute(
            sa.text("SELECT name FROM sqlite_master WHERE type='view' AND sql IS NOT NULL")
        ).scalars():
            op.execute(f'DROP VIEW IF EXISTS "{name}"')
    op.drop_column("model_route_defaults", "embedding_model_id")
    op.drop_column("model_route_defaults", "embedding_provider_id")
    for statement in views:
        op.execute(statement)
