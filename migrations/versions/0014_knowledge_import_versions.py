from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision = "0014_knowledge_import_versions"
down_revision = "0014_import_batch_contract"
branch_labels: str | Sequence[str] | None = None
depends_on = None


def upgrade() -> None:
    op.add_column("knowledge_objects", sa.Column("owner_user_id", sa.String(36), nullable=True))
    op.create_index(
        "ix_knowledge_objects_owner_status",
        "knowledge_objects",
        ["owner_user_id", "lifecycle_status"],
    )
    op.create_index(
        "ix_knowledge_versions_content_version",
        "knowledge_versions",
        ["content_version_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_knowledge_versions_content_version", table_name="knowledge_versions")
    op.drop_index("ix_knowledge_objects_owner_status", table_name="knowledge_objects")
    op.drop_column("knowledge_objects", "owner_user_id")
