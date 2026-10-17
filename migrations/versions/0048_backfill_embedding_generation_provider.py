"""Repair provider provenance on embedding generations built before 0047.

0047 added ``embedding_generations.provider_id`` without a backfill, so active
generations built by a provider-backed Embedding route kept ``provider_id``
NULL and became invisible to the configured-provider lookup.

Pre-0047 index jobs recorded the route's model id as ``model_id`` and the
provider's ``policy_revision`` as ``model_revision``. A NULL-provider
generation is attributed to a provider only when exactly one provider has a
model record for that model id at that policy revision. When several providers
match, the vectors' origin cannot be proven: active generations are retired and
one explicit ``knowledge.index`` rebuild is enqueued, so search does not
silently serve or drop a vector index of unknown provenance. Generations with
no matching provider (local embedders) keep ``provider_id`` NULL.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision = "0048_backfill_embedding_generation_provider"
down_revision: str | Sequence[str] | None = "0047_embedding_generation_provider"
branch_labels = None
depends_on = None

_REBUILD_IDEMPOTENCY_KEY = "migration:0048:embedding-generation-provider"


def upgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            """
            SELECT g.id, g.index_status,
                   COUNT(DISTINCT p.id) AS candidate_count,
                   MIN(p.id) AS candidate_provider_id
            FROM embedding_generations g
            LEFT JOIN model_provider_models m ON m.model_id = g.model_id
            LEFT JOIN model_provider_configs p
              ON p.id = m.provider_id
             AND p.policy_revision = g.model_revision
            WHERE g.provider_id IS NULL
            GROUP BY g.id, g.index_status
            ORDER BY g.id
            """
        )
    ).mappings()

    retired: list[str] = []
    for row in rows.all():
        candidate_count = int(row["candidate_count"])
        if candidate_count == 1:
            bind.execute(
                sa.text(
                    """
                    UPDATE embedding_generations
                    SET provider_id = :provider_id, updated_at = CURRENT_TIMESTAMP
                    WHERE id = :id AND provider_id IS NULL
                    """
                ),
                {"id": row["id"], "provider_id": row["candidate_provider_id"]},
            )
        elif candidate_count > 1 and row["index_status"] == "active":
            bind.execute(
                sa.text(
                    """
                    UPDATE embedding_generations
                    SET index_status = 'retired', updated_at = CURRENT_TIMESTAMP
                    WHERE id = :id AND index_status = 'active'
                    """
                ),
                {"id": row["id"]},
            )
            retired.append(str(row["id"]))

    if retired:
        bind.execute(
            sa.text(
                """
                INSERT OR IGNORE INTO jobs (id, job_type, idempotency_key, payload_json, status)
                VALUES (:id, 'knowledge.index', :idempotency_key, :payload_json, 'pending')
                """
            ),
            {
                "id": str(uuid.uuid4()),
                "idempotency_key": _REBUILD_IDEMPOTENCY_KEY,
                "payload_json": json.dumps(
                    {
                        "reason": "embedding_generation_provider_ambiguous",
                        "retired_generation_ids": retired,
                    },
                    separators=(",", ":"),
                ),
            },
        )


def downgrade() -> None:
    # Backfilled provenance is correct data and 0047's downgrade drops the
    # column. Retired generations stay retired: the enqueued rebuild may already
    # have activated their replacement.
    pass
