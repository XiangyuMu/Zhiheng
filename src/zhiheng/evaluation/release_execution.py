"""Release-bound local replay/shadow execution."""

from __future__ import annotations

import hmac
import json
import sqlite3
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any, cast

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from zhiheng.core.ids import json_text, new_id, sha256_json, sha256_text
from zhiheng.evaluation.execution_records import RUNNER_VERSION, verify_execution_record
from zhiheng.evaluation.g006_registry import REGISTERED_FIXED_CASES
from zhiheng.evaluation.g006_runner import (
    EvaluationSubject,
    G006ExecutionStage,
    ProtectedFixedSuiteRunner,
)
from zhiheng.evolution.contracts import ReleaseState
from zhiheng.evolution.releases import ReleaseController
from zhiheng.evolution.trajectory_repository import TrajectoryRepository

_SUPPORTED_STAGES = {
    G006ExecutionStage.REPLAY,
    G006ExecutionStage.SHADOW,
    G006ExecutionStage.CANARY,
}
_EXPECTED_PREVIOUS_STATES = {
    G006ExecutionStage.REPLAY: ReleaseState.PREPARED,
    G006ExecutionStage.SHADOW: ReleaseState.REPLAY,
    G006ExecutionStage.CANARY: ReleaseState.SHADOW,
}


class ReleaseExecutionService:
    def __init__(
        self, *, session_factory: sessionmaker[Session], project_root: Path,
        deployment_secret: str,
    ) -> None:
        if not deployment_secret:
            raise ValueError("execution records require a deployment secret")
        self._factory = session_factory
        self._project_root = project_root
        self._secret = deployment_secret

    def execute(
        self, *, release_id: str, stage: G006ExecutionStage, idempotency_key: str,
    ) -> dict[str, Any]:
        if stage not in _SUPPORTED_STAGES:
            raise ValueError("release execution supports only replay, shadow, and canary stages")
        if not idempotency_key:
            raise ValueError("execution requires an idempotency key")
        key_digest = sha256_text(f"{release_id}:{stage.value}:{idempotency_key}")
        with self._factory.begin() as session:
            raw = session.connection().connection.driver_connection
            if not isinstance(raw, sqlite3.Connection):
                raise TypeError("release execution requires SQLite")
            controller = ReleaseController.from_db(raw, deployment_secret=self._secret)
            release = controller.load_release(release_id)
            binding = release.binding
            existing = self._load_key(session, key_digest)
            if existing is not None:
                _validate_existing(
                    existing, release_id=release_id, stage=stage,
                    binding_digest=binding.canonical_digest(),
                )
                return existing
            expected_state = _EXPECTED_PREVIOUS_STATES[stage]
            if release.state is not expected_state:
                raise ValueError(
                    f"{stage.value} stage execution requires a {expected_state.value} release"
                )
            controller._validate_release_integrity(release)  # noqa: SLF001
            release_input = _load_release_input(session, release_id)
            proposal_id = str(release_input["proposal_id"])
            artifact = controller.load_release_artifact(release_id)
            baseline = controller.load_release(binding.rollback_target_id)
            baseline_artifact = controller.load_release_artifact(binding.rollback_target_id)
        # No production database transaction spans fixed-case execution.
        with tempfile.TemporaryDirectory(prefix="zhiheng-release-evaluation-") as temporary:
            run = ProtectedFixedSuiteRunner(project_root=self._project_root).run(
                EvaluationSubject.from_artifact(
                    candidate_id=binding.candidate_id,
                    target_component=binding.target_component,
                    artifact_payload=artifact,
                    baseline_artifact_payload=baseline_artifact,
                    release_id=release_id,
                ),
                stage=stage,
                work_dir=Path(temporary),
            )
        record: dict[str, Any] = {
            "id": new_id(),
            "release_id": release_id,
            "proposal_id": proposal_id,
            "idempotency_digest": key_digest,
            "runner_version": RUNNER_VERSION,
            "profile": "local_contract",
            "stage": stage.value,
            "release_state": release.state.value,
            "binding": binding.canonical_payload(),
            "binding_digest": binding.canonical_digest(),
            "artifact_digest": binding.approved_artifact_digest,
            "baseline_binding_digest": baseline.binding_digest,
            "suite_digest": sha256_json({
                "cases": [asdict(case) for case in REGISTERED_FIXED_CASES]
            }),
            "source_validation": {
                "validation_report_id": release_input["validation_report_id"],
                "proposal_execution_run_id": release_input["proposal_execution_run_id"],
                "source_trajectory_ids": release_input["source_trajectory_ids"],
                "fixed_report_digest": release_input["fixed_report_digest"],
            },
            "cases": [asdict(case) for case in run.observed_cases],
            "report": run.evaluation_report.canonical_payload(),
        }
        with self._factory.begin() as session:
            # Serialize racing retries; keep the first complete immutable run.
            session.execute(text("BEGIN IMMEDIATE"))
            existing = self._load_key(session, key_digest)
            if existing is not None:
                _validate_existing(
                    existing, release_id=release_id, stage=stage,
                    binding_digest=binding.canonical_digest(),
                )
                return existing
            raw = session.connection().connection.driver_connection
            if not isinstance(raw, sqlite3.Connection):
                raise TypeError("release execution requires SQLite")
            fresh = ReleaseController.from_db(raw, deployment_secret=self._secret).load_release(
                release_id
            )
            expected_state = _EXPECTED_PREVIOUS_STATES[stage]
            if (
                fresh.state is not expected_state
                or fresh.binding.canonical_digest() != binding.canonical_digest()
            ):
                raise ValueError("release changed before stage execution could be recorded")
            record["trajectory_ids"] = list(ProtectedFixedSuiteRunner(
                project_root=self._project_root
            ).persist_trajectories(
                run,
                repository=TrajectoryRepository(deployment_secret=self._secret),
                idempotency_key_prefix=f"release-execution:{record['id']}",
                session=session,
            ))
            encoded = json_text(record)
            session.execute(text(
                "INSERT INTO release_execution_runs "
                "(id, release_id, proposal_id, stage, idempotency_digest, "
                "record_json, record_hmac) "
                "VALUES (:id, :release, :proposal, :stage, :key, :record, :signature)"
            ), {
                "id": record["id"],
                "release": release_id,
                "proposal": proposal_id,
                "stage": stage.value,
                "key": key_digest,
                "record": encoded,
                "signature": self._signature(encoded),
            })
        return json.loads(encoded)  # type: ignore[no-any-return]

    def load(self, run_id: str) -> dict[str, Any]:
        with self._factory.begin() as session:
            row = session.execute(text(
                "SELECT * FROM release_execution_runs WHERE id = :id"
            ), {"id": run_id}).mappings().one()
            return self._verify(dict(row))

    def _load_key(self, session: Session, digest: str) -> dict[str, Any] | None:
        row = session.execute(text(
            "SELECT * FROM release_execution_runs WHERE idempotency_digest = :key"
        ), {"key": digest}).mappings().first()
        return self._verify(dict(row)) if row is not None else None

    def _signature(self, encoded: str) -> str:
        return hmac.new(
            self._secret.encode(), (RUNNER_VERSION + "\n" + encoded).encode(), "sha256"
        ).hexdigest()

    def _verify(self, row: dict[str, Any]) -> dict[str, Any]:
        record = verify_execution_record(row, secret=self._secret)
        if (
            record.get("release_id") != row["release_id"]
            or record.get("stage") != row["stage"]
        ):
            raise ValueError("release execution record row binding mismatch")
        return record


def _load_release_input(session: Session, release_id: str) -> dict[str, Any]:
    row = session.execute(text(
        """
        SELECT ri.proposal_id, ri.validation_report_id, ri.source_trajectory_ids_json,
               vr.fixed_set_result_json
        FROM strategy_releases sr
        JOIN release_inputs ri ON ri.id = sr.release_input_id
        JOIN validation_reports vr ON vr.id = ri.validation_report_id
        WHERE sr.id = :release_id
        """
    ), {"release_id": release_id}).mappings().one()
    source = json.loads(cast(str, row["source_trajectory_ids_json"]))
    fixed_report = json.loads(cast(str, row["fixed_set_result_json"]))
    if not isinstance(source, dict):
        raise ValueError("release input source evidence is missing")
    execution_run_id = fixed_report.get("execution_run_id")
    if not isinstance(execution_run_id, str) or not execution_run_id:
        raise ValueError("release validation evidence lacks protected execution run")
    return {
        "proposal_id": row["proposal_id"],
        "validation_report_id": row["validation_report_id"],
        "proposal_execution_run_id": execution_run_id,
        "source_trajectory_ids": source.get("source_trajectory_ids", []),
        "fixed_report_digest": fixed_report.get("report_digest"),
    }


def _validate_existing(
    record: dict[str, Any], *, release_id: str, stage: G006ExecutionStage,
    binding_digest: str,
) -> None:
    if (
        record.get("release_id") != release_id
        or record.get("stage") != stage.value
        or record.get("binding_digest") != binding_digest
    ):
        raise ValueError("execution idempotency key belongs to another release binding")
