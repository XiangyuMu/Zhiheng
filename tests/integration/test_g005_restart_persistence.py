from __future__ import annotations

from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from tests.knowledge_helpers import stored_text_artifacts
from zhiheng.api.main import create_app
from zhiheng.core.config import Settings
from zhiheng.db.session import create_session_factory, create_sqlite_engine, session_scope
from zhiheng.knowledge import KnowledgeRepository, KnowledgeUserAuthority, TextEvidenceInput
from zhiheng.memory import MemoryRepository, MemoryValue


def _upgrade(db_path: Path) -> None:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")


def _settings(db_path: Path) -> Settings:
    return Settings(environment="test", database_url=f"sqlite:///{db_path}")


def _client(db_path: Path) -> TestClient:
    return TestClient(create_app(_settings(db_path)))


def _session_factory(db_path: Path) -> sessionmaker[Session]:
    return create_session_factory(create_sqlite_engine(_settings(db_path)))


def _login(client: TestClient) -> str:
    response = client.post(
        "/auth/login",
        json={"username": "solo_user", "password": "correct horse battery staple"},
    )
    assert response.status_code == 200
    return str(response.json()["csrf_token"])


def _bootstrap(client: TestClient) -> str:
    response = client.post(
        "/auth/bootstrap",
        json={"username": "solo_user", "password": "correct horse battery staple"},
    )
    assert response.status_code == 200
    return str(response.json()["csrf_token"])


def _headers(csrf: str, key: str) -> dict[str, str]:
    return {"X-CSRF-Token": csrf, "Idempotency-Key": key}


def _seed(session_factory: sessionmaker[Session], tmp_path: Path) -> dict[str, Any]:
    knowledge_text = "学习量化投资需要先建立风险控制知识，再比较策略选择。"
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
        KnowledgeRepository().ingest_text(
            session,
            TextEvidenceInput(
                title="量化风险控制",
                primary_domain_id="finance.quant",
                text=knowledge_text,
                source_metadata={"fixture": "synthetic"},
                summary="量化风险控制证据",
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
        "goal_generation": goal.generation,
    }


def test_decision_save_recovers_analysis_from_db_after_app_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "zhiheng.db"
    _upgrade(db_path)
    session_factory = _session_factory(db_path)
    app_a = _client(db_path)
    csrf_a = _bootstrap(app_a)
    _seed(session_factory, tmp_path)

    analyzed = app_a.post(
        "/v1/decisions/analyze",
        json={
            "problem": "是否学习量化投资？",
            "options": [{"label": "learn", "description": "每周学习"}],
            "formal_goal_refs": ["goal.finance"],
            "evidence_query": "学习 量化 风险 控制",
        },
        headers=_headers(csrf_a, "decision-analyze"),
    )
    assert analyzed.status_code == 200
    run_id = analyzed.json()["run_id"]

    app_b = _client(db_path)
    csrf_b = _login(app_b)
    saved = app_b.post(
        f"/v1/decisions/{run_id}/save",
        json={},
        headers=_headers(csrf_b, "decision-save-after-restart"),
    )

    assert saved.status_code == 200
    assert saved.json()["external_action_count"] == 0
    with session_scope(session_factory) as session:
        row = (
            session.execute(
                text(
                    """
                SELECT prompt_hash, recommendation_json, review_json
                FROM decision_support_runs
                WHERE id = :id
                """
                ),
                {"id": run_id},
            )
            .mappings()
            .one()
        )
    serialized = f"{row['prompt_hash']} {row['recommendation_json']} {row['review_json']}"
    assert "是否学习量化投资" not in serialized
    assert "citation_refs" in serialized


def test_gap_list_and_dismiss_are_db_persistent_after_app_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "zhiheng.db"
    _upgrade(db_path)
    session_factory = _session_factory(db_path)
    app_a = _client(db_path)
    csrf_a = _bootstrap(app_a)
    ids = _seed(session_factory, tmp_path)

    refreshed = app_a.post(
        "/v1/knowledge-gaps/refresh",
        json={
            "goal": {
                "formal_memory_id": ids["goal_id"],
                "formal_version_id": ids["goal_version_id"],
                "state_key": "goal.finance",
                "effective_generation": ids["goal_generation"],
            },
            "domain_id": "finance.quant",
            "reason_code": "missing_material",
            "missing_coverage": ["risk control"],
            "suggested_search_terms": ["量化 风险控制"],
        },
        headers=_headers(csrf_a, "gap-refresh"),
    )
    assert refreshed.status_code == 200
    gap_id = refreshed.json()["items"][0]["id"]

    app_b = _client(db_path)
    csrf_b = _login(app_b)
    listed = app_b.get(f"/v1/knowledge-gaps?goal_id={ids['goal_id']}")
    dismissed = app_b.post(
        f"/v1/knowledge-gaps/{gap_id}/dismiss",
        json={"reason": "not now"},
        headers=_headers(csrf_b, "gap-dismiss-after-restart"),
    )

    app_c = _client(db_path)
    csrf_c = _login(app_c)
    listed_after = app_c.get(f"/v1/knowledge-gaps?goal_id={ids['goal_id']}")
    replay = app_c.post(
        f"/v1/knowledge-gaps/{gap_id}/dismiss",
        json={"reason": "not now"},
        headers=_headers(csrf_c, "gap-dismiss-after-restart"),
    )

    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()["items"]] == [gap_id]
    assert dismissed.status_code == 200
    assert listed_after.status_code == 200
    assert listed_after.json()["items"] == []
    assert replay.status_code == 200
    assert replay.json()["id"] == gap_id


def test_gap_list_hides_old_goal_generation_after_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "zhiheng.db"
    _upgrade(db_path)
    session_factory = _session_factory(db_path)
    app_a = _client(db_path)
    csrf_a = _bootstrap(app_a)
    ids = _seed(session_factory, tmp_path)

    refreshed = app_a.post(
        "/v1/knowledge-gaps/refresh",
        json={
            "goal": {
                "formal_memory_id": ids["goal_id"],
                "formal_version_id": ids["goal_version_id"],
                "state_key": "goal.finance",
                "effective_generation": ids["goal_generation"],
            },
            "domain_id": "finance.quant",
            "reason_code": "missing_material",
            "missing_coverage": ["position sizing"],
        },
        headers=_headers(csrf_a, "gap-refresh-before-goal-update"),
    )
    assert refreshed.status_code == 200

    with session_scope(session_factory) as session:
        MemoryRepository().commit_explicit_memory(
            session,
            MemoryValue(
                memory_type="goal",
                state_key="goal.finance",
                value={"text": "learn quantitative finance with updated priority"},
            ),
            operation_key="update-goal",
        )

    app_b = _client(db_path)
    _login(app_b)
    listed = app_b.get(f"/v1/knowledge-gaps?goal_id={ids['goal_id']}")

    assert listed.status_code == 200
    assert listed.json()["items"] == []
