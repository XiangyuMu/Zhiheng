from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from sqlalchemy import text

from tests.integration.test_g005_api_impl import _client, _headers, _login, _seed
from zhiheng.db.session import session_scope
from zhiheng.worker.main import process_worker_once


def test_persisted_conversation_creates_multiple_reviewable_conclusion_drafts(
    tmp_path: Path,
) -> None:
    client, factory = _client(tmp_path)
    assert isinstance(client.app, FastAPI)
    csrf = _login(client)
    _seed(client.app.state.session_factory, tmp_path)
    conversation = client.post(
        "/v1/conversations",
        json={"title": "结论提炼"},
        headers=_headers(csrf, "conclusion-conversation"),
    )
    assert conversation.status_code == 200

    answer = client.post(
        "/v1/answers",
        json={
            "query": (
                "请总结。每天复习能提高记忆。\n"
                "前提：每天有可用的复习时间。\n"
                "结论：间隔练习比集中练习更适合长期记忆。\n"
                "前提：有固定练习时间。"
            ),
            "conversation_id": conversation.json()["id"],
        },
        headers=_headers(csrf, "conclusion-answer"),
    )
    assert answer.status_code == 200

    processed = process_worker_once(client.app.state.settings, worker_id="test-conclusion-worker")
    assert processed >= 1

    drafts = client.get("/v1/conclusions/drafts")
    assert drafts.status_code == 200
    items = drafts.json()["items"]
    assert len(items) == 2
    assert {item["claim"] for item in items} == {
        "每天复习能提高记忆。",
        "间隔练习比集中练习更适合长期记忆。",
    }
    assert all(item["status"] == "draft" for item in items)
    assert all(item["source"]["text"] for item in items)
    premises_by_claim = {item["claim"]: item["premises"] for item in items}
    assert premises_by_claim["每天复习能提高记忆。"] == [
        {"text": "每天有可用的复习时间。", "confirmed": False}
    ]
    assert premises_by_claim["间隔练习比集中练习更适合长期记忆。"] == [
        {"text": "有固定练习时间。", "confirmed": False}
    ]
    assert all(item["domain_id"] == "education_learning" for item in items)
    assert all(item["evidence"] for item in items)
    assert client.get("/v1/conclusions/context", params={"query": "复习"}).json()["items"] == []

    process_worker_once(client.app.state.settings, worker_id="test-conclusion-worker-retry")
    assert len(client.get("/v1/conclusions/drafts").json()["items"]) == 2

    history_id = client.get(
        "/v1/answers/history", params={"conversation_id": conversation.json()["id"]}
    ).json()[0]["id"]
    with session_scope(factory) as session:
        session.execute(text("DELETE FROM answer_history WHERE id=:id"), {"id": history_id})
    assert client.get("/v1/conclusions/drafts").json()["items"] == []
