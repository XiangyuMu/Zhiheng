"""Approval preserves original sources and immutable conclusion version history."""

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from tests.integration.test_memory_api import _client, _headers, _login


def _draft_with_source(
    client: TestClient, csrf: str, name: str, claim: str
) -> dict[str, Any]:
    source = client.post(
        "/v1/conclusions/sources",
        json={"text": f"完整对话 {name}：在每天学习的前提下，{claim}。保留讨论语境。"},
        headers=_headers(csrf, f"source-{name.encode().hex()}"),
    )
    assert source.status_code == 200, source.text
    response = client.post(
        "/v1/conclusions",
        json={
            "source_id": source.json()["id"],
            "title": name,
            "claim": claim,
            "domain_id": "education_learning",
            "premises": [{"text": "每天学习", "confirmed": False}],
            "excerpt": claim,
        },
        headers=_headers(csrf, f"draft-{name.encode().hex()}"),
    )
    assert response.status_code == 200, response.text
    return dict(response.json())


@pytest.mark.parametrize(
    ("kind", "new_claim", "old_status"),
    [
        ("supplement", "每天复习能提高记忆并巩固理解", "formal"),
        ("revision", "每周复习能提高记忆", "superseded"),
    ],
)
def test_relation_approval_preserves_sources_versions_and_review_history(
    tmp_path: Path, kind: str, new_claim: str, old_status: str
) -> None:
    client, sessions = _client(tmp_path)
    csrf = _login(client)
    old = _draft_with_source(client, csrf, "原结论", "每天复习能提高记忆")
    edited_old = client.patch(
        f"/v1/conclusions/{old['id']}",
        json={"title": "原结论审核版"},
        headers=_headers(csrf, "edit-old", old["etag"]),
    )
    assert edited_old.status_code == 200, edited_old.text
    approved_old = client.post(
        f"/v1/conclusions/{old['id']}/approve",
        json={},
        headers=_headers(csrf, "approve-old", edited_old.json()["etag"]),
    )
    assert approved_old.status_code == 200, approved_old.text

    new = _draft_with_source(client, csrf, "新结论", new_claim)
    edited_new = client.patch(
        f"/v1/conclusions/{new['id']}",
        json={"title": "新结论审核版"},
        headers=_headers(csrf, "edit-new", new["etag"]),
    )
    assert edited_new.status_code == 200, edited_new.text
    before = {
        entry_id: client.get(f"/v1/conclusions/{entry_id}").json()
        for entry_id in (old["id"], new["id"])
    }
    assert before[old["id"]]["status"] == "formal"
    assert before[new["id"]]["status"] == "draft"
    proposals = client.get(f"/v1/conclusions/{new['id']}/relations").json()["items"]
    assert {item["left_version"] for item in proposals} == {1, 2}
    relation = next(item for item in proposals if item["left_version"] == 2)
    assert relation["kind"] == kind
    assert relation["status"] == "proposed"
    assert relation["right_version"] == 2

    # The API exposes pinned relation versions, but not the older draft versions.
    # Read the durable history to prove approval cannot silently rewrite those versions.
    version_query = text(
        "SELECT entry_id,version,payload_json,approved_at FROM conclusion_versions "
        "WHERE entry_id IN (:old,:new) ORDER BY entry_id,version"
    )
    ids = {"old": old["id"], "new": new["id"]}
    with sessions() as session:
        versions_before = [dict(row) for row in session.execute(version_query, ids).mappings()]
    assert len(versions_before) == 4

    approved = client.post(
        f"/v1/conclusions/relations/{relation['id']}/approve",
        json={},
        headers=_headers(csrf, "approve-relation"),
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "approved"
    assert approved.json()["knowledge_id"] != approved_old.json()["knowledge_id"]

    for entry_id, expected_status in ((old["id"], old_status), (new["id"], "formal")):
        after = client.get(f"/v1/conclusions/{entry_id}").json()
        assert after["status"] == expected_status
        assert after["version"] == after["approved_version"] == 2
        for field in ("source", "claim", "premises", "excerpt", "title", "domain_id"):
            assert after[field] == before[entry_id][field]
        assert after["premises"] == [{"text": "每天学习", "confirmed": False}]
        assert after["source"]["text"].endswith("。保留讨论语境。")
        reviewed = next(item for item in after["relations"] if item["id"] == relation["id"])
        assert reviewed["status"] == "approved"
        assert reviewed["left_status"] == "formal"
        assert reviewed["right_status"] == old_status
        assert reviewed["left_claim"] == new_claim
        assert reviewed["right_claim"] == "每天复习能提高记忆"
        for side, source_entry in (("left", new["id"]), ("right", old["id"])):
            assert reviewed[f"{side}_source"] == before[source_entry]["source"]
            assert reviewed[f"{side}_premises"] == before[source_entry]["premises"]
            assert reviewed[f"{side}_version"] == 2
        assert len(reviewed["history"]) == 2
        assert {(event["from_status"], event["to_status"]) for event in reviewed["history"]} == {
            (None, "proposed"),
            ("proposed", "approved"),
        }
        for event in reviewed["history"]:
            assert event["left_version"] == event["right_version"] == 2
            assert event["left_source_id"] == before[new["id"]]["source"]["id"]
            assert event["right_source_id"] == before[old["id"]]["source"]["id"]
            assert event["actor_user_id"]
            assert event["created_at"]
        original_proposal = next(item for item in after["relations"] if item["left_version"] == 1)
        expected_original = next(item for item in proposals if item["left_version"] == 1)
        assert original_proposal == expected_original | {
            "left_status": "formal", "right_status": old_status
        }

    with sessions() as session:
        versions_after = [dict(row) for row in session.execute(version_query, ids).mappings()]
    assert len(versions_after) == 4
    for previous, current in zip(versions_before, versions_after, strict=True):
        assert {key: value for key, value in current.items() if key != "approved_at"} == {
            key: value for key, value in previous.items() if key != "approved_at"
        }
        if current["entry_id"] == new["id"] and current["version"] == 2:
            assert previous["approved_at"] is None
            assert current["approved_at"] is not None
        else:
            assert current["approved_at"] == previous["approved_at"]
        if current["version"] == 1:
            assert json.loads(current["payload_json"])["title"] in {"原结论", "新结论"}
            assert current["approved_at"] is None
