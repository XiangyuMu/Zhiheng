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


def test_relation_approval_rejects_stale_draft_version(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    csrf = _login(client)
    old = _source_and_draft(client, csrf, "old-formal", "固定复习有效", "固定条件")
    assert client.post(
        f"/v1/conclusions/{old['id']}/approve",
        json={},
        headers=_headers(csrf, "approve-old", old["etag"]),
    ).status_code == 200

    new = _source_and_draft(client, csrf, "new-draft", "固定复习无效", "固定条件")
    relations = client.get(f"/v1/conclusions/{new['id']}/relations").json()["items"]
    relation = next(item for item in relations if item["left_version"] == 1)
    updated = client.patch(
        f"/v1/conclusions/{new['id']}",
        json={"claim": "固定复习在新条件下无效"},
        headers=_headers(csrf, "revise-new", new["etag"]),
    )
    assert updated.status_code == 200
    assert updated.json()["version"] == 2

    stale = client.post(
        f"/v1/conclusions/relations/{relation['id']}/approve",
        json={},
        headers=_headers(csrf, "approve-stale-relation"),
    )
    assert stale.status_code == 409
    assert "stale" in stale.text
    current = client.get(f"/v1/conclusions/{new['id']}").json()
    assert current["status"] == "draft"
    assert current["approved_version"] is None


def test_relation_decision_retry_is_idempotent_after_approval(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    csrf = _login(client)
    old = _source_and_draft(client, csrf, "old-idempotent", "复习有效", "固定条件")
    assert client.post(
        f"/v1/conclusions/{old['id']}/approve",
        json={},
        headers=_headers(csrf, "approve-old-idempotent", old["etag"]),
    ).status_code == 200
    new = _source_and_draft(client, csrf, "new-idempotent", "复习无效", "固定条件")
    relation = client.get(f"/v1/conclusions/{new['id']}/relations").json()["items"][0]
    headers = _headers(csrf, "approve-relation-idempotent")
    first = client.post(
        f"/v1/conclusions/relations/{relation['id']}/approve", json={}, headers=headers
    )
    second = client.post(
        f"/v1/conclusions/relations/{relation['id']}/approve", json={}, headers=headers
    )
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()


def test_concurrent_relation_approvals_have_one_decision(tmp_path: Path) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from sqlalchemy import text

    client, sessions = _client(tmp_path)
    csrf = _login(client)
    old = _source_and_draft(client, csrf, "race-old", "复习有效", "固定条件")
    assert client.post(
        f"/v1/conclusions/{old['id']}/approve", json={},
        headers=_headers(csrf, "race-old-approve", old["etag"]),
    ).status_code == 200
    new = _source_and_draft(client, csrf, "race-new", "复习无效", "固定条件")
    relation = client.get(f"/v1/conclusions/{new['id']}/relations").json()["items"][0]
    barrier = Barrier(2)

    def approve(index):
        barrier.wait(timeout=10)
        return client.post(
            f"/v1/conclusions/relations/{relation['id']}/approve", json={},
            headers=_headers(csrf, f"race-approve-{index}"),
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(approve, range(2)))
    assert sorted(r.status_code for r in responses) == [200, 409]
    with sessions() as session:
        assert session.execute(text(
            "SELECT count(*) FROM conclusion_relation_events "
            "WHERE relation_id=:id AND to_status='approved'"
        ), {"id": relation["id"]}).scalar_one() == 1
    current = client.get(f"/v1/conclusions/{new['id']}").json()
    assert current["approved_version"] == relation["left_version"]
    assert new["id"] in {
        item["id"] for item in client.get(
            "/v1/conclusions/context", params={"query": "复习"}
        ).json()["items"]
    }


def test_changed_right_version_cannot_be_superseded(tmp_path: Path) -> None:
    # Formal-version editing has no HTTP endpoint yet; seed its later persisted state.
    from sqlalchemy import text

    client, sessions = _client(tmp_path)
    csrf = _login(client)
    old = _source_and_draft(client, csrf, "right-old", "复习有效", "固定条件")
    assert client.post(
        f"/v1/conclusions/{old['id']}/approve", json={},
        headers=_headers(csrf, "right-old-approve", old["etag"]),
    ).status_code == 200
    new = _source_and_draft(client, csrf, "right-new", "复习无效", "固定条件")
    relation = client.get(f"/v1/conclusions/{new['id']}/relations").json()["items"][0]
    with sessions.begin() as session:
        session.execute(text(
            "INSERT INTO conclusion_versions(entry_id,version,payload_json,approved_at) "
            "SELECT entry_id,2,payload_json,approved_at FROM conclusion_versions "
            "WHERE entry_id=:id AND version=1"
        ), {"id": old["id"]})
        session.execute(text(
            "UPDATE conclusion_entries SET current_version=2,approved_version=2 WHERE id=:id"
        ), {"id": old["id"]})
        session.execute(text(
            "UPDATE conclusion_relations SET kind='revision' WHERE id=:id"
        ), {"id": relation["id"]})
    response = client.post(
        f"/v1/conclusions/relations/{relation['id']}/approve", json={},
        headers=_headers(csrf, "stale-right-approve"),
    )
    assert response.status_code == 409
    assert "stale" in response.text
    with sessions() as session:
        row = session.execute(text(
            "SELECT status,approved_version FROM conclusion_entries WHERE id=:id"
        ), {"id": old["id"]}).one()
        assert tuple(row) == ("formal", 2)
        assert session.execute(text(
            "SELECT approved_at FROM conclusion_versions WHERE entry_id=:id AND version=1"
        ), {"id": new["id"]}).scalar_one() is None
