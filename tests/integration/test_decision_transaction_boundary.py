from collections.abc import Generator
from pathlib import Path

from fastapi import FastAPI, Request
from sqlalchemy.orm import Session

from tests.integration.test_decision_memory_context import (
    _decision_payload,
    _DecisionRecordingModel,
    _install_model,
)
from tests.integration.test_g005_api_impl import _client, _headers, _login, _seed
from zhiheng.api.retrieval import get_db_session


def test_decision_model_runs_outside_request_transaction(tmp_path: Path) -> None:
    client, factory = _client(tmp_path)
    csrf = _login(client)
    _seed(factory, tmp_path)
    request_sessions: list[Session] = []

    def capture_session(request: Request) -> Generator[Session, None, None]:
        del request
        with factory() as session:
            request_sessions.append(session)
            yield session
            session.commit()

    checked: list[bool] = []

    def assert_no_transaction() -> None:
        assert len(request_sessions) == 1
        assert not request_sessions[0].in_transaction()
        checked.append(True)

    assert isinstance(client.app, FastAPI)
    client.app.dependency_overrides[get_db_session] = capture_session
    _install_model(client, _DecisionRecordingModel(after_generate=assert_no_transaction))
    response = client.post(
        "/v1/decisions/analyze",
        json=_decision_payload(memory_topic_prefix=None),
        headers=_headers(csrf, "decision-no-transaction"),
    )
    assert response.status_code == 200
    assert response.json()["stop_reason"] == "completed"
    assert checked == [True]
