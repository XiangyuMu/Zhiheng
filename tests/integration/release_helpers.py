from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from zhiheng.core.config import Settings
from zhiheng.core.ids import sha256_text
from zhiheng.db.session import create_session_factory, create_sqlite_engine
from zhiheng.evaluation.g006_registry import REGISTERED_FIXED_CASES
from zhiheng.evaluation.g006_runner import G006ExecutionStage
from zhiheng.evaluation.proposal_execution import ProposalExecutionService
from zhiheng.evaluation.release_execution import ReleaseExecutionService
from zhiheng.evolution.artifacts import default_release_artifact
from zhiheng.evolution.contracts import (
    EvolutionRole,
    ReleaseBindingV1,
    ReleaseState,
    command_context_for_role,
)
from zhiheng.evolution.releases import CanaryAssignment, ReleaseContext, ReleaseController
from zhiheng.evolution.trajectories import TrajectoryEnvelopeV1

REPO_ROOT = Path(__file__).resolve().parents[2]


def advance_to_canary_with_execution(
    controller: ReleaseController, release_id: str, *, actor_id: str = "publisher-a",
) -> ReleaseContext:
    database = _controller_database_path(controller)
    engine = create_sqlite_engine(Settings(environment="test", database_url=f"sqlite:///{database}"))
    try:
        service = ReleaseExecutionService(
            session_factory=create_session_factory(engine), project_root=REPO_ROOT,
            deployment_secret=controller._deployment_secret,
        )
        release = controller.load_release(release_id)
        for stage in (ReleaseState.REPLAY, ReleaseState.SHADOW, ReleaseState.CANARY):
            request_id = f"advance-{release_id}-{stage.value}"
            execution = service.execute(
                release_id=release_id, stage=G006ExecutionStage(stage.value),
                idempotency_key=request_id,
            )
            release = controller.advance_release_stage(
                release_id, next_state=stage, execution_run_id=execution["id"],
                publisher_context=command_context_for_role(actor_id, EvolutionRole.PUBLISHER),
                request_id=request_id, step=stage.value,
            )
        return release
    finally:
        engine.dispose()


def prepare_release_with_persisted_evidence(
    controller: ReleaseController,
    *,
    binding: ReleaseBindingV1,
    proposer_id: str,
    reviewer_id: str,
    canary_assignment: CanaryAssignment,
    canary_samples: int,
    request_id: str,
    safety_passed: bool = True,
    budget_passed: bool = True,
    artifact_payload: dict[str, Any] | None = None,
) -> ReleaseContext:
    existing = controller._find_release_by_request(request_id)  # noqa: SLF001 - test fixture bridge
    if existing is not None and existing.binding.canonical_digest() == binding.canonical_digest():
        evidence = _existing_release_evidence(controller, existing.release_id)
        return controller.prepare_release(
            binding=binding,
            proposer_id=proposer_id,
            reviewer_id=reviewer_id,
            reviewer_decision_ref=binding.reviewer_decision_ref,
            validation_report_ref=binding.validation_report_ref,
            canary_assignment=canary_assignment,
            canary_samples=canary_samples,
            validation_report_id=str(evidence["validation_report_id"]),
            review_report_id=str(evidence["review_report_id"]),
            evaluation_report_digest=str(evidence["evaluation_report_digest"]),
            policy_snapshot_digest=str(evidence["policy_snapshot_digest"]),
            request_id=request_id,
        )
    proposal = controller.create_release_proposal(
        binding=binding,
        proposer_context=command_context_for_role(proposer_id, EvolutionRole.PROPOSER),
        artifact_payload=artifact_payload or default_release_artifact(),
    )
    run = _execute_proposal(controller, proposal_id=proposal.proposal_id, request_id=request_id)
    validation = controller.record_release_validation_evidence(
        binding=binding,
        proposal_id=proposal.proposal_id,
        validation_report_ref=binding.validation_report_ref,
        canary_samples=canary_samples,
        trajectory_ids=tuple(run["trajectory_ids"]),
        validator_context=command_context_for_role("validator-a", EvolutionRole.VALIDATOR),
        evaluation_run_id=str(run["id"]),
    )
    review = controller.record_release_review_evidence(
        binding=binding,
        proposal_id=validation.proposal_id,
        proposer_id=proposer_id,
        reviewer_decision_ref=binding.reviewer_decision_ref,
        reviewer_context=command_context_for_role(reviewer_id, EvolutionRole.REVIEWER),
    )
    return controller.prepare_release(
        binding=binding,
        proposer_id=proposer_id,
        reviewer_id=reviewer_id,
        reviewer_decision_ref=binding.reviewer_decision_ref,
        validation_report_ref=binding.validation_report_ref,
        canary_assignment=canary_assignment,
        canary_samples=canary_samples,
        validation_report_id=validation.validation_report_id,
        review_report_id=review.review_report_id,
        evaluation_report_digest=validation.evaluation_report_digest,
        policy_snapshot_digest=validation.policy_snapshot_digest,
        request_id=request_id,
    )


def _existing_release_evidence(
    controller: ReleaseController, release_id: str
) -> dict[str, str]:
    row = controller._connection.execute(  # noqa: SLF001 - test fixture bridge
        """
        SELECT ri.validation_report_id, ri.review_report_id,
               vr.fixed_set_result_json, vr.latency_cost_json
        FROM strategy_releases sr
        JOIN release_inputs ri ON ri.id = sr.release_input_id
        JOIN validation_reports vr ON vr.id = ri.validation_report_id
        WHERE sr.id = ?
        """,
        (release_id,),
    ).fetchone()
    if row is None:
        raise ValueError("existing prepared release evidence is missing")
    fixed_report = json.loads(row["fixed_set_result_json"])
    policy_snapshot = json.loads(row["latency_cost_json"])
    return {
        "validation_report_id": row["validation_report_id"],
        "review_report_id": row["review_report_id"],
        "evaluation_report_digest": fixed_report["report_digest"],
        "policy_snapshot_digest": policy_snapshot["snapshot_digest"],
    }


def _execute_proposal(
    controller: ReleaseController,
    *,
    proposal_id: str,
    request_id: str,
) -> dict[str, Any]:
    db_path = _controller_database_path(controller)
    engine = create_sqlite_engine(Settings(environment="test", database_url=f"sqlite:///{db_path}"))
    try:
        return ProposalExecutionService(
            session_factory=create_session_factory(engine),
            project_root=REPO_ROOT,
            deployment_secret=controller._deployment_secret,  # noqa: SLF001 - fixture bridge
        ).execute(
            proposal_id=proposal_id,
            idempotency_key=f"prepare-release:{proposal_id}",
        )
    finally:
        engine.dispose()


def _controller_database_path(controller: ReleaseController) -> Path:
    rows = controller._connection.execute("PRAGMA database_list").fetchall()  # noqa: SLF001
    for row in rows:
        name = row["name"] if isinstance(row, sqlite3.Row) else row[1]
        file_name = row["file"] if isinstance(row, sqlite3.Row) else row[2]
        if name == "main" and file_name:
            return Path(str(file_name))
    raise ValueError("proposal execution requires a file-backed SQLite controller database")


def _fixed_cases(
    binding: ReleaseBindingV1,
    *,
    safety_passed: bool,
) -> tuple[dict[str, Any], ...]:
    return tuple(
        {
            "case_id": registered.case_id,
            "evidence": {
                "process": [f"{binding.validation_report_ref}/process/{registered.case_id}"],
                "quality": [f"{binding.validation_report_ref}/quality/{registered.case_id}"],
                "result": [f"{binding.validation_report_ref}/result/{registered.case_id}"],
            },
            "process": {
                "status": "clean"
                if safety_passed or registered.set_name != "safety"
                else "violation"
            },
            "quality": {
                "status": "acceptable"
                if safety_passed or registered.set_name != "safety"
                else "fail"
            },
            "result": {
                "status": "success"
                if safety_passed or registered.set_name != "safety"
                else "fail"
            },
            "set_name": registered.set_name,
            "trajectory_id": (
                f"trajectory:{binding.candidate_id}:{registered.case_id}:"
                f"{'pass' if safety_passed or registered.set_name != 'safety' else 'fail'}"
            ),
        }
        for registered in REGISTERED_FIXED_CASES
    )


def _persist_fixed_cases(
    controller: ReleaseController,
    binding: ReleaseBindingV1,
    cases: tuple[dict[str, Any], ...],
) -> tuple[str, ...]:
    trajectory_ids: list[str] = []
    for case in cases:
        trajectory_id = str(case["trajectory_id"])
        trajectory_ids.append(trajectory_id)
        process = dict(case["process"])
        process.update({"case_id": case["case_id"], "set_name": case["set_name"]})
        failure_tags = list(case.get("failure_tags", []))
        envelope = TrajectoryEnvelopeV1.from_mapping(
            {
                "trajectory_id": trajectory_id,
                "task_id": trajectory_id,
                "task_family": binding.target_component,
                "agent_version": "fixed-set-evaluator",
                "knowledge_version": "fixture",
                "environment_version": "test",
                "created_at": "2026-09-06T00:00:00+00:00",
                "result": case["result"],
                "process": process,
                "quality": case["quality"],
                "failure_tags": failure_tags,
                "confidence": 1.0,
                "learning_eligible": True,
                "evidence_state": "active",
                "events": [],
            }
        )
        evidence_refs = {
            "idempotency_key_sha256": f"sha256:{sha256_text(trajectory_id)}",
            "created_at": envelope.created_at,
            "task_id": envelope.task_id,
            "events": [],
            "event_hashes": [],
            "event_chain_digest": envelope.event_chain_digest,
            "request_digest": envelope.canonical_digest(),
            "deployment_hmac_digest": envelope.deployment_hmac_digest(
                controller._deployment_secret
            ),
        }
        controller._connection.execute(  # noqa: SLF001 - test fixture ingress
            """
            INSERT OR IGNORE INTO task_trajectories (
              id, task_family, agent_version, knowledge_version,
              environment_version, status, evidence_refs_json
            ) VALUES (?, ?, 'fixed-set-evaluator', 'fixture', 'test', 'active', ?)
            """,
            (
                trajectory_id,
                binding.target_component,
                json.dumps(evidence_refs, sort_keys=True),
            ),
        )
        controller._connection.execute(  # noqa: SLF001 - test fixture ingress
            """
            INSERT OR IGNORE INTO task_evaluations (
              id, trajectory_id, result_json, process_json, quality_json,
              failure_tags_json, confidence, learning_eligible
            ) VALUES (?, ?, ?, ?, ?, ?, 1.0, 1)
            """,
            (
                f"evaluation:{trajectory_id}",
                trajectory_id,
                json.dumps(case["result"], sort_keys=True),
                json.dumps(process, sort_keys=True),
                json.dumps(case["quality"], sort_keys=True),
                json.dumps(failure_tags, sort_keys=True),
            ),
        )
    controller._connection.commit()  # noqa: SLF001 - test fixture ingress
    return tuple(trajectory_ids)


def insert_signed_canary_observations(
    connection: sqlite3.Connection,
    release_id: str,
    binding: ReleaseBindingV1,
    *,
    cohort: str,
    count: int = 5,
    deployment_secret: str | None = None,
) -> None:
    secret = deployment_secret or Settings().secret_key.get_secret_value()
    for index in range(count):
        trajectory_id = f"trajectory-{release_id}-{index}"
        observation = {
            "schema_version": "g006.canary_observation.v1",
            "source": "query.answer",
            "release_id": release_id,
            "binding_digest": binding.canonical_digest(),
            "target_component": binding.target_component,
            "cohort": cohort,
            "assignment_scope": {"cohort": cohort, "percentage": 5},
        }
        envelope = TrajectoryEnvelopeV1.from_mapping({
            "trajectory_id": trajectory_id,
            "task_id": trajectory_id,
            "task_family": binding.target_component,
            "agent_version": f"release:{release_id}",
            "knowledge_version": "knowledge",
            "environment_version": "env",
            "created_at": "2026-09-06T00:00:00+00:00",
            "result": {"stop_reason": "completed"},
            "process": {
                "release_id": release_id,
                "release_state": "canary",
                "binding_digest": binding.canonical_digest(),
                "canary_observation": observation,
            },
            "quality": {"completed": True},
            "events": [{
                "event_id": f"event-{trajectory_id}",
                "event_type": "result",
                "created_at": "2026-09-06T00:00:00+00:00",
                "payload": {"stop_reason": "completed"},
            }],
        })
        refs = {
            "created_at": envelope.created_at,
            "task_id": envelope.task_id,
            "events": [event.canonical_payload() for event in envelope.events],
            "event_hashes": [event.event_hash for event in envelope.events],
            "event_count": len(envelope.events),
            "event_chain_digest": envelope.event_chain_digest,
            "request_digest": envelope.canonical_digest(),
            "deployment_hmac_digest": envelope.deployment_hmac_digest(secret),
            "idempotency_key_sha256": f"sha256:{sha256_text(trajectory_id)}",
            "canary_observation": observation,
        }
        connection.execute(
            "INSERT INTO task_trajectories "
            "(id, task_family, agent_version, knowledge_version, environment_version, "
            "status, evidence_refs_json) VALUES (?, ?, ?, 'knowledge', 'env', 'active', ?)",
            (trajectory_id, binding.target_component, envelope.agent_version, json.dumps(refs)),
        )
        connection.execute(
            "INSERT INTO task_evaluations "
            "(id, trajectory_id, result_json, process_json, quality_json, "
            "failure_tags_json, confidence, learning_eligible) VALUES (?, ?, ?, ?, ?, '[]', 1, 1)",
            (f"evaluation-{trajectory_id}", trajectory_id, json.dumps(envelope.result),
             json.dumps(envelope.process), json.dumps(envelope.quality)),
        )
    connection.commit()
