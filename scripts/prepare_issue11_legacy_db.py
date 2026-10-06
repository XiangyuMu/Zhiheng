#!/usr/bin/env python3
"""Create a supported pre-head database with one historical conclusion."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

from alembic import command
from alembic.config import Config
from argon2 import PasswordHasher

from zhiheng.core.ids import new_id

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
    user_id = new_id()
    password_hash = PasswordHasher().hash("issue17 workspace passphrase")
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO auth_users "
            "(id, username, password_hash, status) VALUES (?, ?, ?, 'active')",
            (user_id, "issue17-workspace", password_hash),
        )
        connection.execute(
            "INSERT INTO conclusion_sources (id, owner_user_id, body) VALUES (?, ?, ?)",
            ("issue11-legacy-source", user_id, "旧 schema 历史原文"),
        )
        connection.execute(
            "INSERT INTO conclusion_entries "
            "(id, owner_user_id, source_id, current_version, approved_version, status) "
            "VALUES (?, ?, ?, 1, NULL, 'draft')",
            ("issue11-legacy-entry", user_id, "issue11-legacy-source"),
        )
        connection.execute(
            "INSERT INTO conclusion_versions (entry_id, version, payload_json) VALUES (?, 1, ?)",
            ("issue11-legacy-entry", payload),
        )
        connection.execute(
            "INSERT INTO model_provider_configs "
            "(id, provider_kind, display_name, enabled, policy_json, secret_ref, "
            "model_allowlist_json, endpoint_url, endpoint_origin, policy_revision) "
            "VALUES (?, 'openai-compatible', ?, 1, '{}', ?, ?, ?, ?, ?)",
            (
                "issue38-legacy-provider",
                "Issue 38 Legacy Provider",
                "env:ZHIHENG_PRIVATE_ISSUE40_LEGACY",
                '["model-a"]',
                "https://models.example.test/v1",
                "https://models.example.test",
                "legacy-provider-revision",
            ),
        )


if __name__ == "__main__":
    main()
