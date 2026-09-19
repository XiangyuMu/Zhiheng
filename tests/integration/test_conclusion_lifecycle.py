from pathlib import Path

from tests.integration.test_memory_api import _client, _headers, _login


def test_only_reviewed_conclusion_is_available_across_conversations(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    csrf = _login(client)
    source = client.post(
        "/v1/conclusions/sources",
        json={
            "text": "用户：假设每天练习。结论：蓝鹭练习法可用于训练。",
        },
        headers=_headers(csrf, "source"),
    )
    assert source.status_code == 200
    draft = client.post(
        "/v1/conclusions",
        json={
            "source_id": source.json()["id"],
            "title": "蓝鹭练习法",
            "claim": "蓝鹭练习法可用于训练",
            "domain_id": "technology.ai",
            "premises": [{"text": "每天练习", "confirmed": False}],
            "excerpt": "结论：蓝鹭练习法可用于训练。",
        },
        headers=_headers(csrf, "draft"),
    )
    assert draft.status_code == 200
    item = draft.json()
    assert client.get("/v1/conclusions/context", params={"query": "蓝鹭"}).json()["items"] == []
    accepted = client.post(
        f"/v1/conclusions/{item['id']}/approve",
        json={},
        headers=_headers(csrf, "approve", item["etag"]),
    )
    assert accepted.status_code == 200
    result = client.get("/v1/conclusions/context", params={"query": "蓝鹭"}).json()["items"]
    assert len(result) == 1
    assert result[0]["text"] == "如果每天练习，则蓝鹭练习法可用于训练"
    detail = client.get(f"/v1/conclusions/{item['id']}").json()
    assert detail["source"]["text"] == source.json()["text"]
    assert detail["approved_version"] == 1
