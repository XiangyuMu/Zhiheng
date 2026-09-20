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


def test_pending_conclusion_can_be_reopened_from_review_list(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    csrf = _login(client)
    source = client.post(
        "/v1/conclusions/sources",
        json={"text": "原始讨论：先保存这条待审核结论。"},
        headers=_headers(csrf, "review-source"),
    )
    assert source.status_code == 200
    draft = client.post(
        "/v1/conclusions",
        json={
            "source_id": source.json()["id"],
            "title": "待审核结论",
            "claim": "这条结论应当保留",
            "domain_id": "education.learning",
            "premises": [],
            "excerpt": "先保存这条待审核结论。",
        },
        headers=_headers(csrf, "review-draft"),
    )
    assert draft.status_code == 200

    review = client.get("/v1/conclusions/drafts")
    assert review.status_code == 200
    assert len(review.json()["items"]) == 1
    reopened = review.json()["items"][0]
    assert reopened["id"] == draft.json()["id"]
    assert reopened["status"] == "draft"
    assert reopened["claim"] == "这条结论应当保留"
    assert reopened["source"]["text"] == source.json()["text"]


def test_pending_conclusion_can_be_updated_before_approval(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    csrf = _login(client)
    source = client.post(
        "/v1/conclusions/sources",
        json={"text": "讨论原文"},
        headers=_headers(csrf, "update-source"),
    )
    draft = client.post(
        "/v1/conclusions",
        json={
            "source_id": source.json()["id"],
            "title": "可修订结论",
            "claim": "初始判断",
            "domain_id": "education.learning",
            "premises": [],
            "excerpt": "初始判断",
        },
        headers=_headers(csrf, "update-draft"),
    )
    item = draft.json()

    updated = client.patch(
        f"/v1/conclusions/{item['id']}",
        json={"claim": "补充后的判断", "premises": [{"text": "新增前提"}]},
        headers=_headers(csrf, "update-version", item["etag"]),
    )
    assert updated.status_code == 200
    assert updated.json()["version"] == 2
    assert updated.json()["claim"] == "补充后的判断"
    assert updated.json()["premises"] == [{"text": "新增前提"}]
