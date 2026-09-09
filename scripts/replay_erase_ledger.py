#!/usr/bin/env python
"""Replay privacy erases before a restored database serves traffic."""

from __future__ import annotations

import os

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from zhiheng.privacy.erase import PrivacyEraseService


def main() -> None:
    database_url = os.environ.get("ZHIHENG_DATABASE_URL", "")
    if not database_url.startswith("sqlite:///"):
        raise SystemExit("ZHIHENG_DATABASE_URL must be a sqlite URL")
    replay_external = os.environ.get("ZHIHENG_REPLAY_EXTERNAL_ERASE_JOURNAL") == "1"
    engine = create_engine(database_url)
    try:
        with Session(engine) as session:
            service = PrivacyEraseService()
            replayed = (
                service.replay_external_journal(session)
                if replay_external
                else service.replay_pending(session)
            )
            session.commit()
        print(f"replayed_erases={replayed}")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
