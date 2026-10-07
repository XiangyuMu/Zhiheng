"""Make the model-defaults row an atomic singleton."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision = "0045_singleton_model_defaults"
down_revision: str | Sequence[str] | None = "0044_embedding_model_defaults"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ``model_route_defaults`` stores the current selection, not a history.
    # Keep the newest row if an old database contains accidental duplicates.
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT id FROM model_route_defaults "
            "ORDER BY updated_at DESC, id DESC"
        )
    ).scalars().all()
    for duplicate_id in rows[1:]:
        bind.execute(
            sa.text("DELETE FROM model_route_defaults WHERE id=:id"),
            {"id": duplicate_id},
        )

    op.add_column(
        "model_route_defaults",
        sa.Column("singleton_key", sa.String(32), nullable=False, server_default="default"),
    )
    op.create_index(
        "uq_model_route_defaults_singleton",
        "model_route_defaults",
        ["singleton_key"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_model_route_defaults_singleton", table_name="model_route_defaults")
    op.drop_column("model_route_defaults", "singleton_key")
