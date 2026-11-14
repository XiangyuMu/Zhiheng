"""When each pending item was first decided, for the knowledge health report (#115).

The report's share of items handled within 7 days of becoming pending needs
both times after the item has left 待处理, so they are kept here. 延后 and
restoring decide nothing and are not recorded; deciding an item again later
keeps its first decision.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

# Verbs that set an item aside or bring it back instead of deciding it.
NOT_DECISIONS = frozenset({"defer", "restore"})


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds")


class PendingResolutions:
    def __init__(self, clock: Callable[[], datetime] = _utc_now) -> None:
        self.clock = clock

    def record(
        self, session: Session, owner: str, item_type: str, item_id: str, pending_since: datetime
    ) -> None:
        session.execute(
            text(
                "INSERT OR IGNORE INTO pending_resolutions "
                "(owner_user_id, item_type, item_id, pending_since, resolved_at) "
                "VALUES (:o, :type, :id, :since, :at)"
            ),
            {
                "o": owner,
                "type": item_type,
                "id": item_id,
                "since": _iso(pending_since),
                "at": _iso(self.clock()),
            },
        )
