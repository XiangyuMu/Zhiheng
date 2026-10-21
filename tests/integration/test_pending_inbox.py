from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from tests.integration.test_memory_api import _candidate_payload, _client, _headers, _login
from zhiheng.db.session import session_scope


def _draft(
    client: TestClient, csrf: str, key: str, claim: str = "固定条件下复习有效"
) -> dict[str, Any]:
    source = client.post(
        "/v1/conclusions/sources",
        json={"text": f"研究结论原文：{claim}。"},
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
            "premises": [{"text": "固定条件", "confirmed": False}],
            "excerpt": claim,
        },
        headers=_headers(csrf, key + "-draft"),
    )
    assert response.status_code == 200, response.text
    return dict(response.json())


def _items(client: TestClient, **params: Any) -> list[dict[str, Any]]:
    response = client.get("/v1/review/items", params=params)
    assert response.status_code == 200, response.text
    return list(response.json()["items"])


def test_conclusion_draft_is_listed_with_uniform_shape(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    csrf = _login(client)
    draft = _draft(client, csrf, "inbox-draft")

    items = _items(client)

    item = next(item for item in items if item["id"] == draft["id"])
    assert item["type"] == "conclusion_draft"
    assert item["etag"] == draft["etag"]
    assert item["actions"] == ["approve", "reject", "defer"]
    assert item["requires_individual_approval"] is True
    assert item["deferred"] is False
    assert item["title"] == "inbox-draft"
    assert item["pending_since"]


def test_deferred_conclusion_leaves_default_view_and_appears_in_deferred_only(
    tmp_path: Path,
) -> None:
    client, _ = _client(tmp_path)
    csrf = _login(client)
    draft = _draft(client, csrf, "inbox-deferred")
    deferred = client.post(
        f"/v1/conclusions/{draft['id']}/defer",
        json={},
        headers=_headers(csrf, "inbox-defer", draft["etag"]),
    )
    assert deferred.status_code == 200, deferred.text

    assert all(item["id"] != draft["id"] for item in _items(client))
    only = _items(client, deferred="only")
    assert [item["id"] for item in only] == [draft["id"]]
    assert only[0]["deferred"] is True
    assert only[0]["actions"] == ["restore"]


def _act(client: TestClient, csrf: str, item: dict[str, Any], action: str, key: str) -> Any:
    return client.post(
        f"/v1/review/items/{item['type']}/{item['id']}/{action}",
        json={},
        headers=_headers(csrf, key, item["etag"]),
    )


def test_restoring_a_deferred_draft_keeps_its_place_in_the_queue(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    csrf = _login(client)
    draft = _draft(client, csrf, "inbox-restore")
    original = next(item for item in _items(client) if item["id"] == draft["id"])
    assert _act(client, csrf, original, "defer", "inbox-restore-defer").status_code == 200
    deferred = _items(client, deferred="only")[0]

    restored = _act(client, csrf, deferred, "restore", "inbox-restore-restore")
    replay = _act(client, csrf, deferred, "restore", "inbox-restore-restore")

    assert restored.status_code == 200, restored.text
    assert replay.json() == restored.json()
    back = next(item for item in _items(client) if item["id"] == draft["id"])
    assert back["deferred"] is False
    assert back["pending_since"] == original["pending_since"]
    assert _items(client, deferred="only") == []


def test_stale_etag_and_unavailable_action_are_refused(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    csrf = _login(client)
    draft = _draft(client, csrf, "inbox-stale")
    item = next(item for item in _items(client) if item["id"] == draft["id"])

    stale = client.post(
        f"/v1/review/items/conclusion_draft/{draft['id']}/defer",
        json={},
        headers=_headers(csrf, "inbox-stale-defer", "not-the-etag"),
    )
    unavailable = _act(client, csrf, item, "restore", "inbox-stale-restore")
    missing = client.post(
        "/v1/review/items/conclusion_draft/no-such-id/defer",
        json={},
        headers=_headers(csrf, "inbox-stale-missing"),
    )

    assert stale.status_code == 412
    assert unavailable.status_code == 409
    assert missing.status_code == 404


def _relation(client: TestClient, csrf: str, key: str) -> dict[str, Any]:
    old = _draft(client, csrf, key + "-old", "每天复习能提高记忆")
    approved = client.post(
        f"/v1/conclusions/{old['id']}/approve",
        json={},
        headers=_headers(csrf, key + "-approve", old["etag"]),
    )
    assert approved.status_code == 200, approved.text
    new = _draft(client, csrf, key + "-new", "每天复习不能提高记忆")
    relations = client.get(f"/v1/conclusions/{new['id']}/relations").json()["items"]
    assert len(relations) == 1
    return dict(relations[0])


def test_relation_suggestion_is_listed_with_its_own_etag_and_creation_time(
    tmp_path: Path,
) -> None:
    client, _ = _client(tmp_path)
    csrf = _login(client)
    relation = _relation(client, csrf, "inbox-relation")

    item = next(item for item in _items(client) if item["id"] == relation["id"])

    assert item["type"] == "relation_suggestion"
    assert item["etag"] == relation["etag"]
    assert relation["created_at"]
    assert item["actions"] == ["approve", "reject", "defer"]
    assert item["requires_individual_approval"] is True
    assert item["detail"]["kind"] == "conflict"

    assert _act(client, csrf, item, "defer", "inbox-relation-defer").status_code == 200
    deferred = next(i for i in _items(client, deferred="only") if i["id"] == relation["id"])
    assert deferred["actions"] == ["restore"]
    assert deferred["pending_since"] == item["pending_since"]
    assert _act(client, csrf, deferred, "restore", "inbox-relation-restore").status_code == 200
    assert any(i["id"] == relation["id"] for i in _items(client))


def _city(value: str) -> dict[str, Any]:
    return {
        "memory_type": "profile",
        "state_key": "profile.city",
        "value": {"text": value},
        "source_kind": "user_explicit",
        "evidence_refs": [{"excerpt": f"我现在住在{value}"}],
    }


def _conflict(client: TestClient, csrf: str, key: str) -> dict[str, Any]:
    for index, city in enumerate(("北京", "上海")):
        response = client.post(
            "/v1/personal-updates", json=_city(city), headers=_headers(csrf, f"{key}-{index}")
        )
        assert response.status_code == 200, response.text
    result = dict(response.json()["result"])
    assert result["disposition"] == "conflict_pending"
    return result


def test_personal_conflict_is_resolved_in_detail_but_can_be_deferred(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    csrf = _login(client)
    conflict = _conflict(client, csrf, "inbox-conflict")

    item = next(item for item in _items(client) if item["id"] == conflict["conflict_id"])
    assert item["type"] == "personal_conflict"
    assert item["actions"] == ["defer"]
    assert item["detail_actions"] == ["confirm", "reject", "supplement"]
    assert item["detail"]["candidate_etag"]

    assert _act(client, csrf, item, "defer", "inbox-conflict-defer").status_code == 200
    assert all(i["id"] != conflict["conflict_id"] for i in _items(client))
    deferred = next(i for i in _items(client, deferred="only") if i["id"] == item["id"])
    assert deferred["actions"] == ["restore"]
    assert _act(client, csrf, deferred, "restore", "inbox-conflict-restore").status_code == 200
    assert any(i["id"] == item["id"] and not i["deferred"] for i in _items(client))


def test_a_conflict_cannot_be_skipped_so_its_candidate_never_leaves_the_inbox(
    tmp_path: Path,
) -> None:
    client, _ = _client(tmp_path)
    csrf = _login(client)
    conflict = _conflict(client, csrf, "inbox-skip")
    conflict_id = conflict["conflict_id"]

    via_conflict = client.post(
        f"/v1/personal-updates/conflicts/{conflict_id}/decision",
        json={"decision": "skip"},
        headers=_headers(csrf, "inbox-skip-decision"),
    )
    via_prompt = client.post(
        f"/v1/personal-updates/context-prompts/{conflict_id}/decision",
        json={"decision": "skip"},
        headers=_headers(csrf, "inbox-skip-prompt"),
    )

    # ADR 0011: a conflict is resolved by choosing or supplementing, or deferred.
    assert via_conflict.status_code == 422, via_conflict.text
    assert via_prompt.status_code == 400, via_prompt.text
    assert [(i["type"], i["id"]) for i in _items(client)] == [("personal_conflict", conflict_id)]
    pending = client.get("/v1/personal-updates/conflicts").json()["items"]
    assert any(i["id"] == conflict_id for i in pending)
    prompts = client.get(
        "/v1/personal-updates/context-prompts", params={"query": "我现在居住在哪个城市？"}
    ).json()["items"]
    actions = next(p["actions"] for p in prompts if p["id"] == conflict_id)
    assert "skip" not in actions


def _missing_prompt(client: TestClient, csrf: str, key: str) -> dict[str, Any]:
    response = client.post(
        "/v1/answers", json={"query": "我的工作偏好是什么？"}, headers=_headers(csrf, key)
    )
    assert response.status_code == 200, response.text
    return dict(
        next(item for item in response.json()["context_prompts"] if item["kind"] == "missing")
    )


def test_missing_info_prompt_is_listed_and_reject_means_skip(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    csrf = _login(client)
    prompt = _missing_prompt(client, csrf, "inbox-missing")

    item = next(item for item in _items(client) if item["id"] == prompt["id"])
    assert item["type"] == "missing_info_prompt"
    assert item["actions"] == ["reject", "defer"]
    assert item["detail_actions"] == ["supplement"]
    assert item["requires_individual_approval"] is False

    rejected = _act(client, csrf, item, "reject", "inbox-missing-reject")

    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["result"]["status"] == "skipped"
    assert all(i["id"] != prompt["id"] for i in _items(client, deferred="include"))


def test_skipping_a_deferred_prompt_succeeds(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    csrf = _login(client)
    prompt = _missing_prompt(client, csrf, "inbox-missing-deferred")
    decide = f"/v1/personal-updates/context-prompts/{prompt['id']}/decision"
    deferred = client.post(
        decide, json={"decision": "defer"}, headers=_headers(csrf, "inbox-missing-defer")
    )
    assert deferred.status_code == 200, deferred.text
    assert [i["id"] for i in _items(client, deferred="only")] == [prompt["id"]]

    skipped = client.post(
        decide, json={"decision": "skip"}, headers=_headers(csrf, "inbox-missing-skip")
    )

    assert skipped.status_code == 200, skipped.text
    assert skipped.json()["result"]["status"] == "skipped"
    assert _items(client, deferred="include") == []


def _extraction_run(
    client: TestClient, factory: sessionmaker[Session], csrf: str, run_id: str
) -> None:
    source = client.post(
        "/v1/conclusions/sources",
        json={"text": "一段没有明确结论的聊天"},
        headers=_headers(csrf, run_id + "-source"),
    )
    assert source.status_code == 200, source.text
    with session_scope(factory) as session:
        session.execute(
            text(
                """
                INSERT INTO conversation_extraction_runs
                  (id, owner_user_id, conversation_id, history_id, source_id, status,
                   extracted_count, unrecognized_count)
                SELECT :id, owner_user_id, 'conversation', :id, id, 'succeeded', 0, 1
                FROM conclusion_sources WHERE id = :source
                """
            ),
            {"id": run_id, "source": source.json()["id"]},
        )


def test_extraction_needing_a_supplement_can_be_deferred_and_restored(tmp_path: Path) -> None:
    client, factory = _client(tmp_path)
    csrf = _login(client)
    _extraction_run(client, factory, csrf, "run-1")

    item = next(item for item in _items(client) if item["id"] == "run-1")
    assert item["type"] == "extraction_supplement"
    assert item["actions"] == ["defer"]
    assert item["detail_actions"] == ["supplement"]

    assert _act(client, csrf, item, "defer", "inbox-run-defer").status_code == 200
    assert all(i["id"] != "run-1" for i in _items(client))
    deferred = next(i for i in _items(client, deferred="only") if i["id"] == "run-1")
    assert deferred["actions"] == ["restore"]
    assert deferred["etag"] != item["etag"]
    assert _act(client, csrf, deferred, "restore", "inbox-run-restore").status_code == 200
    back = next(i for i in _items(client) if i["id"] == "run-1")
    assert back["pending_since"] == item["pending_since"]
    assert back["etag"] == item["etag"]


def test_memory_candidates_need_individual_approval_only_when_inferred(
    tmp_path: Path,
) -> None:
    client, _ = _client(tmp_path)
    csrf = _login(client)
    created = {}
    for candidate_type in ("inferred", "explicit_extracted"):
        response = client.post(
            "/v1/memory/candidates",
            json={**_candidate_payload(candidate_type), "candidate_type": candidate_type},
            headers=_headers(csrf, f"inbox-candidate-{candidate_type}"),
        )
        assert response.status_code == 200, response.text
        created[candidate_type] = response.json()["result"]
    listed = {c["id"]: c for c in client.get("/v1/memory/candidates").json()["items"]}

    items = {i["id"]: i for i in _items(client, type="memory_candidate")}

    for candidate_type, individual in (("inferred", True), ("explicit_extracted", False)):
        item = items[created[candidate_type]["candidate_id"]]
        assert item["actions"] == ["approve", "reject", "defer"]
        assert item["etag"] == listed[item["id"]]["etag"]
        assert item["requires_individual_approval"] is individual


def test_memory_candidate_defer_and_restore_go_through_the_inbox(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    csrf = _login(client)
    created = client.post(
        "/v1/memory/candidates",
        json=_candidate_payload("defer me"),
        headers=_headers(csrf, "inbox-candidate-defer-create"),
    ).json()["result"]
    item = next(i for i in _items(client) if i["id"] == created["candidate_id"])

    assert _act(client, csrf, item, "defer", "inbox-candidate-defer").status_code == 200

    deferred_api = client.get("/v1/memory/candidates", params={"state": "deferred"}).json()
    assert [c["id"] for c in deferred_api["items"]] == [item["id"]]
    deferred = next(i for i in _items(client, deferred="only") if i["id"] == item["id"])
    assert deferred["actions"] == ["restore"]
    assert deferred["etag"] == deferred_api["items"][0]["etag"]
    assert _act(client, csrf, deferred, "restore", "inbox-candidate-restore").status_code == 200
    back = next(i for i in _items(client) if i["id"] == item["id"])
    assert back["pending_since"] == item["pending_since"]
    confirmed = client.post(
        f"/v1/memory/candidates/{item['id']}/confirm",
        json={},
        headers=_headers(csrf, "inbox-candidate-confirm", back["etag"]),
    )
    assert confirmed.status_code == 200, confirmed.text


def test_conflict_whose_candidate_was_deferred_is_deferred_once(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    csrf = _login(client)
    conflict = _conflict(client, csrf, "inbox-conflict-candidate")
    candidate_etag = next(i for i in _items(client) if i["id"] == conflict["conflict_id"])[
        "detail"
    ]["candidate_etag"]
    deferred_candidate = client.post(
        f"/v1/memory/candidates/{conflict['candidate_id']}/defer",
        json={},
        headers=_headers(csrf, "inbox-conflict-candidate-defer", candidate_etag),
    )
    assert deferred_candidate.status_code == 200, deferred_candidate.text

    assert _items(client) == []
    assert client.get("/v1/review/counts").json()["total"] == 0
    deferred = _items(client, deferred="only")
    assert [(i["type"], i["id"]) for i in deferred] == [
        ("personal_conflict", conflict["conflict_id"])
    ]
    assert deferred[0]["actions"] == ["restore"]

    assert (
        _act(client, csrf, deferred[0], "restore", "inbox-conflict-candidate-restore").status_code
        == 200
    )
    restored = _items(client)
    assert [(i["type"], i["id"]) for i in restored] == [
        ("personal_conflict", conflict["conflict_id"])
    ]
    confirmed = client.post(
        f"/v1/personal-updates/conflicts/{conflict['conflict_id']}/decision",
        json={"decision": "confirm"},
        headers=_headers(
            csrf, "inbox-conflict-candidate-confirm", restored[0]["detail"]["candidate_etag"]
        ),
    )
    assert confirmed.status_code == 200, confirmed.text


def test_candidate_behind_a_conflict_is_counted_once_as_the_conflict(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    csrf = _login(client)
    conflict = _conflict(client, csrf, "inbox-once")

    items = _items(client)

    assert [i["type"] for i in items] == ["personal_conflict"]
    assert all(i["id"] != conflict["candidate_id"] for i in items)


def _import(client: TestClient, csrf: str, key: str, **fields: Any) -> str:
    body = {"title": "代码里的哈希表", "text": "计算机中的查找结构。", **fields}
    response = client.post("/v1/knowledge/imports", json=body, headers=_headers(csrf, key))
    assert response.status_code == 200, response.text
    return str(response.json()["result"]["knowledge_object_id"])


def test_unclassified_knowledge_carries_its_suggested_domain(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    csrf = _login(client)
    knowledge_id = _import(client, csrf, "inbox-unclassified")

    item = next(i for i in _items(client, type="classification") if i["id"] == knowledge_id)

    assert item["kind"] == "unclassified"
    assert item["detail"]["suggested_primary_domain_id"] == "computing_engineering"
    assert item["actions"] == ["approve", "reject", "defer"]
    assert item["requires_individual_approval"] is False
    assert _act(client, csrf, item, "defer", "inbox-unclassified-defer").status_code == 200
    deferred = _items(client, type="classification", deferred="only")
    assert [(i["id"], i["actions"]) for i in deferred] == [(knowledge_id, ["restore"])]
    # The classification page's own restore shares the same 延后 state.
    restored = client.post(
        f"/v1/knowledge/{knowledge_id}/unclassified/restore",
        headers=_headers(csrf, "inbox-unclassified-restore"),
    )
    assert restored.status_code == 200, restored.text
    assert any(i["id"] == knowledge_id for i in _items(client, type="classification"))


def test_pending_taxonomy_proposal_is_a_classification_item(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    csrf = _login(client)
    knowledge_id = _import(client, csrf, "inbox-legacy", primary_domain_id="personal.notes")
    created = client.post(
        "/v1/taxonomy/proposals/legacy-migration",
        json={
            "legacy_domain_id": "personal.notes",
            "mapping": {knowledge_id: "computing_engineering"},
            "reason": "迁移旧领域",
        },
        headers=_headers(csrf, "inbox-legacy-create"),
    )
    assert created.status_code == 201, created.text
    proposal = created.json()["result"]

    item = next(i for i in _items(client, type="classification") if i["id"] == proposal["id"])

    assert item["kind"] == "taxonomy_proposal"
    assert item["etag"] == proposal["etag"]
    assert item["actions"] == ["approve", "reject", "defer"]
    assert item["requires_individual_approval"] is False

    assert _act(client, csrf, item, "defer", "inbox-legacy-defer").status_code == 200
    deferred = next(
        i for i in _items(client, type="classification", deferred="only") if i["id"] == item["id"]
    )
    assert deferred["actions"] == ["restore"]
    assert deferred["pending_since"] == item["pending_since"]
    assert _act(client, csrf, deferred, "restore", "inbox-legacy-restore").status_code == 200
    back = next(i for i in _items(client, type="classification") if i["id"] == item["id"])
    rejected = _act(client, csrf, back, "reject", "inbox-legacy-reject")
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["result"]["status"] == "rejected"
    assert all(
        i["id"] != item["id"] for i in _items(client, type="classification", deferred="include")
    )


def _set_created(factory: sessionmaker[Session], table: str, item_id: str, when: str) -> None:
    with session_scope(factory) as session:
        session.execute(
            text(f"UPDATE {table} SET created_at = :when WHERE id = :id"),
            {
                "when": when,
                "id": item_id,
            },
        )


def test_items_are_ordered_by_when_they_became_pending_and_paginated(tmp_path: Path) -> None:
    client, factory = _client(tmp_path)
    csrf = _login(client)
    late = _draft(client, csrf, "inbox-late")
    early = _missing_prompt(client, csrf, "inbox-early")
    middle = _draft(client, csrf, "inbox-middle")
    _set_created(factory, "conclusion_entries", late["id"], "2026-03-01 08:00:00")
    _set_created(factory, "personal_prompts", early["id"], "2026-01-01 08:00:00")
    _set_created(factory, "conclusion_entries", middle["id"], "2026-02-01 08:00:00")
    expected = [early["id"], middle["id"], late["id"]]

    assert [i["id"] for i in _items(client)] == expected
    assert [i["id"] for i in _items(client, order="newest")] == expected[::-1]
    first = client.get("/v1/review/items", params={"limit": 2}).json()
    second = client.get("/v1/review/items", params={"limit": 2, "offset": 2}).json()
    assert [i["id"] for i in first["items"]] == expected[:2]
    assert first["total"] == 3
    assert first["next_offset"] == 2
    assert [i["id"] for i in second["items"]] == expected[2:]
    assert second["next_offset"] is None
    assert first["items"][0]["pending_since"] == "2026-01-01T08:00:00+00:00"


def test_counts_match_the_default_list_and_exclude_deferred_items(tmp_path: Path) -> None:
    client, factory = _client(tmp_path)
    csrf = _login(client)
    _relation(client, csrf, "inbox-count-relation")
    _conflict(client, csrf, "inbox-count-conflict")
    _missing_prompt(client, csrf, "inbox-count-missing")
    _extraction_run(client, factory, csrf, "run-count")
    _import(client, csrf, "inbox-count-import")
    deferred = _draft(client, csrf, "inbox-count-deferred", "另一个结论")
    client.post(
        f"/v1/conclusions/{deferred['id']}/defer",
        json={},
        headers=_headers(csrf, "inbox-count-defer", deferred["etag"]),
    )

    counts = client.get("/v1/review/counts")
    listed = client.get("/v1/review/items", params={"limit": 500}).json()

    assert counts.status_code == 200, counts.text
    body = counts.json()
    assert body["total"] == listed["total"] == len(listed["items"])
    by_type: dict[str, int] = {}
    for item in listed["items"]:
        by_type[item["type"]] = by_type.get(item["type"], 0) + 1
    assert {k: v for k, v in body["by_type"].items() if v} == by_type
    assert set(body["by_type"]) == {
        "conclusion_draft",
        "relation_suggestion",
        "personal_conflict",
        "missing_info_prompt",
        "extraction_supplement",
        "memory_candidate",
        "classification",
    }
    assert body["by_type"]["conclusion_draft"] == 1  # the relation's draft, not the deferred one
    for item_type, count in body["by_type"].items():
        filtered = client.get("/v1/review/items", params={"type": item_type}).json()
        assert filtered["total"] == count


def test_unknown_type_filter_is_a_client_error(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    _login(client)

    assert client.get("/v1/review/items", params={"type": "evolution"}).status_code == 400
