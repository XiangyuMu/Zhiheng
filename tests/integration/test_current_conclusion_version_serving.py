from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from sqlalchemy import text

from tests.integration.test_memory_api import _client, _headers, _login
from tests.integration.test_relation_publication_history import (
    _approve_draft,
    _source_and_draft,
    _worker_pass,
)
from zhiheng.core.ids import new_id


def _alembic_config(db_path: Path) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return config


def _publish_relation(
    client: Any,
    csrf: str,
    sessions: Any,
    *,
    kind: str = "supplement",
) -> tuple[dict[str, Any], dict[str, Any], str]:
    old = _source_and_draft(
        client,
        csrf,
        suffix=f"{kind}-old",
        claim="每天复习提高记忆",
        key=f"{kind}-old",
    )
    old_approved = _approve_draft(client, csrf, old, key=f"{kind}-old-approve")
    _worker_pass(client, f"{kind}-first-worker")

    new = _source_and_draft(
        client,
        csrf,
        suffix=f"{kind}-new",
        claim="每天复习提高记忆并巩固理解",
        key=f"{kind}-new",
    )
    relation = next(
        item
        for item in client.get(f"/v1/conclusions/{new['id']}/relations").json()["items"]
        if item["kind"] == kind
    )
    approved = client.post(
        f"/v1/conclusions/relations/{relation['id']}/approve",
        json={},
        headers=_headers(csrf, f"{kind}-relation-approve"),
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["knowledge_id"] == old_approved["knowledge_id"]
    for index in range(3):
        _worker_pass(client, f"{kind}-version-worker-{index}")

    with sessions() as session:
        current_entry = session.execute(
            text(
                "SELECT json_extract(eo.source_metadata_json, '$.conclusion_entry_id') "
                "FROM knowledge_objects ko "
                "JOIN knowledge_versions kv ON kv.id=ko.current_version_id "
                "JOIN content_versions cv ON cv.id=kv.content_version_id "
                "JOIN evidence_objects eo ON eo.id=cv.evidence_object_id "
                "WHERE ko.id=:knowledge_id"
            ),
            {"knowledge_id": old_approved["knowledge_id"]},
        ).scalar_one()
    return old, {**new, "current_entry_id": str(current_entry)}, str(old_approved["knowledge_id"])


def test_historical_suspension_and_expiry_do_not_hide_current_version(tmp_path: Path) -> None:
    client, sessions = _client(tmp_path)
    csrf = _login(client)
    old, new, knowledge_id = _publish_relation(client, csrf, sessions)

    with sessions() as session:
        old_payload = session.execute(
            text(
                "SELECT payload_json FROM conclusion_versions "
                "WHERE entry_id=:entry_id AND version=1"
            ),
            {"entry_id": old["id"]},
        ).scalar_one()
        payload = json.loads(str(old_payload))
        payload["valid_until"] = "2000-01-01T00:00:00Z"
        session.execute(
            text(
                "UPDATE conclusion_versions SET payload_json=:payload "
                "WHERE entry_id=:entry_id AND version=1"
            ),
            {"entry_id": old["id"], "payload": json.dumps(payload, ensure_ascii=False)},
        )
        session.execute(
            text(
                "INSERT INTO conclusion_applicability "
                "(entry_id,version,state,reason,evidence_json) "
                "VALUES (:entry_id,1,'suspended','historical-only','{}')"
            ),
            {"entry_id": old["id"]},
        )
        visible = session.execute(
            text("SELECT id FROM current_formal_knowledge WHERE id=:id"),
            {"id": knowledge_id},
        ).scalar_one_or_none()
    assert visible == knowledge_id
    context = client.get("/v1/conclusions/context", params={"query": "巩固理解"})
    assert context.status_code == 200, context.text
    assert [item["id"] for item in context.json()["items"]] == [new["current_entry_id"]]


def test_current_suspension_hides_current_version(tmp_path: Path) -> None:
    client, sessions = _client(tmp_path)
    csrf = _login(client)
    _old, new, knowledge_id = _publish_relation(client, csrf, sessions)

    with sessions() as session:
        session.execute(
            text(
                "INSERT INTO conclusion_applicability "
                "(entry_id,version,state,reason,evidence_json) "
                "VALUES (:entry_id,1,'suspended','current-entry','{}')"
            ),
                {"entry_id": new["current_entry_id"]},
        )
        visible = session.execute(
            text("SELECT id FROM current_formal_knowledge WHERE id=:id"),
            {"id": knowledge_id},
        ).scalar_one_or_none()
    assert visible is None


def test_approved_conflict_hides_both_current_versions(tmp_path: Path) -> None:
    client, sessions = _client(tmp_path)
    csrf = _login(client)
    left = _approve_draft(
        client,
        csrf,
        _source_and_draft(
            client,
            csrf,
            suffix="conflict-left",
            claim="每天复习提高记忆",
            key="conflict-left",
        ),
        key="conflict-left-approve",
    )
    right = _approve_draft(
        client,
        csrf,
        _source_and_draft(
            client,
            csrf,
            suffix="conflict-right",
            claim="每天复习不会提高记忆",
            key="conflict-right",
        ),
        key="conflict-right-approve",
    )
    with sessions() as session:
        owner = str(
            session.execute(
                text("SELECT owner_user_id FROM conclusion_entries WHERE id=:id"),
                {"id": left["id"]},
            ).scalar_one()
        )
        session.execute(
            text(
                "INSERT INTO conclusion_relations "
                "(id,owner_user_id,left_id,right_id,kind,status,left_version,right_version) "
                "VALUES (:id,:owner,:left_id,:right_id,'conflict','approved',1,1)"
            ),
            {
                "id": new_id(),
                "owner": owner,
                "left_id": left["id"],
                "right_id": right["id"],
            },
        )
        visible = session.execute(
            text(
                "SELECT id FROM current_formal_knowledge "
                "WHERE id IN (:left_id,:right_id)"
            ),
            {"left_id": left["knowledge_id"], "right_id": right["knowledge_id"]},
        ).scalars().all()
    assert visible == []


def test_current_conclusion_view_migration_round_trip(tmp_path: Path) -> None:
    db_path = tmp_path / "zhiheng.db"
    config = _alembic_config(db_path)
    command.upgrade(config, "0037_conversation_extraction_review")
    with sqlite3.connect(db_path) as connection:
        before = str(
            connection.execute(
                "SELECT sql FROM sqlite_master WHERE name='current_formal_knowledge'"
            ).fetchone()[0]
        )
    assert "conclusion_entry_id" not in before

    command.upgrade(config, "head")
    with sqlite3.connect(db_path) as connection:
        upgraded = str(
            connection.execute(
                "SELECT sql FROM sqlite_master WHERE name='current_formal_knowledge'"
            ).fetchone()[0]
        )
    assert "conclusion_entry_id" in upgraded

    command.downgrade(config, "0037_conversation_extraction_review")
    with sqlite3.connect(db_path) as connection:
        downgraded = str(
            connection.execute(
                "SELECT sql FROM sqlite_master WHERE name='current_formal_knowledge'"
            ).fetchone()[0]
        )
    assert "conclusion_entry_id" not in downgraded
