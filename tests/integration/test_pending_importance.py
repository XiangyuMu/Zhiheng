"""待处理 lists the most important items first (#112, spec decision 22)."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from tests.integration.test_memory_api import _client, _headers, _login
from tests.integration.test_pending_inbox import (
    _act,
    _conflict,
    _draft,
    _items,
    _missing_prompt,
    _set_created,
)
from zhiheng.db.session import session_scope


def _relation_to_formal(
    client: TestClient, csrf: str, key: str, formal_claim: str, draft_claim: str
) -> dict[str, Any]:
    formal = _draft(client, csrf, key + "-formal", formal_claim)
    approved = client.post(
        f"/v1/conclusions/{formal['id']}/approve",
        json={},
        headers=_headers(csrf, key + "-approve", formal["etag"]),
    )
    assert approved.status_code == 200, approved.text
    draft = _draft(client, csrf, key + "-draft", draft_claim)
    relations = client.get(f"/v1/conclusions/{draft['id']}/relations").json()["items"]
    assert len(relations) == 1
    return {"formal": formal, "draft": draft, "relation": dict(relations[0])}


def _suspend(factory: sessionmaker[Session], entry_id: str) -> None:
    with session_scope(factory) as session:
        session.execute(
            text(
                "INSERT INTO conclusion_applicability (entry_id, version, state, reason, "
                "evidence_json) SELECT id, approved_version, 'suspended', 'validity_expired', "
                "'{}' FROM conclusion_entries WHERE id = :id"
            ),
            {"id": entry_id},
        )


def test_default_order_puts_current_task_and_formal_knowledge_first_then_conflicts_then_drafts(
    tmp_path: Path,
) -> None:
    client, factory = _client(tmp_path)
    csrf = _login(client)
    supplement = _relation_to_formal(
        client, csrf, "imp-supplement", "每天复习能提高记忆", "每天复习能提高长期记忆"
    )
    contradiction = _relation_to_formal(
        client, csrf, "imp-conflict", "早睡能改善专注", "早睡不能改善专注"
    )
    paused = _relation_to_formal(
        client, csrf, "imp-paused", "番茄钟适合写作", "番茄钟适合长时间写作"
    )
    _suspend(factory, paused["formal"]["id"])
    personal = _conflict(client, csrf, "imp-personal")
    prompt = _missing_prompt(client, csrf, "imp-prompt")
    # Newest first in time, so only the importance order can explain the result.
    times = {
        ("conclusion_entries", supplement["draft"]["id"]): "2026-01-01 08:00:00",
        ("conclusion_entries", contradiction["draft"]["id"]): "2026-01-02 08:00:00",
        ("conclusion_entries", paused["draft"]["id"]): "2026-01-03 08:00:00",
        ("memory_conflicts", personal["conflict_id"]): "2026-02-01 08:00:00",
        ("conclusion_relations", paused["relation"]["id"]): "2026-03-01 08:00:00",
        ("conclusion_relations", contradiction["relation"]["id"]): "2026-04-01 08:00:00",
        ("conclusion_relations", supplement["relation"]["id"]): "2026-05-01 08:00:00",
        ("personal_prompts", prompt["id"]): "2026-06-01 08:00:00",
    }
    for (table, item_id), when in times.items():
        _set_created(factory, table, item_id, when)

    listed = _items(client)

    # Approving each formal conclusion also left its knowledge unclassified.
    assert [(item["type"], item["importance"]) for item in listed[-3:]] == [
        ("classification", "ordinary")
    ] * 3
    assert [(item["id"], item["importance"]) for item in listed[:-3]] == [
        (supplement["relation"]["id"], "affects_knowledge"),
        (prompt["id"], "affects_knowledge"),
        (personal["conflict_id"], "conflict_or_review"),
        (paused["relation"]["id"], "conflict_or_review"),
        (contradiction["relation"]["id"], "conflict_or_review"),
        (supplement["draft"]["id"], "ordinary"),
        (contradiction["draft"]["id"], "ordinary"),
        (paused["draft"]["id"], "ordinary"),
    ]
    oldest = [item["id"] for item in _items(client, order="oldest")][:-3]
    assert oldest == [item_id for (_, item_id), _ in sorted(times.items(), key=lambda t: t[1])]
    assert [item["id"] for item in _items(client, order="newest")][3:] == oldest[::-1]


def _daily(client: TestClient) -> dict[str, Any]:
    response = client.get("/v1/review/daily")
    assert response.status_code == 200, response.text
    return dict(response.json())


def _set_daily(client: TestClient, csrf: str, amount: int, key: str, etag: str) -> Any:
    return client.put(
        "/v1/review/daily", json={"suggested": amount}, headers=_headers(csrf, key, etag)
    )


def test_daily_suggested_amount_defaults_to_ten_and_zero_turns_it_off(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    csrf = _login(client)

    default = _daily(client)
    assert (default["suggested"], default["handled_today"], default["reached"]) == (10, 0, False)

    changed = _set_daily(client, csrf, 3, "daily-three", default["etag"])
    assert changed.status_code == 200, changed.text
    assert _daily(client)["suggested"] == 3
    stale = _set_daily(client, csrf, 5, "daily-stale", default["etag"])
    assert stale.status_code == 412
    retried = _set_daily(client, csrf, 3, "daily-retry", default["etag"])
    assert retried.status_code == 200, retried.text

    off = _set_daily(client, csrf, 0, "daily-off", _daily(client)["etag"])
    assert off.status_code == 200, off.text
    assert (off.json()["suggested"], off.json()["reached"]) == (0, False)
    assert _set_daily(client, csrf, -1, "daily-negative", "*").status_code == 422


def test_reaching_the_daily_amount_leaves_unhandled_items_as_they_were_the_next_day(
    tmp_path: Path,
) -> None:
    client, _ = _client(tmp_path)
    csrf = _login(client)
    day = {"today": date(2026, 11, 6)}
    assert isinstance(client.app, FastAPI)
    client.app.state.pending_daily.today = lambda: day["today"]
    assert _set_daily(client, csrf, 2, "reach-two", _daily(client)["etag"]).status_code == 200
    first = _draft(client, csrf, "reach-first", "固定条件下复习有效")
    second = _draft(client, csrf, "reach-second", "间隔练习能改善写作")
    third = _draft(client, csrf, "reach-third", "晨跑让人更清醒")
    by_id = {item["id"]: item for item in _items(client)}

    assert _act(client, csrf, by_id[first["id"]], "defer", "reach-defer").status_code == 200
    deferred = next(i for i in _items(client, deferred="only") if i["id"] == first["id"])
    assert _act(client, csrf, deferred, "restore", "reach-restore").status_code == 200
    assert _daily(client)["handled_today"] == 1  # restoring is not handling
    rejected = client.post(
        "/v1/review/batch",
        json={
            "action": "reject",
            "items": [{"type": "conclusion_draft", "id": second["id"], "etag": second["etag"]}],
        },
        headers=_headers(csrf, "reach-batch"),
    )
    assert rejected.status_code == 200, rejected.text

    reached = _daily(client)
    assert (reached["handled_today"], reached["reached"]) == (2, True)
    left = {item["id"]: item for item in _items(client)}
    assert set(left) == {first["id"], third["id"]}
    assert left[third["id"]] == by_id[third["id"]]
    assert client.get("/v1/review/counts").json()["total"] == 2

    day["today"] = date(2026, 11, 7)

    tomorrow = _daily(client)
    assert (tomorrow["handled_today"], tomorrow["reached"]) == (0, False)
    assert {item["id"]: item for item in _items(client)}[third["id"]] == by_id[third["id"]]


def _report(client: TestClient, csrf: str, item: dict[str, Any], key: str) -> Any:
    return client.post(
        "/v1/review/daily/handled",
        json={"type": item["type"], "id": item["id"], "etag": item["etag"]},
        headers=_headers(csrf, key),
    )


def test_a_decision_made_in_the_detail_view_counts_once_it_changed_the_item(
    tmp_path: Path,
) -> None:
    client, _ = _client(tmp_path)
    csrf = _login(client)
    conflict = _conflict(client, csrf, "detail-conflict")
    item = next(i for i in _items(client) if i["id"] == conflict["conflict_id"])

    unchanged = _report(client, csrf, item, "detail-too-early")
    assert unchanged.status_code == 409, unchanged.text
    assert _daily(client)["handled_today"] == 0

    decided = client.post(
        f"/v1/personal-updates/conflicts/{item['id']}/decision",
        json={"decision": "confirm"},
        headers=_headers(csrf, "detail-confirm", item["detail"]["candidate_etag"]),
    )
    assert decided.status_code == 200, decided.text
    assert _report(client, csrf, item, "detail-reported").status_code == 200
    assert _report(client, csrf, item, "detail-again").status_code == 200
    assert _daily(client)["handled_today"] == 1
