from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from tests.integration.test_topic_taxonomy import _client
from zhiheng.classification.suggestions import CLASSIFICATION_SUGGESTION_JOB_TYPE
from zhiheng.db.session import session_scope
from zhiheng.jobs import memory_extraction
from zhiheng.jobs.classification import process_classification_jobs_once
from zhiheng.jobs.memory_extraction import (
    MEMORY_EXTRACTION_JOB_TYPE,
    process_memory_extraction_jobs_once,
)


class _FailingExecutor:
    def __init__(self, failures: int = 99) -> None:
        self.failures = failures
        self.calls = 0

    def execute(self, session_factory: sessionmaker[Session], job: Any) -> int:
        self.calls += 1
        if self.calls <= self.failures:
            raise ValueError("executor exploded")
        return 0


Worker = Callable[..., int]

WORKERS = [
    pytest.param(
        CLASSIFICATION_SUGGESTION_JOB_TYPE, process_classification_jobs_once, id="classification"
    ),
    pytest.param(
        MEMORY_EXTRACTION_JOB_TYPE, process_memory_extraction_jobs_once, id="memory_extraction"
    ),
]


def _factory(tmp_path: Path, job_type: str, max_attempts: int) -> sessionmaker[Session]:
    factory: sessionmaker[Session] = _client(tmp_path).app.state.session_factory  # type: ignore[attr-defined]
    payload = {
        "history_id": "missing",
        "conversation_id": "missing",
        "owner_user_id": "missing",
        "knowledge_object_id": "missing",
    }
    with session_scope(factory) as session:
        session.execute(
            text(
                "INSERT INTO jobs (id, job_type, idempotency_key, payload_json, status, "
                "max_attempts) VALUES ('job-1', :job_type, 'k', :payload, 'pending', :max)"
            ),
            {"job_type": job_type, "payload": json.dumps(payload), "max": max_attempts},
        )
    return factory


def _job(factory: sessionmaker[Session]) -> dict[str, Any]:
    with session_scope(factory) as session:
        row = (
            session.execute(
                text(
                    "SELECT status, lease_owner, lease_expires_at, "
                    "available_at > CURRENT_TIMESTAMP AS backed_off FROM jobs WHERE id='job-1'"
                )
            )
            .mappings()
            .one()
        )
    return dict(row)


def _attempts(factory: sessionmaker[Session]) -> list[dict[str, Any]]:
    with session_scope(factory) as session:
        rows = (
            session.execute(
                text("SELECT id, status, error_class, error_message FROM job_attempts ORDER BY id")
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


def _make_due(factory: sessionmaker[Session]) -> None:
    with session_scope(factory) as session:
        session.execute(text("UPDATE jobs SET available_at = CURRENT_TIMESTAMP WHERE id='job-1'"))


@pytest.mark.parametrize(("job_type", "worker"), WORKERS)
def test_a_failed_attempt_returns_the_job_to_pending_after_a_wait(
    tmp_path: Path, job_type: str, worker: Worker
) -> None:
    factory = _factory(tmp_path, job_type, max_attempts=3)

    completed = worker(factory, _FailingExecutor(), worker_id="test-worker")

    assert completed == 0
    assert _job(factory) == {
        "status": "pending",
        "lease_owner": None,
        "lease_expires_at": None,
        "backed_off": 1,
    }
    assert _attempts(factory) == [
        {
            "id": "job-1:1",
            "status": "failed",
            "error_class": "ValueError",
            "error_message": "executor exploded",
        }
    ]
    # Still waiting out the backoff: a second pass does not pick it up.
    assert worker(factory, _FailingExecutor(), worker_id="test-worker") == 0
    assert len(_attempts(factory)) == 1


@pytest.mark.parametrize(("job_type", "worker"), WORKERS)
def test_a_job_that_fails_every_attempt_ends_dead(
    tmp_path: Path, job_type: str, worker: Worker
) -> None:
    factory = _factory(tmp_path, job_type, max_attempts=3)
    executor = _FailingExecutor()

    for _ in range(4):
        worker(factory, executor, worker_id="test-worker")
        _make_due(factory)

    assert executor.calls == 3
    job = _job(factory)
    assert (job["status"], job["lease_owner"], job["lease_expires_at"]) == ("dead", None, None)
    assert [row["id"] for row in _attempts(factory)] == ["job-1:1", "job-1:2", "job-1:3"]


@pytest.mark.parametrize(("job_type", "worker"), WORKERS)
def test_a_retried_job_succeeds_on_a_later_attempt(
    tmp_path: Path, job_type: str, worker: Worker
) -> None:
    factory = _factory(tmp_path, job_type, max_attempts=3)
    executor = _FailingExecutor(failures=1)

    worker(factory, executor, worker_id="test-worker")
    _make_due(factory)
    completed = worker(factory, executor, worker_id="test-worker")

    assert completed == 1
    assert _job(factory)["status"] == "succeeded"
    assert [row["id"] for row in _attempts(factory)] == ["job-1:1"]


def test_extraction_shows_its_failure_only_once_the_job_is_dead(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded: list[int] = []
    monkeypatch.setattr(
        memory_extraction,
        "_record_conclusion_extraction_failure",
        lambda session_factory, job, exc: recorded.append(job.attempts),
    )
    factory = _factory(tmp_path, MEMORY_EXTRACTION_JOB_TYPE, max_attempts=3)
    executor: Any = _FailingExecutor()

    for _ in range(3):
        process_memory_extraction_jobs_once(factory, executor, worker_id="test-worker")
        _make_due(factory)

    assert recorded == [3]
