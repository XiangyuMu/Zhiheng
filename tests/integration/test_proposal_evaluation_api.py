from __future__ import annotations

import json
import os
import shutil
import sqlite3
from pathlib import Path
from typing import Any, cast

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import text

from tests.integration.test_g005_api_impl import _seed
from tests.integration.test_g006_release_lifecycle import _binding, _bootstrap_stable
from zhiheng.api.main import create_app
from zhiheng.api.retrieval import EvidenceBoundAnswerModel
from zhiheng.core.config import Settings
from zhiheng.db.session import create_session_factory, create_sqlite_engine, session_scope
from zhiheng.evolution.artifacts import default_release_artifact
from zhiheng.evolution.contracts import EvolutionRole, command_context_for_role
from zhiheng.evolution.jobs import EvolutionJobExecutor, JobRepository
from zhiheng.evolution.learning_evidence import LearningEvidenceLoader
from zhiheng.evolution.releases import ReleaseController
from zhiheng.worker.main import process_outbox_once, process_worker_once

REPO_ROOT = Path(__file__).resolve().parents[2]
TARGET_COMPONENT = "retrieval.answer_strategy"


def test_proposal_evaluation_request_api_enqueues_outbox_and_job(tmp_path: Path) -> None:
    client, db_path, settings = _client(tmp_path)
    proposal_id = _create_candidate_proposal(db_path, settings, "api-job")
    csrf = _login(client)
    proposal = client.get(f"/v1/evolution/proposals/{proposal_id}").json()

    response = client.post(
        f"/v1/evolution/proposals/{proposal_id}/evaluation-requests",
        headers=_headers(csrf, "eval-api-job", proposal["etag"]),
        json={"reason": "run protected fixed proposal validation"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "queued"
    assert response.json()["result"]["event_type"] == (
        "evolution.proposal.validation_requested"
    )
    assert process_outbox_once(settings) == 1
    with sqlite3.connect(db_path) as connection:
        event = connection.execute(
            "SELECT event_type, aggregate_id, payload_json FROM outbox_events"
        ).fetchone()
        job = connection.execute(
            "SELECT job_type, payload_json FROM jobs"
        ).fetchone()

    assert event[0] == "evolution.proposal.validation_requested"
    assert event[1] == proposal_id
    event_payload = json.loads(event[2])
    assert event_payload["reason"] == "run protected fixed proposal validation"
    assert "proposal_id" not in event_payload
    assert job[0] == "proposal-evaluation"
    job_payload = json.loads(job[1])
    assert job_payload["aggregate_id"] == proposal_id
    assert job_payload["proposal_id"] == proposal_id
    assert job_payload["reason"] == "run protected fixed proposal validation"


def test_proposal_evaluation_request_is_idempotent_after_proposal_etag_changes(
    tmp_path: Path,
) -> None:
    client, db_path, settings = _client(tmp_path)
    proposal_id = _create_candidate_proposal(db_path, settings, "api-replay")
    csrf = _login(client)
    proposal = client.get(f"/v1/evolution/proposals/{proposal_id}").json()
    headers = _headers(csrf, "eval-replay", proposal["etag"])

    first = client.post(
        f"/v1/evolution/proposals/{proposal_id}/evaluation-requests",
        headers=headers,
        json={"reason": "first request wins"},
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            UPDATE evolution_proposals
            SET risk_level = 'high', updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (proposal_id,),
        )
        connection.commit()
    replay = client.post(
        f"/v1/evolution/proposals/{proposal_id}/evaluation-requests",
        headers=headers,
        json={"reason": "first request wins"},
    )
    conflict = client.post(
        f"/v1/evolution/proposals/{proposal_id}/evaluation-requests",
        headers=headers,
        json={"reason": "different request body"},
    )

    assert first.status_code == 200
    assert replay.status_code == 200
    assert replay.json() == first.json()
    assert conflict.status_code == 409


@pytest.mark.parametrize("state", ["validating", "approved", "rejected"])
def test_proposal_evaluation_request_rejects_new_request_for_non_candidate(
    tmp_path: Path,
    state: str,
) -> None:
    client, db_path, settings = _client(tmp_path)
    proposal_id = _create_candidate_proposal(db_path, settings, f"api-state-{state}")
    csrf = _login(client)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            UPDATE evolution_proposals
            SET state = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (state, proposal_id),
        )
        connection.commit()
    proposal = client.get(f"/v1/evolution/proposals/{proposal_id}").json()

    response = client.post(
        f"/v1/evolution/proposals/{proposal_id}/evaluation-requests",
        headers=_headers(csrf, "eval-state-reject", proposal["etag"]),
        json={"reason": "new request should not enqueue doomed validation"},
    )

    assert response.status_code == 409
    assert process_outbox_once(settings) == 0


def test_proposal_evaluation_request_rejects_missing_auth_etag_unknown_and_injection(
    tmp_path: Path,
) -> None:
    client, db_path, settings = _client(tmp_path)
    proposal_id = _create_candidate_proposal(db_path, settings, "api-reject")
    csrf = _login(client)
    proposal = client.get(f"/v1/evolution/proposals/{proposal_id}").json()

    no_auth = TestClient(client.app).post(
        f"/v1/evolution/proposals/{proposal_id}/evaluation-requests",
        json={"reason": "missing auth"},
    )
    missing_etag = client.post(
        f"/v1/evolution/proposals/{proposal_id}/evaluation-requests",
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": "missing-etag"},
        json={"reason": "missing etag"},
    )
    unknown = client.post(
        "/v1/evolution/proposals/missing-proposal/evaluation-requests",
        headers=_headers(csrf, "unknown-proposal", proposal["etag"]),
        json={"reason": "unknown proposal"},
    )
    injected = client.post(
        f"/v1/evolution/proposals/{proposal_id}/evaluation-requests",
        headers=_headers(csrf, "inject-proposal-eval", proposal["etag"]),
        json={
            "reason": "try to inject authority",
            "score": 1.0,
            "role": "validator",
            "capability": "validate",
            "execution_id": "run",
            "proposal_id": "other",
        },
    )

    assert no_auth.status_code == 401
    assert missing_etag.status_code == 412
    assert unknown.status_code == 404
    assert injected.status_code == 422


def test_validation_requested_payload_cannot_override_aggregate_proposal_id(
    tmp_path: Path,
) -> None:
    _test_client, db_path, settings = _client(tmp_path)
    del _test_client
    aggregate_id = _create_candidate_proposal(db_path, settings, "aggregate")
    payload_id = _create_candidate_proposal(db_path, settings, "payload")
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO outbox_events (
              id, event_type, aggregate_type, aggregate_id, payload_json, status
            )
            VALUES (
              'forged-event', 'evolution.proposal.validation_requested',
              'evolution_proposal', ?, ?, 'pending'
            )
            """,
            (
                aggregate_id,
                json.dumps({"proposal_id": payload_id, "reason": "forged"}),
            ),
        )
        connection.commit()

    assert process_outbox_once(settings) == 1
    with sqlite3.connect(db_path) as connection:
        payload = json.loads(
            connection.execute("SELECT payload_json FROM jobs").fetchone()[0]
        )
    assert payload["proposal_id"] == aggregate_id
    assert payload["proposal_id"] != payload_id


def test_worker_evaluates_proposal_once_and_does_not_approve_or_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _require_restic(monkeypatch)
    client, db_path, settings = _client(tmp_path)
    proposal_id = _create_candidate_proposal(db_path, settings, "worker-positive")
    csrf = _login(client)
    proposal = client.get(f"/v1/evolution/proposals/{proposal_id}").json()
    response = client.post(
        f"/v1/evolution/proposals/{proposal_id}/evaluation-requests",
        headers=_headers(csrf, "worker-positive", proposal["etag"]),
        json={"reason": "worker should validate exactly once"},
    )
    assert response.status_code == 200, response.text

    assert process_worker_once(settings, worker_id="validator-worker") == 2
    assert process_worker_once(settings, worker_id="validator-worker") == 0
    with sqlite3.connect(db_path) as connection:
        assert _scalar(connection, "SELECT count(*) FROM proposal_execution_runs") == 1
        assert _scalar(
            connection,
            "SELECT count(*) FROM validation_reports WHERE proposal_id = ?",
            (proposal_id,),
        ) == 1
        assert _scalar(
            connection,
            "SELECT count(*) FROM review_reports WHERE proposal_id = ?",
            (proposal_id,),
        ) == 0
        assert _scalar(connection, "SELECT count(*) FROM strategy_releases") == 1
        assert _scalar(
            connection,
            "SELECT count(*) FROM release_transition_events WHERE next_state = 'stable'",
        ) == 1
        assert _scalar(
            connection,
            "SELECT state FROM evolution_proposals WHERE id = ?",
            (proposal_id,),
        ) == "validating"
        assert _scalar(connection, "SELECT status FROM jobs") == "completed"


def test_worker_retry_after_validation_commit_is_idempotent_before_job_ack(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _require_restic(monkeypatch)
    client, db_path, settings = _client(tmp_path)
    proposal_id = _create_candidate_proposal(db_path, settings, "worker-crash")
    csrf = _login(client)
    proposal = client.get(f"/v1/evolution/proposals/{proposal_id}").json()
    response = client.post(
        f"/v1/evolution/proposals/{proposal_id}/evaluation-requests",
        headers=_headers(csrf, "worker-crash", proposal["etag"]),
        json={"reason": "simulate crash after validation commit"},
    )
    assert response.status_code == 200, response.text
    assert process_outbox_once(settings) == 1

    engine = create_sqlite_engine(settings)
    session_factory = create_session_factory(engine)
    executor = EvolutionJobExecutor(
        settings,
        validator_context=command_context_for_role("validator-crash", EvolutionRole.VALIDATOR),
    )
    try:
        with session_scope(session_factory) as session:
            job = JobRepository().claim_available(
                session,
                worker_id="crashing-validator",
                limit=1,
                allowed_job_types=executor.allowed_job_types,
            )[0]
        executor.execute(job)
    finally:
        executor.close()
        engine.dispose()

    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            UPDATE jobs
            SET lease_expires_at = datetime('now', '-1 second')
            WHERE status = 'processing'
            """
        )
        connection.commit()

    assert process_worker_once(settings, worker_id="validator-retry") == 1
    with sqlite3.connect(db_path) as connection:
        assert _scalar(connection, "SELECT count(*) FROM proposal_execution_runs") == 1
        assert _scalar(
            connection,
            "SELECT count(*) FROM validation_reports WHERE proposal_id = ?",
            (proposal_id,),
        ) == 1
        assert _scalar(
            connection,
            "SELECT count(*) FROM review_reports WHERE proposal_id = ?",
            (proposal_id,),
        ) == 0
        assert _scalar(
            connection,
            "SELECT state FROM evolution_proposals WHERE id = ?",
            (proposal_id,),
        ) == "validating"
        assert _scalar(connection, "SELECT status FROM jobs") == "completed"


def _client(tmp_path: Path) -> tuple[TestClient, Path, Settings]:
    db_path = tmp_path / "zhiheng.db"
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.set_main_option("prepend_sys_path", str(REPO_ROOT / "src"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    settings = Settings(environment="test", database_url=f"sqlite:///{db_path}")
    app = create_app(settings)
    return TestClient(app), db_path, settings


def _create_candidate_proposal(db_path: Path, settings: Settings, label: str) -> str:
    # Positive API fixtures must use the same authenticated evidence contract as
    # production proposals; source-less legacy candidates are intentionally
    # rejected by the API provenance preflight.
    engine = create_sqlite_engine(settings)
    factory = create_session_factory(engine)
    try:
        client = TestClient(create_app(settings))
        _seed(factory, db_path.parent)
        csrf = _login(client)
        service = cast(Any, client.app).state.query_answer_service

        class FailedModel:
            def generate_answer(self, **kwargs: Any) -> Any:
                raise RuntimeError("synthetic local model failure")

        with sqlite3.connect(db_path) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            controller = ReleaseController.from_db(
                connection,
                deployment_secret=settings.secret_key.get_secret_value(),
            )
            baseline = _bootstrap_stable(controller)
            binding = _binding(f"candidate-{label}", baseline)
            stable = controller.load_default_head(TARGET_COMPONENT)
            assert stable is not None
            baseline_ids = (
                stable.release_id,
                stable.binding_digest,
                stable.binding.approved_artifact_digest,
            )

        evaluation_ids: list[str] = []
        with factory() as session:
            known = set(session.execute(
                text("SELECT id FROM task_evaluations")
            ).scalars())
        for index in range(5):
            service._rag._model_gateway = FailedModel() if index < 3 else EvidenceBoundAnswerModel()
            query = "中文全文检索 正式视图" if index < 4 else "个人知识库恢复删除日志"
            response = client.post(
                "/v1/answers", json={"query": query},
                headers={"X-CSRF-Token": csrf, "Idempotency-Key": f"synthetic-{label}-{index}"},
            )
            assert response.status_code == 200, response.text
            with factory() as session:
                current = set(session.execute(
                    text("SELECT id FROM task_evaluations")
                ).scalars())
                added = current - known
                assert len(added) == 1
                evaluation_ids.append(str(added.pop()))
                known = current
        with factory() as session:
            graph = LearningEvidenceLoader(
                settings.secret_key.get_secret_value()
            ).build_graph(
                session,
                baseline_release_id=baseline_ids[0],
                baseline_binding_digest=baseline_ids[1],
                baseline_artifact_digest=baseline_ids[2],
                trigger_evaluation_ids=tuple(evaluation_ids[:3]),
                support_evaluation_ids=(evaluation_ids[3],),
                counter_evaluation_ids=(evaluation_ids[4],),
            )
        with sqlite3.connect(db_path) as connection:
            controller = ReleaseController.from_db(
                connection,
                deployment_secret=settings.secret_key.get_secret_value(),
            )
            proposal = controller.create_release_proposal(
                binding=binding,
                artifact_payload=default_release_artifact(),
                proposer_context=command_context_for_role(
                    f"proposer-{label}", EvolutionRole.PROPOSER,
                ),
                source_graph=graph,
            )
            # Evidence generation is part of fixture setup; isolate the API
            # assertion from its answer-event outbox records.
            connection.execute("DELETE FROM outbox_events")
            connection.execute("DELETE FROM jobs")
            connection.commit()
            return proposal.proposal_id
    finally:
        engine.dispose()

def _login(client: TestClient) -> str:
    response = client.post(
        "/auth/bootstrap",
        json={"username": "synthetic-owner", "password": "correct horse battery staple"},
    )
    assert response.status_code == 200
    return str(response.json()["csrf_token"])


def _headers(csrf: str, key: str, etag: str) -> dict[str, str]:
    return {"X-CSRF-Token": csrf, "Idempotency-Key": key, "If-Match": etag}


def _require_restic(monkeypatch: pytest.MonkeyPatch) -> None:
    del monkeypatch
    if os.environ.get("ZHIHENG_RESTIC_BINARY") or shutil.which("restic"):
        return
    pytest.skip("configure real restic to execute full validation contract")


def _scalar(
    connection: sqlite3.Connection,
    statement: str,
    parameters: tuple[Any, ...] = (),
) -> Any:
    return cast(Any, connection.execute(statement, parameters).fetchone()[0])
