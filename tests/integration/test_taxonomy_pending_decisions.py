from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from tests.integration.test_topic_taxonomy import _client, _headers, _import_knowledge, _login


def _reclassification(client: TestClient, csrf: str, knowledge_id: str) -> dict[str, Any]:
    created = client.post(
        "/v1/taxonomy/proposals/reclassification",
        json={
            "knowledge_object_id": knowledge_id,
            "primary_domain_id": "economics_finance_business",
            "record_type": "personal_archive_experience",
            "reason": "主题是投资决策",
        },
        headers=_headers(csrf, f"proposal-{knowledge_id}"),
    )
    assert created.status_code == 201, created.text
    return dict(created.json()["result"])


def test_rejected_proposal_keeps_assignment_and_reason_in_history(tmp_path: Path) -> None:
    client = _client(tmp_path)
    csrf = _login(client)
    knowledge_id = _import_knowledge(client, csrf, "import", "personal_archive_experience")
    proposal = _reclassification(client, csrf, knowledge_id)

    rejected = client.post(
        f"/v1/taxonomy/proposals/{proposal['id']}/reject",
        json={"reason": "这条确实是个人经历"},
        headers=_headers(csrf, "reject", proposal["etag"]),
    )

    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["result"]["status"] == "rejected"
    assignment = client.get(f"/v1/knowledge/{knowledge_id}/classifications").json()
    assert assignment["primary_domain_id"] == "personal_archive_experience"
    detail = client.get(f"/v1/taxonomy/proposals/{proposal['id']}").json()
    assert detail["status"] == "rejected"
    assert detail["payload"]["after"]["primary_domain_id"] == "economics_finance_business"
    assert [(event["action"], event["payload"].get("reason")) for event in detail["history"]] == [
        ("rejected", "这条确实是个人经历")
    ]
    approve = client.post(
        f"/v1/taxonomy/proposals/{proposal['id']}/approve",
        headers=_headers(csrf, "approve-after-reject", detail["etag"]),
    )
    assert (
        client.get(f"/v1/knowledge/{knowledge_id}/classifications").json()["primary_domain_id"]
        == "personal_archive_experience"
    ), approve.text


def test_deferred_proposal_leaves_pending_until_restored(tmp_path: Path) -> None:
    client = _client(tmp_path)
    csrf = _login(client)
    knowledge_id = _import_knowledge(client, csrf, "import", "personal_archive_experience")
    created = _reclassification(client, csrf, knowledge_id)
    proposal = client.get(f"/v1/taxonomy/proposals/{created['id']}").json()

    deferred = client.post(
        f"/v1/taxonomy/proposals/{proposal['id']}/defer",
        headers=_headers(csrf, "defer", proposal["etag"]),
    )

    assert deferred.status_code == 200, deferred.text
    assert deferred.json()["result"]["status"] == "deferred"
    pending = client.get("/v1/taxonomy/proposals", params={"status": "pending"}).json()["items"]
    assert pending == []
    stale_restore = client.post(
        f"/v1/taxonomy/proposals/{proposal['id']}/restore",
        headers=_headers(csrf, "restore-stale", proposal["etag"]),
    )
    assert stale_restore.status_code == 412
    assert (
        client.get(f"/v1/knowledge/{knowledge_id}/classifications").json()["primary_domain_id"]
        == "personal_archive_experience"
    )

    restored = client.post(
        f"/v1/taxonomy/proposals/{proposal['id']}/restore",
        headers=_headers(csrf, "restore", deferred.json()["result"]["etag"]),
    )

    assert restored.status_code == 200, restored.text
    pending = client.get("/v1/taxonomy/proposals", params={"status": "pending"}).json()["items"]
    assert [item["id"] for item in pending] == [proposal["id"]]
    assert pending[0]["created_at"] == proposal["created_at"]
    assert pending[0]["etag"] == proposal["etag"]
    detail = client.get(f"/v1/taxonomy/proposals/{proposal['id']}").json()
    assert [event["action"] for event in detail["history"]] == ["deferred", "restored"]


def test_item_without_target_is_undecided_not_deferred(tmp_path: Path) -> None:
    client = _client(tmp_path)
    csrf = _login(client)
    knowledge_id = _import_knowledge(client, csrf, "import", "personal_archive_experience")
    proposal = client.post(
        "/v1/taxonomy/proposals/legacy-migration",
        json={"mapping": {knowledge_id: "economics_finance_business"}, "reason": "逐条迁移"},
        headers=_headers(csrf, "legacy"),
    ).json()["result"]

    cleared = client.patch(
        f"/v1/taxonomy/proposals/{proposal['id']}/items/{knowledge_id}",
        json={"target_domain_id": None},
        headers=_headers(csrf, "clear", proposal["etag"]),
    ).json()["result"]
    approved = client.post(
        f"/v1/taxonomy/proposals/{proposal['id']}/approve",
        headers=_headers(csrf, "approve", cleared["etag"]),
    ).json()["result"]

    assert [item["migration_status"] for item in cleared["preview"]["items"]] == ["undecided"]
    assert approved["status"] == "pending"
    assert approved["result"]["undecided"] == [knowledge_id]
    assert "deferred" not in approved["result"]


def test_deferred_proposal_can_still_be_decided(tmp_path: Path) -> None:
    client = _client(tmp_path)
    csrf = _login(client)
    knowledge_id = _import_knowledge(client, csrf, "import", "personal_archive_experience")
    proposal = _reclassification(client, csrf, knowledge_id)
    deferred = client.post(
        f"/v1/taxonomy/proposals/{proposal['id']}/defer",
        headers=_headers(csrf, "defer", proposal["etag"]),
    ).json()["result"]

    approved = client.post(
        f"/v1/taxonomy/proposals/{proposal['id']}/approve",
        headers=_headers(csrf, "approve", deferred["etag"]),
    )

    assert approved.status_code == 200, approved.text
    assert approved.json()["result"]["status"] == "approved"
    assert (
        client.get(f"/v1/knowledge/{knowledge_id}/classifications").json()["primary_domain_id"]
        == "economics_finance_business"
    )
