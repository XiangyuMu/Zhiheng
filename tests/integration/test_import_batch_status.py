from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from tests.integration.test_g005_api_impl import _client, _headers, _login
from tests.integration.test_pdf_async_upload import _pdf_bytes
from zhiheng.core.ids import new_id
from zhiheng.db.session import session_scope
from zhiheng.jobs import import_tasks

_KNOWLEDGE_OBJECT_FIELDS = {
    "kind": "imported_document",
    "lifecycle": "formal_current",
    "scope": "formal",
    "level": "private",
}


def _batch_item(client: TestClient, csrf: str, item: dict[str, str], key: str) -> dict[str, Any]:
    created = client.post(
        "/v1/knowledge/import-batches",
        json={"items": [item]},
        headers=_headers(csrf, key),
    )
    assert created.status_code == 202
    detail = client.get(f"/v1/knowledge/import-batches/{created.json()['batch_id']}")
    assert detail.status_code == 200
    return dict(detail.json()["items"][0])


def _index_job(
    session_factory: sessionmaker[Session], status: str, payload: dict[str, str]
) -> None:
    with session_scope(session_factory) as session:
        session.execute(
            text(
                "INSERT INTO jobs (id, job_type, idempotency_key, payload_json, status) "
                "VALUES (:id, 'knowledge.index', :key, :payload, :status)"
            ),
            {
                "id": new_id(),
                "key": new_id(),
                "payload": json.dumps(payload),
                "status": status,
            },
        )


def _parsed_pdf_task(client: TestClient, csrf: str, key: str) -> tuple[str, str]:
    response = client.post(
        "/v1/knowledge/pdf-imports",
        params={"title": "批次 PDF", "primary_domain_id": "technology.ai"},
        content=_pdf_bytes(),
        headers={**_headers(csrf, key), "Content-Type": "application/pdf"},
    )
    assert response.status_code == 202
    task_id = str(response.json()["task_id"])
    return task_id, new_id()


def _mark_parsed(session_factory: sessionmaker[Session], task_id: str) -> None:
    with session_scope(session_factory) as session:
        session.execute(text("UPDATE pdf_tasks SET state='parsed' WHERE id=:id"), {"id": task_id})


def test_text_item_succeeds_when_its_aggregate_index_job_completed(tmp_path: Path) -> None:
    client, session_factory = _client(tmp_path)
    csrf = _login(client)
    knowledge_object_id = new_id()
    # Text imports index through an aggregate-keyed job, not knowledge_object_id.
    _index_job(
        session_factory,
        "completed",
        {"aggregate_id": knowledge_object_id, "aggregate_type": "knowledge_object"},
    )

    item = _batch_item(
        client,
        csrf,
        {"source_id": knowledge_object_id, "source_type": "text", "title": "文本"},
        "batch-text",
    )

    assert item["status"] == "succeeded"


def test_parsed_pdf_item_succeeds_once_its_index_is_searchable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, session_factory = _client(tmp_path)
    csrf = _login(client)
    task_id, knowledge_object_id = _parsed_pdf_task(client, csrf, "batch-pdf-searchable")
    _mark_parsed(session_factory, task_id)
    with session_scope(session_factory) as session:
        session.execute(
            text(
                "INSERT INTO knowledge_objects (id, primary_domain_id, title, object_kind, "
                "lifecycle_status, visibility_scope, sensitivity_level) "
                "VALUES (:id, 'technology.ai', '批次 PDF', :kind, :lifecycle, :scope, :level)"
            ),
            {"id": knowledge_object_id, **_KNOWLEDGE_OBJECT_FIELDS},
        )
    _index_job(
        session_factory,
        "completed",
        {"task_id": task_id, "knowledge_object_id": knowledge_object_id},
    )
    # Formal qualification needs a fully published index; this test fixes only
    # the batch projection, so qualification is pinned to "searchable".
    monkeypatch.setattr(import_tasks, "formal_searchable_sql", lambda _alias="ko": "1=1")

    item = _batch_item(
        client,
        csrf,
        {"source_id": task_id, "task_id": task_id, "source_type": "pdf", "title": "PDF"},
        "batch-pdf-searchable-item",
    )

    assert item["status"] == "succeeded"


def test_parsed_pdf_item_fails_when_its_index_job_failed(tmp_path: Path) -> None:
    client, session_factory = _client(tmp_path)
    csrf = _login(client)
    task_id, knowledge_object_id = _parsed_pdf_task(client, csrf, "batch-pdf-index-failed")
    _mark_parsed(session_factory, task_id)
    _index_job(
        session_factory,
        "dead",
        {"task_id": task_id, "knowledge_object_id": knowledge_object_id},
    )

    item = _batch_item(
        client,
        csrf,
        {"source_id": task_id, "task_id": task_id, "source_type": "pdf", "title": "PDF"},
        "batch-pdf-index-failed-item",
    )

    assert item["status"] == "dead_letter"
    assert item["stage"] == "index"
