from __future__ import annotations

from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from tests.knowledge_helpers import stored_text_artifacts
from zhiheng.api.main import create_app
from zhiheng.core.config import Settings
from zhiheng.db.session import create_session_factory, create_sqlite_engine, session_scope
from zhiheng.knowledge import KnowledgeRepository, KnowledgeUserAuthority, TextEvidenceInput
from zhiheng.memory import MemoryRepository, MemoryValue
from zhiheng.query import AnswerEnvelope, BudgetUsage, StopReason
from zhiheng.retrieval import QueryRoute


def _client(tmp_path: Path) -> tuple[TestClient, sessionmaker[Session]]:
    db_path = tmp_path / "zhiheng.db"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    settings = Settings(environment="test", database_url=f"sqlite:///{db_path}")
    session_factory = create_session_factory(create_sqlite_engine(settings))
    app = create_app(settings)
    return TestClient(app), session_factory


def _login(client: TestClient) -> str:
    response = client.post(
        "/auth/bootstrap",
        json={"username": "solo_user", "password": "correct horse battery staple"},
    )
    assert response.status_code == 200
    return str(response.json()["csrf_token"])


def _headers(csrf: str, key: str) -> dict[str, str]:
    return {"X-CSRF-Token": csrf, "Idempotency-Key": key}


def _seed(session_factory: sessionmaker[Session], tmp_path: Path) -> dict[str, str]:
    knowledge_text = "中文全文检索必须回查正式视图后，才能进入回答上下文。"
    artifacts = stored_text_artifacts(tmp_path, knowledge_text)
    with session_scope(session_factory) as session:
        goal = MemoryRepository().commit_explicit_memory(
            session,
            MemoryValue(
                memory_type="goal",
                state_key="goal.finance",
                value={"text": "learn quantitative finance"},
            ),
            operation_key="seed-goal",
        )
        knowledge = KnowledgeRepository().ingest_text(
            session,
            TextEvidenceInput(
                title="中文检索规范",
                primary_domain_id="technology.ai",
                text=knowledge_text,
                source_metadata={"fixture": "synthetic"},
                summary="中文检索证据",
            ),
            user_authority=KnowledgeUserAuthority("synthetic-test-user"),
            stored_artifacts=artifacts,
        )
    assert goal.formal_memory_id is not None
    assert goal.formal_version_id is not None
    assert goal.generation is not None
    return {
        "goal_id": goal.formal_memory_id,
        "goal_version_id": goal.formal_version_id,
        "goal_generation": str(goal.generation),
        "knowledge_id": knowledge.knowledge_object_id,
    }


def _formal_count(session_factory: sessionmaker[Session]) -> int:
    with session_scope(session_factory) as session:
        return int(session.execute(text("SELECT count(*) FROM formal_memories")).scalar_one())


def test_g005_query_lookup_and_answer_api_contracts(tmp_path: Path) -> None:
    client, session_factory = _client(tmp_path)
    csrf = _login(client)
    ids = _seed(session_factory, tmp_path)

    assert client.get("/v1/lookups/memory/goal.finance").status_code == 200
    assert client.get(f"/v1/lookups/knowledge/{ids['knowledge_id']}").status_code == 200

    query = "中文 全文 检索 正式 视图"
    unauthenticated = TestClient(client.app).post("/v1/answers", json={"query": query})
    no_csrf = client.post(
        "/v1/answers",
        json={"query": query},
        headers={"Idempotency-Key": "answer-no-csrf"},
    )
    extra = client.post(
        "/v1/answers",
        json={"query": query, "user_id": "attacker"},
        headers=_headers(csrf, "answer-extra"),
    )
    first = client.post(
        "/v1/answers",
        json={"query": query},
        headers=_headers(csrf, "answer-same"),
    )
    replay = client.post(
        "/v1/answers",
        json={"query": query},
        headers=_headers(csrf, "answer-same"),
    )
    conflict = client.post(
        "/v1/answers",
        json={"query": "不同问题"},
        headers=_headers(csrf, "answer-same"),
    )

    payload = first.json()
    assert unauthenticated.status_code == 401
    assert no_csrf.status_code == 403
    assert extra.status_code == 422
    assert first.status_code == 200
    assert replay.status_code == 200
    assert conflict.status_code == 409
    assert payload == replay.json()
    assert payload["route"]["route"] == "hybrid"
    assert payload["citations"]
    assert payload["claims"]
    assert payload["budget_usage"]["retrieval_calls"] == 1


def test_g005_decision_api_requires_explicit_save_and_never_executes_external_action(
    tmp_path: Path,
) -> None:
    client, session_factory = _client(tmp_path)
    csrf = _login(client)
    _seed(session_factory, tmp_path)
    before = _formal_count(session_factory)

    analyzed = client.post(
        "/v1/decisions/analyze",
        json={
            "problem": "是否学习量化投资？",
            "options": [{"label": "learn", "description": "每周学习"}],
            "formal_goal_refs": ["goal.finance"],
            "evidence_query": "中文 全文 检索 正式 视图",
        },
        headers=_headers(csrf, "decision-analyze"),
    )
    assert analyzed.status_code == 200
    body = analyzed.json()
    assert body["external_action_count"] == 0
    assert _formal_count(session_factory) == before

    saved = client.post(
        f"/v1/decisions/{body['run_id']}/save",
        json={},
        headers=_headers(csrf, "decision-save"),
    )

    assert saved.status_code == 200
    assert saved.json()["external_action_count"] == 0
    assert _formal_count(session_factory) == before + 1
    replayed = client.post(
        f"/v1/decisions/{body['run_id']}/save",
        json={},
        headers=_headers(csrf, "decision-save"),
    )
    assert replayed.status_code == 200
    assert replayed.json() == saved.json()
    assert _formal_count(session_factory) == before + 1


def test_g005_gap_api_refresh_lists_and_dismisses_without_auto_ingest(tmp_path: Path) -> None:
    client, session_factory = _client(tmp_path)
    csrf = _login(client)
    ids = _seed(session_factory, tmp_path)
    before_knowledge = _knowledge_count(session_factory)

    refreshed = client.post(
        "/v1/knowledge-gaps/refresh",
        json={
            "goal": {
                "formal_memory_id": ids["goal_id"],
                "formal_version_id": ids["goal_version_id"],
                "state_key": "goal.finance",
                "effective_generation": int(ids["goal_generation"]),
            },
            "domain_id": "finance.quant",
            "reason_code": "missing_material",
            "missing_coverage": ["risk control"],
            "suggested_search_terms": ["量化 风险控制"],
        },
        headers=_headers(csrf, "gap-refresh"),
    )
    assert refreshed.status_code == 200
    items = refreshed.json()["items"]
    assert len(items) == 1
    assert "能力不足" not in items[0]["why"]
    assert _knowledge_count(session_factory) == before_knowledge

    listed = client.get(f"/v1/knowledge-gaps?goal_id={ids['goal_id']}")
    dismissed = client.post(
        f"/v1/knowledge-gaps/{items[0]['id']}/dismiss",
        json={},
        headers=_headers(csrf, "gap-dismiss"),
    )
    listed_after = client.get(f"/v1/knowledge-gaps?goal_id={ids['goal_id']}")

    assert listed.status_code == 200
    assert len(listed.json()["items"]) == 1
    assert dismissed.status_code == 200
    assert listed_after.json()["items"] == []
    replayed = client.post(
        f"/v1/knowledge-gaps/{items[0]['id']}/dismiss",
        json={},
        headers=_headers(csrf, "gap-dismiss"),
    )
    assert replayed.status_code == 200
    assert replayed.json() == dismissed.json()


def _knowledge_count(session_factory: sessionmaker[Session]) -> int:
    with session_scope(session_factory) as session:
        return int(session.execute(text("SELECT count(*) FROM knowledge_objects")).scalar_one())


def test_g005_answer_response_has_no_extra_schema_fields(tmp_path: Path) -> None:
    client, session_factory = _client(tmp_path)
    csrf = _login(client)
    _seed(session_factory, tmp_path)

    response = client.post(
        "/v1/answers",
        json={"query": "memory:goal.finance"},
        headers=_headers(csrf, "structured-answer"),
    )
    body: dict[str, Any] = response.json()

    assert response.status_code == 200
    assert set(body) == {
        "personalization_refs",
        "memory_context_digest",
        "route",
        "answer",
        "claims",
        "citations",
        "conflicts",
        "assumptions",
        "insufficiencies",
        "stop_reason",
        "budget_usage",
        "rows",
        "context_prompts",
    }


def test_answer_api_closes_receipt_transaction_before_answer_service(
    tmp_path: Path,
) -> None:
    client, _session_factory = _client(tmp_path)
    csrf = _login(client)

    class _TransactionCheckingAnswerService:
        called = False

        def answer(self, session: Session, query: str, **kwargs: Any) -> AnswerEnvelope:
            del query, kwargs
            self.called = True
            assert not session.in_transaction()
            return AnswerEnvelope(
                answer="ok",
                claims=(),
                citations=(),
                conflicts=(),
                assumptions=(),
                insufficiencies=(),
                route=QueryRoute.HYBRID,
                stop_reason=StopReason.COMPLETED,
                budget_usage=BudgetUsage(),
            )

    service = _TransactionCheckingAnswerService()
    assert isinstance(client.app, FastAPI)
    client.app.state.query_answer_service = service

    response = client.post(
        "/v1/answers",
        json={"query": "事务边界检查"},
        headers=_headers(csrf, "answer-transaction-boundary"),
    )

    assert response.status_code == 200
    assert service.called


def test_g005_ui_assets_are_authenticated_and_render_expected_controls(
    tmp_path: Path,
) -> None:
    client, _ = _client(tmp_path)
    csrf = _login(client)

    page = client.get("/knowledge-agent")
    css = client.get("/knowledge-agent.css")
    js = client.get("/knowledge-agent.js")

    assert csrf
    assert page.status_code == 200
    assert css.status_code == 200
    assert js.status_code == 200
    assert "知识助理" in page.text
    assert "决策分析" in page.text
    assert "知识缺口" in page.text
    assert "/v1/answers" in js.text
