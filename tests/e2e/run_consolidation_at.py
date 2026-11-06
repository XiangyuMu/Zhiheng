"""Run the worker's consolidation step at a given time against the configured database.

Browser stages use this to stand in for the 30 idle minutes the live worker
waits for: it calls exactly what the worker calls, only with a clock.

    python tests/e2e/run_consolidation_at.py 2026-11-06T12:00:00+00:00
"""

from __future__ import annotations

import json
import sys
from datetime import datetime

from zhiheng.consolidation import run_consolidation_if_due
from zhiheng.core.config import get_settings
from zhiheng.db.session import create_session_factory, create_sqlite_engine


def main(argv: list[str]) -> int:
    now = datetime.fromisoformat(argv[1])
    engine = create_sqlite_engine(get_settings())
    try:
        run = run_consolidation_if_due(create_session_factory(engine), now=now)
    finally:
        engine.dispose()
    print(
        json.dumps(
            {"ran": False}
            if run is None
            else {
                "ran": True,
                "suggestions_created": run.suggestions_created,
                "findings_created": run.findings_created,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
