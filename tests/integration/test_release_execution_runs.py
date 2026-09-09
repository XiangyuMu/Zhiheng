import os
import shutil
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import event, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from tests.integration.release_helpers import prepare_release_with_persisted_evidence
from tests.integration.test_g006_release_lifecycle import (
    _assignment,
    _binding,
    _bootstrap_stable,
    _publisher_context,
    _upgrade,
)
from zhiheng.core.config import Settings
from zhiheng.db.session import create_session_factory, create_sqlite_engine
from zhiheng.evaluation.g006_runner import (
    G006ExecutionStage,
    ProtectedFixedSuiteRun,
    ProtectedFixedSuiteRunner,
)
from zhiheng.evaluation.release_execution import ReleaseExecutionService
from zhiheng.evolution.contracts import ReleaseState
from zhiheng.evolution.releases import ReleaseController
from zhiheng.evolution.trajectory_repository import TrajectoryRepository


def test_release_execution_records_replay_and_shadow_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not (os.environ.get("ZHIHENG_RESTIC_BINARY") or shutil.which("restic")):
        pytest.skip("configure real restic to execute full release-stage contract")
    database = tmp_path / "release-execution.sqlite"
    secret = "synthetic-release-execution-secret"
    connection = _upgrade(database)
    try:
        controller = ReleaseController.from_db(connection, deployment_secret=secret)
        binding = _binding("release-execution-probe", _bootstrap_stable(controller))
        prepared = prepare_release_with_persisted_evidence(
            controller,
            binding=binding,
            proposer_id="proposer-a",
            reviewer_id="reviewer-b",
            canary_assignment=_assignment("release-execution"),
            canary_samples=5,
            request_id="prepare-release-execution",
        )
    finally:
        connection.close()

    engine = create_sqlite_engine(Settings(environment="test", database_url=f"sqlite:///{database}"))
    factory = create_session_factory(engine)
    begun: list[Connection] = []
    event.listen(engine, "begin", begun.append)
    run_original = ProtectedFixedSuiteRunner.run

    def checked_run(*args: Any, **kwargs: Any) -> ProtectedFixedSuiteRun:
        assert begun
        assert all(not connection.in_transaction() for connection in begun)
        return run_original(*args, **kwargs)

    monkeypatch.setattr(ProtectedFixedSuiteRunner, "run", checked_run)
    service = ReleaseExecutionService(
        session_factory=factory,
        project_root=Path.cwd(),
        deployment_secret=secret,
    )
    try:
        stage_connection = sqlite3.connect(database)
        stage_connection.execute("PRAGMA foreign_keys=ON")
        stage_controller = ReleaseController.from_db(stage_connection, deployment_secret=secret)
        with pytest.raises(ValueError, match="requires a shadow release"):
            service.execute(
                release_id=prepared.release_id,
                stage=G006ExecutionStage.CANARY,
                idempotency_key="stage-key",
            )
        executed: dict[G006ExecutionStage, dict[str, Any]] = {}
        for stage, next_state in (
            (G006ExecutionStage.REPLAY, ReleaseState.REPLAY),
            (G006ExecutionStage.SHADOW, ReleaseState.SHADOW),
            (G006ExecutionStage.CANARY, ReleaseState.CANARY),
        ):
            record = service.execute(
                release_id=prepared.release_id,
                stage=stage,
                idempotency_key="stage-key",
            )
            executed[stage] = record
            assert record["release_id"] == prepared.release_id
            assert record["proposal_id"]
            assert record["stage"] == stage.value
            assert record["binding"] == binding.canonical_payload()
            assert record["binding_digest"] == binding.canonical_digest()
            assert record["artifact_digest"] == binding.approved_artifact_digest
            assert record["baseline_binding_digest"]
            assert record["source_validation"]["proposal_execution_run_id"]
            assert record["report"]["promotion_eligible"] is True
            assert record["report"]["failure_count"] == 0
            assert len(record["cases"]) == 7
            assert len(set(record["trajectory_ids"])) == 7
            assert service.load(record["id"]) == record
            assert service.execute(
                release_id=prepared.release_id,
                stage=stage,
                idempotency_key="stage-key",
            ) == record
            trajectories = TrajectoryRepository(
                deployment_secret=secret,
                session_factory=factory,
            )
            for trajectory_id in record["trajectory_ids"]:
                trajectory = trajectories.get(trajectory_id)
                assert trajectory.envelope.process["stage"] == stage.value
            stage_controller.advance_release_stage(
                prepared.release_id,
                next_state=next_state,
                publisher_context=_publisher_context(),
                execution_run_id=record["id"],
                request_id=f"advance-{stage.value}",
                step=stage.value,
            )
        stage_connection.close()

        with factory.begin() as session:
            assert session.execute(text(
                "SELECT count(*) FROM release_execution_runs"
            )).scalar_one() == 3
        for operation in ("UPDATE release_execution_runs SET record_hmac = 'fake'",
                          "DELETE FROM release_execution_runs"):
            with pytest.raises(IntegrityError, match="append-only"), factory.begin() as session:
                session.execute(text(operation))
        other_secret = ReleaseExecutionService(
            session_factory=factory,
            project_root=Path.cwd(),
            deployment_secret="wrong-secret",
        )
        first_id = service.execute(
            release_id=prepared.release_id,
            stage=G006ExecutionStage.REPLAY,
            idempotency_key="stage-key",
        )["id"]
        assert first_id == executed[G006ExecutionStage.REPLAY]["id"]
        with pytest.raises(ValueError, match="signature"):
            other_secret.load(first_id)
    finally:
        engine.dispose()


def test_release_execution_rejects_unsupported_stages(tmp_path: Path) -> None:
    database = tmp_path / "unsupported-stage.sqlite"
    connection = _upgrade(database)
    try:
        controller = ReleaseController.from_db(connection)
        release_id = _bootstrap_stable(controller)
    finally:
        connection.close()
    engine = create_sqlite_engine(Settings(environment="test", database_url=f"sqlite:///{database}"))
    service = ReleaseExecutionService(
        session_factory=create_session_factory(engine),
        project_root=Path.cwd(),
        deployment_secret="synthetic-secret",
    )
    try:
        with pytest.raises(ValueError, match="replay, shadow, and canary"):
            service.execute(
                release_id=release_id,
                stage=G006ExecutionStage.VALIDATION,
                idempotency_key="unsupported",
            )
    finally:
        engine.dispose()


def test_release_execution_records_require_existing_release_and_proposal(
    tmp_path: Path,
) -> None:
    connection = _upgrade(tmp_path / "foreign-key.sqlite")
    try:
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            connection.execute(
                """
                INSERT INTO release_execution_runs (
                  id, release_id, proposal_id, stage, idempotency_digest,
                  record_json, record_hmac
                ) VALUES ('run', 'missing-release', 'missing-proposal', 'replay', 'key',
                          '{}', 'fake')
                """
            )
    finally:
        connection.close()
