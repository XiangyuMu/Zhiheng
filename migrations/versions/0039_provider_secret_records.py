"""Persist encrypted local provider secrets."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision = "0039_provider_secret_records"
down_revision: str | Sequence[str] | None = "0038_current_conclusion_version_serving"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "provider_secret_instances",
        sa.Column("singleton_id", sa.String(32), primary_key=True),
        sa.Column("instance_id", sa.String(64), nullable=False, unique=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.execute(
        """
        INSERT INTO provider_secret_instances (singleton_id, instance_id)
        VALUES ('default', lower(hex(randomblob(16))))
        """
    )
    op.create_table(
        "provider_secret_records",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("provider_id", sa.String(36), nullable=False),
        sa.Column("secret_version", sa.Integer(), nullable=False),
        sa.Column("algorithm", sa.String(32), nullable=False),
        sa.Column("nonce_b64", sa.String(64), nullable=False),
        sa.Column("ciphertext_b64", sa.Text(), nullable=False),
        sa.Column("aad_json", sa.Text(), nullable=False),
        sa.Column("secret_fingerprint", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="active"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["provider_id"], ["model_provider_configs.id"]),
        sa.UniqueConstraint("provider_id", "secret_version", name="uq_provider_secret_version"),
    )
    op.create_index(
        "ix_provider_secret_records_provider_status",
        "provider_secret_records",
        ["provider_id", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_provider_secret_records_provider_status", table_name="provider_secret_records")
    op.drop_table("provider_secret_records")
    op.drop_table("provider_secret_instances")
