"""One 延后 store for unclassified knowledge, and no skipped conflicts (#76, ADR 0011).

Unclassified knowledge had two parallel deferral stores: ``unclassified_deferrals``
(per knowledge object, 0051) and a ``deferred`` status on ``classification_suggestions``.
The pending item is the unclassified knowledge object, not one of its suggestions:
the inbox lists the object, suggestions are regenerated as new rows, and the domain
suggestion shown in 待处理 is not a ``classification_suggestions`` row at all. So the
per-object table is kept as the only store; every deferred suggestion becomes a
deferral of its knowledge object and goes back to ``pending``, still decidable.

Skipping a personal conflict dismissed it but left its candidate undecided, and a
candidate behind a conflict is listed only as that conflict, so it vanished from
待处理. Skipping is gone (a conflict is chosen, supplemented or deferred); conflicts
skipped earlier whose candidate is still undecided are reopened.

Downgrade keeps the data as it is: the earlier revisions read it correctly.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision = "0055_single_unclassified_deferral"
down_revision: str | Sequence[str] | None = "0054_extraction_supplement_resolution"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        INSERT OR IGNORE INTO unclassified_deferrals (owner_user_id, knowledge_object_id)
        SELECT DISTINCT owner_user_id, knowledge_object_id
        FROM classification_suggestions WHERE status = 'deferred'
        """
    )
    op.execute("UPDATE classification_suggestions SET status = 'pending' WHERE status = 'deferred'")
    undecided = """
        SELECT c.id FROM memory_conflicts c
        JOIN memory_candidates mc ON mc.id = c.candidate_id
        WHERE c.status = 'dismissed' AND c.resolution = 'skipped'
          AND mc.status IN ('pending_confirmation', 'edited', 'deferred')
    """
    op.execute(
        f"""
        UPDATE personal_conflicts SET status = 'pending', resolved_at = NULL
        WHERE status = 'skipped' AND id IN ({undecided})
        """
    )
    op.execute(
        f"""
        UPDATE memory_conflicts SET status = 'pending', resolution = NULL, resolved_at = NULL
        WHERE id IN ({undecided})
        """
    )


def downgrade() -> None:
    pass
