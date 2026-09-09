from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import event, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from zhiheng.core.config import Settings
from zhiheng.db.session import create_session_factory, create_sqlite_engine, session_scope
from zhiheng.evolution.jobs import EvolutionJobExecutor, EvolutionJobType, JobRepository
from zhiheng.evolution.maintenance import (
    MaintenanceOutput,
    MaintenanceTrigger,
    MaintenanceTriggerKind,
    SleepLearningMaintenanceService,
)

TARGET_COMPONENT = "retrieval.answer_strategy"


def _migrated(tmp_path: Path) -> tuple[Settings, sessionmaker[Session]]:
    db_path = tmp_path / "zhiheng.db"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    settings = Settings(environment="test", database_url=f"sqlite:///{db_path}")
    engine = create_sqlite_engine(settings)
    return settings, create_session_factory(engine)


def _safety_trigger(evidence_ref: str = "synthetic://safety/exception") -> MaintenanceTrigger:
    return MaintenanceTrigger(
        kind=MaintenanceTriggerKind.SAFETY_EXCEPTION,
        target_component=TARGET_COMPONENT,
        evidence_refs=(evidence_ref,),
    )


def _count(session: Session, sql: str) -> int:
    return int(session.execute(text(sql)).scalar_one())


def _draft_artifact_count(session: Session) -> int:
    return _count(session, "SELECT count(*) FROM evolution_artifacts WHERE status = 'draft'")


def _seed_failed_trajectories(
    session: Session,
    *,
    failure_tag: str,
    count: int = 3,
) -> None:
    for index in range(count):
        trajectory_id = f"trajectory-{index}"
        session.execute(
            text(
                """
                INSERT INTO task_trajectories (
                  id, task_family, agent_version, knowledge_version,
                  environment_version, status, evidence_refs_json
                )
                VALUES (
                  :id, 'retrieval', 'agent-v1', 'knowledge-v1',
                  'environment-v1', 'failed', '{}'
                )
                """
            ),
            {"id": trajectory_id},
        )
        session.execute(
            text(
                """
                INSERT INTO task_evaluations (
                  id, trajectory_id, result_json, process_json, quality_json,
                  failure_tags_json, confidence, learning_eligible
                )
                VALUES (
                  :id, :trajectory_id, '{}', '{}', '{}', :failure_tags_json, 0.9, 0
                )
                """
            ),
            {
                "id": f"evaluation-{index}",
                "trajectory_id": trajectory_id,
                "failure_tags_json": f'["{failure_tag}"]',
            },
        )


def test_maintenance_receipt_replays_after_crash_before_job_ack(tmp_path: Path) -> None:
    _settings, session_factory = _migrated(tmp_path)
    with session_scope(session_factory) as session:
        repository = JobRepository()
        repository.enqueue(
            session,
            job_type=EvolutionJobType.MAINTENANCE,
            idempotency_key="maintenance-crash-before-ack",
            payload={
                "trigger_kind": "safety_exception",
                "target_component": TARGET_COMPONENT,
                "evidence_refs": ["synthetic://safety/exception"],
            },
        )
        claimed = repository.claim_available(session, worker_id="worker-a")

    assert len(claimed) == 1

    first_result = EvolutionJobExecutor(_settings).execute(claimed[0])

    with session_scope(session_factory) as session:
        assert _draft_artifact_count(session) == 2
        assert _count(session, "SELECT count(*) FROM maintenance_job_receipts") == 1
        assert _count(session, "SELECT count(*) FROM maintenance_job_locks") == 1
        receipt_payload = session.execute(
            text(
                """
                SELECT r.output_refs_json, l.payload_digest
                FROM maintenance_job_receipts r
                JOIN maintenance_job_locks l
                  ON l.idempotency_digest = r.idempotency_digest
                """
            )
        ).one()
        session.execute(
            text(
                """
                UPDATE jobs
                SET lease_expires_at = datetime('now', '-1 minute')
                WHERE idempotency_key = 'maintenance-crash-before-ack'
                """
            )
        )

    assert "synthetic://safety/exception" not in str(receipt_payload)
    assert "safety_exception" not in str(receipt_payload)

    with session_scope(session_factory) as session:
        reclaimed = JobRepository().claim_available(session, worker_id="worker-b")

    assert len(reclaimed) == 1

    replayed_result = EvolutionJobExecutor(_settings).execute(reclaimed[0])

    assert replayed_result.detail == first_result.detail
    with session_scope(session_factory) as session:
        assert _draft_artifact_count(session) == 2
        assert _count(session, "SELECT count(*) FROM maintenance_job_receipts") == 1
        assert _count(session, "SELECT count(*) FROM maintenance_job_locks") == 1


def test_concurrent_maintenance_same_key_replays_single_output_set(
    tmp_path: Path,
) -> None:
    _settings, session_factory = _migrated(tmp_path)
    first_effect_written = threading.Event()
    second_lock_insert_attempted = threading.Event()
    allow_first_commit = threading.Event()
    lock_insert_attempt_count = 0
    lock_insert_attempt_count_lock = threading.Lock()

    with session_scope(session_factory) as session:
        engine = session.get_bind()

    def observe_lock_insert(
        _conn: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        nonlocal lock_insert_attempt_count
        if "INSERT OR IGNORE INTO maintenance_job_locks" not in statement:
            return
        with lock_insert_attempt_count_lock:
            lock_insert_attempt_count += 1
            if lock_insert_attempt_count == 2:
                second_lock_insert_attempted.set()

    class BlockingMaintenanceService(SleepLearningMaintenanceService):
        def _create_dynamic_eval_case_candidate(
            self,
            session: Session,
            trigger: MaintenanceTrigger,
            *,
            reason: str,
        ) -> MaintenanceOutput:
            output = super()._create_dynamic_eval_case_candidate(
                session,
                trigger,
                reason=reason,
            )
            first_effect_written.set()
            if not allow_first_commit.wait(timeout=5):
                raise AssertionError("second concurrent maintenance call did not start")
            return output

    def run_service(service: SleepLearningMaintenanceService) -> tuple[MaintenanceOutput, ...]:
        with session_scope(session_factory) as session:
            return service.handle_event_trigger(
                session,
                _safety_trigger(),
                idempotency_key="maintenance-concurrent",
            )

    event.listen(engine, "before_cursor_execute", observe_lock_insert)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(run_service, BlockingMaintenanceService())
            assert first_effect_written.wait(timeout=5)
            second = executor.submit(run_service, SleepLearningMaintenanceService())
            assert second_lock_insert_attempted.wait(timeout=5)
            allow_first_commit.set()
            first_outputs = first.result(timeout=5)
            second_outputs = second.result(timeout=5)
    finally:
        event.remove(engine, "before_cursor_execute", observe_lock_insert)

    assert second_outputs == first_outputs
    with session_scope(session_factory) as session:
        assert _draft_artifact_count(session) == 2
        assert _count(session, "SELECT count(*) FROM maintenance_job_receipts") == 1
        assert _count(session, "SELECT count(*) FROM maintenance_job_locks") == 1


def test_maintenance_rollback_removes_lock_receipt_and_partial_effects(
    tmp_path: Path,
) -> None:
    _settings, session_factory = _migrated(tmp_path)

    class FailingAfterFirstEffectService(SleepLearningMaintenanceService):
        def _create_retention_decision(self, *args: Any, **kwargs: Any) -> MaintenanceOutput:
            raise RuntimeError("crash before maintenance receipt insert")

    with (
        pytest.raises(RuntimeError, match="crash before maintenance receipt insert"),
        session_scope(session_factory) as session,
    ):
        FailingAfterFirstEffectService().handle_event_trigger(
            session,
            _safety_trigger(),
            idempotency_key="maintenance-rollback",
        )

    with session_scope(session_factory) as session:
        assert _draft_artifact_count(session) == 0
        assert _count(session, "SELECT count(*) FROM maintenance_job_receipts") == 0
        assert _count(session, "SELECT count(*) FROM maintenance_job_locks") == 0

    with session_scope(session_factory) as session:
        outputs = SleepLearningMaintenanceService().handle_event_trigger(
            session,
            _safety_trigger(),
            idempotency_key="maintenance-rollback",
        )

    assert len(outputs) == 2
    with session_scope(session_factory) as session:
        assert _draft_artifact_count(session) == 2
        assert _count(session, "SELECT count(*) FROM maintenance_job_receipts") == 1
        assert _count(session, "SELECT count(*) FROM maintenance_job_locks") == 1


def test_maintenance_idempotency_key_rejects_payload_mismatch(tmp_path: Path) -> None:
    _settings, session_factory = _migrated(tmp_path)
    service = SleepLearningMaintenanceService()

    with session_scope(session_factory) as session:
        service.handle_event_trigger(
            session,
            _safety_trigger("synthetic://safety/original"),
            idempotency_key="maintenance-same-key",
        )

    with (
        pytest.raises(ValueError, match="different payload"),
        session_scope(session_factory) as session,
    ):
        service.handle_event_trigger(
            session,
            _safety_trigger("synthetic://safety/changed"),
            idempotency_key="maintenance-same-key",
        )

    with session_scope(session_factory) as session:
        assert _draft_artifact_count(session) == 2
        assert _count(session, "SELECT count(*) FROM maintenance_job_receipts") == 1


def test_maintenance_receipts_do_not_store_raw_failure_or_evidence_values(
    tmp_path: Path,
) -> None:
    _settings, session_factory = _migrated(tmp_path)
    service = SleepLearningMaintenanceService()

    with session_scope(session_factory) as session:
        _seed_failed_trajectories(session, failure_tag="private-failure-tag")
        service.handle_event_trigger(
            session,
            MaintenanceTrigger(
                kind=MaintenanceTriggerKind.SAME_FAILURE_THRESHOLD,
                target_component=TARGET_COMPONENT,
                evidence_refs=("synthetic://safety/private-source",),
                failure_tag="private-failure-tag",
            ),
            idempotency_key="maintenance-private-values",
        )
        persisted = session.execute(
            text(
                """
                SELECT r.output_refs_json, l.payload_digest
                FROM maintenance_job_receipts r
                JOIN maintenance_job_locks l
                  ON l.idempotency_digest = r.idempotency_digest
                """
            )
        ).one()
        proposal_count = session.execute(
            text("SELECT count(*) FROM evolution_proposals WHERE proposer_id = 'sleep-learning'")
        ).scalar_one()
        proposal_draft_payload = session.execute(
            text(
                """
                SELECT artifact_json
                FROM evolution_artifacts
                WHERE artifact_kind = 'strategy_proposal_draft'
                """
            )
        ).scalar_one()
        artifact_payload = session.execute(
            text(
                """
                SELECT artifact_json
                FROM evolution_artifacts
                WHERE source_ref = 'synthetic://safety/private-source'
                  AND artifact_kind = 'dynamic_eval_case_candidate'
                """
            )
        ).scalar_one()

    persisted_text = str(persisted)
    assert proposal_count == 0
    assert "private-failure-tag" in str(proposal_draft_payload)
    assert "private-failure-tag" in str(artifact_payload)
    assert "private-failure-tag" not in persisted_text
    assert "synthetic://safety/private-source" not in persisted_text


def test_maintenance_receipts_are_immutable(tmp_path: Path) -> None:
    _settings, session_factory = _migrated(tmp_path)
    service = SleepLearningMaintenanceService()

    with session_scope(session_factory) as session:
        service.handle_event_trigger(
            session,
            _safety_trigger(),
            idempotency_key="maintenance-immutable",
        )

    with (
        pytest.raises(IntegrityError, match="append-only"),
        session_scope(session_factory) as session,
    ):
        session.execute(text("UPDATE maintenance_job_receipts SET output_refs_json = '[]'"))

    with (
        pytest.raises(IntegrityError, match="append-only"),
        session_scope(session_factory) as session,
    ):
        session.execute(text("DELETE FROM maintenance_job_receipts"))

    with (
        pytest.raises(IntegrityError, match="append-only"),
        session_scope(session_factory) as session,
    ):
        session.execute(text("UPDATE maintenance_job_locks SET payload_digest = 'changed'"))
