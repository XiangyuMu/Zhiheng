from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from zhiheng.api.main import create_app
from zhiheng.core.config import Settings
from zhiheng.core.ids import json_text, new_id
from zhiheng.db.session import create_session_factory, create_sqlite_engine, session_scope
from zhiheng.jobs.knowledge_indexing import (
    ClaimedKnowledgeJob,
    KnowledgeIndexResult,
    process_knowledge_jobs_once,
)
from zhiheng.jobs.outbox import OutboxRepository
from zhiheng.jobs.pdf_parsing import PdfParseJobExecutor
from zhiheng.knowledge.object_store import knowledge_object_store_for_settings
from zhiheng.knowledge.pdf_worker import (
    ParserManifestReference,
    ParserReceipt,
    ParserStatus,
    ParserWorkerClient,
)


def _settings_and_session_factory(tmp_path: Path) -> tuple[Settings, sessionmaker[Session]]:
    db_path = tmp_path / "dispatch.db"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(config, "head")
    settings = Settings(
        environment="test",
        database_url=f"sqlite:///{db_path}",
        embedding_dimension=3,
        embedding_model_revision="synthetic",
        knowledge_object_store_path=str(tmp_path / "objects"),
    )
    return settings, create_session_factory(create_sqlite_engine(settings))


def _session_factory(tmp_path: Path) -> sessionmaker[Session]:
    return _settings_and_session_factory(tmp_path)[1]


def _insert_job(session: Session, job_type: str, payload: dict[str, object]) -> None:
    session.execute(
        text(
            """
            INSERT INTO jobs (id, job_type, idempotency_key, payload_json, status)
            VALUES (:id, :job_type, :key, :payload, 'pending')
            """
        ),
        {
            "id": new_id(),
            "job_type": job_type,
            "key": f"dispatch:{job_type}",
            "payload": json_text(payload),
        },
    )


class _RecordingExecutor:
    def __init__(self, result: object) -> None:
        self.result = result
        self.jobs: list[str] = []

    def execute(self, _session_factory: object, job: ClaimedKnowledgeJob) -> object:
        self.jobs.append(job.job_type)
        return self.result


def test_dispatches_parse_and_index_jobs_to_their_respective_executors(
    tmp_path: Path,
) -> None:
    session_factory = _session_factory(tmp_path)
    with session_scope(session_factory) as session:
        _insert_job(session, "knowledge.parse_pdf", {"task_id": "task-1"})
        _insert_job(session, "knowledge.index", {})

    index_executor = _RecordingExecutor(
        KnowledgeIndexResult(fts_indexed=1, vector_indexed=1, generation_id="generation-1")
    )
    parse_executor = _RecordingExecutor(
        SimpleNamespace(
            publication=SimpleNamespace(
                attempt_id="attempt-1",
                formal_block_count=1,
                indexed=True,
            ),
            parser_state="succeeded",
        )
    )

    completed = process_knowledge_jobs_once(
        session_factory,
        index_executor,  # type: ignore[arg-type]
        worker_id="dispatch-worker",
        pdf_executor=parse_executor,
    )

    with session_scope(session_factory) as session:
        statuses = session.execute(
            text("SELECT job_type, status FROM jobs ORDER BY job_type")
        ).all()

    assert completed == 2
    assert index_executor.jobs == ["knowledge.index"]
    assert parse_executor.jobs == ["knowledge.parse_pdf"]
    assert [tuple(row) for row in statuses] == [
        ("knowledge.index", "completed"),
        ("knowledge.parse_pdf", "completed"),
    ]


def test_dispatches_real_parse_executor_and_publishes_manifest(tmp_path: Path) -> None:
    settings, session_factory = _settings_and_session_factory(tmp_path)
    client = TestClient(create_app(settings))
    bootstrap = client.post(
        "/auth/bootstrap",
        json={"username": "solo_user", "password": "correct horse battery staple"},
    )
    assert bootstrap.status_code == 200
    user_id = str(bootstrap.json()["user_id"])
    task_id = new_id()
    evidence_id = new_id()
    source_sha256 = "a" * 64
    with session_scope(session_factory) as session:
        session.execute(
            text(
                """
                INSERT INTO evidence_objects (
                  id, object_uri, sha256, media_type, byte_size, source_kind,
                  source_metadata_json, status, erasable
                )
                VALUES (
                  :id, 'artifact://evidence/source.pdf', :sha256,
                  'application/pdf', 1, 'imported_document', :metadata, 'active', 1
                )
                """
            ),
            {
                "id": evidence_id,
                "sha256": source_sha256,
                "metadata": json_text({"owner_user_id": user_id}),
            },
        )
        session.execute(
            text(
                """
                INSERT INTO pdf_tasks (
                  id, evidence_object_id, backend, options_hash,
                  idempotency_key, state
                )
                VALUES (
                  :task_id, :evidence_id, 'deepdoc', :options_hash,
                  :idempotency_key, 'queued'
                )
                """
            ),
            {
                "task_id": task_id,
                "evidence_id": evidence_id,
                "options_hash": "b" * 64,
                "idempotency_key": f"dispatch:{task_id}",
            },
        )
        _insert_job(
            session,
            "knowledge.parse_pdf",
            {
                "task_id": task_id,
                "evidence_object_id": evidence_id,
                "source_uri": "artifact://evidence/source.pdf",
                "source_sha256": source_sha256,
                "backend": "deepdoc",
                "options_hash": "b" * 64,
                "output_prefix": f"artifact://pdf-attempts/{task_id}",
                "options": {},
                "schema_version": "pdf-parser.manifest.v1",
            },
        )

    native_content_list = [
        {"type": "text", "text": "hello", "bbox": [0, 0, 100, 20], "page_idx": 0}
    ]
    native_payload = {
        "content_list": native_content_list,
        "metadata": {
            "task_id": task_id,
            "evidence_object_id": evidence_id,
            "source_uri": "artifact://evidence/source.pdf",
            "source_sha256": source_sha256,
            "attempt_id": "attempt-1",
            "version": "mineru-fixture",
        },
    }
    native_body = json.dumps(native_payload).encode("utf-8")
    store = knowledge_object_store_for_settings(settings)
    raw_manifest = store.write_binary_artifact(native_body, namespace="manifests/raw")

    class _Parser(ParserWorkerClient):
        def __init__(self) -> None:
            self._allowed_uri_schemes = frozenset({"artifact", "file"})

        def submit(self, request: object) -> ParserReceipt:
            return ParserReceipt("attempt-1", "accepted")

        def status(self, attempt_id: str) -> ParserStatus:
            return ParserStatus(
                "attempt-1",
                "succeeded",
                ParserManifestReference(raw_manifest.uri, raw_manifest.sha256),
                None,
            )

    with session_scope(session_factory) as session:
        claimed = OutboxRepository().claim_pending(session, limit=10)
        assert claimed == []

    parse_executor = PdfParseJobExecutor(
        _Parser(), object_store=store, poll_interval_seconds=0, sleep=lambda _: None
    )
    index_executor = _RecordingExecutor(
        KnowledgeIndexResult(fts_indexed=0, vector_indexed=0, generation_id="generation-1")
    )

    completed = process_knowledge_jobs_once(
        session_factory,
        index_executor,  # type: ignore[arg-type]
        worker_id="dispatch-worker",
        pdf_executor=parse_executor,
    )

    with session_scope(session_factory) as session:
        state = session.execute(
            text(
                """
                SELECT j.status, a.status, COUNT(b.id), a.manifest_uri, a.manifest_sha256
                FROM jobs j
                JOIN pdf_parse_attempts a ON a.task_id = :task_id
                LEFT JOIN evidence_blocks b ON b.attempt_id = a.id
                WHERE j.job_type = 'knowledge.parse_pdf'
                GROUP BY j.status, a.status, a.manifest_uri, a.manifest_sha256
                """
            ),
            {"task_id": task_id},
        ).one()
        index_event = session.execute(
            text(
                """
                SELECT event_type, aggregate_type, aggregate_id, payload_json
                FROM outbox_events
                WHERE event_type = 'knowledge.index' AND aggregate_id = 'attempt-1'
                """
            )
        ).one()
        materialized = session.execute(
            text("""
                SELECT ko.id, kv.id, c.id, c.raw_text, cs.page_no, cv.text_artifact_uri
                FROM knowledge_objects ko
                JOIN knowledge_versions kv ON kv.id=ko.current_version_id
                JOIN chunks c ON c.source_id=ko.id AND c.source_version_id=kv.id
                JOIN content_spans cs ON cs.id=c.content_span_id
                JOIN content_versions cv ON cv.id=kv.content_version_id
                WHERE cv.evidence_object_id=:evidence_id
                """),
            {"evidence_id": evidence_id},
        ).one()

    assert completed == 1
    assert state[0:3] == ("completed", "succeeded", 1)
    canonical_uri = str(state[3])
    canonical_sha256 = str(state[4])
    assert "/manifests/" in canonical_uri
    persisted_body = store.read_bytes(canonical_uri)
    assert hashlib.sha256(persisted_body).hexdigest() == canonical_sha256
    persisted_manifest = json.loads(persisted_body)
    assert persisted_manifest["schema_version"] == "pdf-parser.manifest.v1"
    assert "content_list" not in persisted_manifest
    endpoint = client.get(f"/v1/knowledge/pdf-imports/{task_id}/manifest")
    assert endpoint.status_code == 200
    endpoint_payload = endpoint.json()
    assert endpoint_payload["manifest_uri"] == canonical_uri
    assert endpoint_payload["manifest_sha256"] == canonical_sha256
    assert endpoint_payload["manifest"]["schema_version"] == "pdf-parser.manifest.v1"
    assert "content_list" not in endpoint_payload["manifest"]
    assert index_event[0:3] == ("knowledge.index", "pdf_parse_attempt", "attempt-1")
    assert materialized[3:] == ("hello", 1, canonical_uri)
    assert materialized[0] in str(index_event[3])


def test_missing_pdf_executor_persists_unsupported_terminal_state(
    tmp_path: Path,
) -> None:
    session_factory = _session_factory(tmp_path)
    task_id = new_id()
    evidence_id = new_id()
    with session_scope(session_factory) as session:
        session.execute(
            text(
                """
                INSERT INTO evidence_objects (
                  id, object_uri, sha256, media_type, byte_size, source_kind,
                  source_metadata_json, status, erasable
                )
                VALUES (
                  :id, 'artifact://evidence/unsupported.pdf', :sha256,
                  'application/pdf', 1, 'imported_document', '{}', 'active', 1
                )
                """
            ),
            {"id": evidence_id, "sha256": "a" * 64},
        )
        session.execute(
            text(
                """
                INSERT INTO pdf_tasks (
                  id, evidence_object_id, backend, options_hash,
                  idempotency_key, state
                )
                VALUES (
                  :task_id, :evidence_id, 'deepdoc', :options_hash,
                  :idempotency_key, 'queued'
                )
                """
            ),
            {
                "task_id": task_id,
                "evidence_id": evidence_id,
                "options_hash": "b" * 64,
                "idempotency_key": f"unsupported:{task_id}",
            },
        )
        _insert_job(
            session,
            "knowledge.parse_pdf",
            {
                "task_id": task_id,
                "evidence_object_id": evidence_id,
                "failure_stage": "parse",
            },
        )

    completed = process_knowledge_jobs_once(
        session_factory,
        _RecordingExecutor(
            KnowledgeIndexResult(fts_indexed=0, vector_indexed=0, generation_id="unused")
        ),  # type: ignore[arg-type]
        worker_id="dispatch-worker",
        pdf_executor=None,
    )

    with session_scope(session_factory) as session:
        state = session.execute(
            text(
                """
                SELECT j.status, json_extract(j.payload_json, '$.failure_code'),
                       a.status, a.error_class, p.state
                FROM jobs j
                JOIN job_attempts a ON a.job_id=j.id
                JOIN pdf_tasks p ON p.id=:task_id
                WHERE j.job_type='knowledge.parse_pdf'
                """
            ),
            {"task_id": task_id},
        ).one()

    assert completed == 1
    assert state == (
        "unsupported",
        "unsupported_pdf_parser",
        "unsupported",
        "unsupported_pdf_parser",
        "unsupported",
    )


def test_pdf_publication_event_reaches_durable_index_job(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_scope(session_factory) as session:
        event_id = new_id()
        session.execute(
            text("""
                INSERT INTO outbox_events
                  (id, event_type, aggregate_type, aggregate_id, payload_json, status)
                VALUES (:id, 'knowledge.index', 'pdf_parse_attempt', :attempt, :payload, 'pending')
            """),
            {
                "id": event_id,
                "attempt": "synthetic-pdf-attempt",
                "payload": json_text({"attempt_id": "synthetic-pdf-attempt"}),
            },
        )
        outbox = OutboxRepository()
        events = outbox.claim_pending(session)
        assert outbox.enqueue_jobs_for_events(session, events) == 1
        row = session.execute(
            text("SELECT job_type, status, payload_json FROM jobs WHERE idempotency_key=:id"),
            {"id": event_id},
        ).one()
        assert row.job_type == "knowledge.index"
        assert row.status == "pending"
        assert "synthetic-pdf-attempt" in str(row.payload_json)
        assert outbox.enqueue_jobs_for_events(session, events) == 0
