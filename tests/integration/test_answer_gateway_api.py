from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from tests.integration.test_auth_and_model_gateway import _insert_provider
from tests.integration.test_g005_api_impl import _headers, _login, _seed
from zhiheng.api.main import create_app
from zhiheng.api.memory import _insert_operation_receipt, _request_hash
from zhiheng.api.retrieval import AnswerRequest
from zhiheng.core.config import Settings
from zhiheng.core.ids import sha256_text
from zhiheng.db.session import create_session_factory, create_sqlite_engine, session_scope
from zhiheng.memory import MemoryCandidateInput, MemoryRepository
from zhiheng.models._transports import TransportResponse, TransportRoute, _ApprovedOutboundPayload
from zhiheng.privacy.gateway import DeterministicPatternAnalyzer, PrivacyPipeline


@dataclass
class _AnswerJsonTransport:
    calls: list[str]
    evidence_insufficient: bool = False
    invalid_response_text: str | None = None
    raised_exception: Exception | None = None

    def complete(
        self,
        *,
        route: TransportRoute,
        payload: _ApprovedOutboundPayload,
    ) -> TransportResponse:
        del route
        self.calls.append(payload.text)
        if self.raised_exception is not None:
            raise self.raised_exception
        if self.invalid_response_text is not None:
            return TransportResponse(
                text=self.invalid_response_text,
                response_hash=sha256_text(payload.text),
            )
        prompt = json.loads(payload.text)
        citation_id = prompt["KNOWLEDGE_EVIDENCE"][0]["citation_ids"][0]
        memory_ref_ids = [
            entry["provenance"]["memory_ref_id"]
            for entry in prompt["USER_CONFIRMED_CONTEXT"]["entries"]
        ]
        response = {
            "answer": "网关模型基于授权证据生成回答。",
            "answerability": "supported",
            "claims": [{"text": "授权证据可用于回答", "citation_ids": [citation_id]}],
            "conflicts": [],
            "assumptions": [],
            "insufficiencies": [],
            "output_tokens": 8,
            "personalization_refs": memory_ref_ids,
        }
        if self.evidence_insufficient:
            response.update(
                answer="材料未提供答案，证据不足。",
                answerability="insufficient",
                claims=[],
                insufficiencies=["检索到的材料不支持当前问题。"],
                personalization_refs=[],
            )
        return TransportResponse(
            text=json.dumps(response, ensure_ascii=False),
            response_hash=sha256_text(payload.text),
        )


def _gateway_client(
    tmp_path: Path,
    *,
    external_models_enabled: bool,
    raise_server_exceptions: bool = True,
) -> tuple[TestClient, sessionmaker[Session], _AnswerJsonTransport]:
    db_path = tmp_path / "zhiheng.db"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    settings = Settings(
        environment="test",
        database_url=f"sqlite:///{db_path}",
        external_models_enabled=external_models_enabled,
        answer_provider_id="provider-openai",
        answer_model_id="gpt-test",
    )
    session_factory = create_session_factory(create_sqlite_engine(settings))
    app = create_app(settings)
    transport = _AnswerJsonTransport(calls=[])
    app.state.model_gateway._privacy_pipeline = PrivacyPipeline(
        analyzer=DeterministicPatternAnalyzer()
    )
    app.state.model_gateway._transports = {"openai-compatible:chat_completions": transport}
    return (
        TestClient(app, raise_server_exceptions=raise_server_exceptions),
        session_factory,
        transport,
    )


def test_answer_api_uses_configured_model_gateway_and_counting_transport(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ZHIHENG_PRIVATE_TEST_SECRET", "secret-value")
    client, session_factory, transport = _gateway_client(
        tmp_path,
        external_models_enabled=True,
    )
    csrf = _login(client)
    ids = _seed(session_factory, tmp_path)
    with session_scope(session_factory) as session:
        MemoryRepository().propose_candidate(
            session,
            MemoryCandidateInput(
                candidate_type="inferred",
                memory_type="goal",
                state_key="goal.pending",
                proposed_value={"text": "pending-memory-sentinel"},
                rationale="synthetic pending memory isolation probe",
                source_kind="agent_inferred",
                confidence=0.8,
            ),
        )
    with session_scope(session_factory) as session:
        _insert_provider(session)

    response = client.post(
        "/v1/answers",
        json={"query": "中文 全文 检索 正式 视图"},
        headers=_headers(csrf, "answer-gateway-success"),
    )

    assert response.status_code == 200
    payload = response.json()
    prompt = json.loads(transport.calls[0])
    assert len(transport.calls) == 1
    assert prompt["prompt_version"] == "gateway-answer-model-v5"
    assert prompt["KNOWLEDGE_EVIDENCE"]
    assert prompt["USER_CONFIRMED_CONTEXT"]["entries"]
    assert "pending-memory-sentinel" not in transport.calls[0]
    assert payload["answer"] == "网关模型基于授权证据生成回答。"
    assert payload["claims"][0]["citation_ids"] == [payload["citations"][0]["citation_id"]]
    assert {ref["formal_memory_id"] for ref in payload["personalization_refs"]} == {ids["goal_id"]}
    with session_scope(session_factory) as session:
        assert session.execute(text("SELECT count(*) FROM model_call_audits")).scalar_one() == 1
        audit = session.execute(
            text("SELECT id, provider_id, model_id, status FROM model_call_audits")
        ).mappings().one()
        assert payload["answer_audit_id"] == audit["id"]
        assert payload["answer_provider_id"] == audit["provider_id"]
        assert payload["answer_model_id"] == audit["model_id"]
        assert audit["status"] == "succeeded"


def test_answer_api_insufficient_answer_does_not_present_retrieved_material_as_citations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ZHIHENG_PRIVATE_TEST_SECRET", "secret-value")
    client, session_factory, transport = _gateway_client(tmp_path, external_models_enabled=True)
    transport.evidence_insufficient = True
    csrf = _login(client)
    _seed(session_factory, tmp_path)
    with session_scope(session_factory) as session:
        _insert_provider(session)

    response = client.post(
        "/v1/answers",
        json={"query": "中文 全文 检索 正式 视图"},
        headers=_headers(csrf, "answer-gateway-insufficient"),
    )

    assert response.status_code == 200
    payload = response.json()
    assert json.loads(transport.calls[0])["KNOWLEDGE_EVIDENCE"]
    assert payload["stop_reason"] == "completed"
    assert payload["answer_audit_id"]
    assert payload["insufficiencies"]
    assert payload["claims"] == []
    assert payload["citations"] == []


def test_answer_api_external_model_disabled_returns_privacy_denied_zero_send(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ZHIHENG_PRIVATE_TEST_SECRET", "secret-value")
    client, session_factory, transport = _gateway_client(
        tmp_path,
        external_models_enabled=False,
        raise_server_exceptions=False,
    )
    csrf = _login(client)
    _seed(session_factory, tmp_path)
    with session_scope(session_factory) as session:
        _insert_provider(session)
    response = client.post(
        "/v1/answers",
        json={"query": "中文 全文 检索 正式 视图"},
        headers=_headers(csrf, "answer-gateway-disabled"),
    )

    assert response.status_code == 200
    assert response.json()["stop_reason"] == "privacy_denied"
    assert transport.calls == []
    assert "answer_audit_id" not in response.json()
    assert "answer_provider_id" not in response.json()
    assert "answer_model_id" not in response.json()
    assert "answer_safety_reason_code" not in response.json()
    with session_scope(session_factory) as session:
        assert session.execute(text("SELECT count(*) FROM model_call_audits")).scalar_one() == 0


def test_answer_api_revoke_after_claim_remains_privacy_denied(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ZHIHENG_PRIVATE_TEST_SECRET", "secret-value")
    client, session_factory, transport = _gateway_client(
        tmp_path,
        external_models_enabled=True,
    )
    csrf = _login(client)
    _seed(session_factory, tmp_path)
    with session_scope(session_factory) as session:
        _insert_provider(session)

    gateway = cast(Any, client.app).state.model_gateway
    original_revalidate = gateway._revalidate_dispatch_credential

    def revoke_then_revalidate(route: object) -> None:
        with session_scope(session_factory) as session:
            session.execute(
                text("UPDATE model_provider_configs SET enabled = 0 WHERE id = :provider_id"),
                {"provider_id": "provider-openai"},
            )
        original_revalidate(route)

    monkeypatch.setattr(gateway, "_revalidate_dispatch_credential", revoke_then_revalidate)
    response = client.post(
        "/v1/answers",
        json={"query": "中文 全文 检索 正式 视图"},
        headers=_headers(csrf, "answer-gateway-revoke-after-claim"),
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["stop_reason"] == "privacy_denied"
    assert transport.calls == []
    assert "answer_audit_id" not in payload
    assert "answer_provider_id" not in payload
    assert "answer_model_id" not in payload
    assert "answer_safety_reason_code" not in payload
    with session_scope(session_factory) as session:
        audit = session.execute(
            text("SELECT status FROM model_call_audits")
        ).scalar_one()
        assert audit == "dispatching"


def test_answer_api_transport_exception_fails_closed_with_failed_audit_and_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ZHIHENG_PRIVATE_TEST_SECRET", "secret-value")
    client, session_factory, transport = _gateway_client(
        tmp_path,
        external_models_enabled=True,
    )
    raw_provider_error = "HTTP 503 synthetic provider outage"
    transport.raised_exception = RuntimeError(raw_provider_error)
    csrf = _login(client)
    _seed(session_factory, tmp_path)
    with session_scope(session_factory) as session:
        _insert_provider(session)

    response = client.post(
        "/v1/answers",
        json={"query": "中文 全文 检索 正式 视图"},
        headers=_headers(csrf, "answer-gateway-transport-failure"),
    )
    replay = client.post(
        "/v1/answers",
        json={"query": "中文 全文 检索 正式 视图"},
        headers=_headers(csrf, "answer-gateway-transport-failure"),
    )

    assert response.status_code == 200
    assert replay.status_code == 200
    payload = response.json()
    replay_payload = replay.json()
    assert len(transport.calls) == 1
    assert payload["stop_reason"] == "model_failed"
    assert payload["answer_safety_reason_code"] == "provider_error"
    assert payload["answer"] == "仅返回已授权证据，未生成模型答案。"
    assert raw_provider_error not in response.text
    assert payload["claims"] == []
    assert payload["citations"]
    with session_scope(session_factory) as session:
        audit = session.execute(
            text(
                "SELECT id, provider_id, model_id, status, diagnostic_code "
                "FROM model_call_audits"
            )
        ).mappings().one()
        assert audit["status"] == "failed"
        assert audit["diagnostic_code"] == "provider_error"
        assert payload["answer_audit_id"] == audit["id"]
        assert payload["answer_provider_id"] == audit["provider_id"]
        assert payload["answer_model_id"] == audit["model_id"]
    assert replay_payload["answer_audit_id"] == payload["answer_audit_id"]
    assert replay_payload["answer_provider_id"] == payload["answer_provider_id"]
    assert replay_payload["answer_model_id"] == payload["answer_model_id"]
    assert replay_payload["answer_safety_reason_code"] == payload["answer_safety_reason_code"]
    assert raw_provider_error not in replay.text


def test_answer_api_invalid_model_output_fails_closed_with_audit_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ZHIHENG_PRIVATE_TEST_SECRET", "secret-value")
    client, session_factory, transport = _gateway_client(
        tmp_path,
        external_models_enabled=True,
    )
    raw_model_answer = "RAW_MODEL_ANSWER_MUST_NOT_LEAK"
    transport.invalid_response_text = json.dumps(
        {"answer": raw_model_answer, "claims": []},
        ensure_ascii=False,
    )
    csrf = _login(client)
    _seed(session_factory, tmp_path)
    with session_scope(session_factory) as session:
        _insert_provider(session)

    response = client.post(
        "/v1/answers",
        json={"query": "中文 全文 检索 正式 视图"},
        headers=_headers(csrf, "answer-gateway-invalid-output"),
    )
    replay = client.post(
        "/v1/answers",
        json={"query": "中文 全文 检索 正式 视图"},
        headers=_headers(csrf, "answer-gateway-invalid-output"),
    )

    assert response.status_code == 200
    assert replay.status_code == 200
    payload = response.json()
    replay_payload = replay.json()
    assert len(transport.calls) == 1
    assert payload["stop_reason"] == "invalid_model_output"
    assert payload["answer_safety_reason_code"] == "answer_model_invalid_output"
    assert payload["answer"] == "仅返回已授权证据，未生成模型答案。"
    assert raw_model_answer not in response.text
    assert payload["claims"] == []
    assert payload["citations"]
    with session_scope(session_factory) as session:
        audit = session.execute(
            text(
                "SELECT id, provider_id, model_id, status, diagnostic_code "
                "FROM model_call_audits"
            )
        ).mappings().one()
        assert audit["status"] == "succeeded"
        assert audit["diagnostic_code"] == "answer_model_invalid_output"
        assert payload["answer_audit_id"] == audit["id"]
        assert payload["answer_provider_id"] == audit["provider_id"]
        assert payload["answer_model_id"] == audit["model_id"]
    assert replay_payload["answer_audit_id"] == payload["answer_audit_id"]
    assert replay_payload["answer_provider_id"] == payload["answer_provider_id"]
    assert replay_payload["answer_model_id"] == payload["answer_model_id"]
    assert replay_payload["answer_safety_reason_code"] == payload["answer_safety_reason_code"]
    assert raw_model_answer not in replay.text


def test_answer_api_rejects_retry_while_existing_receipt_is_started(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ZHIHENG_PRIVATE_TEST_SECRET", "secret-value")
    client, session_factory, transport = _gateway_client(
        tmp_path,
        external_models_enabled=True,
    )
    csrf = _login(client)
    request_json = {"query": "中文 全文 检索 正式 视图"}
    operation_payload = AnswerRequest.model_validate(request_json).model_dump(mode="json")
    with session_scope(session_factory) as session:
        _insert_operation_receipt(
            session,
            "api:answers:answer-started-retry",
            "answer_question",
            _request_hash(operation_type="answer_question", payload=operation_payload),
        )

    retry = client.post(
        "/v1/answers",
        json=request_json,
        headers=_headers(csrf, "answer-started-retry"),
    )

    assert retry.status_code == 409
    assert "requires reconciliation" in retry.json()["detail"]
    assert transport.calls == []
