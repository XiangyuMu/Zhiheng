"""每日建议处理量: how many items to handle today before 待处理 says you can stop.

The suggestion only shapes what the page shows (spec decision 22). Reaching it
changes no item, and an item left unhandled is not approved, rejected or
deferred: tomorrow it is still where it was.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from zhiheng.core.ids import sha256_json

DEFAULT_DAILY_SUGGESTION = 10
MAX_DAILY_SUGGESTION = 500


def _local_today() -> date:
    # The app runs on the user's own machine, so its local day is the user's.
    return datetime.now().astimezone().date()


class DailySuggestion:
    def __init__(self, today: Callable[[], date] = _local_today) -> None:
        self.today = today

    def amount(self, session: Session, owner: str) -> int:
        stored = session.execute(
            text("SELECT suggested_amount FROM pending_daily_settings WHERE owner_user_id = :o"),
            {"o": owner},
        ).scalar_one_or_none()
        return DEFAULT_DAILY_SUGGESTION if stored is None else int(stored)

    def set_amount(self, session: Session, owner: str, amount: int) -> None:
        if not 0 <= amount <= MAX_DAILY_SUGGESTION:
            raise ValueError(f"suggested amount must be between 0 and {MAX_DAILY_SUGGESTION}")
        session.execute(
            text(
                "INSERT INTO pending_daily_settings (owner_user_id, suggested_amount) "
                "VALUES (:o, :amount) ON CONFLICT(owner_user_id) DO UPDATE SET "
                "suggested_amount = excluded.suggested_amount, updated_at = CURRENT_TIMESTAMP"
            ),
            {"o": owner, "amount": amount},
        )

    def _day(self) -> str:
        return self.today().isoformat()

    def record(self, session: Session, owner: str, item_type: str, item_id: str) -> None:
        """Count an item as handled today; handling it again today counts once."""
        session.execute(
            text(
                "INSERT OR IGNORE INTO pending_daily_handled "
                "(owner_user_id, handled_on, item_type, item_id) VALUES (:o, :day, :type, :id)"
            ),
            {"o": owner, "day": self._day(), "type": item_type, "id": item_id},
        )

    def _handled_on(self, session: Session, owner: str, day: str) -> int:
        return int(
            session.execute(
                text(
                    "SELECT COUNT(*) FROM pending_daily_handled "
                    "WHERE owner_user_id = :o AND handled_on = :day"
                ),
                {"o": owner, "day": day},
            ).scalar_one()
        )

    def payload(self, session: Session, owner: str) -> dict[str, Any]:
        # Read the day once, so the date and its count always agree at midnight.
        day = self._day()
        suggested = self.amount(session, owner)
        handled = self._handled_on(session, owner, day)
        return {
            "date": day,
            "suggested": suggested,
            "handled_today": handled,
            # 0 turns the suggestion off, so it is never reached.
            "reached": suggested > 0 and handled >= suggested,
            "etag": daily_etag(suggested),
        }


def daily_etag(suggested: int) -> str:
    return f"pending-daily:{sha256_json({'suggested': suggested})}"
