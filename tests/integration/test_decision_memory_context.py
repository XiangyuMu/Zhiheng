from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from tests.integration.test_g005_api_impl import _headers, _login, _seed
from zhiheng.api.main import create_app
from zhiheng.core.config import Settings
from zhiheng.db.session import create_session_factory, create_sqlite_engine, session_scope
from zhiheng.memory import MemoryCandidateInput, MemoryRepository, MemoryValue
from zhiheng.memory.context import MemoryContextSnapshot
from zhiheng.query import AnswerClaim, GeneratedAnswer
from zhiheng.query.contracts import PersonalizationRef
from zhiheng.retrieval.contracts import AuthorizedContextManifest, Citation

EVIDENCE_QUERY = "中文 全文 检索 正式 视图"


class _DecisionRecordingModel:
    def __init__(self, after_generate: Callable[[], None] | None = None) -> None:
        self.queries: list[str] = []
        self.contexts: list[MemoryContextSnapshot] = []
        self._after_generate = after_generate

    def generate_answer(
        self,
        *,
        query: str,
        manifest: AuthorizedContextManifest,
        citations: Sequence[Citation],
        max_output_tokens: int | None = None,
        memory_context: MemoryContextSnapshot | None = None,
    ) -> GeneratedAnswer:
        del manifest, max_output_tokens
        assert memory_context is not None
        self.queries.append(query)
        self.contexts.append(memory_context)
        refs = tuple(
            PersonalizationRef(
                entry.formal_memory_id,
                entry.formal_version_id,
                entry.confirmation_generation,
                entry.state_key,
            )
            for entry in memory_context.entries
        )
        if self._after_generate is not None:
            self._after_generate()
        return GeneratedAnswer(
            answer="建议选择学习路径，但只基于已授权证据推进下一步。",
            claims=(AnswerClaim("正式证据支持先建立风险控制知识", (citations[0].citation_id,)),),
            personalization_refs=refs,
            output_tokens=12,
        )


def _session_factory(db_path: Path) -> sessionmaker[Session]:
    settings = Settings(environment="test", database_url=f"sqlite:///{db_path}")
    return create_session_factory(create_sqlite_engine(settings))


def _client(db_path: Path) -> tuple[TestClient, sessionmaker[Session]]:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    settings = Settings(environment="test", database_url=f"sqlite:///{db_path}")
    return TestClient(create_app(settings)), _session_factory(db_path)


def _restart_client(db_path: Path) -> TestClient:
    settings = Settings(environment="test", database_url=f"sqlite:///{db_path}")
    return TestClient(create_app(settings))


def _install_model(client: TestClient, model: _DecisionRecordingModel) -> None:
    assert isinstance(client.app, FastAPI)
    rag = client.app.state.bounded_rag_service
    rag._model_gateway = model


def _decision_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "problem": "是否学习量化投资？",
        "options": [
            {"label": "learn", "description": "每周学习风险控制和仓位管理"},
            {"label": "wait", "description": "毕业后再系统学习"},
        ],
        "formal_goal_refs": ["goal.finance"],
        "evidence_query": EVIDENCE_QUERY,
        "memory_topic_prefix": "style.",
    }
    payload.update(overrides)
    return payload


def _add_style_memory(factory: sessionmaker[Session]) -> str:
    with session_scope(factory) as session:
        result = MemoryRepository().commit_explicit_memory(
            session,
            MemoryValue(
                memory_type="preference",
                state_key="style.answer",
                value={"text": "prefer concise decision advice"},
            ),
            operation_key="seed-style-answer",
        )
        assert result.formal_memory_id is not None
        MemoryRepository().propose_candidate(
            session,
            MemoryCandidateInput(
                candidate_type="inferred",
                memory_type="goal",
                state_key="goal.fashion",
                proposed_value={"text": "candidate-raw-sentinel"},
                rationale="isolation",
                source_kind="agent_inferred",
                confidence=0.8,
            ),
        )
        return result.formal_memory_id


def test_decision_api_consumes_formal_memory_and_keeps_candidate_values_out(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "zhiheng.db"
    client, factory = _client(db_path)
    csrf = _login(client)
    ids = _seed(factory, tmp_path)
    style_id = _add_style_memory(factory)
    model = _DecisionRecordingModel()
    _install_model(client, model)

    response = client.post(
        "/v1/decisions/analyze",
        json=_decision_payload(),
        headers=_headers(csrf, "decision-memory-positive"),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["stop_reason"] == "completed"
    assert body["recommended_next_step"] == body["recommendation"]
    assert len(body["option_reviews"]) == 2
    assert body["memory_context_digest"]
    assert set(body["memory_source_ids"]) == {ids["goal_id"], style_id}
    assert {ref["formal_memory_id"] for ref in body["personalization_refs"]} == {
        ids["goal_id"],
        style_id,
    }
    assert len(model.queries) == 1
    assert "每周学习风险控制和仓位管理" in model.queries[0]
    assert "goal.finance" in model.queries[0]
    for entry in model.contexts[0].entries:
        assert entry.formal_memory_id not in model.queries[0]
        assert entry.formal_version_id not in model.queries[0]
    serialized_context = json.dumps(model.contexts[0].canonical_payload(), ensure_ascii=False)
    assert "prefer concise decision advice" in serialized_context
    assert "learn quantitative finance" in serialized_context
    assert "candidate-raw-sentinel" not in serialized_context
    assert "candidate-raw-sentinel" not in response.text


def test_decision_api_rejects_candidate_or_absent_goal_ref(tmp_path: Path) -> None:
    db_path = tmp_path / "zhiheng.db"
    client, factory = _client(db_path)
    csrf = _login(client)
    _seed(factory, tmp_path)
    _add_style_memory(factory)
    model = _DecisionRecordingModel()
    _install_model(client, model)

    response = client.post(
        "/v1/decisions/analyze",
        json=_decision_payload(formal_goal_refs=["goal.fashion"]),
        headers=_headers(csrf, "decision-candidate-goal"),
    )

    assert response.status_code == 409
    assert "formal goal ref" in response.json()["detail"]
    assert model.queries == []


def test_decision_discards_model_output_when_memory_changes_during_generation(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "zhiheng.db"
    client, factory = _client(db_path)
    csrf = _login(client)
    _seed(factory, tmp_path)
    _add_style_memory(factory)

    def mutate_memory() -> None:
        with session_scope(factory) as session:
            MemoryRepository().commit_explicit_memory(
                session,
                MemoryValue(
                    memory_type="constraint",
                    state_key="constraint.new",
                    value={"text": "new constraint during decision"},
                ),
                operation_key="decision-context-change-during-model",
            )

    model = _DecisionRecordingModel(after_generate=mutate_memory)
    _install_model(client, model)

    response = client.post(
        "/v1/decisions/analyze",
        json=_decision_payload(),
        headers=_headers(csrf, "decision-memory-mutates"),
    )

    assert response.status_code == 409
    assert "memory changed before publication" in response.json()["detail"]
    assert len(model.queries) == 1
    assert "建议选择学习路径" not in response.text
    with session_scope(factory) as session:
        assert session.execute(text("SELECT count(*) FROM decision_support_runs")).scalar_one() == 0


def test_decision_analyze_replay_rejects_changed_memory(tmp_path: Path) -> None:
    db_path = tmp_path / "zhiheng.db"
    client, factory = _client(db_path)
    csrf = _login(client)
    _seed(factory, tmp_path)
    _add_style_memory(factory)
    _install_model(client, _DecisionRecordingModel())
    headers = _headers(csrf, "decision-stale-analyze")

    first = client.post("/v1/decisions/analyze", json=_decision_payload(), headers=headers)
    assert first.status_code == 200
    with session_scope(factory) as session:
        MemoryRepository().commit_explicit_memory(
            session,
            MemoryValue("goal", "goal.finance", {"text": "changed formal goal"}),
            operation_key="decision-change-before-replay",
        )

    replay = client.post("/v1/decisions/analyze", json=_decision_payload(), headers=headers)

    assert replay.status_code == 409
    assert "context changed" in replay.json()["detail"]


def test_decision_save_replays_saved_target_but_new_save_rejects_stale_analysis(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "zhiheng.db"
    client, factory = _client(db_path)
    csrf = _login(client)
    _seed(factory, tmp_path)
    _add_style_memory(factory)
    _install_model(client, _DecisionRecordingModel())
    analyzed = client.post(
        "/v1/decisions/analyze",
        json=_decision_payload(),
        headers=_headers(csrf, "decision-save-stale-analyze"),
    )
    assert analyzed.status_code == 200
    run_id = analyzed.json()["run_id"]
    first_save = client.post(
        f"/v1/decisions/{run_id}/save",
        json={"note": "first save note"},
        headers=_headers(csrf, "decision-save-stale"),
    )
    assert first_save.status_code == 200
    with session_scope(factory) as session:
        count_after_first_save = session.execute(
            text("SELECT count(*) FROM formal_memories")
        ).scalar_one()
    with session_scope(factory) as session:
        MemoryRepository().commit_explicit_memory(
            session,
            MemoryValue("goal", "goal.finance", {"text": "changed before save replay"}),
            operation_key="decision-change-before-save-replay",
        )

    replay = client.post(
        f"/v1/decisions/{run_id}/save",
        json={"note": "first save note"},
        headers=_headers(csrf, "decision-save-stale"),
    )
    fresh_key = client.post(
        f"/v1/decisions/{run_id}/save",
        json={"note": "new save note"},
        headers=_headers(csrf, "decision-save-stale-new-key"),
    )

    assert replay.status_code == 200
    assert replay.json() == first_save.json()
    assert fresh_key.status_code == 409
    with session_scope(factory) as session:
        count_after_replay = session.execute(
            text("SELECT count(*) FROM formal_memories")
        ).scalar_one()
    assert count_after_replay == count_after_first_save


def test_decision_save_persists_note_and_bindings_after_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "zhiheng.db"
    client, factory = _client(db_path)
    csrf = _login(client)
    _seed(factory, tmp_path)
    style_id = _add_style_memory(factory)
    _install_model(client, _DecisionRecordingModel())
    analyzed = client.post(
        "/v1/decisions/analyze",
        json=_decision_payload(),
        headers=_headers(csrf, "decision-restart-analyze"),
    )
    assert analyzed.status_code == 200
    run_id = analyzed.json()["run_id"]

    restarted = _restart_client(db_path)
    csrf_after_restart = _login(restarted)
    _install_model(restarted, _DecisionRecordingModel())
    saved = restarted.post(
        f"/v1/decisions/{run_id}/save",
        json={"note": "复盘时保留这个备注"},
        headers=_headers(csrf_after_restart, "decision-restart-save"),
    )

    assert saved.status_code == 200
    with session_scope(factory) as session:
        row = (
            session.execute(
                text(
                    """
                SELECT memory_type, value_json
                FROM current_formal_memory
                WHERE state_key = :state_key
                """
                ),
                {"state_key": f"decision.{run_id}"},
            )
            .mappings()
            .one()
        )
    value = json.loads(row["value_json"])
    assert row["memory_type"] == "decision"
    assert value["note"] == "复盘时保留这个备注"
    assert style_id in value["memory_source_ids"]
    assert value["memory_context_digest"]
    assert value["citation_replay_digest"]
