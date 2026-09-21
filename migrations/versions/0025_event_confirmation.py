"""Event confirmation requests and immutable decisions."""
from collections.abc import Sequence
import sqlalchemy as sa
from alembic import op

revision = "0025_event_confirmation"
down_revision: str | Sequence[str] | None = "0024_event_serving_chunks"
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.create_table(
        "event_confirmation_requests",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("event_memory_id", sa.String(36), nullable=False),
        sa.Column("event_version_id", sa.String(36), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("risk_level", sa.String(32), nullable=False, server_default="medium"),
        sa.Column("proposed_value_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["event_memory_id"], ["event_memories.id"]),
        sa.ForeignKeyConstraint(["event_version_id"], ["event_memory_versions.id"]),
        sa.CheckConstraint("status in ('pending','confirmed','rejected','superseded')", name="ck_event_confirmation_status"),
    )
    op.create_index("ix_event_confirmation_pending", "event_confirmation_requests", ["event_memory_id", "status"])
    op.create_table(
        "event_confirmation_decisions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("request_id", sa.String(36), nullable=False),
        sa.Column("decision", sa.String(32), nullable=False),
        sa.Column("event_version_id", sa.String(36), nullable=True),
        sa.Column("generation", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["request_id"], ["event_confirmation_requests.id"]),
    )

def downgrade() -> None:
    op.drop_table("event_confirmation_decisions")
    op.drop_index("ix_event_confirmation_pending", table_name="event_confirmation_requests")
    op.drop_table("event_confirmation_requests")
