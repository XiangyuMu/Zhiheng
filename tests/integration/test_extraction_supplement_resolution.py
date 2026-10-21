"""An extraction needing a supplement stops waiting once supplemented (#80)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from tests.integration.test_memory_api import _client, _headers, _login
from tests.integration.test_pending_inbox import _act, _items
from zhiheng.conclusions.extraction import ConversationConclusionDraftService
from zhiheng.db.session import session_scope


def _history(client: TestClient, factory: sessionmaker[Session], csrf: str) -> tuple[str, str]:
    """An answered question whose extraction we record directly."""
    conversation = client.post(
        "/v1/conversations", json={"title": "聊天"}, headers=_headers(csrf, "conversation")
    )
    assert conversation.status_code == 200, conversation.text
    answer = client.post(
        "/v1/answers",
        json={"query": "一段聊天", "conversation_id": conversation.json()["id"]},
        headers=_headers(csrf, "ask"),
    )
    assert answer.status_code == 200, answer.text
    with session_scope(factory) as session:
        row = session.execute(text("SELECT id, owner_user_id FROM answer_history")).one()
    return str(row[0]), str(row[1])


def _fail(factory: sessionmaker[Session], history: tuple[str, str], reason: str) -> str:
    history_id, owner = history
    with session_scope(factory) as session:
        ConversationConclusionDraftService().record_failure(
            session,
            history_id=history_id,
            conversation_id="conversation-1",
            owner_user_id=owner,
            query="一段聊天",
            answer="",
            failure_code="extractor_error",
            failure_reason=reason,
        )
    return f"conversation-extraction:{history_id}"


def _supplement(client: TestClient, csrf: str, run_id: str) -> Any:
    return client.post(
        f"/v1/conclusions/drafts/{run_id}/supplement",
        json={
            "title": "人工补充",
            "claim": "人工确认的结论",
            "domain_id": "education_learning",
            "excerpt": "一段聊天",
        },
        headers=_headers(csrf, "supplement"),
    )


def _run_ids(client: TestClient, **params: Any) -> list[str]:
    return [i["id"] for i in _items(client, type="extraction_supplement", **params)]


def test_supplemented_extraction_is_resolved_until_its_result_changes(tmp_path: Path) -> None:
    client, factory = _client(tmp_path)
    csrf = _login(client)
    history = _history(client, factory, csrf)
    run_id = _fail(factory, history, "提炼器超时")
    assert _run_ids(client) == [run_id]

    supplemented = _supplement(client, csrf, run_id)

    assert supplemented.status_code == 200, supplemented.text
    assert _run_ids(client, deferred="include") == []
    drafts = _items(client, type="conclusion_draft")
    assert [d["id"] for d in drafts] == [supplemented.json()["id"]]

    _fail(factory, history, "提炼器超时")
    assert _run_ids(client, deferred="include") == []
    _fail(factory, history, "提炼器再次失败")
    assert _run_ids(client) == [run_id]


def test_deferred_extraction_returns_when_its_result_changes(tmp_path: Path) -> None:
    client, factory = _client(tmp_path)
    csrf = _login(client)
    history = _history(client, factory, csrf)
    run_id = _fail(factory, history, "提炼器超时")
    item = _items(client, type="extraction_supplement")[0]
    assert _act(client, csrf, item, "defer", "defer-run").status_code == 200

    _fail(factory, history, "提炼器超时")
    assert _run_ids(client) == []
    _fail(factory, history, "提炼器再次失败")

    assert _run_ids(client) == [run_id]
    assert _run_ids(client, deferred="only") == []
