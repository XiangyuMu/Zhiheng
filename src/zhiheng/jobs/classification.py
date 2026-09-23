from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from zhiheng.classification.suggestions import (
    CLASSIFICATION_SUGGESTION_JOB_TYPE,
    ClassificationSuggestionProvider,
    ClassificationSuggestionService,
)
from zhiheng.db.session import session_scope


@dataclass(frozen=True, slots=True)
class ClaimedClassificationJob:
    id: str
    payload: dict[str, Any]
    attempts: int


class ClassificationSuggestionJobExecutor:
    def __init__(
        self,
        *,
        service: ClassificationSuggestionService | None = None,
        provider_factory: Callable[[], ClassificationSuggestionProvider] | None = None,
    ) -> None:
        self.service = service or ClassificationSuggestionService()
        self.provider_factory = provider_factory

    def execute(
        self,
        session_factory: sessionmaker[Session],
        job: ClaimedClassificationJob,
    ) -> int:
        payload = job.payload
        provider = self.provider_factory() if self.provider_factory else None
        with session_scope(session_factory) as session:
            suggestions = self.service.generate(
                session,
                knowledge_object_id=str(payload["knowledge_object_id"]),
                owner_user_id=str(payload["owner_user_id"]),
                provider=provider,
            )
        return len(suggestions)


def process_classification_jobs_once(
    session_factory: sessionmaker[Session],
    executor: ClassificationSuggestionJobExecutor,
    *,
    worker_id: str,
    limit: int = 10,
) -> int:
    with session_scope(session_factory) as session:
        rows = (
            session.execute(
                text(
                    """
                SELECT id, payload_json, attempts
                FROM jobs
                WHERE job_type = :job_type
                  AND status = 'pending'
                  AND available_at <= CURRENT_TIMESTAMP
                  AND attempts < max_attempts
                ORDER BY available_at, id
                LIMIT :limit
                """
                ),
                {"job_type": CLASSIFICATION_SUGGESTION_JOB_TYPE, "limit": limit},
            )
            .mappings()
            .all()
        )
        claimed: list[ClaimedClassificationJob] = []
        for row in rows:
            session.execute(
                text(
                    """
                    UPDATE jobs
                    SET status = 'processing', lease_owner = :worker,
                        lease_expires_at = datetime('now', '+300 seconds'),
                        heartbeat_at = CURRENT_TIMESTAMP, attempts = attempts + 1,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = :id AND job_type = :job_type AND status = 'pending'
                    """
                ),
                {
                    "id": row["id"],
                    "job_type": CLASSIFICATION_SUGGESTION_JOB_TYPE,
                    "worker": worker_id,
                },
            )
            if int(session.execute(text("SELECT changes()")).scalar_one()) == 1:
                claimed.append(
                    ClaimedClassificationJob(
                        id=str(row["id"]),
                        payload=_json_object(row["payload_json"]),
                        attempts=int(row["attempts"]) + 1,
                    )
                )

    completed = 0
    for job in claimed:
        try:
            executor.execute(session_factory, job)
        except Exception as exc:
            with session_scope(session_factory) as session:
                session.execute(
                    text(
                        """
                        UPDATE jobs
                        SET status = CASE
                                WHEN attempts >= max_attempts THEN 'dead'
                                ELSE 'failed'
                            END,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id = :id AND status = 'processing'
                        """
                    ),
                    {"id": job.id},
                )
                session.execute(
                    text(
                        """
                        INSERT INTO job_attempts
                          (id, job_id, attempt_no, status, started_at, finished_at,
                           error_class, error_message)
                        VALUES (:attempt, :job, :attempt_no, 'failed',
                                CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, :error_class, :error_message)
                        """
                    ),
                    {
                        "attempt": f"{job.id}:{job.attempts}",
                        "job": job.id,
                        "attempt_no": job.attempts,
                        "error_class": type(exc).__name__,
                        "error_message": str(exc)[:1000],
                    },
                )
            continue
        with session_scope(session_factory) as session:
            session.execute(
                text(
                    """
                    UPDATE jobs
                    SET status = 'succeeded', lease_owner = NULL,
                        lease_expires_at = NULL, updated_at = CURRENT_TIMESTAMP
                    WHERE id = :id AND status = 'processing'
                    """
                ),
                {"id": job.id},
            )
        completed += 1
    return completed


def _json_object(value: Any) -> dict[str, Any]:
    loaded = json.loads(value) if isinstance(value, str) else value
    if not isinstance(loaded, dict):
        raise ValueError("classification job payload must be an object")
    return dict(loaded)
