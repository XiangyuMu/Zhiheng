"""Complete the durable import batch contract."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014_import_batch_contract"
down_revision: str | None = "0013_import_batches"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("import_batches", sa.Column("user_id", sa.String(36), nullable=True))
    op.add_column("import_batches", sa.Column("source_type", sa.String(32), nullable=True))
    op.add_column(
        "import_batches",
        sa.Column("revision", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("import_batches", sa.Column("completed_at", sa.DateTime(), nullable=True))
    op.create_index(
        "ix_import_batches_user_created",
        "import_batches",
        ["user_id", "created_at"],
    )

    item_columns = (
        sa.Column("source_id", sa.String(255), nullable=True),
        sa.Column("source_type", sa.String(32), nullable=True),
        sa.Column("filename", sa.String(512), nullable=True),
        sa.Column("media_type", sa.String(128), nullable=True),
        sa.Column("content_sha256", sa.String(64), nullable=True),
        sa.Column("stage", sa.String(32), nullable=True),
        sa.Column("progress_completed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("progress_total", sa.Integer(), nullable=True),
        sa.Column("error_stage", sa.String(32), nullable=True),
        sa.Column("error_summary", sa.String(512), nullable=True),
        sa.Column("error_diagnostic_id", sa.String(128), nullable=True),
        sa.Column("retryable", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("version_id", sa.String(36), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
    )
    for column in item_columns:
        op.add_column("import_batch_items", column)
    op.create_index(
        "ix_import_batch_items_batch_status",
        "import_batch_items",
        ["batch_id", "status"],
    )
    op.create_index(
        "ix_import_batch_items_user_hash",
        "import_batch_items",
        ["source_id", "content_sha256"],
    )

    op.create_table(
        "import_batch_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("batch_id", sa.String(36), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("item_id", sa.String(36), nullable=True),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["batch_id"], ["import_batches.id"]),
        sa.ForeignKeyConstraint(["item_id"], ["import_batch_items.id"]),
        sa.UniqueConstraint("batch_id", "revision", name="uq_import_batch_events_revision"),
    )
    op.create_index(
        "ix_import_batch_events_batch_created",
        "import_batch_events",
        ["batch_id", "created_at"],
    )

    op.create_table(
        "import_batch_retry_operations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("batch_id", sa.String(36), nullable=False),
        sa.Column("operation_key", sa.String(255), nullable=False),
        sa.Column("result_json", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["batch_id"], ["import_batches.id"]),
        sa.UniqueConstraint(
            "batch_id",
            "operation_key",
            name="uq_import_batch_retry_operations_key",
        ),
    )


def downgrade() -> None:
    op.drop_table("import_batch_retry_operations")
    op.drop_index("ix_import_batch_events_batch_created", table_name="import_batch_events")
    op.drop_table("import_batch_events")
    op.drop_index("ix_import_batch_items_user_hash", table_name="import_batch_items")
    op.drop_index("ix_import_batch_items_batch_status", table_name="import_batch_items")
    for name in (
        "updated_at",
        "created_at",
        "version_id",
        "completed_at",
        "retry_count",
        "retryable",
        "error_diagnostic_id",
        "error_summary",
        "error_stage",
        "progress_total",
        "progress_completed",
        "stage",
        "content_sha256",
        "media_type",
        "filename",
        "source_type",
        "source_id",
    ):
        op.drop_column("import_batch_items", name)
    op.drop_index("ix_import_batches_user_created", table_name="import_batches")
    for name in ("completed_at", "revision", "source_type", "user_id"):
        op.drop_column("import_batches", name)
