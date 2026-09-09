"""privacy physical erase queue

Revision ID: 0007_privacy_physical_erases
Revises: 0006_knowledge_confirmation
Create Date: 2026-09-08
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_privacy_physical_erases"
down_revision: str | None = "0006_knowledge_confirmation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamps() -> list[sa.Column[sa.DateTime]]:
    return [
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),  # type: ignore[arg-type]
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),  # type: ignore[arg-type]
            server_default=sa.func.now(),
            nullable=False,
        ),
    ]


def upgrade() -> None:
    op.create_table(
        "privacy_physical_erases",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("erase_request_id", sa.String(length=36), nullable=False),
        sa.Column("knowledge_object_id", sa.String(length=36), nullable=False),
        sa.Column("artifact_kind", sa.String(length=64), nullable=False),
        sa.Column("object_uri", sa.String(length=1024), nullable=False),
        sa.Column("expected_sha256", sa.String(length=64), nullable=False),
        sa.Column("expected_byte_size", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("last_error", sa.String(length=1024), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["erase_request_id"], ["privacy_erase_requests.id"]),
        sa.ForeignKeyConstraint(["knowledge_object_id"], ["knowledge_objects.id"]),
        sa.UniqueConstraint(
            "erase_request_id", "object_uri", name="uq_privacy_physical_erases_request_uri"
        ),
        sa.CheckConstraint(
            "artifact_kind IN ('evidence_object', 'content_artifact', 'knowledge_markdown')",
            name="ck_privacy_physical_erases_artifact_kind",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'completed')",
            name="ck_privacy_physical_erases_status",
        ),
        sa.CheckConstraint(
            "length(expected_sha256) = 64",
            name="ck_privacy_physical_erases_sha256_length",
        ),
        sa.CheckConstraint(
            "expected_byte_size IS NULL OR expected_byte_size >= 0",
            name="ck_privacy_physical_erases_byte_size_nonnegative",
        ),
    )
    op.create_index(
        "ix_privacy_physical_erases_pending",
        "privacy_physical_erases",
        ["erase_request_id", "knowledge_object_id", "status"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_privacy_physical_erases_pending",
        table_name="privacy_physical_erases",
    )
    op.drop_table("privacy_physical_erases")
