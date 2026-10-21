"""Migration 0055: one 延后 store for unclassified knowledge, no skipped conflicts."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import text

from tests.integration.test_classification_pending_decisions import _pending_suggestion
from tests.integration.test_memory_api import _client, _login
from tests.integration.test_pending_inbox import _conflict, _items
from zhiheng.db.session import session_scope


def test_deferred_suggestions_become_item_deferrals_and_skipped_conflicts_reopen(
    tmp_path: Path,
) -> None:
    client, factory = _client(tmp_path)
    csrf = _login(client)
    suggestion_id = _pending_suggestion(client, csrf)
    conflict = _conflict(client, csrf, "migration-conflict")
    with session_scope(factory) as session:
        knowledge_id = session.execute(
            text("SELECT knowledge_object_id FROM classification_suggestions WHERE id = :id"),
            {"id": suggestion_id},
        ).scalar_one()
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{tmp_path / 'zhiheng.db'}")
    command.downgrade(cfg, "0054_extraction_supplement_resolution")
    # The state the earlier revisions could write: a deferred suggestion, a skipped conflict.
    with session_scope(factory) as session:
        session.execute(
            text("UPDATE classification_suggestions SET status = 'deferred' WHERE id = :id"),
            {"id": suggestion_id},
        )
        session.execute(
            text(
                "UPDATE memory_conflicts SET status = 'dismissed', resolution = 'skipped', "
                "resolved_at = CURRENT_TIMESTAMP WHERE id = :id"
            ),
            {"id": conflict["conflict_id"]},
        )
        session.execute(
            text("UPDATE personal_conflicts SET status = 'skipped' WHERE id = :id"),
            {"id": conflict["conflict_id"]},
        )

    command.upgrade(cfg, "head")

    with session_scope(factory) as session:
        status = session.execute(
            text("SELECT status FROM classification_suggestions WHERE id = :id"),
            {"id": suggestion_id},
        ).scalar_one()
    assert status == "pending"
    deferred = _items(client, type="classification", deferred="only")
    assert [i["id"] for i in deferred] == [knowledge_id]
    pending = _items(client, type="personal_conflict")
    assert [i["id"] for i in pending] == [conflict["conflict_id"]]
    assert pending[0]["detail"]["resolution"] is None
