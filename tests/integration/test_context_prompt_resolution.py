"""Resolve contextual prompts through the same HTTP boundary used by the browser."""

from pathlib import Path

import pytest

from tests.integration.test_memory_api import _client, _headers, _login
from tests.integration.test_personal_updates import _update


@pytest.mark.parametrize("decision, expected", [("confirm", "上海"), ("supplement", "杭州")])
def test_conflict_resolution_updates_actual_context(
    tmp_path: Path, decision: str, expected: str
) -> None:
    client, _ = _client(tmp_path)
    csrf = _login(client)
    for index, city in enumerate(("北京", "上海")):
        result = client.post(
            "/v1/personal-updates", json=_update(city), headers=_headers(csrf, f"city-{index}")
        )
        assert result.status_code == 200, result.text
    prompts = client.get(
        "/v1/personal-updates/context-prompts", params={"query": "我的居住城市"}
    ).json()["items"]
    prompt = prompts[0]
    payload = {"decision": decision, "candidate_etag": prompt["candidate_etag"]}
    if decision == "supplement":
        payload["value"] = {"text": expected}
    response = client.post(
        f"/v1/personal-updates/context-prompts/{prompt['id']}/decision",
        json=payload,
        headers=_headers(csrf, "resolve"),
    )
    assert response.status_code == 200, response.text
    assert client.get("/v1/memory/context/l1?prefix=profile.").json() == {
        "profile.city": {"text": expected}
    }
    assert (
        client.get("/v1/personal-updates/context-prompts", params={"query": "我的居住城市"}).json()[
            "items"
        ]
        == []
    )


@pytest.mark.parametrize("defer_first", [False, True])
def test_missing_information_can_be_supplied_after_deferral(
    tmp_path: Path, defer_first: bool
) -> None:
    client, _ = _client(tmp_path)
    csrf = _login(client)
    answer = client.post(
        "/v1/answers", json={"query": "我的工作偏好是什么？"}, headers=_headers(csrf, "answer")
    )
    assert answer.status_code == 200, answer.text
    prompt = next(item for item in answer.json()["context_prompts"] if item["kind"] == "missing")
    url = f"/v1/personal-updates/context-prompts/{prompt['id']}/decision"
    if defer_first:
        deferred = client.post(url, json={"decision": "defer"}, headers=_headers(csrf, "defer"))
        assert deferred.status_code == 200, deferred.text
        assert client.get("/v1/memory/context/l1?prefix=profile.").json() == {}
        listed = client.get(
            "/v1/personal-updates/context-prompts", params={"query": "我的工作偏好是什么？"}
        ).json()["items"]
        assert any(item["id"] == prompt["id"] and item["status"] == "deferred" for item in listed)
    supplied = client.post(
        url,
        json={
            "decision": "supplement",
            "state_key": "profile.work_preference",
            "value": {"text": "远程工作"},
        },
        headers=_headers(csrf, "supply"),
    )
    assert supplied.status_code == 200, supplied.text
    assert client.get("/v1/memory/context/l1?prefix=profile.").json() == {
        "profile.work_preference": {"text": "远程工作"}
    }
    assert (
        client.get(
            "/v1/personal-updates/context-prompts", params={"query": "我的工作偏好是什么？"}
        ).json()["items"]
        == []
    )


def test_conflict_resolution_rejects_stale_candidate_etag(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    csrf = _login(client)
    assert (
        client.post(
            "/v1/personal-updates", json=_update("北京"), headers=_headers(csrf, "old")
        ).status_code
        == 200
    )
    conflict = client.post(
        "/v1/personal-updates", json=_update("上海"), headers=_headers(csrf, "new")
    )
    assert conflict.status_code == 200
    conflict_id = conflict.json()["result"]["conflict_id"]
    prompt = client.get(
        "/v1/personal-updates/context-prompts", params={"query": "居住城市"}
    ).json()["items"][0]
    response = client.post(
        f"/v1/personal-updates/context-prompts/{conflict_id}/decision",
        json={"decision": "confirm", "candidate_etag": "stale-etag"},
        headers=_headers(csrf, "stale"),
    )
    assert response.status_code == 412, response.text
    assert client.get("/v1/memory/context/l1?prefix=profile.").json() == {
        "profile.city": {"text": "北京"}
    }
    assert prompt["status"] == "pending"
