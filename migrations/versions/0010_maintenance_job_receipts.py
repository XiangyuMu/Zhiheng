"""Idempotent maintenance job receipts."""

import sqlalchemy as sa
from alembic import op

revision = "0010_maintenance_job_receipts"
down_revision = "0009_release_execution_runs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "maintenance_job_locks",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("idempotency_digest", sa.String(64), nullable=False, unique=True),
        sa.Column("payload_digest", sa.String(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
    )
    op.create_table(
        "maintenance_job_receipts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("idempotency_digest", sa.String(64), nullable=False, unique=True),
        sa.Column("output_refs_json", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
    )
    for table_name in ("maintenance_job_locks", "maintenance_job_receipts"):
        for operation in ("UPDATE", "DELETE"):
            op.execute(
                f"CREATE TRIGGER {table_name}_no_{operation.lower()} "
                f"BEFORE {operation} ON {table_name} BEGIN "
                f"SELECT RAISE(ABORT, '{table_name} are append-only'); END"
            )


def downgrade() -> None:
    for table_name in ("maintenance_job_locks", "maintenance_job_receipts"):
        for operation in ("update", "delete"):
            op.execute(f"DROP TRIGGER {table_name}_no_{operation}")
    op.drop_table("maintenance_job_receipts")
    op.drop_table("maintenance_job_locks")
