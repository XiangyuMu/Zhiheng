"""Remember which unclassified items the user deferred.

The system suggestion for an unclassified item is computed on the fly, so the
延后 state is kept per knowledge object until the user restores it.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision = "0051_unclassified_deferrals"
down_revision: str | Sequence[str] | None = "0050_unify_domain_catalog"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "unclassified_deferrals",
        sa.Column("owner_user_id", sa.String(36), nullable=False),
        sa.Column("knowledge_object_id", sa.String(36), nullable=False),
        sa.Column(
            "deferred_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("owner_user_id", "knowledge_object_id"),
        sa.ForeignKeyConstraint(["owner_user_id"], ["auth_users.id"]),
        sa.ForeignKeyConstraint(["knowledge_object_id"], ["knowledge_objects.id"]),
    )


def downgrade() -> None:
    op.drop_table("unclassified_deferrals")
