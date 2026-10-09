from __future__ import annotations

import hashlib
import json
import math
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from zhiheng.core.config import Settings
from zhiheng.core.ids import new_id, sha256_text
from zhiheng.db.session import session_scope
from zhiheng.jobs.knowledge_contract import job_etag, retry_idempotency_key
from zhiheng.knowledge import KnowledgeRepository
from zhiheng.knowledge.pdf_worker import ParserTerminalFailure
from zhiheng.models.configuration import embedding_route
from zhiheng.models.embeddings import OpenAIEmbeddingsTransport, TransportRoute
from zhiheng.retrieval.embeddings import BgeM3QueryEmbedder, QueryEmbeddingUnavailableError
from zhiheng.retrieval.vector_index import VectorIndexRepository
from zhiheng.secrets import ProviderSecretStore

KNOWLEDGE_INDEX_JOB_TYPE = "knowledge.index"
EVENT_INDEX_JOB_TYPE = "event.index"
KNOWLEDGE_PARSE_PDF_JOB_TYPE = "knowledge.parse_pdf"
KNOWLEDGE_JOB_TYPES = (KNOWLEDGE_INDEX_JOB_TYPE, KNOWLEDGE_PARSE_PDF_JOB_TYPE)


class TextEmbeddingPort(Protocol):
    def embed_text(
        self,
        text: str,
        *,
        model_id: str,
        model_revision: str,
        dimension: int,
        normalize: bool,
    ) -> Sequence[float]: ...


@dataclass(frozen=True, slots=True)
class ClaimedKnowledgeJob:
    id: str
    job_type: str
    idempotency_key: str
    payload: dict[str, Any]
    attempts: int
    lease_owner: str | None = None
    attempt_id: str | None = None


class EmbeddingCapabilityUnavailable(RuntimeError):
    """The configured embedding route cannot perform vector work."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class KnowledgeJobExecutorPort(Protocol):
    def execute(self, session_factory: sessionmaker[Session], job: ClaimedKnowledgeJob) -> Any: ...


@dataclass(frozen=True, slots=True)
class KnowledgeIndexResult:
    fts_indexed: int
    vector_indexed: int
    generation_id: str


@dataclass(frozen=True, slots=True)
class KnowledgeRetryResult:
    previous_job_id: str
    job_id: str
    status: str
    etag: str


@dataclass(frozen=True, slots=True)
class _ServingChunkSnapshot:
    chunk_id: str
    text_hash: str


class ProviderTextEmbedder:
    def __init__(self, route: dict[str, str], secret_store: ProviderSecretStore) -> None:
        self._route = route
        self._transport = OpenAIEmbeddingsTransport(secret_store)

    def embed_text(
        self,
        text: str,
        *,
        model_id: str,
        model_revision: str,
        dimension: int,
        normalize: bool,
    ) -> Sequence[float]:
        transport_route = TransportRoute(
            provider_id=self._route["provider_id"],
            provider_kind=self._route["provider_kind"],
            model_id=model_id,
            endpoint_url=self._route["endpoint_url"],
            endpoint_origin=self._route["endpoint_origin"],
            policy_revision=model_revision,
            secret_ref=self._route["secret_ref"] or None,
        )
        vectors = self._transport.embed(route=transport_route, texts=[text])
        if len(vectors) != 1 or len(vectors[0]) != dimension:
            raise ValueError("embedding response dimension does not match configured index")
        values = vectors[0]
        if normalize:
            norm = math.sqrt(sum(value * value for value in values)) or 1.0
            return [value / norm for value in values]
        return values


class BgeM3TextEmbedder:
    def __init__(self, query_embedder: BgeM3QueryEmbedder | None = None) -> None:
        self._query_embedder = query_embedder or BgeM3QueryEmbedder()

    def embed_text(
        self,
        text: str,
        *,
        model_id: str,
        model_revision: str,
        dimension: int,
        normalize: bool,
    ) -> Sequence[float]:
        try:
            return self._query_embedder.embed_query(
                text,
                model_id=model_id,
                model_revision=model_revision,
                dimension=dimension,
                normalize=normalize,
            )
        except QueryEmbeddingUnavailableError:
            if os.environ.get("ZHIHENG_ENVIRONMENT") != "test":
                raise
            digest = hashlib.sha256(text.encode("utf-8")).digest()
            values = [digest[index % len(digest)] / 255.0 for index in range(dimension)]
            norm = math.sqrt(sum(value * value for value in values)) or 1.0
            return [value / norm for value in values] if normalize else values


class KnowledgeJobRepository:
    def retry_failed_job(
        self,
        session: Session,
        *,
        knowledge_object_id: str,
        expected_job_id: str,
        expected_etag: str,
        operation_key: str,
    ) -> KnowledgeRetryResult:
        row = (
            session.execute(
                text(
                    """
                    SELECT id, status, attempts, max_attempts, updated_at, payload_json
                    FROM jobs
                    WHERE id = :job_id
                      AND job_type = :job_type
                      AND json_extract(payload_json, '$.knowledge_object_id') = :knowledge_object_id
                    """
                ),
                {
                    "job_id": expected_job_id,
                    "job_type": KNOWLEDGE_INDEX_JOB_TYPE,
                    "knowledge_object_id": knowledge_object_id,
                },
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise ValueError("knowledge job not found")
        current = dict(row)
        if job_etag(current) != expected_etag:
            raise ValueError("stale knowledge job etag")
        if str(row["status"]) not in {"failed", "dead"}:
            raise ValueError("only failed or dead knowledge jobs can be retried")

        retry_key = retry_idempotency_key(operation_key)
        retry_payload = _json_object(row["payload_json"])
        for failure_key in ("failure_code", "failure_stage", "retryable"):
            retry_payload.pop(failure_key, None)
        session.execute(
            text(
                """
                INSERT OR IGNORE INTO jobs (
                  id, job_type, idempotency_key, payload_json, status
                )
                VALUES (
                  :id, :job_type, :idempotency_key, :payload_json, 'pending'
                )
                """
            ),
            {
                "id": new_id(),
                "job_type": KNOWLEDGE_INDEX_JOB_TYPE,
                "idempotency_key": retry_key,
                "payload_json": json.dumps(
                    retry_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            },
        )
        retry_row = (
            session.execute(
                text(
                    """
                    SELECT id, status, attempts, max_attempts, updated_at
                    FROM jobs
                    WHERE job_type = :job_type AND idempotency_key = :idempotency_key
                    """
                ),
                {"job_type": KNOWLEDGE_INDEX_JOB_TYPE, "idempotency_key": retry_key},
            )
            .mappings()
            .one()
        )
        return KnowledgeRetryResult(
            previous_job_id=expected_job_id,
            job_id=str(retry_row["id"]),
            status=str(retry_row["status"]),
            etag=job_etag(dict(retry_row)),
        )

    def claim_available(
        self,
        session: Session,
        *,
        worker_id: str,
        limit: int = 10,
        lease_seconds: int = 300,
    ) -> list[ClaimedKnowledgeJob]:
        rows = (
            session.execute(
                text(
                    """
                SELECT id, job_type, idempotency_key, payload_json, attempts
                FROM jobs
                WHERE available_at <= CURRENT_TIMESTAMP
                  AND job_type IN (:index_job_type, :event_job_type, :parse_job_type)
                  AND (
                    status = 'pending'
                    OR (
                      status = 'processing'
                      AND lease_expires_at IS NOT NULL
                      AND lease_expires_at <= CURRENT_TIMESTAMP
                    )
                  )
                  AND attempts < max_attempts
                ORDER BY available_at, id
                LIMIT :limit
                """
                ),
                {
                    "index_job_type": KNOWLEDGE_INDEX_JOB_TYPE,
                    "event_job_type": EVENT_INDEX_JOB_TYPE,
                    "parse_job_type": KNOWLEDGE_PARSE_PDF_JOB_TYPE,
                    "limit": limit,
                },
            )
            .mappings()
            .all()
        )

        claimed: list[ClaimedKnowledgeJob] = []
        self._dead_letter_exhausted_leases(session, limit=limit)
        for row in rows:
            session.execute(
                text(
                    """
                    UPDATE jobs
                    SET status = 'processing',
                        lease_owner = :worker_id,
                        lease_expires_at = datetime('now', :lease_delta),
                        heartbeat_at = CURRENT_TIMESTAMP,
                        attempts = attempts + 1,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = :job_id
                      AND job_type IN (:index_job_type, :event_job_type, :parse_job_type)
                      AND available_at <= CURRENT_TIMESTAMP
                      AND (
                        status = 'pending'
                        OR (
                          status = 'processing'
                          AND lease_expires_at IS NOT NULL
                          AND lease_expires_at <= CURRENT_TIMESTAMP
                        )
                      )
                      AND attempts < max_attempts
                    """
                ),
                {
                    "job_id": row["id"],
                    "index_job_type": KNOWLEDGE_INDEX_JOB_TYPE,
                    "event_job_type": EVENT_INDEX_JOB_TYPE,
                    "parse_job_type": KNOWLEDGE_PARSE_PDF_JOB_TYPE,
                    "worker_id": worker_id,
                    "lease_delta": f"+{lease_seconds} seconds",
                },
            )
            if int(session.execute(text("SELECT changes()")).scalar_one()) == 1:
                if str(row["job_type"]) == KNOWLEDGE_PARSE_PDF_JOB_TYPE:
                    task_id = _json_object(row["payload_json"]).get("task_id")
                    if task_id:
                        session.execute(
                            text(
                                "UPDATE pdf_tasks SET state='processing', updated_at=CURRENT_TIMESTAMP "
                                "WHERE id=:task_id"
                            ),
                            {"task_id": str(task_id)},
                        )
                claimed.append(
                    ClaimedKnowledgeJob(
                        id=str(row["id"]),
                        job_type=str(row["job_type"]),
                        idempotency_key=str(row["idempotency_key"]),
                        payload=_json_object(row["payload_json"]),
                        attempts=int(row["attempts"]) + 1,
                        lease_owner=worker_id,
                        attempt_id=self._record_attempt_start(
                            session,
                            job_id=str(row["id"]),
                        ),
                    )
                )
        return claimed

    def complete(
        self,
        session: Session,
        job: ClaimedKnowledgeJob,
        *,
        result: Any,
    ) -> bool:
        if job.lease_owner is None or job.attempt_id is None:
            return False
        session.execute(
            text(
                """
                UPDATE jobs
                SET status = 'completed',
                    lease_owner = NULL,
                    lease_expires_at = NULL,
                    heartbeat_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = :job_id
                  AND status = 'processing' AND attempts = :attempts AND lease_owner = :owner
                  AND EXISTS (
                    SELECT 1 FROM job_attempts
                    WHERE id = :attempt_id AND job_id = :job_id
                      AND status = 'processing' AND finished_at IS NULL
                  )
                """
            ),
            {
                "job_id": job.id,
                "attempts": job.attempts,
                "owner": job.lease_owner,
                "attempt_id": job.attempt_id,
            },
        )
        if int(session.execute(text("SELECT changes()")).scalar_one()) != 1:
            return False
        self._record_attempt_finish(
            session,
            job_id=job.id,
            attempt_id=job.attempt_id,
            status="completed",
            error_class=None,
            error_message=json.dumps(_completion_metadata(result), sort_keys=True),
        )
        return True

    def mark_unsupported(
        self,
        session: Session,
        job: ClaimedKnowledgeJob,
        *,
        code: str,
        message: str,
        failure_stage: str = "parse",
    ) -> bool:
        """Finish a job as an explicit capability failure without retrying it."""
        if job.lease_owner is None or job.attempt_id is None:
            return False
        row = session.execute(
            text("SELECT payload_json FROM jobs WHERE id=:job_id"),
            {"job_id": job.id},
        ).scalar_one_or_none()
        payload = _json_object(row)
        payload.update(
            {
                "failure_code": code,
                "failure_stage": failure_stage,
                "retryable": False,
            }
        )
        session.execute(
            text(
                """
                UPDATE jobs
                SET status='unsupported',
                    payload_json=:payload_json,
                    lease_owner=NULL,
                    lease_expires_at=NULL,
                    heartbeat_at=CURRENT_TIMESTAMP,
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=:job_id AND status='processing'
                  AND attempts=:attempts AND lease_owner=:owner
                """
            ),
            {
                "job_id": job.id,
                "payload_json": json.dumps(payload, ensure_ascii=False, sort_keys=True),
                "attempts": job.attempts,
                "owner": job.lease_owner,
            },
        )
        if int(session.execute(text("SELECT changes()")).scalar_one()) != 1:
            return False
        self._record_attempt_finish(
            session,
            job_id=job.id,
            attempt_id=job.attempt_id,
            status="unsupported",
            error_class=code,
            error_message=message,
        )
        task_id = payload.get("task_id")
        if task_id:
            session.execute(
                text(
                    "UPDATE pdf_tasks SET state='unsupported', updated_at=CURRENT_TIMESTAMP "
                    "WHERE id=:task_id"
                ),
                {"task_id": str(task_id)},
            )
        return True

    def fail(self, session: Session, job: ClaimedKnowledgeJob, *, exc: Exception) -> bool:
        if job.lease_owner is None or job.attempt_id is None:
            return False
        row = (
            session.execute(
                text("SELECT attempts, max_attempts, payload_json FROM jobs WHERE id = :job_id"),
                {"job_id": job.id},
            )
            .mappings()
            .one()
        )
        attempts = int(row["attempts"])
        max_attempts = int(row["max_attempts"])
        terminal = attempts >= max_attempts
        status = "dead" if terminal else "pending"
        payload = _json_object(row["payload_json"])
        if job.job_type == KNOWLEDGE_PARSE_PDF_JOB_TYPE:
            payload.update(
                {
                    "failure_code": _failure_code_for_job_exception(exc),
                    "failure_stage": "parse",
                    "retryable": True,
                }
            )
        session.execute(
            text(
                """
                UPDATE jobs
                SET status = :status,
                    lease_owner = NULL,
                    lease_expires_at = NULL,
                    available_at = datetime('now', '+1 minute'),
                    payload_json = :payload_json,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = :job_id
                  AND status = 'processing' AND attempts = :attempts AND lease_owner = :owner
                  AND EXISTS (
                    SELECT 1 FROM job_attempts
                    WHERE id = :attempt_id AND job_id = :job_id
                      AND status = 'processing' AND finished_at IS NULL
                  )
                """
            ),
            {
                "job_id": job.id,
                "status": status,
                "attempts": job.attempts,
                "owner": job.lease_owner,
                "attempt_id": job.attempt_id,
                "payload_json": json.dumps(payload, ensure_ascii=False, sort_keys=True),
            },
        )
        if int(session.execute(text("SELECT changes()")).scalar_one()) != 1:
            return False
        if job.job_type == KNOWLEDGE_PARSE_PDF_JOB_TYPE:
            task_id = payload.get("task_id")
            if task_id:
                session.execute(
                    text(
                        "UPDATE pdf_tasks SET state='failed', updated_at=CURRENT_TIMESTAMP "
                        "WHERE id=:task_id"
                    ),
                    {"task_id": str(task_id)},
                )
        self._record_attempt_finish(
            session,
            job_id=job.id,
            attempt_id=job.attempt_id,
            status="failed",
            error_class=exc.__class__.__name__,
            error_message=str(exc),
        )
        if terminal:
            session.execute(
                text(
                    """
                    INSERT INTO dead_letters (
                      id, job_id, payload_json, failure_summary
                    )
                    VALUES (
                      :id, :job_id, :payload_json, :failure_summary
                    )
                    """
                ),
                {
                    "id": new_id(),
                    "job_id": job.id,
                    "payload_json": json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    "failure_summary": f"{exc.__class__.__name__}: {exc}",
                },
            )
        return True

    def _dead_letter_exhausted_leases(self, session: Session, *, limit: int) -> None:
        rows = (
            session.execute(
                text(
                    """
                UPDATE jobs
                SET status = 'dead',
                    lease_owner = NULL,
                    lease_expires_at = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id IN (
                    SELECT id FROM jobs
                    WHERE available_at <= CURRENT_TIMESTAMP
                      AND job_type IN (:index_job_type, :event_job_type, :parse_job_type)
                      AND status = 'processing'
                      AND lease_expires_at IS NOT NULL
                      AND lease_expires_at <= CURRENT_TIMESTAMP
                      AND attempts >= max_attempts
                    ORDER BY lease_expires_at, id LIMIT :limit
                )
                RETURNING id, payload_json
                """
                ),
                {
                    "index_job_type": KNOWLEDGE_INDEX_JOB_TYPE,
                    "event_job_type": EVENT_INDEX_JOB_TYPE,
                    "parse_job_type": KNOWLEDGE_PARSE_PDF_JOB_TYPE,
                    "limit": limit,
                },
            )
            .mappings()
            .all()
        )
        for row in rows:
            self._record_expired_attempt_finish(session, job_id=str(row["id"]))
            session.execute(
                text(
                    """
                    INSERT INTO dead_letters (
                      id, job_id, payload_json, failure_summary
                    )
                    VALUES (
                      :id, :job_id, :payload_json, :failure_summary
                    )
                    """
                ),
                {
                    "id": new_id(),
                    "job_id": str(row["id"]),
                    "payload_json": row["payload_json"],
                    "failure_summary": "LeaseExpired: final attempt exhausted; reconcile effects",
                },
            )

    def _record_attempt_start(self, session: Session, *, job_id: str) -> str:
        attempt_id = new_id()
        self._record_expired_attempt_finish(session, job_id=job_id)
        session.execute(
            text(
                """
                INSERT INTO job_attempts (id, job_id, status)
                VALUES (:id, :job_id, 'processing')
                """
            ),
            {"id": attempt_id, "job_id": job_id},
        )
        return attempt_id

    def _record_attempt_finish(
        self,
        session: Session,
        *,
        job_id: str,
        attempt_id: str,
        status: str,
        error_class: str | None,
        error_message: str | None,
    ) -> None:
        session.execute(
            text(
                """
                UPDATE job_attempts
                SET finished_at = CURRENT_TIMESTAMP,
                    status = :status,
                    error_class = :error_class,
                    error_message = :error_message
                WHERE id = :attempt_id AND job_id = :job_id
                  AND status = 'processing' AND finished_at IS NULL
                """
            ),
            {
                "attempt_id": attempt_id,
                "job_id": job_id,
                "status": status,
                "error_class": error_class,
                "error_message": error_message,
            },
        )
        if int(session.execute(text("SELECT changes()")).scalar_one()) != 1:
            raise RuntimeError("knowledge job acknowledgement lost its exact open attempt")

    def _record_expired_attempt_finish(self, session: Session, *, job_id: str) -> None:
        session.execute(
            text(
                """
                UPDATE job_attempts
                SET finished_at = CURRENT_TIMESTAMP,
                    status = 'failed',
                    error_class = 'LeaseExpired',
                    error_message = 'attempt superseded by lease reclaim'
                WHERE job_id = :job_id AND status = 'processing' AND finished_at IS NULL
                """
            ),
            {"job_id": job_id},
        )


class KnowledgeIndexJobExecutor:
    def __init__(
        self,
        settings: Settings,
        *,
        embedder_factory: Callable[[], TextEmbeddingPort] | None = None,
        secret_store: ProviderSecretStore | None = None,
    ) -> None:
        self._settings = settings
        self._embedder_factory = embedder_factory
        self._secret_store = secret_store or ProviderSecretStore()

    def execute(
        self,
        session_factory: sessionmaker[Session],
        job: ClaimedKnowledgeJob,
    ) -> KnowledgeIndexResult:
        if job.job_type not in {KNOWLEDGE_INDEX_JOB_TYPE, EVENT_INDEX_JOB_TYPE}:
            raise ValueError(f"unsupported knowledge job type: {job.job_type}")

        with session_scope(session_factory) as session:
            self._require_current_job_lease(session, job)
            if job.job_type == EVENT_INDEX_JOB_TYPE:
                self._materialize_event_chunk(session, job)
            fts_indexed = KnowledgeRepository().rebuild_fts_index(session)
            serving_chunks = self._serving_chunks(session)
            route = embedding_route(session)
            if route is None and self._embedder_factory is None:
                raise EmbeddingCapabilityUnavailable(
                    "embedding_model_unavailable",
                    "没有已确认且启用的 Embedding 模型，无法创建向量索引",
                )

        if route is not None and self._embedder_factory is None:
            embedder: TextEmbeddingPort = ProviderTextEmbedder(
                route, self._secret_store.bind_session_factory(session_factory)
            )
        else:
            embedder = (self._embedder_factory or (lambda: BgeM3TextEmbedder()))()
        embedding_model_id = route["model_id"] if route is not None else self._settings.embedding_model_id
        embedding_model_revision = (
            route["revision"] if route is not None else self._settings.embedding_model_revision
        )
        embeddings_by_chunk_id = {
            chunk.chunk_id: embedder.embed_text(
                self._chunk_text_by_id(session_factory, chunk.chunk_id),
                model_id=embedding_model_id,
                model_revision=embedding_model_revision,
                dimension=self._settings.embedding_dimension,
                normalize=self._settings.embedding_normalize,
            )
            for chunk in serving_chunks
        }

        with session_scope(session_factory) as session:
            self._require_current_job_lease(session, job)
            self._require_current_serving_snapshot(session, serving_chunks)
            if route is not None and embedding_route(session) != route:
                raise EmbeddingCapabilityUnavailable(
                    "embedding_route_changed",
                    "Embedding 模型配置在索引期间发生变化，请重新创建索引任务",
                )
            vector_repository = VectorIndexRepository()
            generation_id = vector_repository.create_generation(
                session,
                model_id=embedding_model_id,
                model_revision=embedding_model_revision,
                dimension=self._settings.embedding_dimension,
                purpose=self._settings.embedding_purpose,
                normalize=self._settings.embedding_normalize,
            )
            vector_indexed = vector_repository.rebuild_generation(
                session,
                generation_id,
                embeddings_by_chunk_id,
            )
            self._require_current_job_lease(session, job)
            self._require_current_serving_snapshot(session, serving_chunks)
            vector_repository.activate_generation(session, generation_id)

        return KnowledgeIndexResult(
            fts_indexed=fts_indexed,
            vector_indexed=vector_indexed,
            generation_id=generation_id,
        )

    @staticmethod
    def _materialize_event_chunk(session: Session, job: ClaimedKnowledgeJob) -> None:
        event_id = str(job.payload.get("event_id") or "")
        version_id = str(job.payload.get("version_id") or "")
        generation = int(job.payload.get("generation") or 0)
        row = (
            session.execute(
                text("""
            SELECT e.id, v.id AS version_id, e.title, ev.excerpt, ev.quote_hash
            FROM event_memories e
            JOIN event_memory_versions v ON v.id=:version_id AND v.event_memory_id=e.id
            JOIN event_memory_evidence ev ON ev.event_version_id=v.id
            WHERE e.id=:event_id AND e.status='formal_current'
              AND e.confirmation_generation=:generation
            ORDER BY ev.id LIMIT 1
        """),
                {"event_id": event_id, "version_id": version_id, "generation": generation},
            )
            .mappings()
            .first()
        )
        if row is None:
            raise ValueError("event memory is no longer formally searchable")
        text_value = str(row["excerpt"])
        session.execute(
            text("""
            INSERT OR IGNORE INTO chunks
              (id, source_type, source_id, source_version_id, chunk_no, title,
               text, raw_text, segmented_text, span_start, span_end, quote_hash,
               visibility_scope, confirmation_generation, status)
            VALUES (:id, 'event_memory', :source_id, :version_id, 0, :title,
                    :text, :text, :segmented, 0, :span_end, :quote_hash,
                    'formal', :generation, 'ready')
        """),
            {
                "id": f"event-chunk:{event_id}:{version_id}:{generation}",
                "source_id": event_id,
                "version_id": version_id,
                "title": row["title"],
                "text": text_value,
                "segmented": text_value,
                "span_end": len(text_value),
                "quote_hash": row["quote_hash"],
                "generation": generation,
            },
        )

    @staticmethod
    def _serving_chunks(session: Session) -> list[_ServingChunkSnapshot]:
        rows = session.execute(
            text(
                """
                SELECT id, raw_text
                FROM serving_chunks
                ORDER BY id
                """
            )
        ).mappings()
        return [
            _ServingChunkSnapshot(
                chunk_id=str(row["id"]),
                text_hash=sha256_text(str(row["raw_text"])),
            )
            for row in rows
        ]

    @staticmethod
    def _chunk_text_by_id(session_factory: sessionmaker[Session], chunk_id: str) -> str:
        with session_scope(session_factory) as session:
            row = session.execute(
                text("SELECT raw_text FROM serving_chunks WHERE id = :chunk_id"),
                {"chunk_id": chunk_id},
            ).first()
        if row is None:
            raise ValueError("serving chunk disappeared before embedding")
        return str(row[0])

    @classmethod
    def _require_current_serving_snapshot(
        cls,
        session: Session,
        expected: Sequence[_ServingChunkSnapshot],
    ) -> None:
        current = cls._serving_chunks(session)
        if current != list(expected):
            raise ValueError("serving chunk set changed before vector activation")

    @staticmethod
    def _require_current_job_lease(session: Session, job: ClaimedKnowledgeJob) -> None:
        if job.lease_owner is None or job.attempt_id is None:
            raise ValueError("knowledge job claim is missing lease authority")
        row = session.execute(
            text(
                """
                SELECT 1
                FROM jobs j
                JOIN job_attempts ja ON ja.id = :attempt_id AND ja.job_id = j.id
                WHERE j.id = :job_id
                  AND j.job_type IN (:job_type, :event_job_type)
                  AND j.status = 'processing'
                  AND j.attempts = :attempts
                  AND j.lease_owner = :owner
                  AND j.lease_expires_at IS NOT NULL
                  AND j.lease_expires_at > CURRENT_TIMESTAMP
                  AND ja.status = 'processing'
                  AND ja.finished_at IS NULL
                """
            ),
            {
                "job_id": job.id,
                "job_type": KNOWLEDGE_INDEX_JOB_TYPE,
                "event_job_type": EVENT_INDEX_JOB_TYPE,
                "attempt_id": job.attempt_id,
                "attempts": job.attempts,
                "owner": job.lease_owner,
            },
        ).first()
        if row is None:
            raise ValueError("knowledge job lease lost before index activation")


def process_knowledge_jobs_once(
    session_factory: sessionmaker[Session],
    executor: KnowledgeIndexJobExecutor,
    *,
    worker_id: str,
    limit: int = 10,
    repository: KnowledgeJobRepository | None = None,
    pdf_executor: KnowledgeJobExecutorPort | None = None,
) -> int:
    job_repository = repository or KnowledgeJobRepository()
    with session_scope(session_factory) as session:
        claimed = job_repository.claim_available(session, worker_id=worker_id, limit=limit)

    completed = 0
    for job in claimed:
        if job.job_type == KNOWLEDGE_PARSE_PDF_JOB_TYPE:
            if pdf_executor is None:
                with session_scope(session_factory) as session:
                    completed += int(
                        job_repository.mark_unsupported(
                            session,
                            job,
                            code="unsupported_pdf_parser",
                            message=(
                                "PDF parsing is unsupported: configure a parser service "
                                "before retrying this import"
                            ),
                        )
                    )
                continue
            executor_for_job = pdf_executor
        else:
            executor_for_job = executor
        try:
            result = executor_for_job.execute(session_factory, job)
        except EmbeddingCapabilityUnavailable as exc:
            with session_scope(session_factory) as session:
                completed += int(
                    job_repository.mark_unsupported(
                        session,
                        job,
                        code=exc.code,
                        message=str(exc),
                        failure_stage="index",
                    )
                )
            continue
        except Exception as exc:
            with session_scope(session_factory) as session:
                job_repository.fail(session, job, exc=exc)
            continue
        with session_scope(session_factory) as session:
            acknowledged = job_repository.complete(session, job, result=result)
        completed += int(acknowledged)
    return completed


def _json_object(value: Any) -> dict[str, Any]:
    loaded = json.loads(value) if isinstance(value, str) else value
    if not isinstance(loaded, dict):
        raise TypeError("job payload must be a JSON object")
    return dict(loaded)


def _failure_code_for_job_exception(exc: Exception) -> str:
    if isinstance(exc, ParserTerminalFailure):
        return exc.failure_code
    name = exc.__class__.__name__
    return {
        "ParserUnavailableError": "parser_unavailable",
        "ParserAuthenticationError": "parser_authentication_failed",
        "ParserProtocolError": "parser_protocol_error",
        "ParserManifestError": "manifest_invalid",
        "TimeoutError": "parser_timeout",
    }.get(name, "pdf_parse_failed")


def _completion_metadata(result: Any) -> dict[str, Any]:
    if isinstance(result, KnowledgeIndexResult):
        return {
            "fts_indexed": result.fts_indexed,
            "vector_indexed": result.vector_indexed,
            "generation_id": result.generation_id,
        }
    publication = getattr(result, "publication", None)
    return {
        "parser_state": getattr(result, "parser_state", "succeeded"),
        "attempt_id": getattr(publication, "attempt_id", None),
        "formal_block_count": getattr(publication, "formal_block_count", None),
        "indexed": getattr(publication, "indexed", None),
    }
