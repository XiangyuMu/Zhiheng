from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from tests.integration.test_memory_api import _client, _headers, _login


def _draft(client: TestClient, csrf: str, key: str) -> dict[str, Any]:
    source = client.post(
        "/v1/conclusions/sources",
        json={"text": "研究结论原文：固定条件下复习有效。"},
        headers=_headers(csrf, key + "-source"),
    ).json()
    response = client.post(
        "/v1/conclusions",
        json={
            "source_id": source["id"],
            "title": "复习结论",
            "claim": "固定条件下复习有效",
            "domain_id": "education_learning",
            "premises": [{"text": "固定条件", "confirmed": False}],
            "excerpt": source["text"],
            "evidence": [{"text": source["text"]}],
        },
        headers=_headers(csrf, key + "-draft"),
    )
    assert response.status_code == 200, response.text
    return dict(response.json())


def test_review_summary_route_is_gone_and_items_carry_drafts(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    csrf = _login(client)
    draft = _draft(client, csrf, "review-center")
    assert client.get("/v1/review/summary").status_code == 404
    listing = client.get("/v1/review/items", params={"type": "conclusion_draft"}).json()
    item = next(item for item in listing["items"] if item["id"] == draft["id"])
    deferred = client.post(
        f"/v1/review/items/conclusion_draft/{draft['id']}/defer",
        headers=_headers(csrf, "review-center-defer", item["etag"]),
    )
    assert deferred.status_code == 200, deferred.text
    assert client.get("/v1/review/counts").json()["by_type"]["conclusion_draft"] == 0
    reopened = client.get(
        "/v1/review/items", params={"type": "conclusion_draft", "deferred": "only"}
    ).json()
    assert [entry["id"] for entry in reopened["items"]] == [draft["id"]]


def test_review_center_page_and_scripts_are_available(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    _login(client)
    page = client.get("/review-center")
    assert page.status_code == 200
    assert "<h1>待处理</h1>" in page.text
    script = client.get("/review-center.js").text
    assert "/v1/review/items" in script
    assert "/v1/review/counts" in script


def test_review_and_context_scripts_send_write_preconditions(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    _login(client)
    review_script = client.get("/review-center.js").text
    knowledge_script = client.get("/knowledge-agent.js").text
    assert '"If-Match": etag' in review_script
    assert '"If-Match": ifMatch' in knowledge_script
    assert "/v1/personal-updates/context-prompts/" in knowledge_script
    assert "/v1/review/items/" in knowledge_script
