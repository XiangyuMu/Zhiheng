"""延后 for pending types whose own tables have no deferred state."""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from zhiheng.pending.inbox import PendingItem


class DeferralStore:
    def deferred_ids(self, session: Session, owner: str, item_type: str) -> set[str]:
        return {
            str(item_id)
            for item_id in session.execute(
                text(
                    "SELECT item_id FROM pending_deferrals "
                    "WHERE owner_user_id = :owner AND item_type = :type"
                ),
                {"owner": owner, "type": item_type},
            ).scalars()
        }

    def defer(self, session: Session, owner: str, item: PendingItem) -> dict[str, Any]:
        session.execute(
            text(
                "INSERT OR IGNORE INTO pending_deferrals (owner_user_id, item_type, item_id) "
                "VALUES (:owner, :type, :id)"
            ),
            {"owner": owner, "type": item.type, "id": item.id},
        )
        return {"status": "deferred"}

    def restore(self, session: Session, owner: str, item: PendingItem) -> dict[str, Any]:
        session.execute(
            text(
                "DELETE FROM pending_deferrals "
                "WHERE owner_user_id = :owner AND item_type = :type AND item_id = :id"
            ),
            {"owner": owner, "type": item.type, "id": item.id},
        )
        return {"status": "pending"}
