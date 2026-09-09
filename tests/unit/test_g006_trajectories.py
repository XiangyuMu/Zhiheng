from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from zhiheng.evolution.trajectories import TrajectoryEnvelopeV1
from zhiheng.evolution.trajectory_repository import TrajectoryRepository


def test_trajectory_envelope_redacts_sensitive_fields_and_replays_idempotently() -> None:
    envelope = TrajectoryEnvelopeV1.from_mapping(
        {
            "trajectory_id": "traj-001",
            "task_id": "task-001",
            "task_family": "retrieval.answer_strategy",
            "agent_version": "agent-0.1",
            "knowledge_version": "knowledge-0.1",
            "environment_version": "sqlite-local",
            "created_at": "2026-09-04T00:00:00Z",
            "result": {
                "summary": "contact bob@example.com or 13800138000",
                "cookie": "sid=abc",
                "key": "secret-key",
            },
            "process": {
                "steps": ["collect", "rank"],
            },
            "quality": {
                "rubric": {"citation_coverage": 1.0},
            },
            "learning_eligible": True,
            "evidence_state": "active",
            "events": [
                {
                    "event_id": "evt-1",
                    "event_type": "result",
                    "created_at": "2026-09-04T00:00:00Z",
                    "payload": {"summary": "bob@example.com"},
                },
                {
                    "event_id": "evt-2",
                    "event_type": "quality",
                    "created_at": "2026-09-04T00:01:00Z",
                    "payload": {"score": 1.0},
                },
            ],
        }
    )

    assert envelope.result["summary"] == "contact [EMAIL] or [PHONE]"
    assert envelope.result["cookie"] == "[REDACTED]"
    assert envelope.result["key"] == "[REDACTED]"
    assert envelope.events[0].previous_event_hash is None
    assert envelope.events[1].previous_event_hash == envelope.events[0].event_hash
    assert envelope.replayability.value == "full"
    assert envelope.effective_learning_eligible is True

    repo = TrajectoryRepository(deployment_secret="dev-secret")
    first = repo.ingest(envelope, idempotency_key="idem-001")
    second = repo.ingest(envelope, idempotency_key="idem-001")

    assert first is second
    assert first.deployment_hmac_digest == envelope.deployment_hmac_digest("dev-secret")
    assert first.deployment_hmac_digest != envelope.deployment_hmac_digest("other-secret")

    degraded = TrajectoryEnvelopeV1.from_mapping(
        {
            "trajectory_id": "traj-002",
            "task_id": "task-002",
            "task_family": "retrieval.answer_strategy",
            "agent_version": "agent-0.1",
            "knowledge_version": "knowledge-0.1",
            "environment_version": "sqlite-local",
            "created_at": "2026-09-04T00:00:00Z",
            "result": {"summary": "ok"},
            "process": {"steps": ["collect"]},
            "quality": {"rubric": {"citation_coverage": 1.0}},
            "learning_eligible": True,
            "evidence_state": "erased",
            "events": [
                {
                    "event_id": "evt-3",
                    "event_type": "result",
                    "created_at": "2026-09-04T00:00:00Z",
                    "payload": {"summary": "ok"},
                }
            ],
        }
    )

    assert degraded.evidence_erasure_degraded is True
    assert degraded.replayability.value == "degraded"
    assert degraded.effective_learning_eligible is False

    with pytest.raises(ValueError, match="raw model/tool payload"):
        TrajectoryEnvelopeV1.from_mapping(
            {
                "trajectory_id": "traj-003",
                "task_id": "task-003",
                "task_family": "retrieval.answer_strategy",
                "agent_version": "agent-0.1",
                "knowledge_version": "knowledge-0.1",
                "environment_version": "sqlite-local",
                "created_at": "2026-09-04T00:00:00Z",
                "result": {"summary": "ok"},
                "process": {"raw_model_payload": {"secret": "x"}},
                "quality": {"rubric": {"citation_coverage": 1.0}},
                "events": [],
            }
        )


def test_trajectory_strings_are_sanitized_before_digesting() -> None:
    envelope = TrajectoryEnvelopeV1.from_mapping(
        {
            "trajectory_id": "traj-bob@example.com",
            "task_id": "task-13800138000",
            "task_family": "retrieval.answer_strategy",
            "agent_version": "agent-0.1",
            "knowledge_version": "knowledge-0.1",
            "environment_version": "sqlite-local",
            "created_at": "2026-09-04T00:00:00Z",
            "result": {"summary": "ok"},
            "process": {"steps": ["contact bob@example.com"]},
            "quality": {"rubric": {"citation_coverage": 1.0}},
            "failure_tags": ["bob@example.com", "phone:13800138000"],
            "user_feedback": "call 13800138000 or bob@example.com",
            "events": [
                {
                    "event_id": "evt-bob@example.com",
                    "event_type": "user_feedback",
                    "created_at": "2026-09-04T00:00:00Z",
                    "payload": {"comment": "bob@example.com 13800138000"},
                }
            ],
        }
    )

    serialized = envelope.canonical_json()
    assert "bob@example.com" not in serialized
    assert "13800138000" not in serialized
    assert envelope.trajectory_id == "[EMAIL]"
    assert envelope.task_id == "task-[PHONE]"
    assert envelope.failure_tags == ("[EMAIL]", "phone:[PHONE]")
    assert envelope.user_feedback == "call [PHONE] or [EMAIL]"
    assert envelope.events[0].event_id == "[EMAIL]"
    assert envelope.events[0].payload["comment"] == "[EMAIL] [PHONE]"


def test_trajectory_db_reload_preserves_sanitized_events_and_integrity() -> None:
    session_factory = _trajectory_session_factory()
    envelope = TrajectoryEnvelopeV1.from_mapping(
        {
            "trajectory_id": "traj-db-001",
            "task_id": "task-db-001",
            "task_family": "retrieval.answer_strategy",
            "agent_version": "agent-0.1",
            "knowledge_version": "knowledge-0.1",
            "environment_version": "sqlite-local",
            "created_at": "2026-09-04T00:00:00Z",
            "result": {"summary": "bob@example.com"},
            "process": {"steps": ["collect", "rank"]},
            "quality": {"rubric": {"citation_coverage": 1.0}},
            "failure_tags": ["phone:13800138000"],
            "user_feedback": "bob@example.com",
            "events": [
                {
                    "event_id": "evt-1",
                    "event_type": "result",
                    "created_at": "2026-09-04T00:00:00Z",
                    "payload": {"summary": "bob@example.com"},
                },
                {
                    "event_id": "evt-2",
                    "event_type": "user_feedback",
                    "created_at": "2026-09-04T00:01:00Z",
                    "payload": {"comment": "13800138000"},
                },
            ],
        }
    )
    repo = TrajectoryRepository(
        deployment_secret="dev-secret",
        session_factory=session_factory,
    )

    stored = repo.ingest(envelope, idempotency_key="idem-db-001")
    reloaded = TrajectoryRepository(
        deployment_secret="dev-secret",
        session_factory=session_factory,
    ).replay("idem-db-001")

    assert reloaded.request_digest == stored.request_digest
    assert reloaded.deployment_hmac_digest == stored.deployment_hmac_digest
    assert reloaded.event_chain_digest == stored.event_chain_digest
    assert reloaded.envelope.events == stored.envelope.events
    assert reloaded.envelope.failure_tags == ("phone:[PHONE]",)
    assert reloaded.envelope.user_feedback == "[EMAIL]"
    assert "bob@example.com" not in reloaded.canonical_json()
    assert "13800138000" not in reloaded.canonical_json()


def test_trajectory_db_reload_rejects_mutated_persisted_payload() -> None:
    session_factory = _trajectory_session_factory()
    repo = TrajectoryRepository(
        deployment_secret="dev-secret",
        session_factory=session_factory,
    )
    repo.ingest_mapping(
        {
            "trajectory_id": "traj-db-mutated",
            "task_id": "task-db-mutated",
            "task_family": "retrieval.answer_strategy",
            "agent_version": "agent-0.1",
            "knowledge_version": "knowledge-0.1",
            "environment_version": "sqlite-local",
            "created_at": "2026-09-04T00:00:00Z",
            "result": {"summary": "ok"},
            "process": {"steps": ["collect"]},
            "quality": {"rubric": {"citation_coverage": 1.0}},
            "events": [
                {
                    "event_id": "evt-1",
                    "event_type": "result",
                    "created_at": "2026-09-04T00:00:00Z",
                    "payload": {"summary": "ok"},
                }
            ],
        },
        idempotency_key="idem-mutated",
    )
    with session_factory() as session:
        row = session.execute(
            text(
                """
                SELECT result_json
                FROM task_evaluations
                WHERE trajectory_id = 'traj-db-mutated'
                """
            )
        ).scalar_one()
        payload = json.loads(row)
        payload["summary"] = "mutated after insert"
        session.execute(
            text(
                """
                UPDATE task_evaluations
                SET result_json = :payload
                WHERE trajectory_id = 'traj-db-mutated'
                """
            ),
            {"payload": json.dumps(payload)},
        )
        session.commit()

    with pytest.raises(ValueError, match="request digest verification"):
        TrajectoryRepository(
            deployment_secret="dev-secret",
            session_factory=session_factory,
        ).replay("idem-mutated")


def _trajectory_session_factory() -> sessionmaker[Session]:
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE task_trajectories (
                  id TEXT PRIMARY KEY,
                  task_family TEXT NOT NULL,
                  agent_version TEXT NOT NULL,
                  knowledge_version TEXT NOT NULL,
                  environment_version TEXT NOT NULL,
                  status TEXT NOT NULL,
                  evidence_refs_json TEXT NOT NULL,
                  created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE task_evaluations (
                  id TEXT PRIMARY KEY,
                  trajectory_id TEXT NOT NULL,
                  result_json TEXT NOT NULL,
                  process_json TEXT NOT NULL,
                  quality_json TEXT NOT NULL,
                  failure_tags_json TEXT NOT NULL,
                  confidence REAL NOT NULL,
                  learning_eligible INTEGER NOT NULL,
                  created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )
    return sessionmaker(bind=engine)
