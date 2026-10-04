from __future__ import annotations

import json
import os
import shutil
import sqlite3
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pytest

from tests.integration.release_helpers import (
    insert_signed_canary_observations,
    prepare_release_with_persisted_evidence,
)
from tests.integration.test_g006_release_lifecycle import (
    _assignment,
    _binding,
    _bootstrap_stable,
    _publisher_context,
    _upgrade,
    _user_approval_context,
)
from zhiheng.core.config import Settings
from zhiheng.db.session import create_session_factory, create_sqlite_engine
from zhiheng.evaluation.g006_runner import G006ExecutionStage
from zhiheng.evaluation.proposal_execution import ProposalExecutionService
from zhiheng.evaluation.release_execution import ReleaseExecutionService
from zhiheng.evolution.contracts import ReleaseState
from zhiheng.evolution.releases import ReleaseContext, ReleaseController
from zhiheng.evolution.trajectory_repository import TrajectoryRepository

_REQUIRES_RESTIC = pytest.mark.skipif(
    not (os.environ.get("ZHIHENG_RESTIC_BINARY") or shutil.which("restic")),
    reason="configure real restic to execute full G006 preparation contract",
)


@_REQUIRES_RESTIC
def test_preparation_reuse_preserves_independent_candidate_and_stage_evidence(
    tmp_path: Path,
) -> None:
    database = tmp_path / "release-preparation-evidence.sqlite"
    secret = "synthetic-g006-preparation-evidence-secret"

    with _upgrade(database) as connection:
        controller = ReleaseController.from_db(connection, deployment_secret=secret)
        baseline_id = _bootstrap_stable(controller)

        first = prepare_release_with_persisted_evidence(
            controller,
            binding=_binding("preparation-evidence-a", baseline_id),
            proposer_id="proposer-a",
            reviewer_id="reviewer-a",
            canary_assignment=_assignment("preparation-evidence-a"),
            canary_samples=5,
            request_id="prepare-preparation-evidence-a",
        )
        second = prepare_release_with_persisted_evidence(
            controller,
            binding=_binding("preparation-evidence-b", baseline_id),
            proposer_id="proposer-b",
            reviewer_id="reviewer-b",
            canary_assignment=_assignment("preparation-evidence-b"),
            canary_samples=5,
            request_id="prepare-preparation-evidence-b",
        )

        stage_engine = create_sqlite_engine(
            Settings(environment="test", database_url=f"sqlite:///{database}")
        )
        try:
            stage_service = ReleaseExecutionService(
                session_factory=create_session_factory(stage_engine),
                project_root=Path.cwd(),
                deployment_secret=secret,
            )
            first_replay_run = _execute_stage(
                stage_service,
                release_id=first.release_id,
                stage=G006ExecutionStage.REPLAY,
            )
            _advance_stage(
                controller,
                release_id=first.release_id,
                stage=G006ExecutionStage.REPLAY,
                execution_run_id=first_replay_run["id"],
            )
            with pytest.raises(ValueError, match="stage execution release binding mismatch"):
                controller.advance_release_stage(
                    second.release_id,
                    next_state=ReleaseState.REPLAY,
                    publisher_context=_publisher_context(),
                    execution_run_id=first_replay_run["id"],
                    request_id="wrong-candidate-valid-replay-run",
                    step="replay",
                )
            assert controller.load_release(first.release_id).state is ReleaseState.REPLAY
            assert controller.load_release(second.release_id).state is ReleaseState.PREPARED

            second_replay_run = _execute_stage(
                stage_service,
                release_id=second.release_id,
                stage=G006ExecutionStage.REPLAY,
            )
            _advance_stage(
                controller,
                release_id=second.release_id,
                stage=G006ExecutionStage.REPLAY,
                execution_run_id=second_replay_run["id"],
            )
            with pytest.raises(ValueError, match="stage execution release binding mismatch"):
                controller.advance_release_stage(
                    second.release_id,
                    next_state=ReleaseState.SHADOW,
                    publisher_context=_publisher_context(),
                    execution_run_id=second_replay_run["id"],
                    request_id="wrong-stage-valid-replay-run",
                    step="shadow",
                )
            assert controller.load_release(second.release_id).state is ReleaseState.REPLAY
            stable_head = controller.load_default_head(first.target_component)
            assert stable_head is not None
            assert stable_head.release_id == baseline_id

            _execute_and_advance_stage(
                controller,
                stage_service,
                release_id=first.release_id,
                stage=G006ExecutionStage.SHADOW,
            )
            first_canary = _execute_and_advance_stage(
                controller,
                stage_service,
                release_id=first.release_id,
                stage=G006ExecutionStage.CANARY,
            )
            _execute_and_advance_stage(
                controller,
                stage_service,
                release_id=second.release_id,
                stage=G006ExecutionStage.SHADOW,
            )
            second_canary = _execute_and_advance_stage(
                controller,
                stage_service,
                release_id=second.release_id,
                stage=G006ExecutionStage.CANARY,
            )
        finally:
            stage_engine.dispose()
        assert first_canary.release_id != second_canary.release_id

        insert_signed_canary_observations(
            connection,
            first.release_id,
            first.binding,
            cohort="preparation-evidence-a",
            deployment_secret=secret,
        )
        promoted = controller.promote_release(
            first.release_id,
            publisher_context=_publisher_context(),
            user_approval_context=_user_approval_context(),
            request_id="promote-preparation-evidence-a",
        )
        assert promoted.release_id == first.release_id

    engine = create_sqlite_engine(
        Settings(environment="test", database_url=f"sqlite:///{database}")
    )
    factory = create_session_factory(engine)
    try:
        proposal_service = ProposalExecutionService(
            session_factory=factory,
            project_root=Path.cwd(),
            deployment_secret=secret,
        )
        release_service = ReleaseExecutionService(
            session_factory=factory,
            project_root=Path.cwd(),
            deployment_secret=secret,
        )
        proposal_runs = _proposal_runs(database, secret)
        release_runs = _release_runs(database, secret)

        assert {run["binding"]["candidate_id"] for run in proposal_runs} == {
            first.binding.candidate_id,
            second.binding.candidate_id,
        }
        assert len({run["id"] for run in proposal_runs}) == 2
        assert len({run["binding_digest"] for run in proposal_runs}) == 2
        assert all(run["stage"] == G006ExecutionStage.VALIDATION.value for run in proposal_runs)

        by_candidate_stage = {
            (run["binding"]["candidate_id"], run["stage"]): run for run in release_runs
        }
        assert set(by_candidate_stage) == {
            (first.binding.candidate_id, G006ExecutionStage.REPLAY.value),
            (first.binding.candidate_id, G006ExecutionStage.SHADOW.value),
            (first.binding.candidate_id, G006ExecutionStage.CANARY.value),
            (second.binding.candidate_id, G006ExecutionStage.REPLAY.value),
            (second.binding.candidate_id, G006ExecutionStage.SHADOW.value),
            (second.binding.candidate_id, G006ExecutionStage.CANARY.value),
        }
        assert len({run["id"] for run in release_runs}) == 6
        assert len({tuple(run["trajectory_ids"]) for run in release_runs}) == 6

        for run in (*proposal_runs, *release_runs):
            assert run["evaluation_contract"]["status"] == "passed"
            assert run["report"]["promotion_eligible"] is True
            assert len(run["trajectory_ids"]) == len(run["cases"])
            assert set(run["trajectory_ids"]).isdisjoint(
                other
                for other_run in (*proposal_runs, *release_runs)
                if other_run["id"] != run["id"]
                for other in other_run["trajectory_ids"]
            )

        repeated_proposal = proposal_service.execute(
            proposal_id=proposal_runs[0]["proposal_id"],
            idempotency_key=f"prepare-release:{proposal_runs[0]['proposal_id']}",
        )
        assert repeated_proposal["id"] == proposal_runs[0]["id"]
        repeated_release = release_service.execute(
            release_id=second.release_id,
            stage=G006ExecutionStage.CANARY,
            idempotency_key=f"advance-{second.release_id}-canary",
        )
        assert repeated_release["id"] == by_candidate_stage[
            (second.binding.candidate_id, G006ExecutionStage.CANARY.value)
        ]["id"]

        _assert_trajectory_bindings(
            database,
            secret,
            proposal_runs=proposal_runs,
            release_runs=release_runs,
        )

    finally:
        engine.dispose()

    _tamper_json_column(
        database,
        table="proposal_execution_runs",
        row_id=proposal_runs[0]["id"],
        mutator=lambda record: record.update({"binding_digest": "sha256:tampered"}),
    )
    with pytest.raises(ValueError, match="signature"):
        _load_proposal_run(database, secret, proposal_runs[0]["id"])

    shadow_run_id = by_candidate_stage[
        (second.binding.candidate_id, G006ExecutionStage.SHADOW.value)
    ]["id"]
    _tamper_json_column(
        database,
        table="release_execution_runs",
        row_id=shadow_run_id,
        mutator=lambda record: record.update({"stage": "tampered-stage"}),
    )
    with pytest.raises(ValueError, match="signature"):
        _load_release_run(database, secret, shadow_run_id)


def _execute_and_advance_stage(
    controller: ReleaseController,
    service: ReleaseExecutionService,
    *,
    release_id: str,
    stage: G006ExecutionStage,
) -> ReleaseContext:
    execution = _execute_stage(service, release_id=release_id, stage=stage)
    return _advance_stage(
        controller,
        release_id=release_id,
        stage=stage,
        execution_run_id=execution["id"],
    )


def _execute_stage(
    service: ReleaseExecutionService,
    *,
    release_id: str,
    stage: G006ExecutionStage,
) -> dict[str, Any]:
    return service.execute(
        release_id=release_id,
        stage=stage,
        idempotency_key=f"advance-{release_id}-{stage.value}",
    )


def _advance_stage(
    controller: ReleaseController,
    *,
    release_id: str,
    stage: G006ExecutionStage,
    execution_run_id: str,
) -> ReleaseContext:
    return controller.advance_release_stage(
        release_id,
        next_state=ReleaseState(stage.value),
        publisher_context=_publisher_context(),
        execution_run_id=execution_run_id,
        request_id=f"advance-{release_id}-{stage.value}",
        step=stage.value,
    )


def _proposal_runs(database: Path, secret: str) -> list[dict[str, Any]]:
    engine = create_sqlite_engine(Settings(environment="test", database_url=f"sqlite:///{database}"))
    factory = create_session_factory(engine)
    try:
        service = ProposalExecutionService(
            session_factory=factory,
            project_root=Path.cwd(),
            deployment_secret=secret,
        )
        with sqlite3.connect(database) as connection:
            ids = [
                row[0]
                for row in connection.execute(
                    "SELECT id FROM proposal_execution_runs ORDER BY id"
                ).fetchall()
            ]
        return [service.load(run_id) for run_id in ids]
    finally:
        engine.dispose()


def _release_runs(database: Path, secret: str) -> list[dict[str, Any]]:
    engine = create_sqlite_engine(Settings(environment="test", database_url=f"sqlite:///{database}"))
    factory = create_session_factory(engine)
    try:
        service = ReleaseExecutionService(
            session_factory=factory,
            project_root=Path.cwd(),
            deployment_secret=secret,
        )
        with sqlite3.connect(database) as connection:
            ids = [
                row[0]
                for row in connection.execute(
                    "SELECT id FROM release_execution_runs ORDER BY id"
                ).fetchall()
            ]
        return [service.load(run_id) for run_id in ids]
    finally:
        engine.dispose()


def _load_proposal_run(database: Path, secret: str, run_id: str) -> dict[str, Any]:
    engine = create_sqlite_engine(Settings(environment="test", database_url=f"sqlite:///{database}"))
    factory = create_session_factory(engine)
    try:
        return ProposalExecutionService(
            session_factory=factory,
            project_root=Path.cwd(),
            deployment_secret=secret,
        ).load(run_id)
    finally:
        engine.dispose()


def _load_release_run(database: Path, secret: str, run_id: str) -> dict[str, Any]:
    engine = create_sqlite_engine(Settings(environment="test", database_url=f"sqlite:///{database}"))
    factory = create_session_factory(engine)
    try:
        return ReleaseExecutionService(
            session_factory=factory,
            project_root=Path.cwd(),
            deployment_secret=secret,
        ).load(run_id)
    finally:
        engine.dispose()


def _assert_trajectory_bindings(
    database: Path,
    secret: str,
    *,
    proposal_runs: Iterable[dict[str, Any]],
    release_runs: Iterable[dict[str, Any]],
) -> None:
    engine = create_sqlite_engine(Settings(environment="test", database_url=f"sqlite:///{database}"))
    factory = create_session_factory(engine)
    try:
        repository = TrajectoryRepository(deployment_secret=secret, session_factory=factory)
        for run in proposal_runs:
            for trajectory_id in run["trajectory_ids"]:
                envelope = repository.get(trajectory_id).envelope
                assert envelope.process["stage"] == G006ExecutionStage.VALIDATION.value
                assert envelope.process["artifact_digest"] == run["artifact_digest"]
                assert envelope.process["release_id"] is None
                assert f":{run['binding']['candidate_id']}:" in envelope.trajectory_id
        for run in release_runs:
            for trajectory_id in run["trajectory_ids"]:
                envelope = repository.get(trajectory_id).envelope
                assert envelope.process["stage"] == run["stage"]
                assert envelope.process["release_id"] == run["release_id"]
                assert envelope.process["artifact_digest"] == run["artifact_digest"]
                assert f":{run['binding']['candidate_id']}:" in envelope.trajectory_id
    finally:
        engine.dispose()


def _tamper_json_column(
    database: Path,
    *,
    table: str,
    row_id: str,
    mutator: Any,
) -> None:
    with sqlite3.connect(database) as connection:
        connection.execute(f"DROP TRIGGER {table}_no_update")
        record_json = connection.execute(
            f"SELECT record_json FROM {table} WHERE id = ?",
            (row_id,),
        ).fetchone()[0]
        record = json.loads(record_json)
        mutator(record)
        connection.execute(
            f"UPDATE {table} SET record_json = ? WHERE id = ?",
            (json.dumps(record, sort_keys=True), row_id),
        )
        connection.commit()
