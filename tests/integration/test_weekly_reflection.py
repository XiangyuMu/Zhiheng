"""Weekly reflection into synthesized drafts (#116, spec decision 23, scenarios 25/29/30).

The knowledge base is built through the real API; the worker's consolidation
step, which reflects once a week, is then run with a clock, and the drafts are
read back through 待处理 and the conclusions API.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from tests.integration.test_background_consolidation import IDLE, _context, _draft_with
from tests.integration.test_memory_api import _client, _headers, _login
from tests.integration.test_pending_importance import _relation_to_formal
from tests.integration.test_pending_inbox import _act, _items
from zhiheng.consolidation import run_consolidation_if_due
from zhiheng.db.session import session_scope

EVENING = [{"text": "晚上学习", "confirmed": True}]


def _formal(
    client: TestClient,
    csrf: str,
    key: str,
    claim: str,
    premises: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    draft = _draft_with(client, csrf, key, claim, EVENING if premises is None else premises)
    approved = client.post(
        f"/v1/conclusions/{draft['id']}/approve",
        json={},
        headers=_headers(csrf, key + "-approve", draft["etag"]),
    )
    assert approved.status_code == 200, approved.text
    return dict(approved.json())


def _reflect(factory: sessionmaker[Session], at: datetime | None = None) -> Any:
    return run_consolidation_if_due(factory, now=at or datetime.now(UTC) + IDLE)


def _syntheses(client: TestClient, **params: Any) -> list[dict[str, Any]]:
    """Synthesized drafts in 待处理, read in full through the conclusions API."""
    drafts = [
        client.get(f"/v1/conclusions/{item['id']}").json()
        for item in _items(client, type="conclusion_draft", **params)
    ]
    return [draft for draft in drafts if draft.get("origin") == "system_synthesis"]


def _basis_ids(draft: dict[str, Any]) -> set[str]:
    return {str(item["entry_id"]) for item in draft["basis"]}


def test_reflection_drafts_a_system_generated_synthesis_citing_only_its_formal_basis(
    tmp_path: Path,
) -> None:
    client, factory = _client(tmp_path)
    csrf = _login(client)
    first = _formal(client, csrf, "syn-a", "番茄钟有助于专注")
    second = _formal(client, csrf, "syn-b", "听白噪音能减少走神")

    run = _reflect(factory)

    assert run is not None and run.reflection_drafts_created == 1
    [draft] = _syntheses(client)
    assert draft["status"] == "draft"
    assert _basis_ids(draft) == {first["id"], second["id"]}
    assert {item["entry_id"] for item in draft["evidence"]} == {first["id"], second["id"]}
    assert "番茄钟有助于专注" in draft["claim"] and "听白噪音能减少走神" in draft["claim"]
    assert [premise["text"] for premise in draft["premises"]] == ["晚上学习"]
    # Not approved, so it does not take part in answers.
    assert draft["id"] not in _context(client, "番茄钟")


def _approve(client: TestClient, csrf: str, draft: dict[str, Any], key: str) -> dict[str, Any]:
    response = client.post(
        f"/v1/conclusions/{draft['id']}/approve",
        json={},
        headers=_headers(csrf, key, draft["etag"]),
    )
    assert response.status_code == 200, response.text
    return dict(response.json())


def test_an_approved_synthesis_is_formal_knowledge_always_marked_as_system_synthesized(
    tmp_path: Path,
) -> None:
    client, factory = _client(tmp_path)
    csrf = _login(client)
    _formal(client, csrf, "mark-a", "番茄钟有助于专注")
    _formal(client, csrf, "mark-b", "听白噪音能减少走神")
    _reflect(factory)
    [draft] = _syntheses(client)
    # The synthesis is drawn from its basis, so it is never suggested as
    # revising or supplementing it.
    assert _items(client, type="relation_suggestion") == []

    approved = _approve(client, csrf, draft, "mark-approve")

    formal = client.get(f"/v1/conclusions/{draft['id']}").json()
    assert formal["status"] == "formal" and formal["origin"] == "system_synthesis"
    knowledge = client.get(f"/v1/knowledge/{approved['knowledge_id']}").json()
    assert knowledge["source_metadata"]["evidence_basis"] == "system_synthesis"
    assert draft["id"] in _context(client, "番茄钟")


def _reject(client: TestClient, csrf: str, draft: dict[str, Any], key: str) -> None:
    response = client.post(
        f"/v1/conclusions/{draft['id']}/reject",
        json={},
        headers=_headers(csrf, key, draft["etag"]),
    )
    assert response.status_code == 200, response.text


def test_reflection_runs_weekly_and_a_rejected_draft_returns_only_with_new_related_knowledge(
    tmp_path: Path,
) -> None:
    client, factory = _client(tmp_path)
    csrf = _login(client)
    first = _formal(client, csrf, "week-a", "番茄钟有助于专注")
    second = _formal(client, csrf, "week-b", "听白噪音能减少走神")
    start = datetime.now(UTC) + IDLE
    _reflect(factory, start)
    [draft] = _syntheses(client)

    # Consolidation runs daily; reflection only once a week.
    assert _reflect(factory, start + timedelta(days=1)).reflection_drafts_created == 0
    assert [item["id"] for item in _syntheses(client)] == [draft["id"]]
    # A week later the same basis drafts nothing again, pending or rejected.
    assert _reflect(factory, start + timedelta(days=7)).reflection_drafts_created == 0
    _reject(client, csrf, draft, "week-reject")
    assert _reflect(factory, start + timedelta(days=14)).reflection_drafts_created == 0
    assert _syntheses(client, deferred="include") == []

    third = _formal(client, csrf, "week-c", "关掉手机通知能保持专注时长")
    run = _reflect(factory, datetime.now(UTC) + IDLE + timedelta(days=21))

    assert run.reflection_drafts_created == 1
    [renewed] = _syntheses(client)
    assert _basis_ids(renewed) == {first["id"], second["id"], third["id"]}


def test_unapproved_syntheses_are_not_reflected_on_and_an_approved_one_stands_for_its_basis(
    tmp_path: Path,
) -> None:
    client, factory = _client(tmp_path)
    csrf = _login(client)
    _formal(client, csrf, "in-a", "番茄钟有助于专注")
    _formal(client, csrf, "in-b", "听白噪音能减少走神")
    start = datetime.now(UTC) + IDLE
    _reflect(factory, start)
    [pending] = _syntheses(client)
    _formal(client, csrf, "in-c", "关掉手机通知能保持专注时长")

    _reflect(factory, datetime.now(UTC) + IDLE + timedelta(days=7))

    # The pending synthesis is not input: the new draft rests on formal knowledge only.
    [newer] = [item for item in _syntheses(client) if item["id"] != pending["id"]]
    assert pending["id"] not in _basis_ids(newer) and len(newer["basis"]) == 3
    approved = _approve(client, csrf, newer, "in-approve")
    fourth = _formal(client, csrf, "in-d", "固定学习时段能减少拖延")

    _reflect(factory, datetime.now(UTC) + IDLE + timedelta(days=14))

    # Once approved, the synthesis is formal knowledge standing for its basis.
    [latest] = [item for item in _syntheses(client) if item["id"] != pending["id"]]
    assert _basis_ids(latest) == {approved["id"], fourth["id"]}


def _approved_synthesis(
    client: TestClient, csrf: str, factory: sessionmaker[Session], key: str
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    basis = [
        _formal(client, csrf, key + "-a", "番茄钟有助于专注"),
        _formal(client, csrf, key + "-b", "听白噪音能减少走神"),
    ]
    _reflect(factory)
    [draft] = _syntheses(client)
    _approve(client, csrf, draft, key + "-approve")
    return draft, basis


def _applicability(client: TestClient, entry_id: str) -> dict[str, Any]:
    return dict(client.get(f"/v1/conclusions/{entry_id}").json()["applicability"])


def test_a_paused_basis_puts_the_synthesis_under_applicability_review(tmp_path: Path) -> None:
    client, factory = _client(tmp_path)
    csrf = _login(client)
    synthesis, [paused, _] = _approved_synthesis(client, csrf, factory, "pause")
    with session_scope(factory) as session:
        session.execute(
            text(
                "UPDATE conclusion_versions SET payload_json = json_set(payload_json, "
                "'$.valid_until', '2000-01-01T00:00:00Z') WHERE entry_id = :id"
            ),
            {"id": paused["id"]},
        )

    state = _applicability(client, synthesis["id"])

    assert state["state"] == "suspended" and state["reason"] == "basis_suspended"
    assert state["evidence"]["basis_entry_id"] == paused["id"]
    assert synthesis["id"] not in _context(client, "番茄钟")
    # The synthesis itself is unchanged: still formal, still the approved version.
    detail = client.get(f"/v1/conclusions/{synthesis['id']}").json()
    assert detail["status"] == "formal" and detail["approved_version"] == 1


def test_a_deleted_basis_puts_the_synthesis_under_applicability_review(tmp_path: Path) -> None:
    client, factory = _client(tmp_path)
    csrf = _login(client)
    synthesis, [_, deleted] = _approved_synthesis(client, csrf, factory, "delete")
    response = client.post(
        f"/v1/knowledge/{deleted['knowledge_id']}/delete",
        json={},
        headers=_headers(csrf, "delete-basis"),
    )
    assert response.status_code == 200, response.text

    state = _applicability(client, synthesis["id"])

    assert state["state"] == "suspended" and state["reason"] == "basis_removed"
    assert synthesis["id"] not in _context(client, "番茄钟")


def test_a_revised_basis_only_raises_a_review_reminder(tmp_path: Path) -> None:
    client, factory = _client(tmp_path)
    csrf = _login(client)
    revise = _relation_to_formal(
        client, csrf, "rev", "每天阅读三十分钟能扩大词汇量", "每天阅读四十分钟能扩大词汇量"
    )
    other = _formal(
        client, csrf, "rev-other", "复述能加深理解", [{"text": "固定条件", "confirmed": False}]
    )
    _reflect(factory)
    [synthesis] = _syntheses(client)
    assert _basis_ids(synthesis) == {revise["formal"]["id"], other["id"]}
    _approve(client, csrf, synthesis, "rev-synthesis-approve")
    relation = next(
        item
        for item in _items(client, type="relation_suggestion")
        if item["id"] == revise["relation"]["id"]
    )
    assert _act(client, csrf, relation, "approve", "rev-relation").status_code == 200

    state = _applicability(client, synthesis["id"])

    assert state["state"] == "review_required" and state["reason"] == "basis_revised"
    # A reminder is not a pause: the synthesis keeps serving answers.
    assert synthesis["id"] in _context(client, "复述")


def test_a_fact_only_in_imported_material_never_appears_in_a_reflection_draft(
    tmp_path: Path,
) -> None:
    client, factory = _client(tmp_path)
    csrf = _login(client)
    fact = "紫藤学习法要求晚上学习前默写七遍"
    imported = client.post(
        "/v1/knowledge/imports",
        json={"title": "晚上学习的方法", "text": f"晚上学习时，{fact}。"},
        headers=_headers(csrf, "import-material"),
    )
    assert imported.status_code == 200, imported.text
    _formal(client, csrf, "imp-a", "番茄钟有助于专注")
    _formal(client, csrf, "imp-b", "听白噪音能减少走神")

    _reflect(factory)

    [draft] = _syntheses(client)
    assert "紫藤" not in json.dumps(draft, ensure_ascii=False)
