import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy import text

from tests.integration.test_decision_memory_context import (
    _decision_payload,
    _DecisionRecordingModel,
    _install_model,
)
from tests.integration.test_g005_api_impl import _client, _headers, _login, _seed
from zhiheng.api import decisions
from zhiheng.privacy.erase import PrivacyEraseService
from zhiheng.privacy.erase_journal import ExternalEraseJournal


@pytest.mark.parametrize("target_type", ["formal_memory", "knowledge_object"])
@pytest.mark.parametrize("stop_mode", ["completed", "model_failed", "budget_exhausted"])
def test_erase_after_generation_validation_cannot_publish_a_decision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target_type: str,
    stop_mode: str,
) -> None:
    client, factory = _client(tmp_path)
    csrf = _login(client)
    ids = _seed(factory, tmp_path)
    model = _DecisionRecordingModel()
    _install_model(client, model)
    assert isinstance(client.app, FastAPI)
    rag = client.app.state.bounded_rag_service
    if stop_mode == "model_failed":

        def fail_model(**kwargs: Any) -> Any:
            raise RuntimeError("synthetic model failure")

        monkeypatch.setattr(model, "generate_answer", fail_model)
    elif stop_mode == "budget_exhausted":
        from zhiheng.query import AgenticBudget

        rag._budget = AgenticBudget(max_input_tokens=1)
    answer = rag.answer_from_manifest
    erase = PrivacyEraseService(
        ExternalEraseJournal(
            tmp_path / "synthetic-publication-erase.jsonl",
            "synthetic-publication-erase-secret",
        )
    )
    target_id = ids["goal_id"] if target_type == "formal_memory" else ids["knowledge_id"]

    def erase_after_validation(*args: Any, **kwargs: Any) -> Any:
        result = answer(*args, **kwargs)
        assert result.stop_reason == stop_mode
        # Model-free exits may still own retrieval writes. Release that earlier
        # transaction, as the publication boundary does before taking its lock.
        args[0].commit()
        with factory() as session:
            intent = erase.request_erase(
                session,
                target_type=target_type,
                target_id=target_id,
                requester="synthetic-user",
                reason="publication race regression",
            )
            if target_type == "formal_memory":
                erase.execute_memory_erase(
                    session,
                    request_id=intent.request_id,
                    target_type=target_type,
                    target_id=target_id,
                )
            else:
                erase.execute_knowledge_erase(
                    session,
                    request_id=intent.request_id,
                    knowledge_object_id=target_id,
                )
            session.commit()
        return result

    monkeypatch.setattr(rag, "answer_from_manifest", erase_after_validation)
    response = client.post(
        "/v1/decisions/analyze",
        json=_decision_payload(memory_topic_prefix=None),
        headers=_headers(csrf, "erased-before-publication"),
    )
    assert response.status_code == 409
    assert "建议选择学习路径" not in response.text
    with factory() as session:
        assert session.execute(text("SELECT count(*) FROM decision_support_runs")).scalar_one() == 0
        rows = session.execute(
            text(
                "SELECT status, result_json FROM memory_operation_receipts "
                "WHERE operation_type='analyze_decision'"
            )
        ).all()
        assert len(rows) == 1 and rows[0].status == "failed"
        assert "建议选择学习路径" not in str(rows)


def test_decision_publication_lock_covers_receipt_completion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, factory = _client(tmp_path)
    csrf = _login(client)
    _seed(factory, tmp_path)
    _install_model(client, _DecisionRecordingModel())
    from zhiheng.api.memory import _complete_operation_receipt

    checked: list[bool] = []

    def complete(session: Any, receipt_id: str, **kwargs: Any) -> None:
        path = session.get_bind().url.database
        with (
            sqlite3.connect(path, timeout=0) as contender,
            pytest.raises(sqlite3.OperationalError, match="locked"),
        ):
            contender.execute("BEGIN IMMEDIATE")
        checked.append(True)
        _complete_operation_receipt(session, receipt_id, **kwargs)

    monkeypatch.setattr(decisions, "_complete_operation_receipt", complete)
    response = client.post(
        "/v1/decisions/analyze",
        json=_decision_payload(memory_topic_prefix=None),
        headers=_headers(csrf, "locked-publication"),
    )
    assert response.status_code == 200
    assert response.json()["stop_reason"] == "completed"
    assert checked == [True]
