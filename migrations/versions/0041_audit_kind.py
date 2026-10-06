"""Separate connectivity and Provider secret lifecycle audit events."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision = "0041_audit_kind"
down_revision: str | Sequence[str] | None = "0040_provider_secret_rotation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "model_connectivity_audits",
        sa.Column("audit_kind", sa.String(32), nullable=False, server_default="connectivity"),
    )
    op.create_index(
        "ix_model_connectivity_audits_kind_created",
        "model_connectivity_audits",
        ["audit_kind", "created_at"],
    )
    op.execute(
        "UPDATE model_connectivity_audits SET audit_kind='secret_lifecycle', model_id='' "
        "WHERE model_id='__secret_lifecycle__' "
        "AND diagnostic_code IN ('secret_rotated', 'secret_revoked', 'secret_migrated') "
        "AND duration_ms IS NULL"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE model_connectivity_audits SET model_id='__secret_lifecycle__' "
        "WHERE audit_kind='secret_lifecycle'"
    )
    op.drop_index(
        "ix_model_connectivity_audits_kind_created",
        table_name="model_connectivity_audits",
    )
    bind = op.get_bind()
    views: list[tuple[str, str]] = []
    if bind.dialect.name == "sqlite":
        views = [
            (str(row[0]), str(row[1]))
            for row in bind.execute(
                sa.text("SELECT name, sql FROM sqlite_master WHERE type='view' AND sql IS NOT NULL")
            ).fetchall()
        ]
        for name, _statement in reversed(views):
            op.execute(f'DROP VIEW IF EXISTS "{name}"')
    op.drop_column("model_connectivity_audits", "audit_kind")
    for _name, statement in views:
        op.execute(statement)
