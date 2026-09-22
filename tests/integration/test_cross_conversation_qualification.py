from pathlib import Path

from tests.integration.test_g005_api_impl import _client, _headers, _login, _seed
from zhiheng.worker.main import process_worker_once


def test_unapproved_conversation_conclusions_do_not_cross_into_another_answer(
    tmp_path: Path,
) -> None:
    client, _ = _client(tmp_path)
    csrf = _login(client)
    _seed(client.app.state.session_factory, tmp_path)
    source_conversation = client.post(
        "/v1/conversations", json={"title": "source"}, headers=_headers(csrf, "conversation-a")
    ).json()
    unique = "蓝鹭协议的独有合成判断"
    response = client.post(
        "/v1/answers",
        json={
            "conversation_id": source_conversation["id"],
            "query": f"结论：{unique}应当只在原会话审核。",
        },
        headers=_headers(csrf, "answer-a"),
    )
    assert response.status_code == 200, response.text
    assert process_worker_once(client.app.state.settings, worker_id="qualification-worker") >= 1
    assert client.get("/v1/conclusions/context", params={"query": unique}).json()["items"] == []

    other_conversation = client.post(
        "/v1/conversations", json={"title": "other"}, headers=_headers(csrf, "conversation-b")
    ).json()
    other = client.post(
        "/v1/answers",
        json={"conversation_id": other_conversation["id"], "query": unique},
        headers=_headers(csrf, "answer-b"),
    )
    assert other.status_code == 200, other.text
    body = other.json()
    assert body["citations"] == []
    assert unique not in body["answer"]


def test_only_the_approved_draft_becomes_cross_conversation_context(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    csrf = _login(client)
    source = client.post(
        "/v1/conclusions/sources",
        json={"text": "两个待审核判断的原文"},
        headers={**_headers(csrf, "qualification-source"), "If-Match": "*"},
    ).json()
    drafts = []
    for index, claim in enumerate(("独有判断甲", "独有判断乙"), start=1):
        result = client.post(
            "/v1/conclusions",
            json={
                "source_id": source["id"],
                "title": claim,
                "claim": claim,
                "domain_id": "education_learning",
                "excerpt": claim,
            },
            headers={**_headers(csrf, f"qualification-draft-{index}"), "If-Match": "*"},
        )
        assert result.status_code == 200, result.text
        drafts.append(result.json())
    assert client.get("/v1/conclusions/context", params={"query": "独有判断"}).json()["items"] == []
    approved = client.post(
        f"/v1/conclusions/{drafts[0]['id']}/approve",
        json={},
        headers={**_headers(csrf, "qualification-approve"), "If-Match": drafts[0]["etag"]},
    )
    assert approved.status_code == 200, approved.text
    available = client.get("/v1/conclusions/context", params={"query": "独有判断"}).json()["items"]
    assert [item["id"] for item in available] == [drafts[0]["id"]]


def test_rejected_or_deferred_drafts_never_gain_cross_conversation_qualification(
    tmp_path: Path,
) -> None:
    client, _ = _client(tmp_path)
    csrf = _login(client)
    source = client.post(
        "/v1/conclusions/sources",
        json={"text": "拒绝和延期的判断原文"},
        headers={**_headers(csrf, "qualification-decision-source"), "If-Match": "*"},
    ).json()
    drafts = []
    for decision in ("reject", "defer"):
        response = client.post(
            "/v1/conclusions",
            json={
                "source_id": source["id"],
                "title": f"{decision} 的判断",
                "claim": f"跨会话不可用的{decision}判断",
                "domain_id": "education_learning",
                "excerpt": "拒绝和延期的判断原文",
            },
            headers={**_headers(csrf, f"qualification-{decision}-draft"), "If-Match": "*"},
        )
        assert response.status_code == 200, response.text
        drafts.append(response.json())
        decided = client.post(
            f"/v1/conclusions/{drafts[-1]['id']}/{decision}",
            json={},
            headers={**_headers(csrf, f"qualification-{decision}"), "If-Match": drafts[-1]["etag"]},
        )
        assert decided.status_code == 200, decided.text

    assert (
        client.get("/v1/conclusions/context", params={"query": "跨会话不可用"}).json()["items"]
        == []
    )
