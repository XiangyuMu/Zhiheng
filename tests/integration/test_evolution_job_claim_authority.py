import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from tests.integration.test_g006_worker_recovery import _migrated
from zhiheng.core.config import Settings
from zhiheng.evolution import jobs as evolution_jobs
from zhiheng.evolution.contracts import EvolutionRole, command_context_for_role
from zhiheng.evolution.jobs import (
    ClaimedJob,
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


class _SlowFirstExecutor(EvolutionJobExecutor):
    """Runs a callback while executing its first job, standing in for a slow job."""

    def __init__(
        self,
        settings: Settings,
        *,
        during_first_job: Callable[[ClaimedJob], None] | None = None,
    ) -> None:
        super().__init__(settings)
        self._during_first_job = during_first_job
        self.executed: list[str] = []

    def execute(self, job: ClaimedJob) -> JobResult:
        self.executed.append(job.id)
        if len(self.executed) == 1 and self._during_first_job is not None:
            self._during_first_job(job)
        return JobResult("completed", {"synthetic_job": job.id})


def _lapse_queued_leases(factory: sessionmaker[Session], running: ClaimedJob) -> None:
    """Advance past the lease of every claim still waiting behind the running job.

    Queued claims were leased no later than the running one, so they lapse first
    when the jobs ahead of them outlast the lease.
    """
    with factory.begin() as session:
        session.execute(
            text(
                "UPDATE jobs SET lease_expires_at=datetime('now','-1 minute') "
                "WHERE status='processing' AND id != :running_id"
            ),
            {"running_id": running.id},
        )


def test_second_worker_does_not_reexecute_jobs_queued_behind_a_slow_job(tmp_path: Path) -> None:
    settings, factory, connection = _migrated(tmp_path)
    connection.close()
    with factory.begin() as session:
        for index in range(3):
            JobRepository().enqueue(
                session,
                job_type=EvolutionJobType.MAINTENANCE,
                idempotency_key=f"synthetic-queued-{index}",
                payload={},
            )
        job_ids = list(session.execute(text("SELECT id FROM jobs")).scalars())
    other_executor = _SlowFirstExecutor(settings)
    other_completed: list[int] = []

    def slow_first_job(running: ClaimedJob) -> None:
        _lapse_queued_leases(factory, running)
        other_completed.append(process_jobs_once(factory, other_executor, worker_id="worker-b"))

    executor = _SlowFirstExecutor(settings, during_first_job=slow_first_job)

    completed = process_jobs_once(factory, executor, worker_id="worker-a")

    assert sorted(executor.executed + other_executor.executed) == sorted(job_ids)
    assert completed + sum(other_completed) == 3
    with factory() as session:
        rows = session.execute(text("SELECT status, attempts FROM jobs")).all()
        assert [tuple(row) for row in rows] == [("completed", 1)] * 3
        attempts = session.execute(text("SELECT status FROM job_attempts")).scalars().all()
        assert attempts == ["completed"] * 3


def _wait_past_lease_unless_renewed(factory: sessionmaker[Session], job: ClaimedJob) -> None:
    """Stand in for a job that runs right up to the end of its lease.

    The lease is moved to expire within a second, then the job keeps running until
    either the lease lapses or a heartbeat pushes it well into the future.
    """
    with factory.begin() as session:
        session.execute(
            text("UPDATE jobs SET lease_expires_at=datetime('now','+1 second') WHERE id=:id"),
            {"id": job.id},
        )
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        with factory() as session:
            renewed = session.execute(
                text(
                    "SELECT lease_expires_at > datetime('now','+60 seconds') FROM jobs WHERE id=:id"
                ),
                {"id": job.id},
            ).scalar_one()
        if renewed:
            return
        time.sleep(0.05)


def test_job_running_past_its_lease_is_not_reclaimed_mid_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(evolution_jobs, "EVOLUTION_JOB_HEARTBEAT_SECONDS", 0.05, raising=False)
    settings, factory, connection = _migrated(tmp_path)
    connection.close()
    with factory.begin() as session:
        JobRepository().enqueue(
            session,
            job_type=EvolutionJobType.MAINTENANCE,
            idempotency_key="synthetic-long-running",
            payload={},
        )
    other_executor = _SlowFirstExecutor(settings)
    other_completed: list[int] = []

    def long_running_job(running: ClaimedJob) -> None:
        _wait_past_lease_unless_renewed(factory, running)
        other_completed.append(process_jobs_once(factory, other_executor, worker_id="worker-b"))

    executor = _SlowFirstExecutor(settings, during_first_job=long_running_job)

    completed = process_jobs_once(factory, executor, worker_id="worker-a")

    assert other_executor.executed == []
    assert other_completed == [0]
    assert completed == 1
    with factory() as session:
        rows = session.execute(text("SELECT status, attempts FROM jobs")).all()
        assert [tuple(row) for row in rows] == [("completed", 1)]
        attempts = session.execute(text("SELECT status FROM job_attempts")).scalars().all()
        assert attempts == ["completed"]


def test_lease_renewal_requires_exact_live_claim(tmp_path: Path) -> None:
    _settings, factory, connection = _migrated(tmp_path)
    connection.close()
    repository = JobRepository()
    with factory.begin() as session:
        repository.enqueue(
            session,
            job_type=EvolutionJobType.MAINTENANCE,
            idempotency_key="synthetic-renewal",
            payload={},
        )
        claimed = repository.claim_available(session, worker_id="owner", lease_seconds=1)[0]
        assert repository.renew_lease(session, claimed, lease_seconds=90) is True
        assert session.execute(
            text("SELECT lease_expires_at > datetime('now','+60 seconds') FROM jobs")
        ).scalar_one()

        assert repository.renew_lease(session, replace(claimed, lease_owner="forged")) is False
        assert repository.renew_lease(session, replace(claimed, attempts=2)) is False
        assert repository.renew_lease(session, replace(claimed, attempt_id="forged")) is False

        session.execute(text("UPDATE jobs SET lease_expires_at=datetime('now','-1 second')"))
        assert repository.renew_lease(session, claimed) is False
        assert session.execute(
            text("SELECT lease_expires_at <= CURRENT_TIMESTAMP FROM jobs")
        ).scalar_one()
