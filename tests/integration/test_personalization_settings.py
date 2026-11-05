"""Primary domains the user keeps free of personalization (#111)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from tests.integration.test_g005_api_impl import _client as answer_client
from tests.integration.test_g005_api_impl import _headers as answer_headers
from tests.integration.test_g005_api_impl import _seed as seed
from tests.integration.test_memory_api import _client, _headers, _login
from tests.knowledge_helpers import stored_text_artifacts
from zhiheng.classification.taxonomy import PRIMARY_DOMAINS
from zhiheng.core.ids import sha256_text
from zhiheng.db.session import session_scope
from zhiheng.evaluation.search_fixtures import mark_formal_knowledge_indexed
from zhiheng.jobs.memory_extraction import (
    MemoryExtractionJobExecutor,
    process_memory_extraction_jobs_once,
)
from zhiheng.jobs.outbox import OutboxRepository
from zhiheng.knowledge import KnowledgeRepository, KnowledgeUserAuthority, TextEvidenceInput
from zhiheng.memory import MemoryRepository, MemoryValue

URL = "/v1/memory/personalization"


def test_user_can_choose_and_change_domains_without_personalization(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    assert client.get(URL).status_code == 401
    csrf = _login(client)

    initial = client.get(URL)
    assert initial.status_code == 200
    body = initial.json()
    assert len(body["domains"]) == 14
    assert body["excluded_domain_ids"] == []
    assert not any(domain["excluded"] for domain in body["domains"])

    saved = client.put(
        URL,
        json={"excluded_domain_ids": ["medicine_health", "economics_finance_business"]},
        headers=_headers(csrf, "exclude-1", body["etag"]),
    )
    assert saved.status_code == 200
    assert saved.json()["excluded_domain_ids"] == [
        "economics_finance_business",
        "medicine_health",
    ]
    health = next(d for d in saved.json()["domains"] if d["id"] == "medicine_health")
    assert health == {"id": "medicine_health", "name": "医学与健康", "excluded": True}

    stale = client.put(
        URL,
        json={"excluded_domain_ids": []},
        headers=_headers(csrf, "exclude-stale", body["etag"]),
    )
    assert stale.status_code == 412

    changed = client.put(
        URL,
        json={"excluded_domain_ids": ["medicine_health"]},
        headers=_headers(csrf, "exclude-2", saved.json()["etag"]),
    )
    assert changed.status_code == 200
    assert client.get(URL).json()["excluded_domain_ids"] == ["medicine_health"]


def test_only_active_primary_domains_can_be_excluded(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    csrf = _login(client)
    etag = client.get(URL).json()["etag"]
    for domain_id in ("unassigned", "health_wellbeing", "technology.ai"):
        response = client.put(
            URL,
            json={"excluded_domain_ids": [domain_id]},
            headers=_headers(csrf, f"bad-{domain_id}", etag),
        )
        assert response.status_code == 422, domain_id
    assert client.get(URL).json()["excluded_domain_ids"] == []


ENGINEERING_TEXT = "软件发布前必须完成回归测试。"
ENGINEERING_QUERY = "软件 发布 回归 测试"


def _seed_engineering_knowledge(factory: sessionmaker[Session], tmp_path: Path) -> None:
    with session_scope(factory) as session:
        knowledge = KnowledgeRepository().ingest_text(
            session,
            TextEvidenceInput(
                title="软件发布规范",
                primary_domain_id="computing_engineering",
                text=ENGINEERING_TEXT,
                source_metadata={"fixture": "synthetic"},
                summary="软件发布证据",
            ),
            user_authority=KnowledgeUserAuthority("synthetic-test-user"),
            stored_artifacts=stored_text_artifacts(tmp_path / "engineering", ENGINEERING_TEXT),
        )
        mark_formal_knowledge_indexed(session, knowledge.knowledge_object_id)


def _exclude(client: TestClient, csrf: str, domain_ids: list[str]) -> None:
    etag = client.get(URL).json()["etag"]
    response = client.put(
        URL,
        json={"excluded_domain_ids": domain_ids},
        headers=_headers(csrf, f"exclude-{sha256_text(','.join(domain_ids))[:16]}", etag),
    )
    assert response.status_code == 200


def test_answer_in_excluded_domain_uses_no_personal_memory(tmp_path: Path) -> None:
    client, factory = answer_client(tmp_path)
    csrf = _login(client)
    seed(factory, tmp_path)
    _seed_engineering_knowledge(factory, tmp_path)

    personalized = client.post(
        "/v1/answers",
        json={"query": ENGINEERING_QUERY},
        headers=answer_headers(csrf, "before-exclusion"),
    ).json()
    assert personalized["personalization_refs"]
    assert personalized["personalization"] == {
        "question_domain_id": "computing_engineering",
        "question_domain_name": "计算机与工程技术",
        "matched_keywords": ["软件"],
        "method": "keyword-rule-v1",
        "applied": True,
        "reason": "domain_not_excluded",
    }

    _exclude(client, csrf, ["computing_engineering"])
    response = client.post(
        "/v1/answers",
        json={"query": ENGINEERING_QUERY},
        headers=answer_headers(csrf, "after-exclusion"),
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["stop_reason"] == "completed"
    assert payload["citations"]
    assert payload["personalization_refs"] == []
    assert payload["memory_context_digest"] is None
    assert "画像仅作背景" not in payload["answer"]
    assert payload["personalization"] == {
        "question_domain_id": "computing_engineering",
        "question_domain_name": "计算机与工程技术",
        "matched_keywords": ["软件"],
        "method": "keyword-rule-v1",
        "applied": False,
        "reason": "domain_excluded",
    }


def test_question_without_a_recognised_domain_is_personalized(tmp_path: Path) -> None:
    client, factory = answer_client(tmp_path)
    csrf = _login(client)
    seed(factory, tmp_path)
    _exclude(client, csrf, [domain.id for domain in PRIMARY_DOMAINS])

    payload = client.post(
        "/v1/answers",
        json={"query": "中文 全文 检索 正式 视图"},
        headers=answer_headers(csrf, "undetermined"),
    ).json()
    assert payload["personalization_refs"]
    assert payload["personalization"] == {
        "question_domain_id": None,
        "question_domain_name": None,
        "matched_keywords": [],
        "method": "keyword-rule-v1",
        "applied": True,
        "reason": "domain_undetermined",
    }


def test_personal_statement_in_excluded_domain_is_still_remembered(tmp_path: Path) -> None:
    client, factory = answer_client(tmp_path)
    csrf = _login(client)
    seed(factory, tmp_path)
    _exclude(client, csrf, ["computing_engineering"])
    conversation = client.post(
        "/v1/conversations", json={}, headers=answer_headers(csrf, "conversation")
    ).json()

    answer = client.post(
        "/v1/answers",
        json={"query": "我是软件工程师", "conversation_id": conversation["id"]},
        headers=answer_headers(csrf, "statement"),
    ).json()
    assert answer["personalization"]["applied"] is False
    assert answer["personalization_refs"] == []

    with session_scope(factory) as session:
        outbox = OutboxRepository()
        outbox.enqueue_jobs_for_events(session, outbox.claim_pending(session, limit=50))
    process_memory_extraction_jobs_once(
        factory, MemoryExtractionJobExecutor(), worker_id="test-worker"
    )

    formal = client.get("/v1/memory/formal").json()["items"]
    role = next(item for item in formal if item["state_key"] == "role.current")
    assert "软件工程师" in str(role["value"])


def test_factual_claims_do_not_change_with_personal_information(tmp_path: Path) -> None:
    client, factory = answer_client(tmp_path)
    csrf = _login(client)
    ids = seed(factory, tmp_path)
    _seed_engineering_knowledge(factory, tmp_path)

    def ask(key: str) -> dict[str, Any]:
        response = client.post(
            "/v1/answers", json={"query": ENGINEERING_QUERY}, headers=answer_headers(csrf, key)
        )
        assert response.status_code == 200
        return dict(response.json())

    before = ask("facts-before")
    with session_scope(factory) as session:
        MemoryRepository().commit_explicit_memory(
            session,
            MemoryValue("goal", "goal.finance", {"text": "become a release manager"}),
            operation_key="change-goal",
        )
        MemoryRepository().commit_explicit_memory(
            session,
            MemoryValue("constraint", "constraint.time", {"text": "only weekends"}),
            operation_key="add-constraint",
        )
    after = ask("facts-after")

    assert before["personalization_refs"] != after["personalization_refs"]

    def facts(payload: dict[str, Any]) -> list[tuple[str, str]]:
        return [(claim["text"], claim["kind"]) for claim in payload["claims"]]

    assert facts(before) == facts(after) == [(ENGINEERING_TEXT, "cited")]
    assert [c["source_id"] for c in before["citations"]] == [
        c["source_id"] for c in after["citations"]
    ]
    assert ids["goal_id"] in {ref["formal_memory_id"] for ref in after["personalization_refs"]}


def test_retrying_a_saved_setting_returns_the_saved_setting(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    csrf = _login(client)
    etag = client.get(URL).json()["etag"]
    body = {"excluded_domain_ids": ["medicine_health"]}
    first = client.put(URL, json=body, headers=_headers(csrf, "retry", etag))
    retry = client.put(URL, json=body, headers=_headers(csrf, "retry", etag))
    assert retry.status_code == 200
    assert retry.json() == first.json()


def test_cached_personalized_answer_is_not_replayed_after_exclusion(tmp_path: Path) -> None:
    client, factory = answer_client(tmp_path)
    csrf = _login(client)
    seed(factory, tmp_path)
    _seed_engineering_knowledge(factory, tmp_path)
    request = {"query": ENGINEERING_QUERY}
    first = client.post("/v1/answers", json=request, headers=answer_headers(csrf, "cached"))
    assert first.json()["personalization_refs"]

    _exclude(client, csrf, ["computing_engineering"])
    replay = client.post("/v1/answers", json=request, headers=answer_headers(csrf, "cached"))
    assert replay.status_code == 409
    assert "personalization" in replay.json()["detail"]


def test_excluded_domain_answer_asks_for_no_personal_context(tmp_path: Path) -> None:
    client, factory = answer_client(tmp_path)
    csrf = _login(client)
    seed(factory, tmp_path)
    question = {"query": "我现在写软件应该注意什么"}

    asked = client.post("/v1/answers", json=question, headers=answer_headers(csrf, "asks")).json()
    assert [prompt["kind"] for prompt in asked["context_prompts"]] == ["missing"]

    _exclude(client, csrf, ["computing_engineering"])
    quiet = client.post("/v1/answers", json=question, headers=answer_headers(csrf, "quiet")).json()
    assert quiet["personalization"]["applied"] is False
    assert quiet["context_prompts"] == []
