"""Keep one authoritative domain catalog: the fourteen v2 primary domains (ADR 0003).

0032 seeded the v2 domains with INSERT OR IGNORE, so `relationships_communication`
kept its v1 English row while the other v1 domains stayed active beside the v2 ones.
Update that shared row in place and disable the remaining v1 domains. Disabled
rows stay in the catalog so historical assignments remain resolvable.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision = "0050_unify_domain_catalog"
down_revision: str | Sequence[str] | None = "0049_worker_heartbeats"
branch_labels = None
depends_on = None

RETIRED_V1_DOMAIN_IDS = (
    "academic_career",
    "technology_engineering",
    "finance_assets",
    "society_public_issues",
    "learning_personal_development",
    "health_wellbeing",
    "lifestyle_aesthetics",
    "arts_creation",
    "personal_archive_experience",
)

_SET_STATUS = sa.text(
    "UPDATE domain_catalog SET status = :status, updated_at = CURRENT_TIMESTAMP WHERE id IN :ids"
).bindparams(sa.bindparam("ids", expanding=True))
_SET_RELATIONSHIPS = sa.text(
    "UPDATE domain_catalog SET name = :name, description = :description, "
    "sort_order = :sort_order, schema_version = :schema_version, status = 'active', "
    "updated_at = CURRENT_TIMESTAMP WHERE id = 'relationships_communication'"
)


def upgrade() -> None:
    connection = op.get_bind()
    connection.execute(
        _SET_RELATIONSHIPS,
        {
            "name": "人际关系与沟通",
            "description": "亲密关系、家庭、社交、沟通与冲突处理",
            "sort_order": 120,
            "schema_version": 2,
        },
    )
    connection.execute(_SET_STATUS, {"status": "disabled", "ids": list(RETIRED_V1_DOMAIN_IDS)})


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(
        _SET_RELATIONSHIPS,
        {
            "name": "Relationships and Communication",
            "description": "Conversation, emotional communication and conflict handling",
            "sort_order": 70,
            "schema_version": 1,
        },
    )
    connection.execute(_SET_STATUS, {"status": "active", "ids": list(RETIRED_V1_DOMAIN_IDS)})
