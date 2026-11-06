"""Idle-time background consolidation (#113, spec decision 23, scenarios 22/27/31).

The knowledge base is built through the real API; the worker's consolidation
step is then run with a clock, as the background worker would run it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from tests.integration.test_memory_api import _client, _headers, _login
from tests.integration.test_pending_inbox import _draft, _items, _set_created
from zhiheng.api.memory import (
    _complete_operation_receipt,
    _insert_operation_receipt,
    _link_operation_receipt_sources,
)
from zhiheng.consolidation import run_consolidation_if_due
from zhiheng.db.session import session_scope
from zhiheng.query.conversations import ConversationRepository

IDLE = timedelta(minutes=31)


def _formal(
    client: TestClient,
    csrf: str,
    key: str,
    claim: str,
    premises: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    draft = (
        _draft(client, csrf, key, claim)
        if premises is None
        else _draft_with(client, csrf, key, claim, premises)
    )
    approved = client.post(
        f"/v1/conclusions/{draft['id']}/approve",
        json={},
        headers=_headers(csrf, key + "-approve", draft["etag"]),
    )
    assert approved.status_code == 200, approved.text
    return dict(approved.json())


def _draft_with(
    client: TestClient, csrf: str, key: str, claim: str, premises: list[dict[str, Any]]
) -> dict[str, Any]:
    source = client.post(
        "/v1/conclusions/sources",
        json={"text": f"讨论原文：{claim}。"},
        headers=_headers(csrf, key + "-source"),
    )
    assert source.status_code == 200, source.text
    response = client.post(
        "/v1/conclusions",
        json={
            "source_id": source.json()["id"],
            "title": key,
            "claim": claim,
            "domain_id": "education_learning",
            "premises": premises,
            "excerpt": claim,
        },
        headers=_headers(csrf, key + "-draft"),
    )
    assert response.status_code == 200, response.text
    return dict(response.json())


def _consolidate(factory: sessionmaker[Session], at: datetime | None = None) -> Any:
    return run_consolidation_if_due(factory, now=at or datetime.now(UTC) + IDLE)


def _consolidation_items(client: TestClient) -> list[dict[str, Any]]:
    return [
        item
        for item in _items(client, type="relation_suggestion", deferred="include")
        if item["detail"].get("origin") == "consolidation"
    ]


def _context(client: TestClient, query: str) -> list[str]:
    response = client.get("/v1/conclusions/context", params={"query": query})
    assert response.status_code == 200, response.text
    return [str(item["id"]) for item in response.json()["items"]]


def test_duplicate_formal_conclusions_become_a_merge_suggestion_without_changing_them(
    tmp_path: Path,
) -> None:
    client, factory = _client(tmp_path)
    csrf = _login(client)
    older = _formal(client, csrf, "dup-old", "间隔复习能提高长期记忆")
    newer = _formal(client, csrf, "dup-new", "间隔复习能提高长期记忆")

    assert _consolidate(factory) is not None

    [suggestion] = _consolidation_items(client)
    assert suggestion["detail"]["kind"] == "duplicate"
    assert {suggestion["detail"]["left_id"], suggestion["detail"]["right_id"]} == {
        older["id"],
        newer["id"],
    }
    assert "approve" in suggestion["actions"]
    # Nothing changes before the user approves.
    assert sorted(_context(client, "间隔复习")) == sorted([older["id"], newer["id"]])


def _approve(client: TestClient, csrf: str, item: dict[str, Any], key: str) -> Any:
    return client.post(
        f"/v1/review/items/{item['type']}/{item['id']}/approve",
        json={},
        headers=_headers(csrf, key, item["etag"]),
    )


def test_conflict_under_the_same_premises_becomes_a_pending_conflict(tmp_path: Path) -> None:
    client, factory = _client(tmp_path)
    csrf = _login(client)
    older = _formal(client, csrf, "conf-old", "早睡能改善专注")
    newer = _formal(client, csrf, "conf-new", "早睡不能改善专注")
    before = client.get("/v1/review/counts").json()["total"]

    _consolidate(factory)

    [suggestion] = _consolidation_items(client)
    assert suggestion["detail"]["kind"] == "conflict"
    assert suggestion["importance"] == "conflict_or_review"
    assert client.get("/v1/review/counts").json()["total"] == before + 1
    # Until the user decides, both conclusions still serve answers.
    assert sorted(_context(client, "早睡")) == sorted([older["id"], newer["id"]])


def test_approving_a_merge_retires_only_the_newer_duplicate(tmp_path: Path) -> None:
    client, factory = _client(tmp_path)
    csrf = _login(client)
    older = _formal(client, csrf, "merge-old", "间隔复习能提高长期记忆")
    newer = _formal(client, csrf, "merge-new", "间隔复习能提高长期记忆")
    _consolidate(factory)
    [suggestion] = _consolidation_items(client)

    approved = _approve(client, csrf, suggestion, "merge-approve")

    assert approved.status_code == 200, approved.text
    assert _context(client, "间隔复习") == [older["id"]]
    assert client.get(f"/v1/conclusions/{newer['id']}").json()["status"] == "merged"
    assert _consolidation_items(client) == []


def test_runs_only_after_thirty_idle_minutes_and_at_most_once_a_day(tmp_path: Path) -> None:
    client, factory = _client(tmp_path)
    csrf = _login(client)
    _formal(client, csrf, "idle-old", "间隔复习能提高长期记忆")
    _formal(client, csrf, "idle-new", "间隔复习能提高长期记忆")
    active = datetime.now(UTC)

    # Approving was review activity, so nothing runs within 30 minutes of it.
    assert _consolidate(factory, active + timedelta(minutes=29)) is None
    assert _consolidation_items(client) == []

    first = _consolidate(factory, active + IDLE)
    assert first is not None and first.suggestions_created == 1
    # Within 24 hours of a run there is no second one, even when idle.
    assert _consolidate(factory, active + IDLE + timedelta(hours=23)) is None


def test_asking_a_question_is_activity_that_postpones_the_run(tmp_path: Path) -> None:
    client, factory = _client(tmp_path)
    csrf = _login(client)
    _formal(client, csrf, "ask-old", "间隔复习能提高长期记忆")
    _formal(client, csrf, "ask-new", "间隔复习能提高长期记忆")
    created = client.post("/v1/conversations", json={}, headers=_headers(csrf, "ask-conversation"))
    assert created.status_code < 500, created.text
    later = datetime.now(UTC) + timedelta(minutes=20)

    assert _consolidate(factory, later) is None


def test_repeated_runs_add_nothing_and_a_rejected_suggestion_stays_rejected(
    tmp_path: Path,
) -> None:
    client, factory = _client(tmp_path)
    csrf = _login(client)
    _formal(client, csrf, "again-old", "间隔复习能提高长期记忆")
    _formal(client, csrf, "again-new", "间隔复习能提高长期记忆")
    start = datetime.now(UTC) + IDLE
    _consolidate(factory, start)
    [suggestion] = _consolidation_items(client)

    second = _consolidate(factory, start + timedelta(days=1))
    assert second is not None and second.suggestions_created == 0
    assert [item["id"] for item in _consolidation_items(client)] == [suggestion["id"]]

    rejected = client.post(
        f"/v1/review/items/relation_suggestion/{suggestion['id']}/reject",
        json={},
        headers=_headers(csrf, "again-reject", suggestion["etag"]),
    )
    assert rejected.status_code == 200, rejected.text
    third = _consolidate(factory, start + timedelta(days=2, minutes=31))
    assert third is not None and third.suggestions_created == 0
    assert _consolidation_items(client) == []


def _findings(client: TestClient, **params: Any) -> list[dict[str, Any]]:
    response = client.get("/v1/consolidation/findings", params=params)
    assert response.status_code == 200, response.text
    return list(response.json()["items"])


def _kinds(findings: list[dict[str, Any]], subject_id: str) -> set[str]:
    return {item["kind"] for item in findings if item["subject_id"] == subject_id}


def _cite(factory: sessionmaker[Session], knowledge_id: str, when: str) -> None:
    with session_scope(factory) as session:
        owner = str(session.execute(text("SELECT id FROM auth_users")).scalar_one())
        conversations = ConversationRepository()
        conversation = conversations.create(session, owner_user_id=owner)
        history_id = conversations.append(
            session,
            conversation_id=conversation["id"],
            owner_user_id=owner,
            query="复习怎么安排？",
            response={
                "answer": "按间隔复习。",
                "citations": [{"source_type": "knowledge_object", "source_id": knowledge_id}],
                "route": {"route": "knowledge"},
                "stop_reason": "completed",
            },
        )
    _set_created(factory, "answer_history", history_id, when)


CONFIRMED = [{"text": "每天学习新内容", "confirmed": True}]


def test_observations_become_findings_that_are_not_pending_and_change_nothing(
    tmp_path: Path,
) -> None:
    client, factory = _client(tmp_path)
    csrf = _login(client)
    bare = _formal(client, csrf, "find-bare", "费曼学习法有助于理解概念", premises=[])
    assumed = _formal(client, csrf, "find-assumed", "番茄钟适合长时间写作")
    cited = _formal(client, csrf, "find-cited", "睡前回顾能巩固记忆", premises=CONFIRMED)
    _goal(client, csrf, "find-goal", "三个月内通过考试")
    later = datetime.now(UTC) + timedelta(days=181)
    _cite(
        factory, cited["knowledge_id"], (later - timedelta(days=10)).strftime("%Y-%m-%d %H:%M:%S")
    )
    pending_before = client.get("/v1/review/counts").json()
    answerable_before = sorted(_context(client, ""))

    run = _consolidate(factory, later)

    findings = _findings(client)
    assert run is not None and run.findings_created == len(findings)
    assert _kinds(findings, bare["id"]) == {"missing_premise", "isolated", "unused"}
    assert _kinds(findings, assumed["id"]) == {"unconfirmed_assumption", "isolated", "unused"}
    # Cited ten days ago: not unused, though still unrelated to anything.
    assert _kinds(findings, cited["id"]) == {"isolated"}
    [stale] = [item for item in findings if item["subject_type"] == "memory"]
    assert stale["kind"] == "stale_volatile"
    assert stale["detail"]["state_key"] == "goal.current"
    # Findings are not pending, and long-unused knowledge still serves answers.
    assert client.get("/v1/review/counts").json() == pending_before
    assert sorted(_context(client, "")) == answerable_before


def _goal(client: TestClient, csrf: str, key: str, value: str) -> None:
    response = client.post(
        "/v1/memory/formal",
        json={
            "memory_type": "fact",
            "state_key": "goal.current",
            "value": {"text": value},
            "time_sensitivity": "current",
        },
        headers=_headers(csrf, key),
    )
    assert response.status_code == 200, response.text


def test_a_dismissed_finding_stays_dismissed_until_its_subject_has_a_new_version(
    tmp_path: Path,
) -> None:
    client, factory = _client(tmp_path)
    csrf = _login(client)
    _goal(client, csrf, "dismiss-goal", "三个月内通过考试")
    start = datetime.now(UTC) + timedelta(days=91)
    _consolidate(factory, start)
    [stale] = _findings(client)
    pending_before = client.get("/v1/review/counts").json()
    memory_before = client.get("/v1/memory/formal").json()

    dismissed = client.post(
        f"/v1/consolidation/findings/{stale['id']}/dismiss",
        json={},
        headers=_headers(csrf, "dismiss-it"),
    )

    assert dismissed.status_code == 200, dismissed.text
    assert _findings(client) == []
    # Dismissing is not deferring: nothing enters or leaves 待处理, no memory changes.
    assert client.get("/v1/review/counts").json() == pending_before
    assert _items(client, deferred="only") == []
    assert client.get("/v1/memory/formal").json() == memory_before
    assert _consolidate(factory, start + timedelta(days=1)) is not None
    assert _findings(client) == []

    _goal(client, csrf, "dismiss-goal-v2", "半年内通过考试")
    assert _consolidate(factory, start + timedelta(days=93)) is not None
    [again] = _findings(client)
    assert again["kind"] == "stale_volatile" and again["id"] != stale["id"]
    assert again["subject_id"] == stale["subject_id"]


def test_a_conclusion_whose_source_text_is_empty_lacks_a_source(tmp_path: Path) -> None:
    client, factory = _client(tmp_path)
    csrf = _login(client)
    entry = _formal(client, csrf, "nosource", "睡前回顾能巩固记忆", premises=CONFIRMED)
    with session_scope(factory) as session:
        session.execute(
            text(
                "UPDATE conclusion_sources SET body = '' WHERE id = "
                "(SELECT source_id FROM conclusion_entries WHERE id = :id)"
            ),
            {"id": entry["id"]},
        )

    _consolidate(factory)

    assert "missing_source" in _kinds(_findings(client), entry["id"])


def _answer_without_conversation(factory: sessionmaker[Session], knowledge_id: str) -> None:
    """An answer outside any conversation leaves only its receipt and source links."""
    with session_scope(factory) as session:
        receipt = _insert_operation_receipt(session, "api:answers:solo", "answer_question", "h")
        _complete_operation_receipt(session, receipt, status_value="completed", result={})
        _link_operation_receipt_sources(session, receipt, [("knowledge_object", knowledge_id)])
        session.execute(
            text("UPDATE memory_operation_receipts SET created_at = :at WHERE id = :id"),
            {"at": (datetime.now(UTC) + timedelta(days=171)).isoformat(), "id": receipt},
        )


def test_knowledge_cited_by_an_answer_outside_a_conversation_is_not_unused(
    tmp_path: Path,
) -> None:
    client, factory = _client(tmp_path)
    csrf = _login(client)
    entry = _formal(client, csrf, "solo-cited", "睡前回顾能巩固记忆", premises=CONFIRMED)
    _answer_without_conversation(factory, entry["knowledge_id"])

    _consolidate(factory, datetime.now(UTC) + timedelta(days=181))

    assert "unused" not in _kinds(_findings(client), entry["id"])
