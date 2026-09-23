from __future__ import annotations

from pathlib import Path

from tests.integration.test_g005_api_impl import _client, _headers, _login, _seed


def test_conversation_context_history_and_favorite_are_owner_scoped(tmp_path: Path) -> None:
    client, factory = _client(tmp_path)
    csrf = _login(client)
    _seed(factory, tmp_path)
    created = client.post(
        "/v1/conversations",
        json={"title": "检索上下文"},
        headers=_headers(csrf, "conversation-create"),
    )
    assert created.status_code == 200
    conversation_id = created.json()["id"]

    answer = client.post(
        "/v1/answers",
        json={"query": "中文检索规范是什么？", "conversation_id": conversation_id},
        headers=_headers(csrf, "conversation-answer"),
    )
    assert answer.status_code == 200
    history = client.get("/v1/answers/history", params={"conversation_id": conversation_id})
    assert history.status_code == 200
    item = history.json()[0]
    assert item["query"] == "中文检索规范是什么？"
    assert item["response"]["answer"] == answer.json()["answer"]

    favorite = client.post(
        f"/v1/answers/history/{item['id']}/favorite",
        headers=_headers(csrf, "history-favorite"),
    )
    assert favorite.status_code == 200
    assert favorite.json() == {"is_favorite": True}
    assert (
        client.get("/v1/answers/history", params={"favorite": "true"}).json()[0]["id"] == item["id"]
    )

    detail = client.get(f"/v1/conversations/{conversation_id}")
    assert detail.status_code == 200
    assert detail.json()["turn_count"] == 1
