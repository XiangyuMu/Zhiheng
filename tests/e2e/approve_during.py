"""Move the approval of the given conclusions to a given time in the configured database.

The #115 browser stage reports on a week that the Worker's clock has skipped
ahead to; this stands in for the user having approved the stage's own
conclusions during that week. It touches nothing else.

    python tests/e2e/approve_during.py 2026-11-12T12:00:00+00:00 <entry id> [<entry id> ...]
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime

from sqlalchemy import text

from zhiheng.core.config import get_settings
from zhiheng.db.session import create_session_factory, create_sqlite_engine, session_scope


def main(argv: list[str]) -> int:
    at = datetime.fromisoformat(argv[1]).astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")
    engine = create_sqlite_engine(get_settings())
    try:
        with session_scope(create_session_factory(engine)) as session:
            for entry_id in argv[2:]:
                session.execute(
                    text(
                        "UPDATE conclusion_versions SET approved_at = :at "
                        "WHERE entry_id = :id AND approved_at IS NOT NULL"
                    ),
                    {"at": at, "id": entry_id},
                )
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
