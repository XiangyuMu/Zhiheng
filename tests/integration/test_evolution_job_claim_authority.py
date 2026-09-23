from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import text

from tests.integration.test_g006_worker_recovery import _migrated
from zhiheng.evolution.contracts import EvolutionRole, command_context_for_role
from zhiheng.evolution.jobs import (
    EvolutionJobExecutor,
    EvolutionJobType,
    JobRepository,
    JobResult,
    process_jobs_once,
)
from zhiheng.worker.main import process_worker_once


def test_plain_worker_does_not_consume_publisher_job_attempts(tmp_path: Path) -> None:
    settings, factory, connection = _migrated(tmp_path)
    connection.close()
    privileged = set(EvolutionJobType) - {
        EvolutionJobType.MAINTENANCE,
        EvolutionJobType.PROPOSAL_EVALUATION,
    }
    with factory.begin() as session:
        for kind in privileged:
            JobRepository().enqueue(
                session,
                job_type=kind,
                idempotency_key=f"synthetic-{kind}",
                payload={"release_id": "synthetic-release", "publisher_id": "forged"},
            )
    assert process_worker_once(settings) == 0
    with factory() as session:
        rows = session.execute(text("SELECT status, attempts FROM jobs")).all()
        assert len(rows) == len(privileged)
        assert all(row.status == "pending" and row.attempts == 0 for row in rows)
        assert session.execute(text("SELECT count(*) FROM job_attempts")).scalar_one() == 0


def test_job_filter_comes_from_injected_executor_not_payload(tmp_path: Path) -> None:
    settings, factory, connection = _migrated(tmp_path)
    connection.close()
    with factory.begin() as session:
        for kind in EvolutionJobType:
            JobRepository().enqueue(
                session,
                job_type=kind,
                idempotency_key=f"synthetic-{kind}",
                payload={"role": "publisher", "capabilities": ["publish"]},
            )
    seen: list[EvolutionJobType] = []

    class RecordingExecutor(EvolutionJobExecutor):
        def execute(self, job: object) -> JobResult:
            from zhiheng.evolution.jobs import ClaimedJob

            assert isinstance(job, ClaimedJob)
            seen.append(job.job_type)
            return JobResult("completed", {"synthetic_dispatch_probe": True})

    assert process_jobs_once(factory, RecordingExecutor(settings), worker_id="ordinary") == 1
    assert seen == [EvolutionJobType.MAINTENANCE]
    publisher = RecordingExecutor(
        settings,
        publisher_context=command_context_for_role("publisher", EvolutionRole.PUBLISHER),
        validator_context=command_context_for_role("validator", EvolutionRole.VALIDATOR),
    )
    assert process_jobs_once(factory, publisher, worker_id="publisher") == len(EvolutionJobType) - 1
    assert set(seen) == set(EvolutionJobType)


def test_stale_claim_cannot_finish_or_fail_reclaimed_attempt(tmp_path: Path) -> None:
    _settings, factory, connection = _migrated(tmp_path)
    connection.close()
    repository = JobRepository()
    with factory.begin() as session:
        repository.enqueue(
            session,
            job_type=EvolutionJobType.MAINTENANCE,
            idempotency_key="synthetic-expired",
            payload={},
        )
        first = repository.claim_available(session, worker_id="old")[0]
    with factory.begin() as session:
        session.execute(text("UPDATE jobs SET lease_expires_at=datetime('now','-1 minute')"))
        second = repository.claim_available(session, worker_id="new")[0]
    with factory.begin() as session:
        assert repository.complete(session, first, result=JobResult("completed", {})) is False
        assert repository.fail(session, first, exc=RuntimeError("synthetic stale failure")) is False
        row = session.execute(text("SELECT status,lease_owner,attempts FROM jobs")).one()
        assert tuple(row) == ("processing", "new", 2)
        assert repository.complete(session, second, result=JobResult("completed", {})) is True
    with factory() as session:
        rows = session.execute(text("SELECT status FROM job_attempts")).scalars().all()
        assert rows.count("completed") == 1
        assert rows.count("failed") == 1
        assert session.execute(text("SELECT count(*) FROM dead_letters")).scalar_one() == 0


def test_last_attempt_crash_is_dead_lettered_only_by_authorized_queue(tmp_path: Path) -> None:
    _settings, factory, connection = _migrated(tmp_path)
    connection.close()
    repository = JobRepository()
    with factory.begin() as session:
        repository.enqueue(
            session,
            job_type=EvolutionJobType.REPLAY,
            idempotency_key="synthetic-final-crash",
            payload={},
        )
        session.execute(text("UPDATE jobs SET max_attempts=1"))
        first = repository.claim_available(session, worker_id="publisher")[0]
        session.execute(text("UPDATE jobs SET lease_expires_at=datetime('now','-1 minute')"))
    with factory.begin() as session:
        assert (
            repository.claim_available(
                session,
                worker_id="ordinary",
                allowed_job_types=frozenset({EvolutionJobType.MAINTENANCE}),
            )
            == []
        )
        assert session.execute(text("SELECT status FROM jobs")).scalar_one() == "processing"
        for _ in range(2):
            assert (
                repository.claim_available(
                    session,
                    worker_id="replacement-publisher",
                    allowed_job_types=frozenset({EvolutionJobType.REPLAY}),
                )
                == []
            )
        assert session.execute(text("SELECT status FROM jobs")).scalar_one() == "dead"
        assert tuple(
            session.execute(
                text("SELECT status, error_class FROM job_attempts"),
            ).one()
        ) == ("failed", "LeaseExpired")
        assert session.execute(text("SELECT count(*) FROM dead_letters")).scalar_one() == 1
        assert repository.complete(session, first, result=JobResult("completed", {})) is False


@pytest.mark.parametrize("operation", ["complete", "fail"])
@pytest.mark.parametrize("attempt_kind", ["other_job", "missing", "finished"])
def test_ack_requires_exact_open_attempt(
    tmp_path: Path,
    operation: str,
    attempt_kind: str,
) -> None:
    _settings, factory, connection = _migrated(tmp_path)
    connection.close()
    repository = JobRepository()
    with factory.begin() as session:
        for index in range(2):
            repository.enqueue(
                session,
                job_type=EvolutionJobType.MAINTENANCE,
                idempotency_key=f"synthetic-attempt-{index}",
                payload={},
            )
        first, second = repository.claim_available(session, worker_id="same-worker")
        if attempt_kind == "finished":
            session.execute(
                text(
                    "UPDATE job_attempts SET status='failed', finished_at=CURRENT_TIMESTAMP "
                    "WHERE id=:attempt_id"
                ),
                {"attempt_id": first.attempt_id},
            )
        attempt_id = {
            "other_job": second.attempt_id,
            "missing": "synthetic-nonexistent-attempt",
            "finished": first.attempt_id,
        }[attempt_kind]
        forged = replace(first, attempt_id=attempt_id)
    with factory.begin() as session:
        before_jobs = session.execute(text("SELECT * FROM jobs ORDER BY id")).all()
        before_attempts = session.execute(text("SELECT * FROM job_attempts ORDER BY id")).all()
        if operation == "complete":
            accepted = repository.complete(session, forged, result=JobResult("completed", {}))
        else:
            accepted = repository.fail(session, forged, exc=RuntimeError("synthetic failure"))
        assert accepted is False
        assert session.execute(text("SELECT * FROM jobs ORDER BY id")).all() == before_jobs
        assert (
            session.execute(text("SELECT * FROM job_attempts ORDER BY id")).all() == before_attempts
        )
        assert session.execute(text("SELECT count(*) FROM dead_letters")).scalar_one() == 0
