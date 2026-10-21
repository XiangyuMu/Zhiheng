"""延后 means the same thing for every pending type (ADR 0011).

A deferred item offers only 恢复: approving or rejecting it is refused the same way
for every type, on the item action route and in a batch.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from tests.integration.test_memory_api import _candidate_payload, _client, _headers, _login
from tests.integration.test_pending_inbox import (
    _act,
    _conflict,
    _draft,
    _extraction_run,
    _import,
    _items,
    _missing_prompt,
    _relation,
)
from tests.integration.test_taxonomy_pending_decisions import _reclassification


def _one_of_each(
    client: TestClient, factory: sessionmaker[Session], csrf: str
) -> list[dict[str, Any]]:
    """One pending item of every type, all deferred through the inbox."""
    relation = _relation(client, csrf, "uniform-relation")
    ids = {
        _draft(client, csrf, "uniform-draft", "单独的结论")["id"],
        relation["id"],
        _conflict(client, csrf, "uniform-conflict")["conflict_id"],
        _missing_prompt(client, csrf, "uniform-missing")["id"],
        _import(client, csrf, "uniform-unclassified"),
        _reclassification(
            client,
            csrf,
            _import(
                client,
                csrf,
                "uniform-proposal-target",
                title="投资复盘",
                text="一次投资决策的经历。",
                primary_domain_id="personal_archive_experience",
            ),
        )["id"],
        client.post(
            "/v1/memory/candidates",
            json=_candidate_payload("uniform"),
            headers=_headers(csrf, "uniform-candidate"),
        ).json()["result"]["candidate_id"],
    }
    _extraction_run(client, factory, csrf, "uniform-run")
    ids.add("uniform-run")
    for index, item in enumerate(i for i in _items(client, limit=500) if i["id"] in ids):
        response = _act(client, csrf, item, "defer", f"uniform-defer-{index}")
        assert response.status_code == 200, response.text
    deferred = [i for i in _items(client, deferred="only", limit=500) if i["id"] in ids]
    assert {i["type"] for i in deferred} == {
        "conclusion_draft",
        "relation_suggestion",
        "personal_conflict",
        "missing_info_prompt",
        "extraction_supplement",
        "memory_candidate",
        "classification",
    }
    return deferred


def test_every_deferred_item_offers_only_restore(tmp_path: Path) -> None:
    client, factory = _client(tmp_path)
    csrf = _login(client)

    deferred = _one_of_each(client, factory, csrf)

    assert {(i["type"], i.get("kind")): i["actions"] for i in deferred} == {
        (i["type"], i.get("kind")): ["restore"] for i in deferred
    }
    assert all(i["detail_actions"] == [] for i in deferred)


def test_approving_or_rejecting_a_deferred_item_is_refused_alike(tmp_path: Path) -> None:
    client, factory = _client(tmp_path)
    csrf = _login(client)
    deferred = _one_of_each(client, factory, csrf)

    for index, item in enumerate(deferred):
        for action in ("approve", "reject"):
            response = _act(client, csrf, item, action, f"uniform-{action}-{index}")
            assert response.status_code == 409, (item["type"], action, response.text)
            assert response.json()["detail"] == f"{action} is not available for this item"

    for action in ("approve", "reject"):
        batch = client.post(
            "/v1/review/batch",
            json={
                "action": action,
                "items": [{"type": i["type"], "id": i["id"], "etag": i["etag"]} for i in deferred],
            },
            headers=_headers(csrf, f"uniform-batch-{action}"),
        )
        assert batch.status_code == 200, batch.text
        results = batch.json()["result"]["items"]
        assert {(r["status"], r["reason"]) for r in results} == {("failed", "action_unavailable")}

    assert {i["id"] for i in _items(client, deferred="only", limit=500)} == {
        i["id"] for i in deferred
    }


CONCLUSIONS = "conclusion_draft,relation_suggestion"


def _conclusion_counts(client: TestClient) -> tuple[int, int]:
    by_type = client.get("/v1/review/counts").json()["by_type"]
    return by_type["conclusion_draft"], by_type["relation_suggestion"]


def test_deferring_a_draft_sets_its_relation_suggestions_aside_too(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    csrf = _login(client)
    relation = _relation(client, csrf, "aside")
    draft = next(i for i in _items(client) if i["id"] == relation["left_id"])

    assert _act(client, csrf, draft, "defer", "aside-defer").status_code == 200

    assert _items(client, type=CONCLUSIONS) == []
    assert _conclusion_counts(client) == (0, 0)
    hidden = next(i for i in _items(client, deferred="only") if i["id"] == relation["id"])
    assert hidden["deferred"] is True
    assert hidden["actions"] == ["restore"]

    deferred_draft = next(i for i in _items(client, deferred="only") if i["id"] == draft["id"])
    assert _act(client, csrf, deferred_draft, "restore", "aside-restore").status_code == 200

    back = {i["id"]: i for i in _items(client, type=CONCLUSIONS)}
    assert set(back) == {draft["id"], relation["id"]}
    assert back[relation["id"]]["actions"] == ["approve", "reject", "defer"]
    assert _conclusion_counts(client) == (1, 1)


def test_restoring_a_relation_set_aside_with_its_draft_restores_the_draft(
    tmp_path: Path,
) -> None:
    client, _ = _client(tmp_path)
    csrf = _login(client)
    relation = _relation(client, csrf, "aside-relation")
    draft = next(i for i in _items(client) if i["id"] == relation["left_id"])
    assert _act(client, csrf, draft, "defer", "aside-relation-defer").status_code == 200
    hidden = next(i for i in _items(client, deferred="only") if i["id"] == relation["id"])

    restored = _act(client, csrf, hidden, "restore", "aside-relation-restore")

    assert restored.status_code == 200, restored.text
    assert {i["id"] for i in _items(client, type=CONCLUSIONS)} == {draft["id"], relation["id"]}
    assert _items(client, type=CONCLUSIONS, deferred="only") == []
