from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.orm import Session

from tests.integration.test_decision_memory_context import (
    _decision_payload,
    _DecisionRecordingModel,
    _install_model,
)
from tests.integration.test_g005_api_impl import _client, _headers, _login, _seed
from zhiheng.api import decisions
from zhiheng.core.ids import json_text
from zhiheng.db.session import session_scope
from zhiheng.decisions import DecisionSupportService
from zhiheng.decisions.service import begin_decision_save
from zhiheng.memory import MemoryRepository, MemoryValue
from zhiheng.query import AgenticBudget, agentic
from zhiheng.retrieval.replay import CitationReplayValidator


def test_decision_input_budget_exhaustion_is_not_server_error(tmp_path: Path) -> None:
    client, factory = _client(tmp_path)
    csrf = _login(client)
    _seed(factory, tmp_path)
    model = _DecisionRecordingModel()
    _install_model(client, model)
    assert isinstance(client.app, FastAPI)
    client.app.state.bounded_rag_service._budget = AgenticBudget(max_input_tokens=1)
    response = client.post(
        "/v1/decisions/analyze",
        json=_decision_payload(memory_topic_prefix=None),
        headers=_headers(csrf, "decision-budget"),
    )
    assert response.status_code == 200
    assert response.json()["stop_reason"] == "budget_exhausted"
    assert response.json()["budget_usage"]["model_calls"] == 0
    assert response.json()["recommendation"] is None
    assert model.queries == []


def test_failed_model_invocation_is_counted_in_response_and_persisted_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, factory = _client(tmp_path)
    csrf = _login(client)
    _seed(factory, tmp_path)
    model = _DecisionRecordingModel()
    _install_model(client, model)
    calls: list[bool] = []

    def fail_model(**kwargs: Any) -> Any:
        calls.append(True)
        raise RuntimeError("synthetic failure after invocation")

    monkeypatch.setattr(model, "generate_answer", fail_model)
    response = client.post(
        "/v1/decisions/analyze",
        json=_decision_payload(memory_topic_prefix=None),
        headers=_headers(csrf, "failed-invocation-count"),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["stop_reason"] == "model_failed"
    assert calls == [True]
    assert body["budget_usage"]["model_calls"] == 1
    with session_scope(factory) as session:
        analysis = DecisionSupportService().get_analysis(session, body["run_id"])
        assert analysis is not None and analysis.model_call_count == 1
        assert analysis.budget_usage is not None
        assert analysis.budget_usage.model_calls == 1


def test_slow_decision_model_output_is_discarded_and_elapsed_time_persists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, factory = _client(tmp_path)
    csrf = _login(client)
    _seed(factory, tmp_path)
    clock = [100.0]
    monkeypatch.setattr(agentic, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    def advance_clock() -> None:
        clock[0] += 0.050

    model = _DecisionRecordingModel(after_generate=advance_clock)
    _install_model(client, model)
    assert isinstance(client.app, FastAPI)
    client.app.state.bounded_rag_service._budget = AgenticBudget(max_wall_clock_ms=10)
    response = client.post(
        "/v1/decisions/analyze",
        json=_decision_payload(memory_topic_prefix=None),
        headers=_headers(csrf, "slow-model-budget"),
    )
    assert response.status_code == 200
    body = response.json()
    assert len(model.queries) == 1
    assert body["stop_reason"] == "budget_exhausted"
    assert body["recommendation"] is None
    assert body["budget_usage"]["wall_clock_ms"] >= 49
    with session_scope(factory) as session:
        analysis = DecisionSupportService().get_analysis(session, body["run_id"])
        assert analysis is not None and analysis.budget_usage is not None
        assert analysis.budget_usage.wall_clock_ms == body["budget_usage"]["wall_clock_ms"]


@pytest.mark.parametrize("refs", [["goal.finance", "goal.finance"], ["goal."], ["x" * 129]])
def test_decision_rejects_invalid_goal_keys_before_receipt(
    tmp_path: Path,
    refs: list[str],
) -> None:
    client, factory = _client(tmp_path)
    csrf = _login(client)
    response = client.post(
        "/v1/decisions/analyze",
        json=_decision_payload(formal_goal_refs=refs),
        headers=_headers(csrf, "invalid-goal-ref"),
    )
    assert response.status_code == 422
    with session_scope(factory) as session:
        assert (
            session.execute(
                text(
                    "SELECT count(*) FROM memory_operation_receipts "
                    "WHERE operation_type='analyze_decision'"
                )
            ).scalar_one()
            == 0
        )


def test_save_checks_and_writes_under_same_sqlite_write_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, factory = _client(tmp_path)
    csrf = _login(client)
    _seed(factory, tmp_path)
    _install_model(client, _DecisionRecordingModel())
    response = client.post(
        "/v1/decisions/analyze",
        json=_decision_payload(memory_topic_prefix=None),
        headers=_headers(csrf, "save-lock-analysis"),
    )
    assert response.status_code == 200
    run_id = response.json()["run_id"]
    with factory() as session:
        db_path = session.bind.url.database  # type: ignore[union-attr]
    assert db_path is not None
    checked: list[str] = []

    def assert_write_lock(label: str) -> None:
        with (
            sqlite3.connect(db_path, timeout=0) as contender,
            pytest.raises(sqlite3.OperationalError, match="locked"),
        ):
            contender.execute("BEGIN IMMEDIATE")
        checked.append(label)

    original_check = decisions._ensure_analysis_current

    def check(session: Session, analysis: Any, **kwargs: Any) -> None:
        assert_write_lock("validate")
        original_check(session, analysis, **kwargs)

    original_save = decisions.FormalDecisionMemorySavePort.save_decision_memory

    def save(self: Any, session: Session, analysis: Any, **kwargs: Any) -> str:
        assert_write_lock("write")
        return original_save(self, session, analysis, **kwargs)

    monkeypatch.setattr(decisions, "_ensure_analysis_current", check)
    monkeypatch.setattr(decisions.FormalDecisionMemorySavePort, "save_decision_memory", save)
    response = client.post(
        f"/v1/decisions/{run_id}/save",
        json={},
        headers=_headers(csrf, "save-locked"),
    )
    assert response.status_code == 200
    assert checked == ["validate", "write"]


def test_change_before_save_lock_is_revalidated_without_writing_decision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, factory = _client(tmp_path)
    csrf = _login(client)
    _seed(factory, tmp_path)
    _install_model(client, _DecisionRecordingModel())
    response = client.post(
        "/v1/decisions/analyze",
        json=_decision_payload(memory_topic_prefix=None),
        headers=_headers(csrf, "save-race-analysis"),
    )
    assert response.status_code == 200
    run_id = response.json()["run_id"]
    original_begin = begin_decision_save

    def change_before_lock(session: Session) -> None:
        session.commit()
        with session_scope(factory) as other:
            MemoryRepository().commit_explicit_memory(
                other,
                MemoryValue("goal", "goal.finance", {"text": "changed goal"}),
                operation_key="race-goal-change",
            )
        original_begin(session)

    monkeypatch.setattr(decisions, "begin_decision_save", change_before_lock)
    response = client.post(
        f"/v1/decisions/{run_id}/save",
        json={},
        headers=_headers(csrf, "save-race"),
    )
    assert response.status_code == 409
    with session_scope(factory) as session:
        assert (
            session.execute(
                text("SELECT count(*) FROM formal_memories WHERE memory_type='decision'")
            ).scalar_one()
            == 0
        )


def test_saved_receipt_cannot_bind_an_unrelated_current_memory(tmp_path: Path) -> None:
    client, factory = _client(tmp_path)
    csrf = _login(client)
    ids = _seed(factory, tmp_path)
    _install_model(client, _DecisionRecordingModel())
    analysis = client.post(
        "/v1/decisions/analyze",
        json=_decision_payload(memory_topic_prefix=None),
        headers=_headers(csrf, "save-binding-analysis"),
    ).json()
    run_id = analysis["run_id"]
    headers = _headers(csrf, "save-binding")
    response = client.post(f"/v1/decisions/{run_id}/save", json={}, headers=headers)
    assert response.status_code == 200
    forged = {**response.json(), "saved_memory_id": ids["goal_id"]}
    with session_scope(factory) as session:
        session.execute(
            text(
                "UPDATE memory_operation_receipts SET result_json=:result "
                "WHERE operation_type='save_decision'"
            ),
            {"result": json_text(forged)},
        )
    replay = client.post(f"/v1/decisions/{run_id}/save", json={}, headers=headers)
    assert replay.status_code == 409


def test_second_citation_check_staleness_returns_409_and_terminal_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, factory = _client(tmp_path)
    csrf = _login(client)
    _seed(factory, tmp_path)
    model = _DecisionRecordingModel()
    _install_model(client, model)
    original = CitationReplayValidator.digest
    calls = 0

    def digest(self: Any, session: Session, citations: Any) -> str | None:
        nonlocal calls
        calls += 1
        return original(self, session, citations) if calls == 1 else None

    monkeypatch.setattr(CitationReplayValidator, "digest", digest)
    response = client.post(
        "/v1/decisions/analyze",
        json=_decision_payload(memory_topic_prefix=None),
        headers=_headers(csrf, "stale-second-check"),
    )
    assert response.status_code == 409
    assert model.queries == []
    with session_scope(factory) as session:
        row = session.execute(
            text(
                "SELECT status, result_json FROM memory_operation_receipts "
                "WHERE operation_type='analyze_decision'"
            )
        ).one()
        assert row.status == "failed"
        assert json.loads(row.result_json) == {"reason": "context_stale"}
