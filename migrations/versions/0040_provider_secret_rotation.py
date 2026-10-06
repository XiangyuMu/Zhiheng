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
    bind = op.get_bind()
    view_sql: list[tuple[str, str]] = []
    if bind.dialect.name == "sqlite":
        view_sql = [
            (str(row[0]), str(row[1]))
            for row in bind.execute(
                sa.text("SELECT name, sql FROM sqlite_master WHERE type='view' AND sql IS NOT NULL")
            ).fetchall()
        ]
        for view_name, _statement in reversed(view_sql):
            op.execute(f'DROP VIEW IF EXISTS "{view_name}"')
    op.drop_column("model_connectivity_audits", "secret_version")
    for _view_name, statement in view_sql:
        op.execute(statement)
