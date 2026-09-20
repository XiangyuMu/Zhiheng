from pathlib import Path

from tests.integration.test_memory_api import _client, _headers, _login


def _source_and_draft(client, csrf: str, suffix: str, claim: str, premise: str) -> dict:
    source = client.post(
        "/v1/conclusions/sources",
        json={"text": f"来源 {suffix}"},
        headers=_headers(csrf, f"relation-source-{suffix}"),
    )
    assert source.status_code == 200
    draft = client.post(
        "/v1/conclusions",
        json={
            "source_id": source.json()["id"],
            "title": suffix,
            "claim": claim,
            "domain_id": "education_learning",
            "premises": [{"text": premise, "confirmed": False}],
            "excerpt": claim,
        },
        headers=_headers(csrf, f"relation-draft-{suffix}"),
    )
    assert draft.status_code == 200
    return draft.json()


def test_formal_conclusion_gets_explainable_relation_proposal_and_review_decision(
    tmp_path: Path,
) -> None:
    client, _ = _client(tmp_path)
    csrf = _login(client)
    old = _source_and_draft(client, csrf, "old-conclusion", "每天复习能提高记忆", "固定时间")
    approved = client.post(
        f"/v1/conclusions/{old['id']}/approve",
        json={},
        headers=_headers(csrf, "approve-old", old["etag"]),
    )
    assert approved.status_code == 200

    new = _source_and_draft(client, csrf, "new-conflict", "每天复习不能提高记忆", "固定时间")
    relations = client.get(f"/v1/conclusions/{new['id']}/relations")
    assert relations.status_code == 200
    items = relations.json()["items"]
    assert len(items) == 1
    relation = items[0]
    assert relation["kind"] == "conflict"
    assert relation["status"] == "proposed"
    assert "前提相同" in relation["explanation"]
    assert relation["left_version"] == 1
    assert relation["right_version"] == 1

    rejected = client.post(
        f"/v1/conclusions/relations/{relation['id']}/reject",
        json={},
        headers=_headers(csrf, "reject-relation"),
    )
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected"
    relation_items = client.get(f"/v1/conclusions/{new['id']}/relations").json()["items"]
    assert relation_items[0]["status"] == "rejected"
    assert client.get("/v1/conclusions/context", params={"query": "每天复习"}).json()["items"]
