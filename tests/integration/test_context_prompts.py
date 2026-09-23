from pathlib import Path

from tests.integration.test_memory_api import _client, _headers, _login
from tests.integration.test_personal_updates import _update


def test_relevant_conflict_is_returned_with_sources_and_can_be_deferred(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    csrf = _login(client)
    assert (
        client.post(
            "/v1/personal-updates", json=_update("北京"), headers=_headers(csrf, "context-city-1")
        ).status_code
        == 200
    )
    conflict = client.post(
        "/v1/personal-updates", json=_update("上海"), headers=_headers(csrf, "context-city-2")
    ).json()["result"]

    response = client.post(
        "/v1/answers",
        json={"query": "我现在居住在哪个城市？"},
        headers=_headers(csrf, "context-answer"),
    )
    assert response.status_code == 200, response.text
    prompt = next(item for item in response.json()["context_prompts"] if item["kind"] == "conflict")
    assert prompt["conflict_id"] == conflict["conflict_id"]
    assert prompt["candidate_source"] == "user_explicit"
    assert prompt["existing_source"] == "explicit_direct"
    assert set(("confirm", "supplement", "defer", "skip")) <= set(prompt["actions"])

    deferred = client.post(
        f"/v1/personal-updates/context-prompts/{prompt['id']}/decision",
        json={"decision": "defer"},
        headers=_headers(csrf, "context-defer"),
    )
    assert deferred.status_code == 200, deferred.text
    assert deferred.json()["result"]["status"] == "deferred"
    deferred_items = client.get(
        "/v1/personal-updates/context-prompts", params={"query": "我现在居住在哪个城市？"}
    ).json()["items"]
    assert deferred_items[0]["status"] == "deferred"
    follow_up = client.post(
        "/v1/answers",
        json={"query": "我现在居住在哪个城市？"},
        headers=_headers(csrf, "context-answer-after-defer"),
    )
    assert follow_up.status_code == 200, follow_up.text
    assert any(
        item["id"] == prompt["id"] and item["status"] == "deferred"
        for item in follow_up.json()["context_prompts"]
    )


def test_missing_context_prompt_is_relevant_persistent_and_skippable(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    csrf = _login(client)
    response = client.post(
        "/v1/answers",
        json={"query": "我的工作偏好是什么？"},
        headers=_headers(csrf, "context-missing"),
    )
    assert response.status_code == 200, response.text
    prompt = next(item for item in response.json()["context_prompts"] if item["kind"] == "missing")
    assert "个人" in prompt["reason"]
    assert "supplement" in prompt["actions"]
    listed = client.get(
        "/v1/personal-updates/context-prompts", params={"query": "我的工作偏好是什么？"}
    )
    assert listed.status_code == 200
    assert listed.json()["items"][0]["id"] == prompt["id"]

    skipped = client.post(
        f"/v1/personal-updates/context-prompts/{prompt['id']}/decision",
        json={"decision": "skip"},
        headers=_headers(csrf, "context-skip"),
    )
    assert skipped.status_code == 200, skipped.text
    assert skipped.json()["result"]["status"] == "skipped"
    assert (
        client.get(
            "/v1/personal-updates/context-prompts", params={"query": "我的工作偏好是什么？"}
        ).json()["items"]
        == []
    )
