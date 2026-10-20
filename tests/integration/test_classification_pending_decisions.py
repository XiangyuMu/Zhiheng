from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import text

from tests.integration.test_topic_taxonomy import _client, _headers, _import_knowledge, _login
from zhiheng.core.ids import json_text, new_id
from zhiheng.db.session import session_scope


def _unclassified_ids(client: TestClient, **params: str) -> list[str]:
    response = client.get("/v1/knowledge/unclassified", params=params)
    assert response.status_code == 200, response.text
    return [str(item["knowledge_object_id"]) for item in response.json()["items"]]


def _pending_suggestion(client: TestClient, csrf: str) -> str:
    """Arrange one pending node suggestion the way the classification job would."""
    node = client.post(
        "/v1/classifications",
        json={"domain_id": "computing_engineering", "name": "哈希表"},
        headers=_headers(csrf, "node"),
    )
    assert node.status_code == 201, node.text
    imported = client.post(
        "/v1/knowledge/imports",
        json={"title": "哈希表 入门", "text": "哈希表 是一种查找结构。"},
        headers=_headers(csrf, "suggestion-import"),
    )
    assert imported.status_code == 200, imported.text
    knowledge_id = str(imported.json()["result"]["knowledge_object_id"])
    owner = client.get("/me").json()["user_id"]
    suggestion_id = new_id()
    # Arranged directly: the job's generator is not exercised by this ticket.
    with session_scope(client.app.state.session_factory) as session:  # type: ignore[attr-defined]
        session.execute(
            text(
                """
                INSERT INTO classification_suggestions
                  (id, knowledge_object_id, owner_user_id, candidate_json, confidence,
                   explanation, status)
                VALUES (:id, :knowledge, :owner, :candidate, 0.35, '标题匹配', 'pending')
                """
            ),
            {
                "id": suggestion_id,
                "knowledge": knowledge_id,
                "owner": owner,
                "candidate": json_text({"node_id": node.json()["id"], "source": "rule"}),
            },
        )
    return suggestion_id


def _suggestion_ids(client: TestClient, status: str) -> list[str]:
    response = client.get("/v1/classification-suggestions", params={"status": status})
    assert response.status_code == 200, response.text
    return [str(item["id"]) for item in response.json()["items"]]


def test_classification_suggestion_can_be_deferred_restored_and_rejected(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
    csrf = _login(client)
    suggestion_id = _pending_suggestion(client, csrf)
    assert _suggestion_ids(client, "pending") == [suggestion_id]

    deferred = client.post(
        f"/v1/classification-suggestions/{suggestion_id}/defer", headers=_headers(csrf, "defer")
    )

    assert deferred.status_code == 200, deferred.text
    assert deferred.json()["status"] == "deferred"
    assert _suggestion_ids(client, "pending") == []
    assert _suggestion_ids(client, "deferred") == [suggestion_id]

    restored = client.post(
        f"/v1/classification-suggestions/{suggestion_id}/restore",
        headers=_headers(csrf, "restore"),
    )
    assert restored.status_code == 200, restored.text
    assert _suggestion_ids(client, "pending") == [suggestion_id]

    client.post(
        f"/v1/classification-suggestions/{suggestion_id}/defer", headers=_headers(csrf, "defer-2")
    )
    rejected = client.post(
        f"/v1/classification-suggestions/{suggestion_id}/reject", headers=_headers(csrf, "reject")
    )
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["status"] == "rejected"
    assert _suggestion_ids(client, "deferred") == []
    again = client.post(
        f"/v1/classification-suggestions/{suggestion_id}/accept", headers=_headers(csrf, "accept")
    )
    assert again.status_code == 409


def test_accepted_classification_suggestion_is_assigned(tmp_path: Path) -> None:
    client = _client(tmp_path)
    csrf = _login(client)
    suggestion_id = _pending_suggestion(client, csrf)

    accepted = client.post(
        f"/v1/classification-suggestions/{suggestion_id}/accept", headers=_headers(csrf, "accept")
    )

    assert accepted.status_code == 200, accepted.text
    body = accepted.json()
    assert body["status"] == "accepted"
    assignment = client.get(f"/v1/knowledge/{body['knowledge_object_id']}/classifications").json()
    assert body["candidate_node_id"] in {str(node["id"]) for node in assignment["tags"]}


def test_unclassified_suggestion_can_be_deferred_and_restored(tmp_path: Path) -> None:
    client = _client(tmp_path)
    csrf = _login(client)
    knowledge_id = _import_knowledge(client, csrf, "import", "unassigned")
    assert knowledge_id in _unclassified_ids(client)

    deferred = client.post(
        f"/v1/knowledge/{knowledge_id}/unclassified/defer",
        headers=_headers(csrf, "defer"),
    )

    assert deferred.status_code == 200, deferred.text
    assert deferred.json()["status"] == "deferred"
    assert knowledge_id not in _unclassified_ids(client)
    listed = client.get("/v1/knowledge/unclassified", params={"include_deferred": "true"})
    item = next(
        entry for entry in listed.json()["items"] if entry["knowledge_object_id"] == knowledge_id
    )
    assert item["deferred"] is True
    assert (
        client.get(f"/v1/knowledge/{knowledge_id}/classifications").json()["primary_domain_id"]
        == "unassigned"
    )

    restored = client.post(
        f"/v1/knowledge/{knowledge_id}/unclassified/restore",
        headers=_headers(csrf, "restore"),
    )

    assert restored.status_code == 200, restored.text
    assert restored.json()["status"] == "pending"
    assert knowledge_id in _unclassified_ids(client)


def test_deferring_unknown_knowledge_is_not_found(tmp_path: Path) -> None:
    client = _client(tmp_path)
    csrf = _login(client)

    response = client.post(
        "/v1/knowledge/missing/unclassified/defer", headers=_headers(csrf, "defer")
    )

    assert response.status_code == 404
