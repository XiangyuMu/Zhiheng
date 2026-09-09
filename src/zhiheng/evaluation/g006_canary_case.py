from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config

from zhiheng.core.config import Settings
from zhiheng.core.ids import json_text, new_id
from zhiheng.db.session import create_session_factory, create_sqlite_engine
from zhiheng.evolution.artifacts import artifact_digest, validate_strategy_artifact
from zhiheng.evolution.contracts import (
    EvolutionRole,
    ReleaseBindingV1,
    ReleaseState,
    command_context_for_role,
)
from zhiheng.evolution.releases import CanaryAssignment, ReleaseController
from zhiheng.evolution.trajectories import TrajectoryEnvelopeV1
from zhiheng.evolution.trajectory_repository import TrajectoryRepository

_CASE_ID = "safety-canary-insufficient-samples-001"
_EXPECTED_ERROR = "canary requires real append-only observations before stable"
_TARGET_COMPONENT = "retrieval.answer_strategy"
_BASELINE_RELEASE_ID = "00000000-0000-4000-8000-000000000501"
_CANARY_SAMPLE_COUNTS = (0, 1, 2, 3, 4)
_DEPLOYMENT_SECRET = "g006-canary-case-deployment-secret"


def execute_canary_case(
    *,
    project_root: Path,
    work_dir: Path,
    candidate_id: str,
    target_component: str,
    artifact_payload: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, bool]]:
    """Exercise the protected canary undersampling gate against real release code.

    The probe is intentionally not a valid releasable candidate: it carries no
    protected passing execution record. That keeps the case negative while still
    proving undersampled canary traffic is rejected before stable publication.
    """
    validate_strategy_artifact(artifact_payload)
    if target_component != _TARGET_COMPONENT:
        raise ValueError("canary case only supports retrieval.answer_strategy")

    work_dir.mkdir(parents=True, exist_ok=True)
    attempts: list[dict[str, Any]] = []
    for sample_count in _CANARY_SAMPLE_COUNTS:
        attempts.append(
            _run_attempt(
                project_root=project_root,
                work_dir=work_dir / f"samples-{sample_count}",
                candidate_id=candidate_id,
                artifact_payload=artifact_payload,
                sample_count=sample_count,
            )
        )

    insufficient_blocks = all(
        attempt["exact_insufficient_rejection"]
        and attempt["observed_sample_count"] == attempt["requested_sample_count"]
        for attempt in attempts
    )
    binding_complete = all(attempt["binding_roundtrip_complete"] for attempt in attempts)
    no_stable_promotion = all(
        attempt["stable_head_unchanged"] and attempt["probe_remained_canary"]
        for attempt in attempts
    )
    facts = {
        "artifact_digest": artifact_digest(artifact_payload),
        "attempts": attempts,
        "case_id": _CASE_ID,
        "expected_error": _EXPECTED_ERROR,
        "sample_counts": list(_CANARY_SAMPLE_COUNTS),
        "target_component": target_component,
    }
    outcomes = {
        "release.insufficient_canary_samples_blocks_stable": insufficient_blocks,
        "release.binding_complete": binding_complete,
        "release.no_stable_promotion": no_stable_promotion,
    }
    return facts, outcomes


def _run_attempt(
    *,
    project_root: Path,
    work_dir: Path,
    candidate_id: str,
    artifact_payload: dict[str, Any],
    sample_count: int,
) -> dict[str, Any]:
    work_dir.mkdir(parents=True, exist_ok=True)
    db_path = work_dir / "canary.sqlite"
    _migrate(project_root=project_root, db_path=db_path)

    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        controller = ReleaseController.from_db(
            connection, deployment_secret=_DEPLOYMENT_SECRET
        )
        stable_before = controller.load_default_head(_TARGET_COMPONENT)
        if stable_before is None:
            raise ValueError("migration baseline stable release missing")
        binding = ReleaseBindingV1(
            candidate_id=candidate_id,
            target_component=_TARGET_COMPONENT,
            source_evaluation_ids=("boundary", "migration", "retention", "safety"),
            source_evidence_refs=(f"g006-canary-case://{candidate_id}/negative-probe",),
            validation_report_ref=f"g006-canary-case://{candidate_id}/validation",
            reviewer_decision_ref=f"g006-canary-case://{candidate_id}/review",
            approved_artifact_digest=artifact_digest(artifact_payload),
            rollback_target_id=stable_before.release_id,
        )
        assignment = CanaryAssignment(
            scope={"cohort": f"{candidate_id}-cohort", "percentage": 5},
            expires_at="2026-10-01T00:00:00+00:00",
        )
        release_id = _insert_negative_canary_probe(
            connection=connection,
            binding=binding,
            assignment=assignment,
            artifact_payload=artifact_payload,
        )
        _persist_canary_observations(
            db_path=db_path,
            release_id=release_id,
            binding=binding,
            assignment=assignment,
            count=sample_count,
        )
        actual_sample_count = _actual_matching_observation_count(
            connection=connection, release_id=release_id, binding=binding,
            assignment=assignment,
        )

        error_message = ""
        try:
            controller.promote_release(
                release_id,
                user_approval_context=command_context_for_role(
                    "canary-case-user-approver", EvolutionRole.USER_APPROVER
                ),
                publisher_context=command_context_for_role(
                    "canary-case-publisher", EvolutionRole.PUBLISHER
                ),
                request_id=f"promote-negative-canary-{sample_count}",
            )
        except ValueError as exc:
            error_message = str(exc)

        stable_after = controller.load_default_head(_TARGET_COMPONENT)
        probe_after = controller.load_release(release_id)
        loaded_binding = probe_after.binding
        return {
            "protected_execution_run_count": connection.execute(
                "SELECT count(*) FROM proposal_execution_runs"
            ).fetchone()[0],
            "binding_digest": binding.canonical_digest(),
            "binding_roundtrip_complete": (
                loaded_binding.matches(binding)
                and probe_after.rollback_target_id == stable_before.release_id
                and probe_after.canary_assignment == assignment
            ),
            "error_message": error_message,
            "exact_insufficient_rejection": error_message == _EXPECTED_ERROR,
            "observed_sample_count": actual_sample_count,
            "requested_sample_count": sample_count,
            "probe_release_id": release_id,
            "probe_remained_canary": probe_after.state is ReleaseState.CANARY,
            "stable_head_after": stable_after.release_id if stable_after else None,
            "stable_head_before": stable_before.release_id,
            "stable_head_unchanged": (
                stable_after is not None
                and stable_after.release_id == stable_before.release_id
                and stable_after.release_id == _BASELINE_RELEASE_ID
            ),
        }


def _migrate(*, project_root: Path, db_path: Path) -> None:
    config = Config(str(project_root / "alembic.ini"))
    config.set_main_option("script_location", str(project_root / "migrations"))
    config.set_main_option("prepend_sys_path", str(project_root / "src"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(config, "head")


def _insert_negative_canary_probe(
    *,
    connection: sqlite3.Connection,
    binding: ReleaseBindingV1,
    assignment: CanaryAssignment,
    artifact_payload: dict[str, Any],
) -> str:
    release_id = new_id()
    release_input_id = new_id()
    proposal_id = new_id()
    validation_report_id = new_id()
    review_report_id = new_id()
    binding_digest = binding.canonical_digest()
    source_ids = {
        "negative_probe": True,
        "reason": "missing protected passing execution by design",
    }
    connection.execute(
        """
        INSERT INTO evolution_proposals (
          id, target_component, state, risk_level, minimal_diff_json,
          support_refs_json, counter_refs_json, proposer_id
        ) VALUES (?, ?, 'candidate', 'high', ?, ?, '[]', ?)
        """,
        (
            proposal_id,
            binding.target_component,
            json_text({"candidate_id": binding.candidate_id, **source_ids}),
            json_text(list(binding.source_evidence_refs)),
            "canary-case-proposer",
        ),
    )
    connection.execute(
        """
        INSERT INTO validation_reports (
          id, proposal_id, fixed_set_result_json, dynamic_set_result_json,
          latency_cost_json, status
        ) VALUES (?, ?, '{}', '{}', '{}', 'pending')
        """,
        (validation_report_id, proposal_id),
    )
    connection.execute(
        """
        INSERT INTO review_reports (
          id, proposal_id, reviewer_id, decision, rationale, evidence_refs_json
        ) VALUES (?, ?, ?, 'pending', ?, ?)
        """,
        (
            review_report_id,
            proposal_id,
            "canary-case-reviewer",
            binding.reviewer_decision_ref,
            json_text(list(binding.source_evidence_refs)),
        ),
    )
    connection.execute(
        """
        INSERT INTO evolution_artifacts (
          id, artifact_kind, binding_digest, artifact_digest, artifact_json,
          status, source_ref
        ) VALUES (?, 'retrieval_strategy', ?, ?, ?, 'draft', ?)
        """,
        (
            new_id(),
            binding_digest,
            binding.approved_artifact_digest,
            json_text(artifact_payload),
            f"g006-canary-case://{binding.candidate_id}/draft-artifact",
        ),
    )
    connection.execute(
        """
        INSERT INTO release_inputs (
          id, release_input_id, target_component, proposal_id,
          source_trajectory_ids_json, validation_report_id, review_report_id,
          risk_policy_snapshot_id, rollback_target_release_id, input_sha256
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            release_input_id,
            f"negative-canary-{binding.candidate_id}",
            binding.target_component,
            proposal_id,
            json_text(source_ids),
            validation_report_id,
            review_report_id,
            "negative-canary-risk-policy",
            binding.rollback_target_id,
            binding_digest,
        ),
    )
    connection.execute(
        """
        INSERT INTO strategy_releases (
          id, release_input_id, target_component, state, risk_level,
          canary_scope_json, rollback_target_release_id, activated_at
        ) VALUES (?, ?, ?, 'canary', 'high', ?, ?, NULL)
        """,
        (
            release_id,
            release_input_id,
            binding.target_component,
            json_text(assignment.canonical_payload()),
            binding.rollback_target_id,
        ),
    )
    connection.execute(
        """
        INSERT INTO strategy_release_heads (
          release_id, target_component, binding_digest, release_state,
          head_event_id, approved_artifact_digest
        ) VALUES (?, ?, ?, 'canary', ?, ?)
        """,
        (
            release_id,
            binding.target_component,
            binding_digest,
            new_id(),
            binding.approved_artifact_digest,
        ),
    )
    _insert_transition_event(
        connection=connection,
        release_id=release_id,
        binding=binding,
        assignment=assignment,
        next_state="prepared",
        previous_state="prepared",
        actor_role="reviewer",
        actor_id="canary-case-reviewer",
        reason="negative_canary_probe_prepared",
    )
    _insert_transition_event(
        connection=connection,
        release_id=release_id,
        binding=binding,
        assignment=assignment,
        next_state="canary",
        previous_state="shadow",
        actor_role="publisher",
        actor_id="canary-case-publisher",
        reason="negative_canary_probe_canary",
    )
    connection.commit()
    return release_id


def _insert_transition_event(
    *,
    connection: sqlite3.Connection,
    release_id: str,
    binding: ReleaseBindingV1,
    assignment: CanaryAssignment,
    next_state: str,
    previous_state: str,
    actor_role: str,
    actor_id: str,
    reason: str,
) -> None:
    event_json = {
        "binding": binding.as_record(),
        "canary_assignment": assignment.canonical_payload(),
        "canary_samples": 5,
        "proposer_id": "canary-case-proposer",
        "reviewer_id": "canary-case-reviewer",
        "request_id": reason,
        "step": reason,
    }
    connection.execute(
        """
        INSERT INTO release_transition_events (
          id, release_id, previous_state, next_state, actor_role, actor_id,
          binding_digest, reason, event_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            new_id(),
            release_id,
            previous_state,
            next_state,
            actor_role,
            actor_id,
            binding.canonical_digest(),
            reason,
            json_text(event_json),
        ),
    )


def _actual_matching_observation_count(
    *,
    connection: sqlite3.Connection,
    release_id: str,
    binding: ReleaseBindingV1,
    assignment: CanaryAssignment,
) -> int:
    row = connection.execute(
        """
        SELECT COUNT(DISTINCT tt.id)
        FROM task_trajectories tt
        JOIN task_evaluations te ON te.trajectory_id = tt.id
        WHERE tt.task_family = ?
          AND tt.agent_version = ?
          AND json_extract(tt.evidence_refs_json, '$.canary_observation.release_id') = ?
          AND json_extract(tt.evidence_refs_json, '$.canary_observation.binding_digest') = ?
          AND json_extract(tt.evidence_refs_json, '$.canary_observation.cohort') = ?
          AND json_extract(tt.evidence_refs_json, '$.canary_observation.target_component') = ?
          AND json_extract(te.process_json, '$.release_id') = ?
          AND json_extract(te.process_json, '$.release_state') = 'canary'
          AND json_extract(te.process_json, '$.binding_digest') = ?
          AND te.learning_eligible = 1
        """,
        (
            binding.target_component,
            f"release:{release_id}",
            release_id,
            binding.canonical_digest(),
            str(assignment.scope.get("cohort", "")),
            binding.target_component,
            release_id,
            binding.canonical_digest(),
        ),
    ).fetchone()
    return int(row[0])


def _persist_canary_observations(
    *,
    db_path: Path,
    release_id: str,
    binding: ReleaseBindingV1,
    assignment: CanaryAssignment,
    count: int,
) -> None:
    settings = Settings(environment="test", database_url=f"sqlite:///{db_path}")
    engine = create_sqlite_engine(settings)
    factory = create_session_factory(engine)
    repository = TrajectoryRepository(
        deployment_secret=_DEPLOYMENT_SECRET,
        session_factory=factory,
    )
    try:
        for index in range(count):
            envelope = _canary_observation_envelope(
                release_id=release_id,
                binding=binding,
                assignment=assignment,
                index=index,
            )
            repository.ingest(
                envelope,
                idempotency_key=f"{_CASE_ID}:{release_id}:{index}",
            )
    finally:
        engine.dispose()


def _canary_observation_envelope(
    *,
    release_id: str,
    binding: ReleaseBindingV1,
    assignment: CanaryAssignment,
    index: int,
) -> TrajectoryEnvelopeV1:
    cohort = str(assignment.scope.get("cohort", ""))
    observation = {
        "schema_version": "g006.canary_observation.v1",
        "assignment_scope": dict(assignment.scope),
        "binding_digest": binding.canonical_digest(),
        "cohort": cohort,
        "release_id": release_id,
        "source": "g006.canary_case",
        "target_component": binding.target_component,
    }
    trajectory_id = f"g006-canary-case:{release_id}:{index}"
    created_at = datetime.now(UTC).isoformat()
    event_payload = {
        "completed": False,
        "non_authorizing": True,
        "index": index,
        "release_id": release_id,
        "target_component": binding.target_component,
    }
    return TrajectoryEnvelopeV1.from_mapping(
        {
            "trajectory_id": trajectory_id,
            "task_id": trajectory_id,
            "task_family": binding.target_component,
            "agent_version": f"release:{release_id}",
            "knowledge_version": binding.approved_artifact_digest,
            "environment_version": "g006-canary-case",
            "created_at": created_at,
            "result": {"stop_reason": "evidence_only", "non_authorizing": True},
            "process": {
                "binding_digest": binding.canonical_digest(),
                "canary_observation": observation,
                "release_id": release_id,
                "release_state": "canary",
            },
            "quality": {"completed": False, "non_authorizing": True},
            "failure_tags": [],
            "confidence": 1.0,
            "learning_eligible": True,
            "evidence_state": "active",
            "events": [
                {
                    "created_at": created_at,
                    "event_id": f"{trajectory_id}:result",
                    "event_type": "result",
                    "payload": event_payload,
                }
            ],
        }
    )
