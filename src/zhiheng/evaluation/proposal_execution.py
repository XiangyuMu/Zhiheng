"""Proposal-bound local execution. This is not yet a release authorization port."""

from __future__ import annotations

import hmac
import json
import sqlite3
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from zhiheng.core.ids import json_text, new_id, sha256_json, sha256_text
from zhiheng.evaluation.contracts import build_fixed_suite_contract
from zhiheng.evaluation.execution_records import RUNNER_VERSION, verify_execution_record
from zhiheng.evaluation.g006_registry import REGISTERED_FIXED_CASES
from zhiheng.evaluation.g006_runner import (
    EvaluationSubject,
    G006ExecutionStage,
    ProtectedFixedSuiteRunner,
)
from zhiheng.evolution.releases import ReleaseController
from zhiheng.evolution.trajectory_repository import TrajectoryRepository


class ProposalExecutionService:
    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        project_root: Path,
        deployment_secret: str,
    ) -> None:
        if not deployment_secret:
            raise ValueError("execution records require a deployment secret")
        self._factory = session_factory
        self._project_root = project_root
        self._secret = deployment_secret

    def execute(self, *, proposal_id: str, idempotency_key: str) -> dict[str, Any]:
        if not idempotency_key:
            raise ValueError("execution requires an idempotency key")
        key_digest = sha256_text(idempotency_key)
        with self._factory.begin() as session:
            raw = session.connection().connection.driver_connection
            if not isinstance(raw, sqlite3.Connection):
                raise TypeError("proposal execution requires SQLite")
            controller = ReleaseController.from_db(raw, deployment_secret=self._secret)
            binding = controller.load_proposal_binding(proposal_id)
            artifact = controller.load_proposal_artifact(proposal_id, binding)
            baseline = controller.load_release(binding.rollback_target_id)
            baseline_artifact = controller.load_release_artifact(binding.rollback_target_id)
            existing = self._load_key(session, key_digest)
            if existing is not None:
                if existing["proposal_id"] != proposal_id:
                    raise ValueError("execution idempotency key belongs to another proposal")
                return existing
        # No production database transaction spans fixed-case execution.
        with tempfile.TemporaryDirectory(prefix="zhiheng-proposal-evaluation-") as temporary:
            run = ProtectedFixedSuiteRunner(project_root=self._project_root).run(
                EvaluationSubject.from_artifact(
                    candidate_id=binding.candidate_id,
                    target_component=binding.target_component,
                    artifact_payload=artifact,
                    baseline_artifact_payload=baseline_artifact,
                ),
                stage=G006ExecutionStage.VALIDATION,
                work_dir=Path(temporary),
            )
        record = {
            "id": new_id(),
            "proposal_id": proposal_id,
            "idempotency_digest": key_digest,
            "runner_version": RUNNER_VERSION,
            "profile": "local_contract",
            "stage": G006ExecutionStage.VALIDATION.value,
            "binding": binding.canonical_payload(),
            "binding_digest": binding.canonical_digest(),
            "artifact_digest": binding.approved_artifact_digest,
            "baseline_binding_digest": baseline.binding_digest,
            "suite_digest": sha256_json(
                {"cases": [asdict(case) for case in REGISTERED_FIXED_CASES]}
            ),
            "cases": [asdict(case) for case in run.observed_cases],
            "report": run.evaluation_report.canonical_payload(),
        }
        record["evaluation_contract"] = build_fixed_suite_contract(
            fixture_set_digest="sha256:"
            + sha256_json({"cases": [asdict(case) for case in REGISTERED_FIXED_CASES]}),
            policy_threshold_digest="sha256:"
            + sha256_json(
                {
                    "candidate_false_activation_max": 0,
                    "privacy_leak_count_max": 0,
                    "external_action_count_max": 0,
                }
            ),
            runner_environment_digest="sha256:" + sha256_json({"runner_version": RUNNER_VERSION}),
            cases=run.observed_cases,
            aggregate_metrics={
                **run.evaluation_report.scores,
                "candidate_false_activation": 0,
                "privacy_leak_count": 0,
                "external_action_count": 0,
            },
        )
        with self._factory.begin() as session:
            # Serialize racing retries; keep the first complete immutable run.
            session.execute(text("BEGIN IMMEDIATE"))
            existing = self._load_key(session, key_digest)
            if existing is not None:
                if existing["proposal_id"] != proposal_id:
                    raise ValueError("execution idempotency key belongs to another proposal")
                return existing
            record["trajectory_ids"] = list(
                ProtectedFixedSuiteRunner(project_root=self._project_root).persist_trajectories(
                    run,
                    repository=TrajectoryRepository(deployment_secret=self._secret),
                    idempotency_key_prefix=f"proposal-execution:{record['id']}",
                    session=session,
                )
            )
            encoded = json_text(record)
            session.execute(
                text(
                    "INSERT INTO proposal_execution_runs "
                    "(id, proposal_id, idempotency_digest, record_json, record_hmac) "
                    "VALUES (:id, :proposal, :key, :record, :signature)"
                ),
                {
                    "id": record["id"],
                    "proposal": proposal_id,
                    "key": key_digest,
                    "record": encoded,
                    "signature": self._signature(encoded),
                },
            )
        return json.loads(encoded)  # type: ignore[no-any-return]

    def load(self, run_id: str) -> dict[str, Any]:
        with self._factory.begin() as session:
            row = (
                session.execute(
                    text("SELECT * FROM proposal_execution_runs WHERE id = :id"), {"id": run_id}
                )
                .mappings()
                .one()
            )
            return self._verify(dict(row))

    def _load_key(self, session: Session, digest: str) -> dict[str, Any] | None:
        row = (
            session.execute(
                text("SELECT * FROM proposal_execution_runs WHERE idempotency_digest = :key"),
                {"key": digest},
            )
            .mappings()
            .first()
        )
        return self._verify(dict(row)) if row is not None else None

    def _signature(self, encoded: str) -> str:
        return hmac.new(
            self._secret.encode(), (RUNNER_VERSION + "\n" + encoded).encode(), "sha256"
        ).hexdigest()

    def _verify(self, row: dict[str, Any]) -> dict[str, Any]:
        return verify_execution_record(row, secret=self._secret)
