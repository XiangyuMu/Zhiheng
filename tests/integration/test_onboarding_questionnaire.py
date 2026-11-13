"""The fixed five-question first-use questionnaire (#114, spec decision 25)."""

from __future__ import annotations

from pathlib import Path

from tests.integration.test_memory_api import _client, _headers, _login
from zhiheng.db.session import session_scope
from zhiheng.memory import MemoryRepository, MemoryValue

URL = "/v1/memory/onboarding"


def test_questionnaire_is_offered_on_first_use_and_skipping_stops_it(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    assert client.get(URL).status_code == 401
    csrf = _login(client)

    first = client.get(URL).json()
    assert first["status"] == "not_started"
    assert first["should_prompt"] is True
    assert [question["id"] for question in first["questions"]] == [
        "occupation",
        "interests",
        "current_goal",
        "answer_style",
        "excluded_domains",
    ]
    domains = first["questions"][4]["options"]
    assert len(domains) == 14
    assert domains[0] == {"id": "mathematics_formal_sciences", "name": "数学与形式科学"}

    skipped = client.post(f"{URL}/skip", headers=_headers(csrf, "skip-1"))
    assert skipped.status_code == 200
    assert skipped.json()["status"] == "skipped"
    assert skipped.json()["should_prompt"] is False

    # Retrying the skip is harmless, and the questionnaire stays out of the way.
    assert client.post(f"{URL}/skip", headers=_headers(csrf, "skip-2")).status_code == 200
    later = client.get(URL).json()
    assert later["status"] == "skipped"
    assert later["should_prompt"] is False
    assert client.get("/v1/memory/formal").json()["items"] == []
    assert client.get("/v1/memory/candidates").json()["items"] == []


ANSWERS = {
    "occupation": "软件工程师",
    "interests": "历史和音乐",
    "current_goal": "今年完成一个开源项目",
    "answer_style": "简洁，先给结论",
    "excluded_domains": ["medicine_health", "economics_finance_business"],
}


def test_completed_answers_become_memory_and_inferences_await_confirmation(
    tmp_path: Path,
) -> None:
    client, _ = _client(tmp_path)
    csrf = _login(client)

    response = client.post(URL, json={"answers": ANSWERS}, headers=_headers(csrf, "finish-1"))
    assert response.status_code == 200
    assert response.json()["status"] == "completed"
    assert response.json()["should_prompt"] is False
    assert client.get(URL).json()["status"] == "completed"

    formal = {item["state_key"]: item for item in client.get("/v1/memory/formal").json()["items"]}
    assert {key: item["value"]["text"] for key, item in formal.items()} == {
        "role.current": "软件工程师",
        "interest.primary": "历史和音乐",
        "goal.personal": "今年完成一个开源项目",
        "preference.response_style": "简洁，先给结论",
    }
    assert formal["goal.personal"]["time_sensitivity"] == "volatile"
    assert formal["role.current"]["time_sensitivity"] == "persistent"

    # Domains the system reads into the answers are only proposed.
    candidates = client.get("/v1/memory/candidates").json()["items"]
    inferred = {item["state_key"]: item for item in candidates}
    assert set(inferred) == {"inference.work_domain", "inference.interest_domains"}
    assert all(item["candidate_type"] == "inferred" for item in candidates)
    assert all(item["status"] == "pending_confirmation" for item in candidates)
    assert inferred["inference.work_domain"]["value"]["domain_ids"] == ["computing_engineering"]
    assert inferred["inference.interest_domains"]["value"]["domain_ids"] == [
        "history_philosophy_religion",
        "language_literature_arts",
    ]

    settings = client.get("/v1/memory/personalization").json()
    assert settings["excluded_domain_ids"] == ["economics_finance_business", "medicine_health"]


def test_an_answer_that_contradicts_existing_memory_awaits_confirmation(tmp_path: Path) -> None:
    client, factory = _client(tmp_path)
    csrf = _login(client)
    with session_scope(factory) as session:
        MemoryRepository().commit_explicit_memory(
            session,
            MemoryValue(memory_type="fact", state_key="role.current", value={"text": "产品经理"}),
            operation_key="seed-role",
        )

    response = client.post(
        URL,
        json={"answers": {"occupation": "软件工程师"}},
        headers=_headers(csrf, "finish-conflict"),
    )
    assert response.status_code == 200
    occupation = next(
        item
        for item in response.json()["answers"]
        if item["question_id"] == "occupation" and not item.get("inferred")
    )
    assert occupation["disposition"] == "conflict_pending"

    formal = {item["state_key"]: item for item in client.get("/v1/memory/formal").json()["items"]}
    assert formal["role.current"]["value"] == {"text": "产品经理"}
    # Leaving the fifth question unanswered keeps the existing setting.
    assert client.get("/v1/memory/personalization").json()["excluded_domain_ids"] == []


def test_a_skipped_questionnaire_can_still_be_completed_once(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    csrf = _login(client)
    assert client.post(f"{URL}/skip", headers=_headers(csrf, "skip")).status_code == 200

    bad = client.post(
        URL,
        json={"answers": {"occupation": "软件工程师", "excluded_domains": ["unassigned"]}},
        headers=_headers(csrf, "finish-bad"),
    )
    assert bad.status_code == 422
    assert client.get("/v1/memory/formal").json()["items"] == []
    assert client.get(URL).json()["status"] == "skipped"

    body = {"answers": {"answer_style": "简洁", "excluded_domains": ["medicine_health"]}}
    first = client.post(URL, json=body, headers=_headers(csrf, "finish"))
    assert first.status_code == 200
    assert first.json()["status"] == "completed"

    # A retried submission returns the same outcome without remembering twice.
    retried = client.post(URL, json=body, headers=_headers(csrf, "finish"))
    assert retried.status_code == 200
    assert retried.json()["answers"] == first.json()["answers"]
    assert len(client.get("/v1/memory/formal").json()["items"]) == 1

    again = client.post(URL, json=body, headers=_headers(csrf, "finish-again"))
    assert again.status_code == 409
    assert client.post(f"{URL}/skip", headers=_headers(csrf, "skip-late")).json()["status"] == (
        "completed"
    )
