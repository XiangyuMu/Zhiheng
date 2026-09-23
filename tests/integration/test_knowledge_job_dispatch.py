from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from zhiheng.core.config import Settings
from zhiheng.core.ids import json_text, new_id, sha256_text
from zhiheng.db.session import create_session_factory, create_sqlite_engine, session_scope
from zhiheng.jobs.knowledge_indexing import (
    ClaimedKnowledgeJob,
    KnowledgeIndexResult,
    process_knowledge_jobs_once,
)
from zhiheng.jobs.outbox import OutboxRepository
from zhiheng.jobs.pdf_parsing import PdfParseJobExecutor
from zhiheng.knowledge.pdf_worker import (
    ParserManifestReference,
    ParserReceipt,
    ParserStatus,
)


def _session_factory(tmp_path: Path) -> sessionmaker[Session]:
    db_path = tmp_path / "dispatch.db"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(config, "head")
    settings = Settings(
        environment="test",
        database_url=f"sqlite:///{db_path}",
        embedding_dimension=3,
        embedding_model_revision="synthetic",
    )
    return create_session_factory(create_sqlite_engine(settings))


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
    assert statuses == [
        ("knowledge.index", "completed"),
        ("knowledge.parse_pdf", "completed"),
    ]


def test_dispatches_real_parse_executor_and_publishes_manifest(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
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
                  'application/pdf', 1, 'imported_document', '{}', 'active', 1
                )
                """
            ),
            {"id": evidence_id, "sha256": source_sha256},
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

    manifest = {
        "schema_version": "pdf-parser.manifest.v1",
        "task_id": task_id,
        "source": {
            "evidence_object_id": evidence_id,
            "uri": "artifact://evidence/source.pdf",
            "sha256": source_sha256,
        },
        "parser": {
            "backend": "deepdoc",
            "version": "test",
            "attempt_id": "attempt-1",
        },
        "pages": [
            {
                "page_no": 1,
                "width": 200,
                "height": 100,
                "rotation": 0,
                "crop_box": [0, 0, 200, 100],
                "user_unit": 1,
                "render": None,
                "status": "parsed",
            }
        ],
        "blocks": [
            {
                "key": "body-1",
                "page_no": 1,
                "region_type": "paragraph",
                "reading_order": 0,
                "bbox": [0, 0, 100, 20],
                "raw_bbox": [0, 0, 100, 20],
                "raw_space": "pdf_bottom_left",
                "transform_version": "pdf-crop-rotate-v1",
                "text": "hello",
                "quote_hash": sha256_text("hello"),
                "confidence": None,
                "status": "formal",
                "parent_key": None,
                "text_source": "text_layer",
            }
        ],
        "tables": [],
        "images": [],
        "diagnostics": [],
        "artifacts": [],
    }

    class _Parser:
        def submit(self, _request: object) -> ParserReceipt:
            return ParserReceipt("attempt-1", "accepted")

        def status(self, _attempt_id: str) -> ParserStatus:
            return ParserStatus(
                "attempt-1",
                "succeeded",
                ParserManifestReference("artifact://manifests/attempt-1.json", "d" * 64),
                None,
            )

        def load_manifest(self, _reference: object, *, read_bytes: object) -> dict[str, object]:
            return manifest

    class _Store:
        def read_bytes(self, _uri: str) -> bytes:
            return b"unused"

    with session_scope(session_factory) as session:
        claimed = OutboxRepository().claim_pending(session, limit=10)
        assert claimed == []

    parse_executor = PdfParseJobExecutor(
        _Parser(), object_store=_Store(), poll_interval_seconds=0, sleep=lambda _: None
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
                SELECT j.status, a.status, COUNT(b.id)
                FROM jobs j
                JOIN pdf_parse_attempts a ON a.task_id = :task_id
                LEFT JOIN evidence_blocks b ON b.attempt_id = a.id
                WHERE j.job_type = 'knowledge.parse_pdf'
                GROUP BY j.status, a.status
                """
            ),
            {"task_id": task_id},
        ).one()
        index_event = session.execute(
            text(
                """
                SELECT event_type, aggregate_type, aggregate_id
                FROM outbox_events
                WHERE event_type = 'knowledge.index' AND aggregate_id = 'attempt-1'
                """
            )
        ).one()

    assert completed == 1
    assert state == ("completed", "succeeded", 1)
    assert index_event == ("knowledge.index", "pdf_parse_attempt", "attempt-1")
