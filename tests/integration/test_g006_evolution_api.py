from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, cast

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import text

from tests.integration.release_helpers import prepare_release_with_persisted_evidence
from zhiheng.api.main import create_app
from zhiheng.core.config import Settings
from zhiheng.db.session import session_scope
from zhiheng.evolution.artifacts import artifact_digest, default_release_artifact
from zhiheng.evolution.contracts import ReleaseBindingV1
from zhiheng.evolution.releases import CanaryAssignment, ReleaseController
from zhiheng.worker.main import process_worker_once

TARGET_COMPONENT = "retrieval.answer_strategy"
FIXED_EVAL_SETS = ("boundary", "migration", "retention", "safety")


@pytest.mark.parametrize(
    "mode",
    [
        {"trigger_kind": "same_failure_threshold", "failure_tag": "synthetic_failure"},
        {"trigger_kind": "safety_exception"},
        {"trigger_kind": "knowledge_conflict"},
        {"trigger_kind": "retrieval_failure"},
        {"cadence": "weekly"},
        {"cadence": "monthly"},
        {"cadence": "quarterly"},
    ],
)
def test_maintenance_http_outbox_worker_roundtrip(tmp_path: Path, mode: dict[str, str]) -> None:
    client, db_path = _client(tmp_path)
    csrf = _login(client)
    overview = client.get("/v1/evolution/overview")
    response = client.post(
        "/v1/evolution/maintenance-requests",
        json={"target_component": TARGET_COMPONENT, **mode},
        headers=_headers(csrf, "maintenance-roundtrip", overview.headers["etag"]),
    )
    assert response.status_code == 200, response.text
    settings = Settings(environment="test", database_url=f"sqlite:///{db_path}")
    process_worker_once(settings)
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            "SELECT status FROM jobs WHERE job_type = 'maintenance'"
        ).fetchall()
        assert rows == [("completed",)]
        assert connection.execute(
            "SELECT count(*) FROM release_transition_events WHERE next_state = 'stable'"
        ).fetchone()[0] == 1


def _client(tmp_path: Path) -> tuple[TestClient, Path]:
    db_path = tmp_path / "zhiheng.db"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    app = create_app(Settings(environment="test", database_url=f"sqlite:///{db_path}"))
    return TestClient(app), db_path


def _login(client: TestClient) -> str:
    response = client.post(
        "/auth/bootstrap",
        json={"username": "synthetic-owner", "password": "correct horse battery staple"},
    )
    assert response.status_code == 200
    return str(response.json()["csrf_token"])


def _headers(csrf: str, key: str, etag: str) -> dict[str, str]:
    return {"X-CSRF-Token": csrf, "Idempotency-Key": key, "If-Match": etag}


def _digest(label: str) -> str:
    del label
    return artifact_digest(default_release_artifact())


def _assignment(cohort: str) -> CanaryAssignment:
    return CanaryAssignment(
        scope={"cohort": cohort, "percentage": 5},
        expires_at="2026-10-01T00:00:00+00:00",
    )


def _binding(candidate_id: str, rollback_target_id: str) -> ReleaseBindingV1:
    return ReleaseBindingV1(
        candidate_id=candidate_id,
        target_component=TARGET_COMPONENT,
        source_evaluation_ids=FIXED_EVAL_SETS,
        source_evidence_refs=(f"synthetic://evidence/{candidate_id}",),
        validation_report_ref=f"synthetic://validation/{candidate_id}",
        reviewer_decision_ref=f"synthetic://review/{candidate_id}",
        approved_artifact_digest=_digest(candidate_id),
        rollback_target_id=rollback_target_id,
    )


def _seed_release_graph(db_path: Path) -> dict[str, str]:
    connection = sqlite3.connect(db_path)
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        controller = ReleaseController.from_db(connection)
        baseline = controller.load_default_head(TARGET_COMPONENT)
        assert baseline is not None
        binding = _binding("candidate-v1", baseline.release_id)
        prepared = prepare_release_with_persisted_evidence(
            controller,
            binding=binding,
            proposer_id="proposer-a",
            reviewer_id="reviewer-b",
            canary_assignment=_assignment("candidate-v1"),
            canary_samples=5,
            request_id="prepare-candidate-v1",
        )
        proposal_id = connection.execute(
            "SELECT proposal_id FROM release_inputs WHERE id = ?",
            (connection.execute(
                "SELECT release_input_id FROM strategy_releases WHERE id = ?",
                (prepared.release_id,),
            ).fetchone()[0],),
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO task_trajectories (
              id, task_family, agent_version, knowledge_version, environment_version,
              status, evidence_refs_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "trajectory-safe",
                "answer",
                "agent-v1",
                "knowledge-v1",
                "env-v1",
                "eligible",
                json.dumps(
                    {
                        "task_id": "task-safe",
                        "api_key": "secret-value",
                        "query": "private query",
                        "learning_eligible": True,
                    }
                ),
            ),
        )
        connection.execute(
            """
            INSERT INTO task_evaluations (
              id, trajectory_id, result_json, process_json, quality_json,
              failure_tags_json, confidence, learning_eligible
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "eval-safe",
                "trajectory-safe",
                json.dumps({"answer_correct": True, "raw_text": "hidden"}),
                json.dumps({"steps": 3, "tool_args": {"token": "hidden"}}),
                json.dumps({"citation_coverage": 1.0}),
                json.dumps([]),
                0.97,
                True,
            ),
        )
        connection.commit()
        return {
            "baseline": baseline.release_id,
            "prepared": prepared.release_id,
            "proposal": proposal_id,
        }
    finally:
        connection.close()


def _json_contains(value: Any, needle: str) -> bool:
    return needle in json.dumps(value, ensure_ascii=False, sort_keys=True)


def test_evolution_api_returns_redacted_projection_and_etags(tmp_path: Path) -> None:
    client, db_path = _client(tmp_path)
    _seed_release_graph(db_path)
    _login(client)

    overview = client.get("/v1/evolution/overview")
    assert overview.status_code == 200
    assert overview.headers["etag"].startswith("evolution:")
    body = overview.json()
    assert body["summary"]["counts"]["releases"] == 2
    assert body["sets"]["fixed"] == list(FIXED_EVAL_SETS)
    assert body["sets"]["protected"] == ["privacy", "security", "authorization", "rollback"]
    assert not _json_contains(body, "secret-value")
    assert not _json_contains(body, "private query")
    assert _json_contains(body, "[redacted]")

    release_id = body["releases"][0]["id"]
    release = client.get(f"/v1/evolution/releases/{release_id}")
    assert release.status_code == 200
    assert release.headers["etag"] == release.json()["etag"]
    assert "transitions" in release.json()
    assert "canary" in release.json()


def test_evolution_write_endpoints_require_auth_csrf_idempotency_and_fresh_etag(
    tmp_path: Path,
) -> None:
    client, db_path = _client(tmp_path)
    ids = _seed_release_graph(db_path)
    csrf = _login(client)
    release = client.get(f"/v1/evolution/releases/{ids['prepared']}").json()

    no_auth = TestClient(client.app).post(
        f"/v1/evolution/releases/{ids['prepared']}/promotion-requests",
        json={"reason": "promote"},
    )
    assert no_auth.status_code == 401

    missing_csrf = client.post(
        f"/v1/evolution/releases/{ids['prepared']}/promotion-requests",
        headers={"Idempotency-Key": "promote-no-csrf", "If-Match": release["etag"]},
        json={"reason": "promote"},
    )
    assert missing_csrf.status_code == 403

    stale = client.post(
        f"/v1/evolution/releases/{ids['prepared']}/promotion-requests",
        headers=_headers(csrf, "promote-stale", "stale"),
        json={"reason": "promote"},
    )
    assert stale.status_code == 412

    first = client.post(
        f"/v1/evolution/releases/{ids['prepared']}/promotion-requests",
        headers=_headers(csrf, "promote-once", release["etag"]),
        json={"reason": "promote"},
    )
    second = client.post(
        f"/v1/evolution/releases/{ids['prepared']}/promotion-requests",
        headers=_headers(csrf, "promote-once", release["etag"]),
        json={"reason": "promote"},
    )
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json() == second.json()

    with session_scope(cast(Any, client.app).state.session_factory) as session:
        assert session.execute(text("SELECT count(*) FROM outbox_events")).scalar_one() == 1
        transition_count = session.execute(
            text("SELECT count(*) FROM release_transition_events")
        ).scalar_one()
        assert session.execute(text("SELECT count(*) FROM jobs")).scalar_one() == 0
    assert transition_count == 2


def test_evolution_user_decision_and_preview_do_not_mutate_release_authority(
    tmp_path: Path,
) -> None:
    client, db_path = _client(tmp_path)
    ids = _seed_release_graph(db_path)
    csrf = _login(client)
    proposal = client.get(f"/v1/evolution/proposals/{ids['proposal']}").json()
    release = client.get(f"/v1/evolution/releases/{ids['prepared']}").json()

    decision = client.post(
        f"/v1/evolution/proposals/{ids['proposal']}/decision",
        headers=_headers(csrf, "decision-once", proposal["etag"]),
        json={"decision": "approve", "rationale": "evidence reviewed"},
    )
    preview = client.post(
        f"/v1/evolution/releases/{ids['prepared']}/canary-preview",
        headers=_headers(csrf, "preview-once", release["etag"]),
        json={
            "sample_size": 3,
            "cohort": "small",
            "percentage": 25,
            "budget": {"max_model_calls": 0},
        },
    )

    assert decision.status_code == 200
    assert decision.json()["status"] == "queued"
    assert preview.status_code == 200
    assert preview.json()["result"]["release_mutation"] is False
    assert preview.json()["result"]["assignment"]["blockers"] == [
        "sample_size_below_minimum",
        "percentage_above_default_guardrail",
    ]

    with session_scope(cast(Any, client.app).state.session_factory) as session:
        assert session.execute(text("SELECT count(*) FROM outbox_events")).scalar_one() == 1
        state = session.execute(
            text("SELECT state FROM strategy_releases WHERE id = :id"),
            {"id": ids["prepared"]},
        ).scalar_one()
    assert state == "prepared"
