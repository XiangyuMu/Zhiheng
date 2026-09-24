import os
import shutil
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import event, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from tests.integration.test_g006_release_lifecycle import _binding, _bootstrap_stable, _upgrade
from zhiheng.core.config import Settings
from zhiheng.db.session import create_session_factory, create_sqlite_engine
from zhiheng.evaluation.g006_runner import ProtectedFixedSuiteRun, ProtectedFixedSuiteRunner
from zhiheng.evaluation.proposal_execution import ProposalExecutionService
from zhiheng.evolution.artifacts import default_release_artifact
from zhiheng.evolution.contracts import EvolutionRole, command_context_for_role
from zhiheng.evolution.releases import ReleaseController
from zhiheng.evolution.trajectory_repository import TrajectoryRepository


@pytest.mark.parametrize("failure", ["missing_restic", "unindexed_knowledge"])
def test_execution_uses_immutable_proposal_and_records_failed_suite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    if failure == "missing_restic":
        # Exercise a real dependency failure independently of the host's setup.
        monkeypatch.delenv("ZHIHENG_RESTIC_BINARY", raising=False)
        monkeypatch.setattr("zhiheng.evaluation.g006_recovery_case.shutil.which", lambda _: None)
    else:
        if not (os.environ.get("ZHIHENG_RESTIC_BINARY") or shutil.which("restic")):
            pytest.skip("real restic required to isolate the serving-fixture regression")
        # Reproduce the original serving-fixture regression without weakening
        # production authorization or manufacturing evaluation scores.
        monkeypatch.setattr(
            "zhiheng.evaluation.g006_knowledge_case.mark_formal_knowledge_indexed",
            lambda session, knowledge_object_id: None,
        )
    database = tmp_path / "execution.sqlite"
    connection = _upgrade(database)
    try:
        controller = ReleaseController.from_db(connection)
        binding = _binding("execution-probe", _bootstrap_stable(controller))
        proposal = controller.create_release_proposal(
            binding=binding,
            artifact_payload=default_release_artifact(),
            proposer_context=command_context_for_role("proposer", EvolutionRole.PROPOSER),
        )
    finally:
        connection.close()
    engine = create_sqlite_engine(
        Settings(environment="test", database_url=f"sqlite:///{database}")
    )
    factory = create_session_factory(engine)
    begun: list[Connection] = []
    event.listen(engine, "begin", begun.append)
    run_original = ProtectedFixedSuiteRunner.run

    def checked_run(*args: Any, **kwargs: Any) -> ProtectedFixedSuiteRun:
        assert begun
        assert all(not connection.in_transaction() for connection in begun)
        return run_original(*args, **kwargs)

    monkeypatch.setattr(ProtectedFixedSuiteRunner, "run", checked_run)
    service = ProposalExecutionService(
        session_factory=factory,
        project_root=Path.cwd(),
        deployment_secret="synthetic-secret",
    )
    try:
        record = service.execute(proposal_id=proposal.proposal_id, idempotency_key="probe")
        assert record["binding_digest"] == binding.canonical_digest()
        assert record["artifact_digest"] == binding.approved_artifact_digest
        assert len(record["cases"]) == 7
        assert len(set(record["trajectory_ids"])) == 7
        trajectories = TrajectoryRepository(
            deployment_secret="synthetic-secret",
            session_factory=factory,
        )
        for trajectory_id, observed in zip(record["trajectory_ids"], record["cases"], strict=True):
            trajectory = trajectories.get(trajectory_id)
            assert (
                trajectory.envelope.process["observation_digest"]
                == (observed["observation_digest"])
            )
            assert trajectory.envelope.created_at == observed["observed_at"]
            linked = {"case_id": observed["case_id"], "process": dict(trajectory.envelope.process)}
            ReleaseController._validate_execution_case_links(record, [linked])
            linked["process"]["observation_digest"] = "substituted-history"
            with pytest.raises(ValueError, match="differ from protected execution"):
                ReleaseController._validate_execution_case_links(record, [linked])
        assert record["report"]["promotion_eligible"] is False
        assert record["evaluation_contract"]["status"] == "failed"
        assert record["report"]["failure_count"] == (2 if failure == "unindexed_knowledge" else 1)
        boundary = next(
            case
            for case in record["cases"]
            if case["case_id"] == "boundary-rag-citation-conflict-001"
        )
        if failure == "unindexed_knowledge":
            assert {case["case_id"] for case in record["cases"] if case["failure_tags"]} == {
                "boundary-rag-citation-conflict-001",
                "migration-answer-strategy-transfer-001",
            }
            assert "assertion_failed:rag.recall_at_10" in boundary["failure_tags"]
            assert "assertion_failed:rag.citation_coverage" in boundary["failure_tags"]
            assert "assertion_failed:rag.conflict_detected" in boundary["failure_tags"]
        else:
            assert boundary["failure_tags"] == []
        assert service.load(record["id"]) == record
        assert service.execute(proposal_id=proposal.proposal_id, idempotency_key="probe") == record
        with sqlite3.connect(database) as gate_connection:
            gate = ReleaseController.from_db(gate_connection, deployment_secret="synthetic-secret")
            validation: dict[str, Any] = dict(
                binding=binding,
                proposal_id=proposal.proposal_id,
                validation_report_ref=binding.validation_report_ref,
                canary_samples=5,
                validator_context=command_context_for_role("validator", EvolutionRole.VALIDATOR),
                trajectory_ids=(),
            )
            with pytest.raises(ValueError, match="requires a protected proposal execution run"):
                gate.record_release_validation_evidence(**validation)
            with pytest.raises(ValueError, match="failed fixed assertions"):
                gate.record_release_validation_evidence(
                    **validation, evaluation_run_id=record["id"]
                )
            stable = gate.load_default_head(binding.target_component)
            assert stable is not None
            assert stable.release_id == binding.rollback_target_id
            with pytest.raises(ValueError, match="protected execution reference"):
                gate._validate_report_execution(
                    {},
                    proposal_id=proposal.proposal_id,
                    binding=binding,
                )
            with pytest.raises(ValueError, match="reference not found"):
                gate._validate_report_execution(
                    {"execution_run_id": "missing"},
                    proposal_id=proposal.proposal_id,
                    binding=binding,
                )
            with pytest.raises(ValueError, match="failed fixed assertions"):
                gate._validate_report_execution(
                    {"execution_run_id": record["id"]},
                    proposal_id=proposal.proposal_id,
                    binding=binding,
                )
        with factory.begin() as session:
            assert (
                session.execute(
                    text("SELECT state FROM evolution_proposals WHERE id = :id"),
                    {"id": proposal.proposal_id},
                ).scalar_one()
                == "candidate"
            )
            count = session.execute(text("SELECT count(*) FROM proposal_execution_runs")).scalar()
            assert count == 1
        for operation in (
            "UPDATE proposal_execution_runs SET record_hmac = 'fake'",
            "DELETE FROM proposal_execution_runs",
        ):
            with pytest.raises(IntegrityError, match="append-only"), factory.begin() as session:
                session.execute(text(operation))
        other_secret = ProposalExecutionService(
            session_factory=factory,
            project_root=Path.cwd(),
            deployment_secret="wrong-secret",
        )
        with pytest.raises(ValueError, match="signature"):
            other_secret.load(record["id"])
        with pytest.raises(ValueError, match="immutable binding origin"):
            service.execute(proposal_id="missing-proposal", idempotency_key="missing")
    finally:
        engine.dispose()


def test_execution_records_require_existing_proposal(tmp_path: Path) -> None:
    connection = _upgrade(tmp_path / "foreign-key.sqlite")
    try:
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            connection.execute(
                "INSERT INTO proposal_execution_runs VALUES ('run', 'missing', 'key', '{}', 'fake')"
            )
    finally:
        connection.close()


def test_real_passing_execution_validates_but_does_not_approve(tmp_path: Path) -> None:
    if not (os.environ.get("ZHIHENG_RESTIC_BINARY") or shutil.which("restic")):
        pytest.skip("configure real restic to execute full validation contract")
    database = tmp_path / "passing-execution.sqlite"
    connection = _upgrade(database)
    secret = "synthetic-execution-secret"
    controller = ReleaseController.from_db(connection, deployment_secret=secret)
    binding = _binding("passing-execution", _bootstrap_stable(controller))
    proposal = controller.create_release_proposal(
        binding=binding,
        artifact_payload=default_release_artifact(),
        proposer_context=command_context_for_role("proposer", EvolutionRole.PROPOSER),
    )
    engine = create_sqlite_engine(
        Settings(environment="test", database_url=f"sqlite:///{database}")
    )
    service = ProposalExecutionService(
        session_factory=create_session_factory(engine),
        project_root=Path.cwd(),
        deployment_secret=secret,
    )
    try:
        record = service.execute(proposal_id=proposal.proposal_id, idempotency_key="passing")
        assert record["report"]["promotion_eligible"] is True
        assert record["evaluation_contract"]["status"] == "passed"
        assert record["report"]["failure_count"] == 0
        validation_kwargs = dict(
            binding=binding,
            proposal_id=proposal.proposal_id,
            validation_report_ref=binding.validation_report_ref,
            canary_samples=5,
            validator_context=command_context_for_role("validator", EvolutionRole.VALIDATOR),
            evaluation_run_id=record["id"],
        )
        with pytest.raises(ValueError, match="trajectories must match"):
            controller.record_release_validation_evidence(
                **validation_kwargs,
                trajectory_ids=record["trajectory_ids"][:-1],
            )
        evidence = controller.record_release_validation_evidence(
            **validation_kwargs,
            trajectory_ids=record["trajectory_ids"],
        )
        assert evidence.proposal_id == proposal.proposal_id
        assert (
            connection.execute(
                "SELECT state FROM evolution_proposals WHERE id = ?",
                (proposal.proposal_id,),
            ).fetchone()[0]
            == "validating"
        )
        assert connection.execute("SELECT count(*) FROM review_reports").fetchone()[0] == 1
        stable = controller.load_default_head(binding.target_component)
        assert stable is not None
        assert stable.release_id == binding.rollback_target_id
    finally:
        connection.close()
        engine.dispose()
