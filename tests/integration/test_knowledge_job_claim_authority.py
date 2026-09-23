from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from tests.integration.test_worker_knowledge_indexing import _FakeEmbedder, _ingest, _migrated
from tests.knowledge_helpers import stored_text_artifacts
from zhiheng.core.ids import json_text, new_id
from zhiheng.jobs.knowledge_contract import job_etag
from zhiheng.jobs.knowledge_indexing import (
    KNOWLEDGE_INDEX_JOB_TYPE,
    ClaimedKnowledgeJob,
    KnowledgeIndexJobExecutor,
    KnowledgeIndexResult,
    KnowledgeJobRepository,
    process_knowledge_jobs_once,
)
from zhiheng.worker.main import process_outbox_once


def test_stale_knowledge_claim_cannot_finish_or_fail_reclaimed_attempt(
    tmp_path: Path,
) -> None:
    _settings, factory = _migrated(tmp_path)
    repository = KnowledgeJobRepository()
    with factory.begin() as session:
        _enqueue_knowledge_job(session, "knowledge-expired")
        first = repository.claim_available(session, worker_id="old-worker")[0]
    with factory.begin() as session:
        session.execute(text("UPDATE jobs SET lease_expires_at=datetime('now','-1 minute')"))
        second = repository.claim_available(session, worker_id="new-worker")[0]
    with factory.begin() as session:
        assert repository.complete(session, first, result=_result("stale-complete")) is False
        assert repository.fail(session, first, exc=RuntimeError("synthetic stale failure")) is False
        assert tuple(
            session.execute(text("SELECT status, lease_owner, attempts FROM jobs")).one()
        ) == ("processing", "new-worker", 2)
        assert repository.complete(session, second, result=_result("fresh-complete")) is True
    with factory() as session:
        statuses = (
            session.execute(text("SELECT status FROM job_attempts ORDER BY started_at, id"))
            .scalars()
            .all()
        )
        assert statuses.count("failed") == 1
        assert statuses.count("completed") == 1
        assert session.execute(text("SELECT count(*) FROM dead_letters")).scalar_one() == 0


def test_knowledge_ack_requires_exact_open_attempt(tmp_path: Path) -> None:
    _settings, factory = _migrated(tmp_path)
    repository = KnowledgeJobRepository()
    with factory.begin() as session:
        _enqueue_knowledge_job(session, "knowledge-attempt-a")
        _enqueue_knowledge_job(session, "knowledge-attempt-b")
        first, second = repository.claim_available(session, worker_id="same-worker")
        forged = replace(first, attempt_id=second.attempt_id)
    with factory.begin() as session:
        before_jobs = session.execute(text("SELECT * FROM jobs ORDER BY id")).all()
        before_attempts = session.execute(text("SELECT * FROM job_attempts ORDER BY id")).all()
        assert repository.complete(session, forged, result=_result("forged")) is False
        assert repository.fail(session, forged, exc=RuntimeError("forged")) is False
        assert session.execute(text("SELECT * FROM jobs ORDER BY id")).all() == before_jobs
        assert session.execute(text("SELECT * FROM job_attempts ORDER BY id")).all() == (
            before_attempts
        )
        assert session.execute(text("SELECT count(*) FROM dead_letters")).scalar_one() == 0


def test_failed_knowledge_job_retry_creates_fenced_job_and_replays_idempotently(
    tmp_path: Path,
) -> None:
    _settings, factory = _migrated(tmp_path)
    repository = KnowledgeJobRepository()
    with factory.begin() as session:
        job_id = _enqueue_knowledge_job(
            session, "knowledge-retry-source", knowledge_object_id="synthetic-knowledge"
        )
        claimed = repository.claim_available(session, worker_id="worker")[0]
        assert repository.fail(session, claimed, exc=ValueError("parse failed")) is True
        session.execute(text("UPDATE jobs SET status = 'failed' WHERE id = :id"), {"id": job_id})

    with factory.begin() as session:
        row = dict(
            session.execute(
                text(
                    "SELECT id, status, attempts, max_attempts, updated_at FROM jobs WHERE id = :id"
                ),
                {"id": job_id},
            )
            .mappings()
            .one()
        )
        first = repository.retry_failed_job(
            session,
            knowledge_object_id="synthetic-knowledge",
            expected_job_id=job_id,
            expected_etag=job_etag(row),
            operation_key="retry-operation",
        )
        second = repository.retry_failed_job(
            session,
            knowledge_object_id="synthetic-knowledge",
            expected_job_id=job_id,
            expected_etag=job_etag(row),
            operation_key="retry-operation",
        )
        assert first == second
        assert first.previous_job_id == job_id
        assert first.status == "pending"
        assert session.execute(text("SELECT count(*) FROM jobs")).scalar_one() == 2
        assert (
            session.execute(
                text("SELECT status FROM jobs WHERE id = :id"), {"id": job_id}
            ).scalar_one()
            == "failed"
        )


def test_failed_knowledge_job_retry_rejects_processing_and_stale_etag(tmp_path: Path) -> None:
    _settings, factory = _migrated(tmp_path)
    repository = KnowledgeJobRepository()
    with factory.begin() as session:
        job_id = _enqueue_knowledge_job(
            session, "knowledge-retry-guards", knowledge_object_id="synthetic-knowledge"
        )
        repository.claim_available(session, worker_id="worker")
        current = dict(
            session.execute(
                text(
                    "SELECT id, status, attempts, max_attempts, updated_at FROM jobs WHERE id = :id"
                ),
                {"id": job_id},
            )
            .mappings()
            .one()
        )
        with pytest.raises(ValueError, match="only failed or dead"):
            repository.retry_failed_job(
                session,
                knowledge_object_id="synthetic-knowledge",
                expected_job_id=job_id,
                expected_etag=job_etag(current),
                operation_key="retry-processing",
            )


def test_exhausted_knowledge_lease_is_dead_lettered_once(tmp_path: Path) -> None:
    _settings, factory = _migrated(tmp_path)
    repository = KnowledgeJobRepository()
    with factory.begin() as session:
        _enqueue_knowledge_job(session, "knowledge-final-lease")
        session.execute(text("UPDATE jobs SET max_attempts=1"))
        first = repository.claim_available(session, worker_id="old-worker")[0]
        session.execute(text("UPDATE jobs SET lease_expires_at=datetime('now','-1 minute')"))

    with factory.begin() as session:
        assert repository.claim_available(session, worker_id="replacement-worker") == []
        assert repository.claim_available(session, worker_id="replacement-worker") == []
        assert tuple(
            session.execute(text("SELECT status, lease_owner, attempts FROM jobs")).one()
        ) == ("dead", None, 1)
        assert tuple(
            session.execute(text("SELECT status, error_class FROM job_attempts")).one()
        ) == ("failed", "LeaseExpired")
        assert session.execute(text("SELECT count(*) FROM dead_letters")).scalar_one() == 1
        assert repository.complete(session, first, result=_result("stale")) is False


def test_process_knowledge_jobs_counts_only_acknowledged_completion(tmp_path: Path) -> None:
    _settings, factory = _migrated(tmp_path)

    class FakeExecutor(KnowledgeIndexJobExecutor):
        def execute(
            self,
            session_factory: Any,
            job: ClaimedKnowledgeJob,
        ) -> KnowledgeIndexResult:
            del session_factory, job
            return _result("not-acked")

    class RejectingRepository(KnowledgeJobRepository):
        def claim_available(
            self,
            session: Session,
            *,
            worker_id: str,
            limit: int = 10,
            lease_seconds: int = 300,
        ) -> list[ClaimedKnowledgeJob]:
            del session, worker_id, limit, lease_seconds
            return [
                ClaimedKnowledgeJob(
                    id="synthetic-job",
                    job_type=KNOWLEDGE_INDEX_JOB_TYPE,
                    idempotency_key="synthetic-key",
                    payload={},
                    attempts=1,
                    lease_owner="worker",
                    attempt_id="attempt",
                )
            ]

        def complete(
            self,
            session: Session,
            job: ClaimedKnowledgeJob,
            *,
            result: KnowledgeIndexResult,
        ) -> bool:
            del session, job, result
            return False

    assert (
        process_knowledge_jobs_once(
            factory,
            FakeExecutor(_settings),
            worker_id="worker",
            repository=RejectingRepository(),
        )
        == 0
    )


def test_stale_knowledge_claim_cannot_activate_vector_generation(tmp_path: Path) -> None:
    settings, factory = _migrated(tmp_path)
    repository = KnowledgeJobRepository()
    text_value = "过期知识索引 claim 不能激活向量 generation。"
    with factory.begin() as session:
        _ingest(session, stored_text_artifacts(tmp_path, text_value), text_value)
    assert process_outbox_once(settings) == 1
    with factory.begin() as session:
        first = repository.claim_available(session, worker_id="old-worker")[0]
    with factory.begin() as session:
        session.execute(text("UPDATE jobs SET lease_expires_at=datetime('now','-1 minute')"))
        second = repository.claim_available(session, worker_id="new-worker")[0]

    executor = KnowledgeIndexJobExecutor(
        settings,
        embedder_factory=lambda: _FakeEmbedder(),
    )
    with factory.begin() as session:
        try:
            executor.execute(factory, first)
        except ValueError as exc:
            assert "knowledge job lease lost" in str(exc)
        else:
            raise AssertionError("stale claim activated index generation")
        assert repository.fail(session, first, exc=RuntimeError("late failure")) is False
    with factory() as session:
        assert (
            session.execute(
                text("SELECT count(*) FROM embedding_generations WHERE index_status = 'active'")
            ).scalar_one()
            == 0
        )

    result = executor.execute(factory, second)
    with factory.begin() as session:
        assert repository.complete(session, second, result=result) is True
    with factory() as session:
        assert (
            session.execute(
                text("SELECT count(*) FROM embedding_generations WHERE index_status = 'active'")
            ).scalar_one()
            == 1
        )
        assert session.execute(text("SELECT status FROM jobs")).scalar_one() == "completed"


def _enqueue_knowledge_job(
    session: Session,
    idempotency_key: str,
    *,
    knowledge_object_id: str = "synthetic-knowledge",
) -> str:
    job_id = new_id()
    session.execute(
        text(
            """
            INSERT INTO jobs (id, job_type, idempotency_key, payload_json, status)
            VALUES (:id, :job_type, :idempotency_key, :payload_json, 'pending')
            """
        ),
        {
            "id": job_id,
            "job_type": KNOWLEDGE_INDEX_JOB_TYPE,
            "idempotency_key": idempotency_key,
            "payload_json": json_text(
                {"fixture": "synthetic", "knowledge_object_id": knowledge_object_id}
            ),
        },
    )
    return job_id


def _result(generation_id: str) -> KnowledgeIndexResult:
    return KnowledgeIndexResult(
        fts_indexed=0,
        vector_indexed=0,
        generation_id=generation_id,
    )
