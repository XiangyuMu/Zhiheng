from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from tests.integration.test_auth_and_model_gateway import _insert_provider
from tests.integration.test_g005_api_impl import _headers, _login, _seed
from zhiheng.api.main import create_app
from zhiheng.core.config import Settings
from zhiheng.core.ids import sha256_text
from zhiheng.db.session import create_session_factory, create_sqlite_engine, session_scope
from zhiheng.memory import MemoryCandidateInput, MemoryRepository
from zhiheng.models._transports import TransportResponse, TransportRoute, _ApprovedOutboundPayload
from zhiheng.privacy.gateway import DeterministicPatternAnalyzer, PrivacyPipeline


@dataclass
class _DecisionJsonTransport:
    calls: list[str]

    def complete(
        self,
        *,
        route: TransportRoute,
        payload: _ApprovedOutboundPayload,
    ) -> TransportResponse:
        del route
        self.calls.append(payload.text)
        prompt = json.loads(payload.text)
        citation_id = prompt["KNOWLEDGE_EVIDENCE"][0]["citation_ids"][0]
        memory_ref_ids = [
            entry["provenance"]["memory_ref_id"]
            for entry in prompt["USER_CONFIRMED_CONTEXT"]["entries"]
        ]
        response = {
            "answer": "网关决策建议已基于授权证据和目标上下文生成。",
            "claims": [{"text": "授权证据支持继续学习", "citation_ids": [citation_id]}],
            "conflicts": [],
            "assumptions": ["仅提供建议"],
            "insufficiencies": [],
            "output_tokens": 9,
            "personalization_refs": memory_ref_ids,
        }
        return TransportResponse(
            text=json.dumps(response, ensure_ascii=False),
            response_hash=sha256_text(payload.text),
        )


def _gateway_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[TestClient, sessionmaker[Session], _DecisionJsonTransport]:
    monkeypatch.setenv("ZHIHENG_PRIVATE_TEST_SECRET", "secret-value")
    db_path = tmp_path / "zhiheng.db"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    settings = Settings(
        environment="test",
        database_url=f"sqlite:///{db_path}",
        external_models_enabled=True,
        answer_provider_id="provider-openai",
        answer_model_id="gpt-test",
    )
    session_factory = create_session_factory(create_sqlite_engine(settings))
    app = create_app(settings)
    transport = _DecisionJsonTransport(calls=[])
    app.state.model_gateway._privacy_pipeline = PrivacyPipeline(
        analyzer=DeterministicPatternAnalyzer()
    )
    app.state.model_gateway._transports = {"openai-compatible": transport}
    return TestClient(app), session_factory, transport


def test_decision_api_gateway_payload_uses_opaque_memory_aliases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, session_factory, transport = _gateway_client(tmp_path, monkeypatch)
    csrf = _login(client)
    ids = _seed(session_factory, tmp_path)
    with session_scope(session_factory) as session:
        _insert_provider(session)
        MemoryRepository().propose_candidate(
            session,
            MemoryCandidateInput(
                candidate_type="inferred",
                memory_type="goal",
                state_key="goal.pending",
                proposed_value={"text": "candidate-memory-sentinel"},
                rationale="gateway payload candidate isolation",
                source_kind="agent_inferred",
                confidence=0.8,
            ),
        )

    response = client.post(
        "/v1/decisions/analyze",
        json={
            "problem": "是否学习量化投资？",
            "options": [{"label": "learn", "description": "每周学习风险控制"}],
            "formal_goal_refs": ["goal.finance"],
            "evidence_query": "中文 全文 检索 正式 视图",
        },
        headers=_headers(csrf, "decision-gateway-payload"),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["stop_reason"] == "completed"
    assert body["claims"]
    assert body["citations"]
    prompt = json.loads(transport.calls[0])
    outbound = transport.calls[0]
    assert len(transport.calls) == 1
    assert prompt["prompt_version"] == "gateway-answer-model-v3"
    assert "goal.finance" in prompt["user_query"]
    assert ids["goal_id"] not in prompt["user_query"]
    assert ids["goal_version_id"] not in prompt["user_query"]
    assert ids["goal_id"] not in outbound
    assert ids["goal_version_id"] not in outbound
    assert "candidate-memory-sentinel" not in outbound
    memory_aliases = [
        entry["provenance"]["memory_ref_id"]
        for entry in prompt["USER_CONFIRMED_CONTEXT"]["entries"]
    ]
    assert memory_aliases and all(alias.startswith("memory-") for alias in memory_aliases)
    assert body["personalization_refs"][0]["formal_memory_id"] == ids["goal_id"]
    assert body["claims"][0]["citation_ids"] == [body["citations"][0]["citation_id"]]

    with session_scope(session_factory) as session:
        row = session.execute(
            text(
                """
                SELECT recommendation_json
                FROM decision_support_runs
                WHERE id = :id
                """
            ),
            {"id": body["run_id"]},
        ).scalar_one()
    persisted = json.loads(row)
    assert persisted["claims"][0]["citation_ids"] == [body["citations"][0]["citation_id"]]
