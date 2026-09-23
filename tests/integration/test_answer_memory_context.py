"""Real API consumers keep formal personalization separate from knowledge evidence."""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.orm import Session

from tests.integration.test_g005_api_impl import _client, _headers, _login, _seed
from zhiheng.api.retrieval import EvidenceBoundAnswerModel
from zhiheng.memory import MemoryCandidateInput, MemoryRepository, MemoryValue
from zhiheng.memory.context import MemoryContextService, MemoryContextSnapshot
from zhiheng.query.contracts import GeneratedAnswer

QUERY = "中文 全文 检索 正式 视图"


class _RecordingProductionModel(EvidenceBoundAnswerModel):
    def __init__(self, after_generate: Callable[[], None] | None = None) -> None:
        self.contexts: list[MemoryContextSnapshot] = []
        self._after_generate = after_generate

    def generate_answer(self, **kwargs: Any) -> GeneratedAnswer:
        context = kwargs["memory_context"]
        assert isinstance(context, MemoryContextSnapshot)
        self.contexts.append(context)
        result = super().generate_answer(**kwargs)
        if self._after_generate is not None:
            self._after_generate()
        return result


def test_production_answer_consumes_formal_context_and_keeps_citations_separate(
    tmp_path: Path,
) -> None:
    client, factory = _client(tmp_path)
    csrf = _login(client)
    ids = _seed(factory, tmp_path)
    with factory.begin() as session:
        topic = MemoryRepository().commit_explicit_memory(
            session,
            MemoryValue("preference", "style.answer", {"text": "synthetic formal L1"}),
            operation_key="synthetic-topic",
        )
        for state_key in ("goal.finance", "identity.pending"):
            MemoryRepository().propose_candidate(
                session,
                MemoryCandidateInput(
                    candidate_type="inferred",
                    memory_type="goal",
                    state_key=state_key,
                    proposed_value={"text": "unconfirmed-private-sentinel"},
                    rationale="synthetic isolation probe",
                    source_kind="agent_inferred",
                    confidence=0.7,
                ),
            )
    assert isinstance(client.app, FastAPI)
    model = _RecordingProductionModel()
    client.app.state.query_answer_service._rag._model_gateway = model
    response = client.post(
        "/v1/answers",
        json={"query": QUERY, "memory_topic_prefix": "style."},
        headers=_headers(csrf, "context-positive"),
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["stop_reason"] == "completed"
    assert len(model.contexts) == 1
    serialized_context = json.dumps(model.contexts[0].canonical_payload())
    assert "synthetic formal L1" in serialized_context
    assert "learn quantitative finance" in serialized_context
    assert "unconfirmed-private-sentinel" not in serialized_context
    assert {ref["formal_memory_id"] for ref in payload["personalization_refs"]} == {
        ids["goal_id"],
        topic.formal_memory_id,
    }
    assert payload["citations"]
    assert {citation["source_type"] for citation in payload["citations"]} == {"knowledge_object"}
    assert "画像仅作背景" in payload["answer"]
    with factory() as session:
        processes = session.execute(text("SELECT process_json FROM task_evaluations")).all()
    serialized_processes = "\n".join(str(row[0]) for row in processes)
    assert ids["goal_id"] in serialized_processes
    assert "synthetic formal L1" not in serialized_processes
    assert "learn quantitative finance" not in serialized_processes
    assert "unconfirmed-private-sentinel" not in serialized_processes
    assert "unconfirmed-private-sentinel" not in response.text


def test_new_constraint_after_context_load_blocks_model_entry(tmp_path: Path) -> None:
    class _ChangedAfterLoad(MemoryContextService):
        def load(self, session: Session, **kwargs: Any) -> MemoryContextSnapshot:
            snapshot = super().load(session, **kwargs)
            MemoryRepository().commit_explicit_memory(
                session,
                MemoryValue("constraint", "constraint.new", {"text": "new constraint"}),
                operation_key="new-constraint-before-model",
            )
            return snapshot

    client, factory = _client(tmp_path)
    csrf = _login(client)
    _seed(factory, tmp_path)
    assert isinstance(client.app, FastAPI)
    service = client.app.state.query_answer_service
    service._memory_context_service = _ChangedAfterLoad()
    model = _RecordingProductionModel()
    service._rag._model_gateway = model
    response = client.post(
        "/v1/answers",
        json={"query": QUERY},
        headers=_headers(csrf, "context-before-change"),
    )
    assert response.status_code == 200
    assert response.json()["stop_reason"] == "memory_context_changed"
    assert response.json()["budget_usage"]["model_calls"] == 0
    assert not model.contexts


def test_memory_deleted_during_model_discards_output(tmp_path: Path) -> None:
    client, factory = _client(tmp_path)
    csrf = _login(client)
    ids = _seed(factory, tmp_path)

    def delete_memory() -> None:
        with factory.begin() as session:
            MemoryRepository().soft_delete(
                session,
                formal_memory_id=ids["goal_id"],
                operation_key="delete-during-model",
            )

    assert isinstance(client.app, FastAPI)
    model = _RecordingProductionModel(delete_memory)
    client.app.state.query_answer_service._rag._model_gateway = model
    response = client.post(
        "/v1/answers",
        json={"query": QUERY},
        headers=_headers(csrf, "context-after-change"),
    )
    payload = response.json()
    assert response.status_code == 200
    assert len(model.contexts) == 1
    assert payload["stop_reason"] == "memory_context_changed"
    assert payload["personalization_refs"] == []
    assert "画像仅作背景" not in payload["answer"]


def test_answer_receipt_cannot_replay_outdated_personalization(tmp_path: Path) -> None:
    client, factory = _client(tmp_path)
    csrf = _login(client)
    _seed(factory, tmp_path)
    headers = _headers(csrf, "context-replay")
    first = client.post("/v1/answers", json={"query": QUERY}, headers=headers)
    assert first.status_code == 200
    assert first.json()["personalization_refs"]
    with factory.begin() as session:
        MemoryRepository().commit_explicit_memory(
            session,
            MemoryValue("goal", "goal.finance", {"text": "changed formal goal"}),
            operation_key="change-after-answer",
        )
    replay = client.post("/v1/answers", json={"query": QUERY}, headers=headers)
    assert replay.status_code == 409
    assert "context changed" in replay.json()["detail"]


def test_api_uses_same_memory_policy_for_answer_validation_and_replay(tmp_path: Path) -> None:
    client, factory = _client(tmp_path)
    csrf = _login(client)
    _seed(factory, tmp_path)
    assert isinstance(client.app, FastAPI)
    app = client.app
    service = app.state.memory_context_service
    assert app.state.query_answer_service._memory_context_service is service
    assert app.state.query_answer_service._rag._memory_context_service is service
    original = service.load
    calls: list[str] = []

    def observed_load(session: Session, **kwargs: Any) -> MemoryContextSnapshot:
        calls.append(kwargs["query_hash"])
        snapshot = original(session, **kwargs)
        assert isinstance(snapshot, MemoryContextSnapshot)
        return snapshot

    service.load = observed_load
    headers = _headers(csrf, "same-context-policy")
    first = client.post("/v1/answers", json={"query": QUERY}, headers=headers)
    assert first.status_code == 200
    initial_calls = len(calls)
    assert initial_calls >= 3  # load, pre-generation, post-generation
    replay = client.post("/v1/answers", json={"query": QUERY}, headers=headers)
    assert replay.status_code == 200
    assert replay.json() == first.json()
    assert len(calls) == initial_calls + 1
