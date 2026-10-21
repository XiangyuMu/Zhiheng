"""POST /v1/review/batch: one action over items of several pending types (#80)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from tests.integration.test_memory_api import _candidate_payload, _client, _headers, _login
from tests.integration.test_pending_inbox import _act, _draft, _import, _items, _relation
from tests.integration.test_taxonomy_pending_decisions import _reclassification


def _batch(
    client: TestClient, csrf: str, action: str, items: list[dict[str, Any]], key: str
) -> Any:
    return client.post(
        "/v1/review/batch",
        json={
            "action": action,
            "items": [{"type": i["type"], "id": i["id"], "etag": i["etag"]} for i in items],
        },
        headers=_headers(csrf, key),
    )


def _candidate(client: TestClient, csrf: str, key: str, candidate_type: str) -> dict[str, Any]:
    response = client.post(
        "/v1/memory/candidates",
        json={**_candidate_payload(key), "candidate_type": candidate_type},
        headers=_headers(csrf, key),
    )
    assert response.status_code == 200, response.text
    return _pending(client, response.json()["result"]["candidate_id"])


def _pending(client: TestClient, item_id: str, **params: Any) -> dict[str, Any]:
    return next(i for i in _items(client, deferred="include", **params) if i["id"] == item_id)


def test_one_stale_item_fails_alone_and_the_others_are_deferred(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    csrf = _login(client)
    draft = _pending(client, _draft(client, csrf, "batch-a")["id"])
    candidate = _candidate(client, csrf, "batch-candidate", "explicit_extracted")
    stale = {**_candidate(client, csrf, "batch-stale", "inferred"), "etag": "outdated"}

    response = _batch(client, csrf, "defer", [draft, stale, candidate], "batch-defer")

    assert response.status_code == 200, response.text
    results = response.json()["result"]["items"]
    assert [(r["type"], r["id"], r["status"]) for r in results] == [
        ("conclusion_draft", draft["id"], "succeeded"),
        ("memory_candidate", stale["id"], "failed"),
        ("memory_candidate", candidate["id"], "succeeded"),
    ]
    assert results[1]["reason"] == "stale"
    assert results[0]["reason"] is None
    deferred = {i["id"] for i in _items(client, deferred="only")}
    assert deferred == {draft["id"], candidate["id"]}


def test_batch_approve_naming_an_individually_approved_item_changes_nothing(
    tmp_path: Path,
) -> None:
    client, _ = _client(tmp_path)
    csrf = _login(client)
    explicit = _candidate(client, csrf, "batch-explicit", "explicit_extracted")
    inferred = _candidate(client, csrf, "batch-inferred", "inferred")
    draft = _pending(client, _draft(client, csrf, "batch-draft")["id"])
    relation = _pending(client, _relation(client, csrf, "batch-relation")["id"])
    before = _items(client, deferred="include")

    for individual in (draft, relation, inferred):
        refused = _batch(client, csrf, "approve", [explicit, individual], "batch-approve")
        assert refused.status_code == 400, refused.text

    assert _items(client, deferred="include") == before
    # Nothing was recorded under the key either: it can still be used.
    deferred = _batch(client, csrf, "defer", [explicit], "batch-approve")
    assert deferred.status_code == 200, deferred.text
    assert deferred.json()["result"]["items"][0]["status"] == "succeeded"


def test_replaying_the_idempotency_key_returns_the_same_result(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    csrf = _login(client)
    draft = _pending(client, _draft(client, csrf, "batch-replay")["id"])
    stale = {**_candidate(client, csrf, "batch-replay-stale", "inferred"), "etag": "outdated"}

    first = _batch(client, csrf, "defer", [draft, stale], "batch-replay")
    replay = _batch(client, csrf, "defer", [draft, stale], "batch-replay")
    reused = _batch(client, csrf, "reject", [draft], "batch-replay")

    assert first.status_code == 200, first.text
    assert replay.status_code == 200, replay.text
    assert replay.json() == first.json()
    assert reused.status_code == 409
    assert _pending(client, draft["id"])["deferred"] is True


def test_explicit_candidates_are_approved_and_any_candidate_rejected_in_bulk(
    tmp_path: Path,
) -> None:
    client, _ = _client(tmp_path)
    csrf = _login(client)
    explicit = _candidate(client, csrf, "batch-ok-explicit", "explicit_extracted")
    inferred = _candidate(client, csrf, "batch-ok-inferred", "inferred")

    approved = _batch(client, csrf, "approve", [explicit], "batch-ok-approve")
    rejected = _batch(client, csrf, "reject", [inferred], "batch-ok-reject")

    assert approved.status_code == 200, approved.text
    assert rejected.status_code == 200, rejected.text
    assert [r["status"] for r in approved.json()["result"]["items"]] == ["succeeded"]
    assert [r["status"] for r in rejected.json()["result"]["items"]] == ["succeeded"]
    assert _items(client, deferred="include") == []
    formal = client.get("/v1/memory/formal").json()["items"]
    assert [m["value"] for m in formal] == [{"text": "batch-ok-explicit"}]


def test_classification_suggestions_and_proposals_are_approved_in_bulk(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    csrf = _login(client)
    unclassified = _pending(client, _import(client, csrf, "batch-import"))
    reclassified = _import(client, csrf, "batch-import-2", title="理财记录", text="投资决策。")
    proposal = _pending(client, _reclassification(client, csrf, reclassified)["id"])

    approved = _batch(client, csrf, "approve", [unclassified, proposal], "batch-classify")

    assert approved.status_code == 200, approved.text
    assert [r["status"] for r in approved.json()["result"]["items"]] == [
        "succeeded",
        "succeeded",
    ]
    assignment = client.get(f"/v1/knowledge/{unclassified['id']}/classifications").json()
    assert assignment["primary_domain_id"] == "computing_engineering"
    moved = client.get(f"/v1/knowledge/{reclassified}/classifications").json()
    assert moved["primary_domain_id"] == "economics_finance_business"
    remaining = {i["id"] for i in _items(client, type="classification", deferred="include")}
    assert unclassified["id"] not in remaining
    assert proposal["id"] not in remaining
    listed = client.get("/v1/knowledge/unclassified").json()["items"]
    assert unclassified["id"] not in {i["knowledge_object_id"] for i in listed}


def test_drafts_and_relations_are_approved_one_at_a_time_and_rejected_in_bulk(
    tmp_path: Path,
) -> None:
    client, _ = _client(tmp_path)
    csrf = _login(client)
    draft = _pending(client, _draft(client, csrf, "single-draft")["id"])
    relation = _pending(client, _relation(client, csrf, "single-relation")["id"])
    rejected_draft = _pending(client, _draft(client, csrf, "bulk-reject", "睡眠影响记忆")["id"])

    approved_draft = _act(client, csrf, draft, "approve", "single-draft-approve")
    approved_relation = _act(client, csrf, relation, "approve", "single-relation-approve")
    rejected = _batch(client, csrf, "reject", [rejected_draft], "bulk-reject")

    assert approved_draft.status_code == 200, approved_draft.text
    assert approved_draft.json()["result"]["status"] == "formal"
    assert approved_draft.json()["result"]["knowledge_id"]
    assert approved_relation.status_code == 200, approved_relation.text
    assert approved_relation.json()["result"]["status"] == "approved"
    assert approved_relation.json()["result"]["knowledge_id"]
    assert rejected.json()["result"]["items"][0]["status"] == "succeeded"
    conclusions = "conclusion_draft,relation_suggestion"
    assert _items(client, deferred="include", type=conclusions) == []
    assert client.get(f"/v1/conclusions/{rejected_draft['id']}").json()["status"] == "rejected"
