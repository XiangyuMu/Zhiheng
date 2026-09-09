#!/usr/bin/env python
"""Run Alembic migrations against an explicit SQLite database path."""

from __future__ import annotations

import sys
from pathlib import Path

from alembic import command
from alembic.config import Config


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: upgrade_database.py /path/to/database.sqlite")
    db_path = Path(sys.argv[1])
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")


if __name__ == "__main__":
    main()
