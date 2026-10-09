from __future__ import annotations

import hashlib
import json
from io import BytesIO
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from pypdf import PdfWriter
from sqlalchemy import text

from tests.integration.test_g005_api_impl import _client, _headers, _login
from zhiheng.api.main import create_app
from zhiheng.core.config import Settings
from zhiheng.core.ids import new_id, sha256_text
from zhiheng.db.session import session_scope
from zhiheng.jobs.knowledge_indexing import (
    KnowledgeIndexJobExecutor,
    process_knowledge_jobs_once,
)
from zhiheng.jobs.outbox import OutboxRepository
from zhiheng.knowledge.object_store import knowledge_object_store_for_settings
from zhiheng.knowledge.pdf_repository import PdfRepository
from zhiheng.knowledge.pdf_worker import ParserUnavailableError


def _pdf_bytes() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=100)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def _manifest(
    *,
    task_id: str,
    evidence_id: str,
    source_sha256: str,
    attempt_id: str,
) -> dict[str, object]:
    return {
        "schema_version": "pdf-parser.manifest.v1",
        "task_id": task_id,
        "source": {
            "evidence_object_id": evidence_id,
            "uri": "file:///source.pdf",
            "sha256": source_sha256,
        },
        "parser": {"backend": "deepdoc", "version": "test", "attempt_id": attempt_id},
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


def test_pdf_upload_is_async_idempotent_and_emits_parse_event(tmp_path: Path) -> None:
    client, session_factory = _client(tmp_path)
    csrf = _login(client)
    body = _pdf_bytes()
    headers = {
        **_headers(csrf, "pdf-upload-1"),
        "Content-Type": "application/pdf",
    }
    response = client.post(
        "/v1/knowledge/pdf-imports",
        params={"title": "测试 PDF", "primary_domain_id": "technology.ai"},
        content=body,
        headers=headers,
    )
    replay = client.post(
        "/v1/knowledge/pdf-imports",
        params={"title": "测试 PDF", "primary_domain_id": "technology.ai"},
        content=body,
        headers=headers,
    )

    assert response.status_code == replay.status_code == 202
    assert response.json() == replay.json()
    payload = response.json()
    assert payload["state"] == "queued"
    assert payload["source_sha256"] == hashlib.sha256(body).hexdigest()
    status_response = client.get(payload["status_url"])
    assert status_response.status_code == 200
    assert status_response.json()["state"] == "queued"
    with session_scope(session_factory) as session:
        event = (
            session.execute(
                text(
                    """
                SELECT event_type, aggregate_type, aggregate_id, payload_json
                FROM outbox_events
                WHERE aggregate_id = :task_id
                """
                ),
                {"task_id": payload["task_id"]},
            )
            .mappings()
            .one()
        )
        assert event["event_type"] == "knowledge.parse_pdf"
        assert event["aggregate_type"] == "pdf_task"
        event_payload = json.loads(event["payload_json"])
        assert event_payload["task_id"] == payload["task_id"]
        assert event_payload["evidence_object_id"] == payload["evidence_object_id"]
        assert event_payload["source_sha256"] == payload["source_sha256"]
        assert event_payload["source_uri"].startswith("file://")
        assert event_payload["backend"] == "deepdoc"
        assert len(event_payload["options_hash"]) == 64
        assert event_payload["output_prefix"] == f"artifact://pdf-attempts/{payload['task_id']}"
        assert event_payload["options"] == {}
        assert event_payload["schema_version"] == "pdf-parser.manifest.v1"


def test_pdf_upload_uses_configured_parser_backend_when_query_omits_backend(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "configured-backend.db"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(config, "head")
    settings = Settings(
        environment="test",
        database_url=f"sqlite:///{db_path}",
        pdf_parser_backend="mineru",
    )
    client = TestClient(create_app(settings))
    csrf = _login(client)
    response = client.post(
        "/v1/knowledge/pdf-imports",
        params={"title": "configured", "primary_domain_id": "technology.ai"},
        content=_pdf_bytes(),
        headers={**_headers(csrf, "configured-backend"), "Content-Type": "application/pdf"},
    )
    assert response.status_code == 202
    assert response.json()["state"] == "queued"
    assert client.get(response.json()["status_url"]).json()["backend"] == "mineru"


def test_pdf_status_exposes_unsupported_worker_diagnostics(tmp_path: Path) -> None:
    client, session_factory = _client(tmp_path)
    csrf = _login(client)
    response = client.post(
        "/v1/knowledge/pdf-imports",
        params={"title": "未配置解析器 PDF", "primary_domain_id": "technology.ai"},
        content=_pdf_bytes(),
        headers={
            **_headers(csrf, "pdf-unsupported"),
            "Content-Type": "application/pdf",
        },
    )
    assert response.status_code == 202
    task = response.json()
    with session_scope(session_factory) as session:
        events = OutboxRepository().claim_pending(session, limit=10)
        assert len(events) == 1
        assert OutboxRepository().enqueue_jobs_for_events(session, events) == 1

    completed = process_knowledge_jobs_once(
        session_factory,
        KnowledgeIndexJobExecutor(Settings(environment="test")),
        worker_id="pdf-unsupported-worker",
        pdf_executor=None,
    )
    assert completed == 1

    status = client.get(task["status_url"])
    assert status.status_code == 200
    body = status.json()
    assert body["state"] == "unsupported"
    assert body["error_code"] == "unsupported_pdf_parser"
    assert "configure a parser service" in body["redacted_summary"]
    assert body["retryable"] is False


def test_pdf_parser_failure_is_projected_to_retryable_task_status(tmp_path: Path) -> None:
    client, session_factory = _client(tmp_path)
    csrf = _login(client)
    response = client.post(
        "/v1/knowledge/pdf-imports",
        params={"title": "失败 PDF", "primary_domain_id": "technology.ai"},
        content=_pdf_bytes(),
        headers={**_headers(csrf, "pdf-parser-failure"), "Content-Type": "application/pdf"},
    )
    assert response.status_code == 202
    task = response.json()
    with session_scope(session_factory) as session:
        events = OutboxRepository().claim_pending(session, limit=10)
        assert OutboxRepository().enqueue_jobs_for_events(session, events) == 1
        pending = session.execute(
            text(
                "SELECT job_type, status, available_at, attempts, max_attempts "
                "FROM jobs ORDER BY created_at DESC LIMIT 1"
            )
        ).mappings().one()
        assert pending["job_type"] == "knowledge.parse_pdf"
        session.execute(text("UPDATE jobs SET available_at=CURRENT_TIMESTAMP"))

    class FailingParser:
        def execute(self, *_: object) -> None:
            raise ParserUnavailableError("parser unavailable")

    assert (
        process_knowledge_jobs_once(
            session_factory,
            KnowledgeIndexJobExecutor(Settings(environment="test")),
            worker_id="pdf-failure-worker",
            pdf_executor=FailingParser(),
        )
        == 0
    )
    body = client.get(task["status_url"]).json()
    assert body["state"] == "failed"
    assert body["error_code"] == "parser_unavailable"
    assert body["retryable"] is True


def test_pdf_manifest_persists_attempt_pages_and_blocks(tmp_path: Path) -> None:
    settings = Settings(
        environment="test",
        database_url=f"sqlite:///{tmp_path / 'manifest.db'}",
        knowledge_object_store_path=str(tmp_path / "objects"),
    )
    from zhiheng.db.session import create_session_factory, create_sqlite_engine

    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", settings.database_url)
    command.upgrade(config, "head")
    factory = create_session_factory(create_sqlite_engine(settings))
    body = _pdf_bytes()
    store = knowledge_object_store_for_settings(settings)
    source = store.write_binary_artifact(body, namespace="evidence/pdf")
    evidence_id = new_id()
    attempt_id = new_id()
    with session_scope(factory) as session:
        created = PdfRepository().create_task(
            session,
            evidence_object_id=evidence_id,
            source_uri=source.uri,
            source_sha256=source.sha256,
            byte_size=source.byte_size,
            title="fixture",
            primary_domain_id="technology.ai",
            backend="deepdoc",
            options_hash="a" * 64,
            idempotency_key="test:manifest",
        )
        task_id = created.task_id
        manifest = _manifest(
            task_id=task_id,
            evidence_id=evidence_id,
            source_sha256=source.sha256,
            attempt_id=attempt_id,
        )
        PdfRepository().persist_manifest(
            session, manifest, manifest_uri="file:///manifest.json", manifest_sha256="b" * 64
        )
        counts = (
            session.execute(
                text(
                    """
                SELECT
                  (SELECT count(*) FROM pdf_parse_attempts WHERE task_id = :task_id) AS attempts,
                  (SELECT count(*) FROM pdf_pages WHERE attempt_id = :attempt_id) AS pages,
                  (SELECT count(*) FROM evidence_blocks WHERE attempt_id = :attempt_id) AS blocks
                """
                ),
                {"task_id": task_id, "attempt_id": attempt_id},
            )
            .mappings()
            .one()
        )
    assert counts == {"attempts": 1, "pages": 1, "blocks": 1}


def test_pdf_manifest_rejects_mismatched_source_hash(tmp_path: Path) -> None:
    settings = Settings(
        environment="test",
        database_url=f"sqlite:///{tmp_path / 'manifest-mismatch.db'}",
        knowledge_object_store_path=str(tmp_path / "objects"),
    )
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", settings.database_url)
    command.upgrade(config, "head")
    from zhiheng.db.session import create_session_factory, create_sqlite_engine

    factory = create_session_factory(create_sqlite_engine(settings))
    source_sha256 = hashlib.sha256(_pdf_bytes()).hexdigest()
    evidence_id = new_id()
    with session_scope(factory) as session:
        created = PdfRepository().create_task(
            session,
            evidence_object_id=evidence_id,
            source_uri="file:///source.pdf",
            source_sha256=source_sha256,
            byte_size=1,
            title="fixture",
            primary_domain_id="technology.ai",
            backend="deepdoc",
            options_hash="a" * 64,
            idempotency_key="test:manifest-mismatch",
        )
        manifest = _manifest(
            task_id=created.task_id,
            evidence_id=evidence_id,
            source_sha256="f" * 64,
            attempt_id=new_id(),
        )
        with pytest.raises(ValueError, match="manifest source hash mismatch"):
            PdfRepository().persist_manifest(
                session,
                manifest,
                manifest_uri="file:///manifest.json",
                manifest_sha256="b" * 64,
            )

        assert (
            session.execute(
                text("SELECT count(*) FROM pdf_parse_attempts WHERE task_id = :task_id"),
                {"task_id": created.task_id},
            ).scalar_one()
            == 0
        )
