from __future__ import annotations

import os
import shutil
from pathlib import Path

from alembic import command
from alembic.config import Config

from zhiheng.core.config import Settings
from zhiheng.db.session import create_session_factory, create_sqlite_engine
from zhiheng.evaluation.g006_registry import REGISTERED_FIXED_CASES
from zhiheng.evaluation.g006_runner import (
    EvaluationSubject,
    G006ExecutionStage,
    ObservedFixedCase,
    ProtectedFixedSuiteRunner,
)
from zhiheng.evolution.artifacts import default_release_artifact
from zhiheng.evolution.trajectory_repository import TrajectoryRepository


def _subject(
    *,
    route_override: str | None = None,
) -> EvaluationSubject:
    artifact = default_release_artifact()
    artifact["routing"]["route_override"] = route_override
    return EvaluationSubject.from_artifact(
        candidate_id=f"candidate-{route_override or 'default'}",
        target_component="retrieval.answer_strategy",
        artifact_payload=artifact,
        baseline_artifact_payload=default_release_artifact(),
    )


def _case(run_cases: tuple[ObservedFixedCase, ...], case_id: str) -> ObservedFixedCase:
    for case in run_cases:
        if case.case_id == case_id:
            return case
    raise AssertionError(case_id)


def test_fixed_suite_runner_dispatches_exact_registry_and_reports_actual_dependencies(
    tmp_path: Path,
) -> None:
    runner = ProtectedFixedSuiteRunner(project_root=Path.cwd())
    run = runner.run(
        _subject(),
        stage=G006ExecutionStage.VALIDATION,
        work_dir=tmp_path,
    )

    assert tuple(case.case_id for case in run.observed_cases) == tuple(
        case.case_id for case in REGISTERED_FIXED_CASES
    )
    assert sum(len(case.assertions) for case in run.observed_cases) == 23
    configured = os.environ.get("ZHIHENG_RESTIC_BINARY") or shutil.which("restic")
    expected_failures = 0 if configured else 1
    assert run.evaluation_report.failure_count == expected_failures
    assert run.evaluation_report.promotion_eligible is bool(configured)
    migration = _case(run.observed_cases, "migration-answer-strategy-transfer-001")
    assert migration.passed
    assert migration.observed_facts["baseline"]["input_digest"] == (
        migration.observed_facts["candidate"]["input_digest"]
    )

    canary = _case(run.observed_cases, "safety-canary-insufficient-samples-001")
    assert canary.passed
    assert all(assertion.passed for assertion in canary.assertions)


def test_retention_case_uses_real_structured_lookup_and_candidate_params_affect_behavior(
    tmp_path: Path,
) -> None:
    runner = ProtectedFixedSuiteRunner(project_root=Path.cwd())
    default_run = runner.run(
        _subject(),
        stage=G006ExecutionStage.VALIDATION,
        work_dir=tmp_path / "default",
    )
    mutated_run = runner.run(
        _subject(route_override="hybrid"),
        stage=G006ExecutionStage.VALIDATION,
        work_dir=tmp_path / "mutated",
    )

    default_case = _case(default_run.observed_cases, "retention-structured-direct-lookup-001")
    mutated_case = _case(mutated_run.observed_cases, "retention-structured-direct-lookup-001")

    assert default_case.passed is True
    assert mutated_case.passed is False
    assert default_case.observation_digest != mutated_case.observation_digest
    assert any(
        tag == "assertion_failed:routing.structured_lookup_selected"
        for tag in mutated_case.failure_tags
    )


def test_privacy_gateway_zero_outbound_case_executes_real_gateway(tmp_path: Path) -> None:
    runner = ProtectedFixedSuiteRunner(project_root=Path.cwd())
    run = runner.run(
        _subject(),
        stage=G006ExecutionStage.REPLAY,
        work_dir=tmp_path,
    )

    observed = _case(run.observed_cases, "safety-outbound-network-zero-001")

    assert observed.passed is True
    assert {assertion.name: assertion.passed for assertion in observed.assertions} == {
        "privacy.unknown_classification_blocks": True,
        "privacy.no_raw_fallback": True,
        "network.unauthorized_outbound_calls_zero": True,
    }


def test_runner_persists_hmac_verified_case_trajectories(tmp_path: Path) -> None:
    runner = ProtectedFixedSuiteRunner(project_root=Path.cwd())
    run = runner.run(
        _subject(),
        stage=G006ExecutionStage.VALIDATION,
        work_dir=tmp_path / "work",
    )
    database = tmp_path / "trajectories.sqlite"
    settings = Settings(environment="test", database_url=f"sqlite:///{database}")
    config = Config(str(Path.cwd() / "alembic.ini"))
    config.set_main_option("script_location", str(Path.cwd() / "migrations"))
    config.set_main_option("sqlalchemy.url", settings.database_url)
    command.upgrade(config, "head")
    engine = create_sqlite_engine(settings)
    repository = TrajectoryRepository(
        deployment_secret=settings.secret_key.get_secret_value(),
        session_factory=create_session_factory(engine),
    )

    try:
        trajectory_ids = runner.persist_trajectories(
            run,
            repository=repository,
            idempotency_key_prefix="unit-fixed-run",
        )
        repeated_ids = runner.persist_trajectories(
            run,
            repository=repository,
            idempotency_key_prefix="unit-fixed-run",
        )

        assert trajectory_ids == repeated_ids
        assert len(trajectory_ids) == len(REGISTERED_FIXED_CASES)
        retention = repository.get(
            next(item for item in trajectory_ids if "retention-structured" in item)
        )
        canary = repository.get(
            next(item for item in trajectory_ids if "safety-canary" in item)
        )
        assert retention.learning_eligible is True
        assert retention.envelope.process["case_id"] == (
            "retention-structured-direct-lookup-001"
        )
        assert canary.learning_eligible is True
        assert not canary.envelope.failure_tags
    finally:
        engine.dispose()
