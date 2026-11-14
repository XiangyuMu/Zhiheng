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
from zhiheng.jobs.classification import process_classification_jobs_once
from zhiheng.jobs.memory_extraction import (
    MEMORY_EXTRACTION_JOB_TYPE,
    process_memory_extraction_jobs_once,
)


class _FailingExecutor:
    def execute(self, session_factory: sessionmaker[Session], job: Any) -> int:
        raise ValueError("executor exploded")


Worker = Callable[..., int]

WORKERS = [
    pytest.param(
        CLASSIFICATION_SUGGESTION_JOB_TYPE, process_classification_jobs_once, id="classification"
    ),
    pytest.param(
        MEMORY_EXTRACTION_JOB_TYPE, process_memory_extraction_jobs_once, id="memory_extraction"
    ),
]


def _seed_job(factory: sessionmaker[Session], job_type: str, max_attempts: int) -> None:
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


@pytest.mark.parametrize(("job_type", "worker"), WORKERS)
@pytest.mark.parametrize(("max_attempts", "final_status"), [(3, "failed"), (1, "dead")])
def test_a_failing_job_leaves_processing_and_records_its_attempt(
    tmp_path: Path, job_type: str, worker: Worker, max_attempts: int, final_status: str
) -> None:
    factory = _client(tmp_path).app.state.session_factory  # type: ignore[attr-defined]
    _seed_job(factory, job_type, max_attempts)

    completed = worker(factory, _FailingExecutor(), worker_id="test-worker")

    assert completed == 0
    with session_scope(factory) as session:
        status = session.execute(text("SELECT status FROM jobs WHERE id='job-1'")).scalar_one()
        attempts = (
            session.execute(text("SELECT id, status, error_class, error_message FROM job_attempts"))
            .mappings()
            .all()
        )
    assert status == final_status
    assert [dict(row) for row in attempts] == [
        {
            "id": "job-1:1",
            "status": "failed",
            "error_class": "ValueError",
            "error_message": "executor exploded",
        }
    ]
