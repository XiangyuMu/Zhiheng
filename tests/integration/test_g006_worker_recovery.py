from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from tests.integration.release_helpers import (
    advance_to_canary_with_execution,
    prepare_release_with_persisted_evidence,
)
from tests.integration.release_helpers import (
    insert_signed_canary_observations as _insert_canary_observations,
)
from zhiheng.api.evolution import MaintenancePayload
from zhiheng.core.config import Settings
from zhiheng.db.session import create_session_factory, create_sqlite_engine, session_scope
from zhiheng.evaluation.g006_registry import REGISTERED_FIXED_CASES
from zhiheng.evolution.artifacts import artifact_digest, default_release_artifact
from zhiheng.evolution.contracts import (
    EvolutionCommandContext,
    EvolutionRole,
    ReleaseBindingV1,
    ReleaseState,
    command_context_for_role,
)
from zhiheng.evolution.jobs import (
    ClaimedJob,
    EvolutionJobExecutor,
    EvolutionJobType,
    JobRepository,
    process_jobs_once,
)
from zhiheng.evolution.releases import CanaryAssignment, ReleaseController
from zhiheng.evolution.trajectory_repository import TrajectoryRepository
from zhiheng.worker.main import process_worker_once

TARGET_COMPONENT = "retrieval.answer_strategy"
FIXED_EVAL_SETS = ("boundary", "migration", "retention", "safety")


def _migrated(tmp_path: Path) -> tuple[Settings, sessionmaker[Session], sqlite3.Connection]:
    db_path = tmp_path / "zhiheng.db"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    settings = Settings(environment="test", database_url=f"sqlite:///{db_path}")
    engine = create_sqlite_engine(settings)
    connection = sqlite3.connect(db_path)
    connection.execute("PRAGMA foreign_keys=ON")
    connection.row_factory = sqlite3.Row
    return settings, create_session_factory(engine), connection


def _digest(label: str) -> str:
    del label
    return artifact_digest(default_release_artifact())


def _assignment(cohort: str) -> CanaryAssignment:
    return CanaryAssignment(
        scope={"cohort": cohort, "percentage": 5},
        expires_at="2026-10-01T00:00:00+00:00",
    )


def _publisher_context(actor_id: str = "publisher-a") -> EvolutionCommandContext:
    return command_context_for_role(actor_id, EvolutionRole.PUBLISHER)


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


def _user_approval_payload(actor_id: str = "user-approver-a") -> dict[str, object]:
    context = command_context_for_role(actor_id, EvolutionRole.USER_APPROVER)
    return {
        "actor_id": context.actor_id,
        "role": context.role.value,
        "capabilities": [cap.value for cap in context.capabilities],
    }



def _advance_to_canary(controller: ReleaseController, release_id: str) -> None:
    advance_to_canary_with_execution(controller, release_id)


def _prepare_release(connection: sqlite3.Connection, candidate_id: str) -> tuple[str, str]:
    controller = ReleaseController.from_db(connection)
    baseline = controller.load_default_head(TARGET_COMPONENT)
    if baseline is None:
        baseline = controller.bootstrap_stable_release(
            binding=_binding("stable-baseline", "bootstrap-root"),
            proposer_id="proposer-a",
            reviewer_id="reviewer-a",
            reviewer_decision_ref="synthetic://review/stable-baseline",
            validation_report_ref="synthetic://validation/stable-baseline",
            canary_assignment=_assignment("bootstrap"),
            canary_samples=5,
            request_id=f"bootstrap-{candidate_id}",
        )
    binding = _binding(candidate_id, baseline.release_id)
    prepared = prepare_release_with_persisted_evidence(
        controller,
        binding=binding,
        proposer_id="proposer-a",
        reviewer_id="reviewer-b",
        canary_assignment=_assignment(candidate_id),
        canary_samples=5,
        request_id=f"prepare-{candidate_id}",
    )
    _advance_to_canary(controller, prepared.release_id)
    _insert_canary_observations(
        connection,
        prepared.release_id,
        prepared.binding,
        cohort=candidate_id,
    )
    return baseline.release_id, prepared.release_id


def _stable_transition_count(connection: sqlite3.Connection, release_id: str) -> int:
    return int(
        connection.execute(
            """
            SELECT count(*)
            FROM release_transition_events
            WHERE release_id = ? AND next_state = 'stable'
            """,
            (release_id,),
        ).fetchone()[0]
    )


def test_promotion_job_recovers_after_crash_without_double_publishing(tmp_path: Path) -> None:
    settings, session_factory, connection = _migrated(tmp_path)
    baseline_id, prepared_id = _prepare_release(connection, "candidate-crash")

    with session_scope(session_factory) as session:
        JobRepository().enqueue(
            session,
            job_type=EvolutionJobType.PROMOTION_REQUEST,
            idempotency_key="promote-candidate-crash",
            payload={
                "release_id": prepared_id,
                "request_id": "promote-candidate-crash",
                "user_approval": _user_approval_payload(),
            },
        )
        claimed = JobRepository().claim_available(session, worker_id="worker-a")

    assert len(claimed) == 1
    result = EvolutionJobExecutor(
        settings,
        publisher_context=command_context_for_role("publisher-a", EvolutionRole.PUBLISHER),
    ).execute(claimed[0])
    assert result.detail["state"] == ReleaseState.STABLE.value
    head = ReleaseController.from_db(connection).load_default_head(TARGET_COMPONENT)
    assert head is not None
    assert head.release_id == prepared_id
    assert _stable_transition_count(connection, prepared_id) == 1

    with session_scope(session_factory) as session:
        session.execute(
            text(
                """
                UPDATE jobs
                SET lease_expires_at = datetime('now', '-1 minute')
                WHERE idempotency_key = 'promote-candidate-crash'
                """
            )
        )

    assert process_jobs_once(
        session_factory,
        EvolutionJobExecutor(
            settings,
            publisher_context=command_context_for_role("publisher-a", EvolutionRole.PUBLISHER),
        ),
        worker_id="worker-b",
    ) == 1
    recovered_head = ReleaseController.from_db(connection).load_default_head(TARGET_COMPONENT)
    assert recovered_head is not None
    assert recovered_head.release_id == prepared_id
    assert (
        ReleaseController.from_db(connection).load_release(baseline_id).state
        is ReleaseState.ROLLED_BACK
    )
    assert _stable_transition_count(connection, prepared_id) == 1

    with session_scope(session_factory) as session:
        row = session.execute(
            text("SELECT status, attempts FROM jobs WHERE idempotency_key = :key"),
            {"key": "promote-candidate-crash"},
        ).mappings().one()
        attempts = session.execute(text("SELECT count(*) FROM job_attempts")).scalar_one()

    assert row["status"] == "completed"
    assert row["attempts"] == 2
    assert attempts == 2


def test_promotion_job_rejects_payload_self_reported_publisher(tmp_path: Path) -> None:
    settings, session_factory, connection = _migrated(tmp_path)
    _, prepared_id = _prepare_release(connection, "candidate-forged-publisher")

    with session_scope(session_factory) as session:
        JobRepository().enqueue(
            session,
            job_type=EvolutionJobType.PROMOTION_REQUEST,
            idempotency_key="promote-forged-publisher",
            payload={
                "release_id": prepared_id,
                "request_id": "promote-forged-publisher",
                "publisher_id": "payload-publisher",
                "user_approval": _user_approval_payload(),
            },
        )

    assert process_jobs_once(
        session_factory,
        EvolutionJobExecutor(
            settings,
            publisher_context=command_context_for_role("publisher-a", EvolutionRole.PUBLISHER),
        ),
        worker_id="publisher-a",
    ) == 0

    controller = ReleaseController.from_db(connection)
    prepared = controller.load_release(prepared_id)
    assert prepared.state is ReleaseState.CANARY
    assert controller.load_default_head(TARGET_COMPONENT) is not None

    with session_scope(session_factory) as session:
        status = session.execute(
            text("SELECT status FROM jobs WHERE idempotency_key = :key"),
            {"key": "promote-forged-publisher"},
        ).scalar_one()

    assert status == "pending"


def test_same_promotion_idempotency_key_replays_to_single_job(tmp_path: Path) -> None:
    settings, session_factory, connection = _migrated(tmp_path)
    _, prepared_id = _prepare_release(connection, "candidate-idempotent")

    with session_scope(session_factory) as session:
        repository = JobRepository()
        assert repository.enqueue(
            session,
            job_type=EvolutionJobType.PROMOTION_REQUEST,
            idempotency_key="same-key",
            payload={
                "release_id": prepared_id,
                "request_id": "same-key",
                "user_approval": _user_approval_payload(),
            },
        )
        assert not repository.enqueue(
            session,
            job_type=EvolutionJobType.PROMOTION_REQUEST,
            idempotency_key="same-key",
            payload={
                "release_id": prepared_id,
                "request_id": "same-key",
                "user_approval": _user_approval_payload(),
            },
        )

    assert process_jobs_once(
        session_factory,
        EvolutionJobExecutor(
            settings,
            publisher_context=command_context_for_role("publisher-a", EvolutionRole.PUBLISHER),
        ),
        worker_id="worker-a",
    ) == 1
    assert process_jobs_once(
        session_factory,
        EvolutionJobExecutor(
            settings,
            publisher_context=command_context_for_role("publisher-a", EvolutionRole.PUBLISHER),
        ),
        worker_id="worker-a",
    ) == 0
    assert _stable_transition_count(connection, prepared_id) == 1

    with session_scope(session_factory) as session:
        assert session.execute(text("SELECT count(*) FROM jobs")).scalar_one() == 1


def test_maintenance_job_cannot_mutate_stable_head(tmp_path: Path) -> None:
    settings, session_factory, connection = _migrated(tmp_path)
    baseline_id, _ = _prepare_release(connection, "candidate-unused")

    with session_scope(session_factory) as session:
        session.execute(
            text(
                """
                INSERT INTO outbox_events (
                  id, event_type, aggregate_type, aggregate_id, payload_json, status
                )
                VALUES (
                  'outbox-maintenance-1', 'maintenance', 'evolution',
                  'retrieval.answer_strategy', :payload_json, 'pending'
                )
                """
            ),
            {
                "payload_json": json.dumps(
                    {
                        "trigger_kind": "safety_exception",
                        "target_component": TARGET_COMPONENT,
                        "evidence_refs": ["synthetic://safety/exception"],
                    }
                )
            },
        )
        JobRepository().enqueue(
            session,
            job_type=EvolutionJobType.MAINTENANCE,
            idempotency_key="maintenance-safety",
            payload={
                "trigger_kind": "safety_exception",
                "target_component": TARGET_COMPONENT,
                "evidence_refs": ["synthetic://safety/exception"],
            },
        )

    assert process_worker_once(settings, worker_id="worker-a") == 3
    head = ReleaseController.from_db(connection).load_default_head(TARGET_COMPONENT)

    assert head is not None
    assert head.release_id == baseline_id
    assert (
        connection.execute(
            """
            SELECT count(*)
            FROM release_transition_events
            WHERE next_state = 'stable'
            """
        ).fetchone()[0]
        == 1
    )

    with session_scope(session_factory) as session:
        artifacts = session.execute(
            text(
                """
                SELECT artifact_kind, status, artifact_json
                FROM evolution_artifacts
                ORDER BY artifact_kind
                """
            )
    ).mappings().all()
        job_status = session.execute(
            text("SELECT status FROM jobs WHERE idempotency_key = 'maintenance-safety'")
        ).scalar_one()

    assert job_status == "completed"
    draft_artifacts = [row for row in artifacts if row["status"] == "draft"]
    assert {row["artifact_kind"] for row in draft_artifacts} == {
        "dynamic_eval_case_candidate",
        "retention_decision",
    }
    assert any(
        row["artifact_kind"] == "retrieval_strategy" and row["status"] == "published"
        for row in artifacts
    )
    assert all(
        json.loads(str(row["artifact_json"])).get("may_mutate_head") is not True
        for row in artifacts
    )


@pytest.mark.parametrize(
    "trigger_kind",
    [
        "same_failure_threshold",
        "safety_exception",
        "knowledge_conflict",
        "retrieval_failure",
    ],
)
def test_every_api_event_maintenance_value_is_worker_executable(
    tmp_path: Path,
    trigger_kind: str,
) -> None:
    settings, session_factory, _connection = _migrated(tmp_path)
    request = MaintenancePayload.model_validate(
        {
            "target_component": TARGET_COMPONENT,
            "trigger_kind": trigger_kind,
            "failure_tag": "synthetic_failure"
            if trigger_kind == "same_failure_threshold"
            else None,
            "evidence_refs": [f"synthetic://maintenance/{trigger_kind}"],
        }
    )
    executor = EvolutionJobExecutor(settings)

    with session_scope(session_factory) as session:
        outputs = executor._execute_maintenance_in_session(
            session,
            request.model_dump(mode="json", exclude_none=True),
            idempotency_key=f"synthetic-event-{trigger_kind}",
        )

    assert isinstance(outputs, tuple)


@pytest.mark.parametrize("cadence", ["weekly", "monthly", "quarterly"])
def test_every_api_periodic_maintenance_value_is_worker_executable(
    tmp_path: Path,
    cadence: str,
) -> None:
    settings, session_factory, _connection = _migrated(tmp_path)
    request = MaintenancePayload.model_validate(
        {
            "target_component": TARGET_COMPONENT,
            "cadence": cadence,
            "evidence_refs": [f"synthetic://maintenance/{cadence}"],
        }
    )
    executor = EvolutionJobExecutor(settings)

    with session_scope(session_factory) as session:
        outputs = executor._execute_maintenance_in_session(
            session,
            request.model_dump(mode="json", exclude_none=True),
            idempotency_key=f"synthetic-periodic-{cadence}",
        )

    assert outputs


def test_maintenance_api_requires_exactly_one_supported_mode() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        MaintenancePayload.model_validate({"target_component": TARGET_COMPONENT})
    with pytest.raises(ValueError, match="exactly one"):
        MaintenancePayload.model_validate(
            {
                "target_component": TARGET_COMPONENT,
                "trigger_kind": "safety_exception",
                "cadence": "weekly",
            }
        )


def test_stage_worker_fails_closed_without_publisher_capability(tmp_path: Path) -> None:
    settings, session_factory, connection = _migrated(tmp_path)
    _, prepared_id = _prepare_release(connection, "candidate-stage-capability")
    with session_scope(session_factory) as session:
        JobRepository().enqueue(
            session,
            job_type=EvolutionJobType.REPLAY,
            idempotency_key="replay-without-capability",
            payload={"release_id": prepared_id},
        )
        claimed = JobRepository().claim_available(session, worker_id="worker-a")
    with pytest.raises(PermissionError):
        EvolutionJobExecutor(settings).execute(claimed[0])


def test_stage_jobs_execute_bound_strategy_and_persist_signed_runs(
    tmp_path: Path,
) -> None:
    settings, session_factory, connection = _migrated(tmp_path)
    controller = ReleaseController.from_db(connection)
    baseline = controller.load_default_head(TARGET_COMPONENT)
    assert baseline is not None
    binding = _binding("candidate-signed-stage", baseline.release_id)
    repository = TrajectoryRepository(
        deployment_secret=settings.secret_key.get_secret_value(),
        session_factory=session_factory,
    )
    prepared = prepare_release_with_persisted_evidence(
        controller,
        binding=binding,
        proposer_id="proposer-a",
        reviewer_id="reviewer-b",
        canary_assignment=_assignment("candidate-signed-stage"),
        canary_samples=5,
        request_id="prepare-signed-stage",
    )
    executor = EvolutionJobExecutor(settings, publisher_context=_publisher_context())
    replay_result = executor.execute(
        ClaimedJob(
            id="job-replay",
            job_type=EvolutionJobType.REPLAY,
            idempotency_key="job-replay",
            payload={"release_id": prepared.release_id},
            attempts=1,
        )
    )
    evaluation_result = executor.execute(
        ClaimedJob(
            id="job-evaluation",
            job_type=EvolutionJobType.EVALUATION,
            idempotency_key="job-evaluation",
            payload={"release_id": prepared.release_id},
            attempts=1,
        )
    )
    assert replay_result.detail["state"] == "replay"
    assert evaluation_result.detail["state"] == "shadow"
    assert replay_result.detail["execution_run_id"] != evaluation_result.detail["execution_run_id"]
    assert len(replay_result.detail["stage_evidence_ids"]) == len(REGISTERED_FIXED_CASES)
    assert len(evaluation_result.detail["stage_evidence_ids"]) == len(REGISTERED_FIXED_CASES)
    for trajectory_id in evaluation_result.detail["stage_evidence_ids"]:
        process = repository.get(str(trajectory_id)).envelope.process
        assert process["stage"] == "shadow"
        assert process["release_id"] == prepared.release_id
        assert "source_trajectory_id" not in process
    canary_result = executor.execute(ClaimedJob(
        id="job-canary", job_type=EvolutionJobType.CANARY, idempotency_key="job-canary",
        payload={"release_id": prepared.release_id}, attempts=1,
    ))
    assert canary_result.detail["state"] == "canary"
    assert len({replay_result.detail["execution_run_id"],
                evaluation_result.detail["execution_run_id"],
                canary_result.detail["execution_run_id"]}) == 3
