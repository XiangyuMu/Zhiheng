from __future__ import annotations

import io
from pathlib import Path

from pypdf import PdfWriter
from sqlalchemy import text

from tests.integration.test_g005_api_impl import _client, _headers, _login
from zhiheng.core.ids import json_text, new_id
from zhiheng.db.session import session_scope


def _pdf_bytes() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=100)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def test_import_task_projection_lists_queued_pdf_with_progress(tmp_path: Path) -> None:
    client, _session_factory = _client(tmp_path)
    csrf = _login(client)
    response = client.post(
        "/v1/knowledge/pdf-imports",
        params={"title": "projection fixture", "primary_domain_id": "technology.ai"},
        content=_pdf_bytes(),
        headers={
            **_headers(csrf, "projection-pdf-upload"),
            "Content-Type": "application/pdf",
        },
    )
    assert response.status_code == 202

    listed = client.get("/v1/knowledge/import-tasks?task_type=pdf")

    assert listed.status_code == 200
    body = listed.json()
    assert body["limit"] == 50
    assert body["offset"] == 0
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["task_type"] == "pdf"
    assert item["task_id"] == response.json()["task_id"]
    assert item["source_id"] == response.json()["task_id"]
    assert item["status"] == "queued"
    assert item["progress_completed"] == 0
    assert item["progress_total"] == 0
    assert item["retryable"] is False
    assert item["failure"] is None


def test_import_task_projection_exposes_failure_and_status_filter(
    tmp_path: Path,
) -> None:
    client, session_factory = _client(tmp_path)
    _login(client)
    job_id = new_id()
    with session_scope(session_factory) as session:
        session.execute(
            text(
                """
                INSERT INTO jobs (
                  id, job_type, idempotency_key, payload_json, status, attempts
                )
                VALUES (
                  :id, 'knowledge.index', :idempotency_key, :payload, 'failed', 1
                )
                """
            ),
            {
                "id": job_id,
                "idempotency_key": "projection-failed",
                "payload": json_text(
                    {
                        "knowledge_object_id": "missing-knowledge",
                        "failure_code": "parser_timeout",
                        "failure_stage": "parse",
                        "retryable": True,
                    }
                ),
            },
        )
        session.execute(
            text(
                """
                INSERT INTO job_attempts (
                  id, job_id, status, error_class, error_message
                )
                VALUES (
                  :id, :job_id, 'failed', 'ParserError', 'parser timed out'
                )
                """
            ),
            {"id": new_id(), "job_id": job_id},
        )

    listed = client.get("/v1/knowledge/import-tasks?status=failed")

    assert listed.status_code == 200
    items = listed.json()["items"]
    assert len(items) == 1
    assert items[0]["task_id"] == job_id
    assert items[0]["task_type"] == "knowledge"
    assert items[0]["status"] == "failed"
    assert items[0]["retryable"] is True
    assert items[0]["failure"]["code"] == "parser_timeout"
    assert items[0]["failure"]["stage"] == "parse"
