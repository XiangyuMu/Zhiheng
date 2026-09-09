"""Immutable release stage execution records."""

import sqlalchemy as sa
from alembic import op

revision = "0009_release_execution_runs"
down_revision = "0008_proposal_execution_runs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "release_execution_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("release_id", sa.String(36), nullable=False),
        sa.Column("proposal_id", sa.String(36), nullable=False),
        sa.Column("stage", sa.String(16), nullable=False),
        sa.Column("idempotency_digest", sa.String(64), nullable=False, unique=True),
        sa.Column("record_json", sa.Text(), nullable=False),
        sa.Column("record_hmac", sa.String(64), nullable=False),
        sa.ForeignKeyConstraint(["release_id"], ["strategy_releases.id"]),
        sa.ForeignKeyConstraint(["proposal_id"], ["evolution_proposals.id"]),
    )
    for operation in ("UPDATE", "DELETE"):
        op.execute(
            f"CREATE TRIGGER release_execution_runs_no_{operation.lower()} "
            f"BEFORE {operation} ON release_execution_runs BEGIN "
            "SELECT RAISE(ABORT, 'release execution runs are append-only'); END"
        )


def downgrade() -> None:
    for operation in ("update", "delete"):
        op.execute(f"DROP TRIGGER release_execution_runs_no_{operation}")
    op.drop_table("release_execution_runs")
