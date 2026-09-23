from pathlib import Path

from tests.integration.test_memory_api import _client, _headers, _login


def _draft(client, csrf: str, key: str) -> dict:
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
    return response.json()


def test_review_center_summary_exposes_details_and_defer_is_reopenable(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    csrf = _login(client)
    draft = _draft(client, csrf, "review-center")
    summary = client.get("/v1/review/summary")
    assert summary.status_code == 200
    body = summary.json()
    item = next(item for item in body["conclusions"] if item["id"] == draft["id"])
    assert item["premises"][0]["text"] == "固定条件"
    assert item["source"]["text"]
    assert "relations" in item
    deferred = client.post(
        f"/v1/conclusions/{draft['id']}/defer",
        json={},
        headers=_headers(csrf, "review-center-defer", draft["etag"]),
    )
    assert deferred.status_code == 200, deferred.text
    reopened = client.get("/v1/review/summary").json()
    reopened_item = next(item for item in reopened["conclusions"] if item["id"] == draft["id"])
    assert reopened_item["status"] == "deferred"


def test_review_center_page_and_scripts_are_available(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    _login(client)
    page = client.get("/review-center")
    assert page.status_code == 200
    assert "集中审核" in page.text
    assert "/v1/review/summary" in client.get("/review-center.js").text


def test_review_and_context_scripts_send_write_preconditions(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    _login(client)
    review_script = client.get("/review-center.js").text
    knowledge_script = client.get("/knowledge-agent.js").text
    assert '"If-Match": etag' in review_script
    assert '"If-Match": ifMatch' in knowledge_script
    assert "/v1/personal-updates/context-prompts/" in knowledge_script
