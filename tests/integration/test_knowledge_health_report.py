"""Weekly knowledge health report and change summary (#115, spec decision 24, scenarios 23/28).

The knowledge base is built through the real API, with timestamps moved back
where a scenario needs history; the worker's consolidation step is then run
with a clock, as the background worker would run it, and the report is read
back through the API.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from tests.integration.test_background_consolidation import CONFIRMED, IDLE, _formal, _goal
from tests.integration.test_memory_api import _client, _headers, _login
from tests.integration.test_pending_importance import _relation_to_formal
from tests.integration.test_pending_inbox import _act, _conflict, _draft, _items, _set_created
from zhiheng.conclusions.applicability import ConclusionApplicabilityService
from zhiheng.consolidation import run_consolidation_if_due
from zhiheng.consolidation.findings import iso_utc
from zhiheng.db.session import session_scope


def _sql_time(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")


def _approved_at(factory: sessionmaker[Session], entry_id: str, when: datetime) -> None:
    with session_scope(factory) as session:
        session.execute(
            text("UPDATE conclusion_versions SET approved_at = :at WHERE entry_id = :id"),
            {"at": _sql_time(when), "id": entry_id},
        )


def _latest(client: TestClient) -> dict[str, Any]:
    response = client.get("/v1/knowledge-health/reports/latest")
    assert response.status_code == 200, response.text
    return dict(response.json())


def test_a_run_publishes_a_report_listing_knowledge_added_in_the_last_seven_days(
    tmp_path: Path,
) -> None:
    client, factory = _client(tmp_path)
    csrf = _login(client)
    now = datetime.now(UTC) + IDLE
    recent = _formal(client, csrf, "health-recent", "番茄钟有助于专注")
    older = _formal(client, csrf, "health-older", "复述能加深理解")
    _approved_at(factory, older["id"], now - timedelta(days=9))

    run_consolidation_if_due(factory, now=now)

    report = _latest(client)
    assert report["period_start"] == iso_utc(now - timedelta(days=7))
    assert report["period_end"] == iso_utc(now)
    added = report["changes"]["knowledge"]["added"]
    assert [item["id"] for item in added] == [recent["id"]]
    assert added[0]["title"] == "health-recent"
    assert added[0]["href"] == f"/knowledge-agent?knowledge={recent['knowledge_id']}#library"


def _approve_relation(client: TestClient, csrf: str, relation_id: str, key: str) -> None:
    item = next(i for i in _items(client, type="relation_suggestion") if i["id"] == relation_id)
    response = _act(client, csrf, item, "approve", key)
    assert response.status_code == 200, response.text


def test_the_change_summary_lists_revisions_merges_and_pauses_in_the_period(
    tmp_path: Path,
) -> None:
    client, factory = _client(tmp_path)
    csrf = _login(client)
    merge = _relation_to_formal(
        client, csrf, "health-merge", "间隔复习能提高长期记忆", "间隔复习能提高长期记忆"
    )
    assert merge["relation"]["kind"] == "duplicate"
    _approve_relation(client, csrf, merge["relation"]["id"], "health-merge-approve")
    revise = _relation_to_formal(
        client,
        csrf,
        "health-revise",
        "每天阅读三十分钟能扩大词汇量",
        "每天阅读四十分钟能扩大词汇量",
    )
    assert revise["relation"]["kind"] == "revision"
    _approve_relation(client, csrf, revise["relation"]["id"], "health-revise-approve")
    paused = _formal(client, csrf, "health-paused", "旧版教材适合当前课程")
    with session_scope(factory) as session:
        session.execute(
            text(
                "UPDATE conclusion_versions SET payload_json = json_set(payload_json, "
                "'$.valid_until', '2000-01-01T00:00:00Z') WHERE entry_id = :id"
            ),
            {"id": paused["id"]},
        )
        assert ConclusionApplicabilityService().sweep(session) == 1

    run_consolidation_if_due(factory, now=datetime.now(UTC) + IDLE)

    knowledge = _latest(client)["changes"]["knowledge"]
    assert [i["id"] for i in knowledge["merged"]] == [merge["draft"]["id"]]
    assert knowledge["merged"][0]["into"]["id"] == merge["formal"]["id"]
    assert [i["id"] for i in knowledge["revised"]] == [revise["draft"]["id"]]
    assert knowledge["revised"][0]["revises"]["id"] == revise["formal"]["id"]
    assert [i["id"] for i in knowledge["paused"]] == [paused["id"]]
    # A merged duplicate or an approved revision is not new knowledge of its own.
    assert [i["id"] for i in knowledge["added"]] == [
        merge["formal"]["id"],
        revise["formal"]["id"],
        paused["id"],
    ]


def _backdate_memory(factory: sessionmaker[Session], memory_id: str, when: datetime) -> None:
    """Move a memory's versions back in time, past the append-only guard."""
    with session_scope(factory) as session:
        guards = session.execute(
            text(
                "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' "
                "AND tbl_name = 'formal_memory_versions'"
            )
        ).all()
        for name, _sql in guards:
            session.execute(text(f'DROP TRIGGER "{name}"'))
        session.execute(
            text("UPDATE formal_memory_versions SET created_at = :at WHERE formal_memory_id = :id"),
            {"at": _sql_time(when), "id": memory_id},
        )
        for _name, sql in guards:
            session.execute(text(sql))


def _remember(client: TestClient, csrf: str, state_key: str, value: str) -> dict[str, Any]:
    response = client.post(
        "/v1/memory/formal",
        json={
            "memory_type": "profile",
            "state_key": state_key,
            "value": {"text": value},
            "sensitivity_level": "private",
            "confidence": 1.0,
        },
        headers=_headers(csrf, f"remember-{state_key}"),
    )
    assert response.status_code in (200, 201), response.text
    items = client.get("/v1/memory/formal").json()["items"]
    return dict(next(item for item in items if item["state_key"] == state_key))


def test_the_change_summary_lists_personal_information_and_the_pending_overview(
    tmp_path: Path,
) -> None:
    client, factory = _client(tmp_path)
    csrf = _login(client)
    now = datetime.now(UTC) + IDLE
    remembered = _remember(client, csrf, "profile.city", "上海")
    earlier = _remember(client, csrf, "profile.role", "研究生")
    _backdate_memory(factory, earlier["id"], now - timedelta(days=20))
    patched = client.patch(
        f"/v1/memory/items/{earlier['id']}",
        json={"value": {"text": "博士生"}, "change_reason": "升学"},
        headers=_headers(csrf, "role-patch", earlier["etag"]),
    )
    assert patched.status_code == 200, patched.text
    _draft(client, csrf, "health-waiting")
    old = _draft(client, csrf, "health-old-waiting")
    _set_created(factory, "conclusion_entries", old["id"], _sql_time(now - timedelta(days=30)))
    deferred = _draft(client, csrf, "health-deferred")
    item = next(i for i in _items(client) if i["id"] == deferred["id"])
    assert _act(client, csrf, item, "defer", "health-defer").status_code == 200
    rejected = _draft(client, csrf, "health-rejected")
    item = next(i for i in _items(client) if i["id"] == rejected["id"])
    assert _act(client, csrf, item, "reject", "health-reject").status_code == 200

    run_consolidation_if_due(factory, now=now)

    changes = _latest(client)["changes"]
    memory = changes["memory"]
    assert [i["id"] for i in memory["remembered"]] == [remembered["id"]]
    assert [i["id"] for i in memory["updated"]] == [earlier["id"]]
    assert memory["remembered"][0]["title"] == "profile.city"
    pending = changes["pending"]
    assert pending["open"] == 2
    assert pending["deferred"] == 1
    assert pending["by_type"]["conclusion_draft"] == 2
    # Became pending in the period: the waiting, the deferred and the rejected draft.
    assert pending["entered"] == 3


def _metric(report: dict[str, Any], name: str) -> Any:
    return report["metrics"][name]["value"]


def test_health_metrics_match_the_constructed_knowledge_base(tmp_path: Path) -> None:
    client, factory = _client(tmp_path)
    csrf = _login(client)
    _formal(client, csrf, "metric-early", "早睡能改善专注", premises=CONFIRMED)
    _formal(client, csrf, "metric-early-not", "早睡不能改善专注", premises=CONFIRMED)
    _formal(client, csrf, "metric-dup-a", "间隔复习能提高长期记忆")
    _formal(client, csrf, "metric-dup-b", "间隔复习能提高长期记忆")
    _formal(client, csrf, "metric-bare", "费曼学习法有助于理解概念", premises=[])
    _conflict(client, csrf, "metric-personal")
    _goal(client, csrf, "metric-goal", "三个月内通过考试")

    run_consolidation_if_due(factory, now=datetime.now(UTC) + timedelta(days=91))

    report = _latest(client)
    # One merge suggestion between the two duplicates.
    assert _metric(report, "duplicate_suggestions") == 1
    # Four of the five formal conclusions state a premise.
    assert _metric(report, "explicit_premise_share") == 0.8
    assert report["metrics"]["explicit_premise_share"]["numerator"] == 4
    assert report["metrics"]["explicit_premise_share"]["denominator"] == 5
    # The suggested conflict between formal conclusions, and the personal one.
    assert _metric(report, "unresolved_conflicts") == 2
    # The duplicates rest on an unconfirmed premise.
    assert _metric(report, "unconfirmed_assumption_conclusions") == 2
    # No conclusion has an approved relation or a related domain yet.
    assert _metric(report, "isolated_entries") == 5
    # The goal has gone unconfirmed for 91 days.
    assert _metric(report, "stale_volatile_memories") == 1


def test_the_share_handled_within_seven_days_counts_decisions_not_deferrals(
    tmp_path: Path,
) -> None:
    client, factory = _client(tmp_path)
    csrf = _login(client)
    real_now = datetime.now(UTC)

    def pending(key: str, days_ago: int) -> dict[str, Any]:
        draft = _draft(client, csrf, key)
        when = _sql_time(real_now - timedelta(days=days_ago))
        _set_created(factory, "conclusion_entries", draft["id"], when)
        return next(i for i in _items(client, deferred="include") if i["id"] == draft["id"])

    approved = pending("ratio-approved", 0)
    in_detail = pending("ratio-detail", 3)
    waiting = pending("ratio-waiting", 3)
    deferred = pending("ratio-deferred", 5)
    too_old = pending("ratio-too-old", 20)
    assert _act(client, csrf, approved, "approve", "ratio-approve").status_code == 200
    rejected = client.post(
        f"/v1/conclusions/{in_detail['id']}/reject",
        json={},
        headers=_headers(csrf, "ratio-reject", in_detail["etag"]),
    )
    assert rejected.status_code == 200, rejected.text
    reported = client.post(
        "/v1/review/daily/handled",
        json={"type": in_detail["type"], "id": in_detail["id"], "etag": in_detail["etag"]},
        headers=_headers(csrf, "ratio-report"),
    )
    assert reported.status_code == 200, reported.text
    assert _act(client, csrf, deferred, "defer", "ratio-defer").status_code == 200
    assert _act(client, csrf, too_old, "reject", "ratio-reject-old").status_code == 200
    # Approving the draft made its knowledge pending to classify, since today.
    [classify] = _items(client, type="classification")
    assert {waiting["id"], deferred["id"], classify["id"]} == {
        i["id"] for i in _items(client, deferred="include")
    }

    # The report eight days on looks at what became pending 8 to 14 days before
    # it ends, so each item has had its full 7 days.
    run_consolidation_if_due(factory, now=real_now + timedelta(days=8))

    share = _latest(client)["metrics"]["handled_within_7_days_share"]
    # Approved, and decided in the detail view, of those two plus the waiting,
    # the deferred and the classification item.
    assert (share["numerator"], share["denominator"], share["value"]) == (2, 5, 0.4)


def _snapshot(factory: sessionmaker[Session], report_id: str) -> Any:
    with session_scope(factory) as session:
        return session.execute(
            text("SELECT * FROM knowledge_health_reports WHERE id = :id"), {"id": report_id}
        ).one()


def _trend(report: dict[str, Any], name: str) -> tuple[Any, Any, Any]:
    metric = report["metrics"][name]
    return metric["value"], metric["previous"], metric["change"]


def test_a_weekly_report_shows_the_change_from_the_last_and_keeps_every_snapshot(
    tmp_path: Path,
) -> None:
    client, factory = _client(tmp_path)
    csrf = _login(client)
    _formal(client, csrf, "trend-first", "番茄钟有助于专注", premises=CONFIRMED)
    first_at = datetime.now(UTC) + IDLE
    run_consolidation_if_due(factory, now=first_at)
    first = _latest(client)
    assert first["previous_report_id"] is None
    assert _trend(first, "isolated_entries") == (1, None, None)
    stored = _snapshot(factory, first["id"])

    _formal(client, csrf, "trend-second", "费曼学习法有助于理解概念", premises=[])
    # Consolidation runs daily, but a report comes once a week.
    run_consolidation_if_due(factory, now=first_at + timedelta(days=1))
    assert _latest(client)["id"] == first["id"]
    # Idle days can push a report later; it then covers everything since the last.
    run_consolidation_if_due(factory, now=first_at + timedelta(days=8))

    second = _latest(client)
    assert second["id"] != first["id"]
    assert second["previous_report_id"] == first["id"]
    assert second["period_start"] == first["period_end"]
    assert second["period_end"] == iso_utc(first_at + timedelta(days=8))
    assert _trend(second, "isolated_entries") == (2, 1, 1)
    assert _trend(second, "explicit_premise_share") == (0.5, 1.0, -0.5)
    listed = client.get("/v1/knowledge-health/reports").json()["items"]
    assert [item["id"] for item in listed] == [second["id"], first["id"]]
    # The earlier snapshot reads as it did, and cannot be changed or removed.
    assert client.get(f"/v1/knowledge-health/reports/{first['id']}").json() == first
    assert _snapshot(factory, first["id"]) == stored
    for statement in (
        "UPDATE knowledge_health_reports SET report_json = '{}' WHERE id = :id",
        "DELETE FROM knowledge_health_reports WHERE id = :id",
    ):
        with pytest.raises(IntegrityError), session_scope(factory) as session:
            session.execute(text(statement), {"id": first["id"]})
    assert _snapshot(factory, first["id"]) == stored


def _activity(factory: sessionmaker[Session]) -> Any:
    with session_scope(factory) as session:
        return session.execute(text("SELECT last_active_at FROM consolidation_activity")).all()


def test_opening_a_report_changes_no_pending_item_and_is_not_activity(tmp_path: Path) -> None:
    client, factory = _client(tmp_path)
    csrf = _login(client)
    _formal(client, csrf, "open-dup-a", "间隔复习能提高长期记忆")
    _formal(client, csrf, "open-dup-b", "间隔复习能提高长期记忆")
    _draft(client, csrf, "open-waiting")
    at = datetime.now(UTC) + IDLE
    run_consolidation_if_due(factory, now=at)
    items_before = _items(client, deferred="include")
    counts_before = client.get("/v1/review/counts").json()
    findings_before = client.get("/v1/consolidation/findings", params={"status": "all"}).json()
    activity_before = _activity(factory)

    report = _latest(client)
    assert client.get(f"/v1/knowledge-health/reports/{report['id']}").status_code == 200
    assert client.get("/v1/knowledge-health/reports").status_code == 200

    assert _items(client, deferred="include") == items_before
    assert client.get("/v1/review/counts").json() == counts_before
    assert (
        client.get("/v1/consolidation/findings", params={"status": "all"}).json() == findings_before
    )
    assert _activity(factory) == activity_before


def test_findings_link_to_their_entries_and_suggestions_to_the_filtered_pending_list(
    tmp_path: Path,
) -> None:
    client, factory = _client(tmp_path)
    csrf = _login(client)
    bare = _formal(client, csrf, "link-bare", "费曼学习法有助于理解概念", premises=[])
    _formal(client, csrf, "link-dup-a", "间隔复习能提高长期记忆", premises=CONFIRMED)
    _formal(client, csrf, "link-dup-b", "间隔复习能提高长期记忆", premises=CONFIRMED)
    _goal(client, csrf, "link-goal", "三个月内通过考试")
    goal = next(
        i
        for i in client.get("/v1/memory/formal").json()["items"]
        if i["state_key"] == "goal.current"
    )

    run_consolidation_if_due(factory, now=datetime.now(UTC) + timedelta(days=91))

    report = _latest(client)
    by_subject = {(f["kind"], f["subject_id"]): f for f in report["findings"]}
    missing = by_subject[("missing_premise", bare["id"])]
    assert missing["title"] == "link-bare"
    assert missing["href"] == f"/knowledge-agent?knowledge={bare['knowledge_id']}#library"
    stale = by_subject[("stale_volatile", goal["id"])]
    assert stale["href"] == f"/memory-center?memory={goal['id']}"
    assert [link["href"] for link in report["metrics"]["duplicate_suggestions"]["links"]] == [
        "/review-center?type=relation_suggestion"
    ]
    assert [link["href"] for link in report["metrics"]["unresolved_conflicts"]["links"]] == [
        "/review-center?type=relation_suggestion",
        "/review-center?type=personal_conflict",
    ]
    links = report["changes"]["pending"]["links"]
    assert links["relation_suggestion"] == "/review-center?type=relation_suggestion"
