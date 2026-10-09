from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from zhiheng.core.config import Settings
from zhiheng.core.ids import json_text, new_id, sha256_text
from zhiheng.db.session import create_session_factory, create_sqlite_engine, session_scope
from zhiheng.jobs.knowledge_indexing import (
    KnowledgeIndexJobExecutor,
    KnowledgeJobRepository,
    process_knowledge_jobs_once,
)
from zhiheng.jobs.pdf_parsing import PdfParseJobExecutor
from zhiheng.knowledge.object_store import knowledge_object_store_for_settings
from zhiheng.knowledge.pdf_repository import PdfRepository
from zhiheng.knowledge.pdf_worker import (
    ParserManifestReference,
    ParserReceipt,
    ParserStatus,
    ParserTerminalFailure,
    ParserWorkerClient,
)


def _migrated(tmp_path: Path) -> tuple[Settings, sessionmaker[Session]]:
    database_url = f"sqlite:///{tmp_path / 'pdf-lease.db'}"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "head")
    settings = Settings(
        environment="test",
        database_url=database_url,
        knowledge_object_store_path=str(tmp_path / "objects"),
    )
    return settings, create_session_factory(create_sqlite_engine(settings))


def _manifest(
    *, task_id: str, evidence_id: str, source_sha256: str, attempt_id: str
) -> dict[str, Any]:
    text_value = "durable PDF lease contract"
    return {
        "schema_version": "pdf-parser.manifest.v1",
        "task_id": task_id,
        "source": {
            "evidence_object_id": evidence_id,
            "uri": "artifact://source.pdf",
            "sha256": source_sha256,
        },
        "parser": {"backend": "deepdoc", "version": "fixture", "attempt_id": attempt_id},
        "pages": [
            {
                "page_no": 1,
                "width": 612,
                "height": 792,
                "rotation": 0,
                "crop_box": [0, 0, 612, 792],
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
                "bbox": [36, 36, 360, 72],
                "raw_bbox": [36, 720, 360, 756],
                "raw_space": "pdf_bottom_left",
                "transform_version": "pdf-crop-rotate-v1",
                "text": text_value,
                "quote_hash": sha256_text(text_value),
                "confidence": 0.99,
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


class _ParserFixture:
    def __init__(
        self,
        manifest: dict[str, Any],
        on_load: Any = None,
        terminal_state: str = "succeeded",
    ) -> None:
        self.manifest = manifest
        self.on_load = on_load
        self.terminal_state = terminal_state
        self.submitted: list[str] = []
        self.loaded = 0

    def submit(self, request: Any) -> ParserReceipt:
        self.submitted.append(request.attempt_id)
        return ParserReceipt(request.attempt_id, "accepted")

    def status(self, attempt_id: str) -> ParserStatus:
        if self.terminal_state == "failed":
            return ParserStatus(attempt_id, "failed", None, "whole_task_parse_failed")
        return ParserStatus(
            attempt_id,
            "succeeded",
            ParserManifestReference("artifact://manifest.json", "c" * 64),
            None,
        )

    def load_manifest(
        self, reference: ParserManifestReference, *, read_bytes: Any
    ) -> dict[str, Any]:
        del reference, read_bytes
        self.loaded += 1
        if self.on_load is not None:
            self.on_load()
        return self.manifest


class _ObjectStore:
    def read_bytes(self, uri: str) -> bytes:
        del uri
        return b"{}"


def _seed_parse_job(
    session_factory: sessionmaker[Session],
    settings: Settings,
) -> tuple[str, str, str, dict[str, Any]]:
    store = knowledge_object_store_for_settings(settings)
    source = store.write_binary_artifact(b"synthetic pdf source", namespace="evidence/pdf")
    evidence_id = new_id()
    with session_scope(session_factory) as session:
        created = PdfRepository().create_task(
            session,
            evidence_object_id=evidence_id,
            source_uri=source.uri,
            source_sha256=source.sha256,
            byte_size=source.byte_size,
            title="lease fixture",
            primary_domain_id="technology.ai",
            backend="deepdoc",
            options_hash="a" * 64,
            idempotency_key=f"lease-fixture:{new_id()}",
        )
        actual_task_id = created.task_id
        session.execute(
            text("DELETE FROM outbox_events WHERE aggregate_id=:id"), {"id": actual_task_id}
        )
        attempt_id = new_id()
        payload = {
            "task_id": actual_task_id,
            "attempt_id": attempt_id,
            "lease_generation": 1,
            "backend": "deepdoc",
            "source_uri": source.uri,
            "source_sha256": source.sha256,
            "output_prefix": f"artifact://pdf-attempts/{actual_task_id}",
            "options_hash": "a" * 64,
            "options": {},
        }
        job_id = new_id()
        session.execute(
            text(
                "INSERT INTO jobs (id, job_type, idempotency_key, payload_json, status) "
                "VALUES (:id, 'knowledge.parse_pdf', :key, :payload, 'pending')"
            ),
            {"id": job_id, "key": f"lease-job:{actual_task_id}", "payload": json_text(payload)},
        )
    manifest = _manifest(
        task_id=actual_task_id,
        evidence_id=evidence_id,
        source_sha256=source.sha256,
        attempt_id=attempt_id,
    )
    return job_id, actual_task_id, attempt_id, manifest


def _executor(parser: _ParserFixture) -> PdfParseJobExecutor:
    return PdfParseJobExecutor(
        cast(ParserWorkerClient, parser),
        repository=PdfRepository(),
        object_store=_ObjectStore(),
        poll_interval_seconds=0,
        sleep=lambda _: None,
    )


def test_pdf_parser_job_uses_durable_lease_for_successful_publication(tmp_path: Path) -> None:
    settings, factory = _migrated(tmp_path)
    job_id, task_id, _attempt_id, manifest = _seed_parse_job(factory, settings)
    parser = _ParserFixture(manifest)

    completed = process_knowledge_jobs_once(
        factory,
        KnowledgeIndexJobExecutor(settings),
        worker_id="pdf-worker",
        pdf_executor=_executor(parser),
    )

    assert completed == 1
    assert parser.submitted == [manifest["parser"]["attempt_id"]]
    assert parser.loaded == 1
    with session_scope(factory) as session:
        assert (
            session.execute(
                text("SELECT status FROM jobs WHERE id=:id"), {"id": job_id}
            ).scalar_one()
            == "completed"
        )
        assert (
            session.execute(
                text("SELECT state FROM pdf_tasks WHERE id=:id"), {"id": task_id}
            ).scalar_one()
            == "parsed"
        )
        assert (
            session.execute(
                text("SELECT count(*) FROM pdf_parse_attempts WHERE task_id=:id"), {"id": task_id}
            ).scalar_one()
            == 1
        )
        assert (
            session.execute(
                text("SELECT count(*) FROM outbox_events WHERE event_type='knowledge.index'")
            ).scalar_one()
            == 1
        )


@pytest.mark.parametrize("mismatch", ["owner", "attempts", "status"])
def test_pdf_parser_lease_mismatch_blocks_manifest_publication(
    tmp_path: Path, mismatch: str
) -> None:
    settings, factory = _migrated(tmp_path)
    job_id, task_id, _attempt_id, manifest = _seed_parse_job(factory, settings)

    def lose_lease() -> None:
        with session_scope(factory) as session:
            if mismatch == "owner":
                session.execute(
                    text("UPDATE jobs SET lease_owner='replacement-worker' WHERE id=:id"),
                    {"id": job_id},
                )
            elif mismatch == "attempts":
                session.execute(
                    text("UPDATE jobs SET attempts=attempts + 1 WHERE id=:id"),
                    {"id": job_id},
                )
            else:
                session.execute(
                    text("UPDATE jobs SET status='pending' WHERE id=:id"),
                    {"id": job_id},
                )

    parser = _ParserFixture(manifest, on_load=lose_lease)
    completed = process_knowledge_jobs_once(
        factory,
        KnowledgeIndexJobExecutor(settings),
        worker_id="pdf-worker",
        pdf_executor=_executor(parser),
    )

    assert completed == 0
    with session_scope(factory) as session:
        expected_status = "pending" if mismatch == "status" else "processing"
        assert (
            session.execute(
                text("SELECT status FROM jobs WHERE id=:id"), {"id": job_id}
            ).scalar_one()
            == expected_status
        )
        assert (
            session.execute(
                text("SELECT count(*) FROM pdf_parse_attempts WHERE task_id=:id"), {"id": task_id}
            ).scalar_one()
            == 0
        )
        assert (
            session.execute(
                text("SELECT count(*) FROM outbox_events WHERE event_type='knowledge.index'")
            ).scalar_one()
            == 0
        )


def test_pdf_parser_failure_is_recorded_and_can_retry(tmp_path: Path) -> None:
    settings, factory = _migrated(tmp_path)
    job_id, task_id, _attempt_id, manifest = _seed_parse_job(factory, settings)
    failed_parser = _ParserFixture(manifest, terminal_state="failed")
    succeeded_parser = _ParserFixture(manifest)

    with session_scope(factory) as session:
        job = KnowledgeJobRepository().claim_available(session, worker_id="pdf-worker")[0]
    with pytest.raises(ParserTerminalFailure, match="parser task failed") as error:
        _executor(failed_parser).execute(factory, job)
    assert error.value.failure_code == "whole_task_parse_failed"
    with session_scope(factory) as session:
        assert (
            KnowledgeJobRepository().fail(
                session,
                job,
                exc=ParserTerminalFailure("whole_task_parse_failed", "parser task failed"),
            )
            is True
        )
        session.execute(
            text("UPDATE jobs SET available_at=CURRENT_TIMESTAMP WHERE id=:id"),
            {"id": job_id},
        )

    retried = process_knowledge_jobs_once(
        factory,
        KnowledgeIndexJobExecutor(settings),
        worker_id="pdf-retry-worker",
        pdf_executor=_executor(succeeded_parser),
    )

    assert retried == 1
    assert failed_parser.submitted == [manifest["parser"]["attempt_id"]]
    assert succeeded_parser.loaded == 1
    with session_scope(factory) as session:
        assert (
            session.execute(
                text("SELECT status FROM jobs WHERE id=:id"), {"id": job_id}
            ).scalar_one()
            == "completed"
        )
        assert (
            session.execute(
                text("SELECT count(*) FROM pdf_parse_attempts WHERE task_id=:id"),
                {"id": task_id},
            ).scalar_one()
            == 1
        )
        statuses = (
            session.execute(
                text("SELECT status FROM job_attempts WHERE job_id=:id ORDER BY started_at"),
                {"id": job_id},
            )
            .scalars()
            .all()
        )
        assert statuses == ["failed", "completed"]
