#!/usr/bin/env python3
"""Create a supported pre-head database with one historical conclusion."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

from alembic import command
from alembic.config import Config

LEGACY_REVISION = "0034_conclusion_applicability"


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: prepare_issue11_legacy_db.py /path/to/database.sqlite")
    path = Path(sys.argv[1])
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    command.upgrade(config, LEGACY_REVISION)
    payload = json.dumps(
        {
            "title": "Issue 11 legacy conclusion",
            "claim": "历史结论在升级后仍可读取",
            "domain_id": "education_learning",
            "premises": [{"text": "旧 schema 数据", "confirmed": True}],
            "excerpt": "旧 schema 历史原文",
            "evidence": [{"text": "旧 schema 历史原文"}],
        },
        ensure_ascii=False,
    )
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO conclusion_sources (id, owner_user_id, body) VALUES (?, ?, ?)",
            ("issue11-legacy-source", "issue17-workspace", "旧 schema 历史原文"),
        )
        connection.execute(
            "INSERT INTO conclusion_entries "
            "(id, owner_user_id, source_id, current_version, approved_version, status) "
            "VALUES (?, ?, ?, 1, 1, 'draft')",
            ("issue11-legacy-entry", "issue17-workspace", "issue11-legacy-source"),
        )
        connection.execute(
            "INSERT INTO conclusion_versions (entry_id, version, payload_json) VALUES (?, 1, ?)",
            ("issue11-legacy-entry", payload),
        )


if __name__ == "__main__":
    main()
