from __future__ import annotations

import json
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from zhiheng.core.config import Settings
from zhiheng.core.ids import json_text
from zhiheng.db.session import create_session_factory, create_sqlite_engine, session_scope
from zhiheng.evolution.maintenance import (
    MaintenanceCadence,
    MaintenanceTrigger,
    MaintenanceTriggerKind,
    SleepLearningMaintenanceService,
)


def _migrated_session_factory(tmp_path: Path) -> sessionmaker[Session]:
    db_path = tmp_path / "zhiheng.db"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    settings = Settings(environment="test", database_url=f"sqlite:///{db_path}")
    return create_session_factory(create_sqlite_engine(settings))


def _insert_failed_trajectory(
    session: Session,
    *,
    trajectory_id: str,
    task_family: str,
    failure_tags: list[str],
) -> None:
    session.execute(
        text(
            """
            INSERT INTO task_trajectories (
              id, task_family, agent_version, knowledge_version, environment_version,
              status, evidence_refs_json
            )
            VALUES (
              :id, :task_family, 'agent-test', 'knowledge-test', 'env-test',
              'active', :evidence_refs_json
            )
            """
        ),
        {
            "id": trajectory_id,
            "task_family": task_family,
            "evidence_refs_json": json_text({"fixture": trajectory_id}),
        },
    )
    session.execute(
        text(
            """
            INSERT INTO task_evaluations (
              id, trajectory_id, result_json, process_json, quality_json,
              failure_tags_json, confidence, learning_eligible
            )
            VALUES (
              :id, :trajectory_id, '{}', '{}', '{}', :failure_tags_json, 1.0, 0
            )
            """
        ),
        {
            "id": f"eval-{trajectory_id}",
            "trajectory_id": trajectory_id,
            "failure_tags_json": json.dumps(failure_tags),
        },
    )


def test_same_failure_threshold_generates_only_drafts(tmp_path: Path) -> None:
    session_factory = _migrated_session_factory(tmp_path)
    service = SleepLearningMaintenanceService(same_failure_threshold=3)

    with session_scope(session_factory) as session:
        for index in range(3):
            _insert_failed_trajectory(
                session,
                trajectory_id=f"traj-{index}",
                task_family="retrieval.answer_strategy",
                failure_tags=["retrieval.no_authorized_context"],
            )

        outputs = service.handle_event_trigger(
            session,
            MaintenanceTrigger(
                kind=MaintenanceTriggerKind.SAME_FAILURE_THRESHOLD,
                target_component="retrieval.answer_strategy",
                task_family="retrieval.answer_strategy",
                failure_tag="retrieval.no_authorized_context",
                evidence_refs=("synthetic://trajectory/failures",),
            ),
        )
        stable_count = session.execute(
            text("SELECT count(*) FROM strategy_releases WHERE state = 'stable'")
        ).scalar_one()
        proposal_count = session.execute(
            text("SELECT count(*) FROM evolution_proposals WHERE state = 'candidate'")
        ).scalar_one()
        artifacts = session.execute(
            text(
                """
                SELECT artifact_kind, status, artifact_json
                FROM evolution_artifacts
                WHERE status = 'draft'
                ORDER BY artifact_kind
                """
            )
        ).mappings().all()

    assert [output.output_type for output in outputs] == [
        "strategy_proposal_draft",
        "dynamic_eval_case_candidate",
    ]
    assert proposal_count == 0
    assert {artifact["artifact_kind"] for artifact in artifacts} == {
        "dynamic_eval_case_candidate",
        "strategy_proposal_draft",
    }
    assert {artifact["status"] for artifact in artifacts} == {"draft"}
    dynamic_payload = next(
        json.loads(str(artifact["artifact_json"]))
        for artifact in artifacts
        if artifact["artifact_kind"] == "dynamic_eval_case_candidate"
    )
    draft_payload = next(
        json.loads(str(artifact["artifact_json"]))
        for artifact in artifacts
        if artifact["artifact_kind"] == "strategy_proposal_draft"
    )
    assert dynamic_payload["may_promote_release"] is False
    assert draft_payload == {
        "details": {},
        "evidence_refs": ["synthetic://trajectory/failures"],
        "failure_tag": "retrieval.no_authorized_context",
        "output_type": "strategy_proposal_draft",
        "reason": "same failure threshold reached: retrieval.no_authorized_context",
        "target_component": "retrieval.answer_strategy",
        "task_family": "retrieval.answer_strategy",
        "trigger": "same_failure_threshold",
    }
    assert stable_count == 1


def test_periodic_and_retirement_decisions_preserve_reasons(tmp_path: Path) -> None:
    session_factory = _migrated_session_factory(tmp_path)
    service = SleepLearningMaintenanceService()

    with session_scope(session_factory) as session:
        outputs = service.run_periodic(
            session,
            cadence=MaintenanceCadence.MONTHLY,
            target_component="retrieval.answer_strategy",
            evidence_refs=("synthetic://maintenance/monthly",),
        )
        deprecated = service.record_retirement_decision(
            session,
            target_id="proposal-retire",
            target_component="retrieval.answer_strategy",
            action="revalidate",
            reason="validator rejected due to stale evidence",
            evidence_refs=("synthetic://review/reject",),
            outcome="revalidation_required",
        )
        payloads = [
            json.loads(str(row["artifact_json"]))
            for row in session.execute(
                text(
                    """
                    SELECT artifact_json
                    FROM evolution_artifacts
                    WHERE artifact_kind = 'retention_decision'
                    ORDER BY created_at, id
                    """
                )
            ).mappings()
        ]

    assert [output.reason for output in outputs] == [
        "monthly maintenance",
        "monthly maintenance",
    ]
    assert deprecated.reason == "validator rejected due to stale evidence"
    assert {payload["action"] for payload in payloads} == {
        "merge",
        "deprecated",
        "revalidate",
    }
    revalidation_payload = next(
        payload for payload in payloads if payload["outcome"] == "revalidation_required"
    )
    assert revalidation_payload["reason"] == "validator rejected due to stale evidence"
    assert revalidation_payload["may_mutate_head"] is False
