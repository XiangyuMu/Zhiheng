from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from tests.integration.test_g005_api_impl import _client, _headers, _login
from tests.integration.test_import_task_projection import _pdf_bytes
from tests.integration.test_pdf_search_qualification import _pdf_knowledge
from tests.knowledge_helpers import announce_pdf_parser_worker
from zhiheng.core.ids import json_text, new_id
from zhiheng.db.session import session_scope

# A completed index attempt stores its result in job_attempts.error_message.
INDEX_RESULT = '{"fts_indexed": 25, "generation_id": "g-1", "vector_indexed": 25}'


def _import(client: TestClient, csrf: str, key: str) -> str:
    imported = client.post(
        "/v1/knowledge/imports",
        json={"title": "已完成的资料", "text": "这份资料已经建好索引。"},
        headers=_headers(csrf, key),
    )
    assert imported.status_code == 200, imported.text
    return str(imported.json()["result"]["knowledge_object_id"])


def _completed_index_job(
    session_factory: sessionmaker[Session], knowledge_id: str, key: str
) -> str:
    job_id = new_id()
    with session_scope(session_factory) as session:
        session.execute(
            text(
                """
                INSERT INTO jobs (id, job_type, idempotency_key, payload_json, status, attempts)
                VALUES (:id, 'knowledge.index', :key, :payload, 'completed', 1)
                """
            ),
            {"id": job_id, "key": key, "payload": json_text({"knowledge_object_id": knowledge_id})},
        )
        session.execute(
            text(
                """
                INSERT INTO job_attempts (id, job_id, status, error_class, error_message)
                VALUES (:id, :job_id, 'completed', NULL, :result)
                """
            ),
            {"id": new_id(), "job_id": job_id, "result": INDEX_RESULT},
        )
    return job_id


def _task(client: TestClient, task_id: str) -> dict[str, Any]:
    listed = client.get("/v1/knowledge/import-tasks")
    assert listed.status_code == 200, listed.text
    return next(item for item in listed.json()["items"] if item["task_id"] == task_id)


def test_a_completed_attempt_result_is_not_reported_as_a_failure(tmp_path: Path) -> None:
    client, session_factory = _client(tmp_path)
    csrf = _login(client)
    knowledge_id = _import(client, csrf, "completed-import")
    job_id = _completed_index_job(session_factory, knowledge_id, "completed-index")

    task = _task(client, job_id)

    assert task["failure"] is None
    assert "fts_indexed" not in str(task)


def test_tasks_of_deleted_knowledge_are_deleted_not_failed(tmp_path: Path) -> None:
    client, session_factory = _client(tmp_path)
    csrf = _login(client)
    knowledge_id = _import(client, csrf, "deleted-import")
    job_id = _completed_index_job(session_factory, knowledge_id, "deleted-index")
    deleted = client.post(
        f"/v1/knowledge/{knowledge_id}/delete", headers=_headers(csrf, "deleted-delete")
    )
    assert deleted.status_code == 200, deleted.text

    task = _task(client, job_id)

    assert task["status"] == "deleted"
    assert task["failure"] is None
    assert task["retryable"] is False


def test_restoring_the_knowledge_brings_its_task_status_back(tmp_path: Path) -> None:
    client, session_factory = _client(tmp_path)
    csrf = _login(client)
    knowledge_id = _import(client, csrf, "restore-import")
    job_id = _completed_index_job(session_factory, knowledge_id, "restore-index")
    client.post(f"/v1/knowledge/{knowledge_id}/delete", headers=_headers(csrf, "restore-del"))
    restored = client.post(
        f"/v1/knowledge/{knowledge_id}/restore", headers=_headers(csrf, "restore-restore")
    )
    assert restored.status_code == 200, restored.text

    assert _task(client, job_id)["status"] != "deleted"


def test_a_parsed_pdf_whose_knowledge_was_deleted_is_not_left_processing(
    tmp_path: Path,
) -> None:
    client, session_factory = _client(tmp_path)
    csrf = _login(client)
    knowledge_id = _pdf_knowledge(session_factory, tmp_path)
    with session_scope(session_factory) as session:
        task_id = str(
            session.execute(
                text(
                    """
                    SELECT t.id FROM pdf_tasks t
                    JOIN content_versions cv ON cv.evidence_object_id = t.evidence_object_id
                    JOIN knowledge_versions kv ON kv.content_version_id = cv.id
                    WHERE kv.knowledge_object_id = :id
                    """
                ),
                {"id": knowledge_id},
            ).scalar_one()
        )
        job_id = new_id()
        session.execute(
            text(
                """
                INSERT INTO jobs (id, job_type, idempotency_key, payload_json, status, attempts)
                VALUES (:id, 'knowledge.index', 'pdf-index', :payload, 'completed', 1)
                """
            ),
            {
                "id": job_id,
                "payload": json_text({"task_id": task_id, "knowledge_object_id": knowledge_id}),
            },
        )
        session.execute(
            text(
                """
                INSERT INTO job_attempts (id, job_id, status, error_message)
                VALUES (:id, :job_id, 'completed', :result)
                """
            ),
            {"id": new_id(), "job_id": job_id, "result": INDEX_RESULT},
        )
    deleted = client.post(f"/v1/knowledge/{knowledge_id}/delete", headers=_headers(csrf, "pdf-del"))
    assert deleted.status_code == 200, deleted.text

    task = _task(client, task_id)

    assert task["task_type"] == "pdf"
    assert task["status"] == "deleted"
    assert task["failure"] is None


def _pdf_upload(client: TestClient, csrf: str, key: str) -> str:
    response = client.post(
        "/v1/knowledge/pdf-imports",
        params={"title": "never imported", "primary_domain_id": "technology.ai"},
        content=_pdf_bytes(),
        headers={**_headers(csrf, key), "Content-Type": "application/pdf"},
    )
    assert response.status_code == 202, response.text
    return str(response.json()["task_id"])


def _set_pdf_state(session_factory: sessionmaker[Session], task_id: str, state: str) -> None:
    with session_scope(session_factory) as session:
        session.execute(
            text("UPDATE pdf_tasks SET state = :state WHERE id = :id"),
            {"state": state, "id": task_id},
        )


def test_a_failed_pdf_that_never_became_knowledge_can_be_dismissed(tmp_path: Path) -> None:
    client, session_factory = _client(tmp_path)
    csrf = _login(client)
    announce_pdf_parser_worker(session_factory)
    task_id = _pdf_upload(client, csrf, "dismiss-upload")
    _set_pdf_state(session_factory, task_id, "failed")

    dismissed = client.post(
        f"/v1/knowledge/pdf-imports/{task_id}/dismiss", headers=_headers(csrf, "dismiss")
    )

    assert dismissed.status_code == 200, dismissed.text
    assert _task(client, task_id)["status"] == "dismissed"
    assert _task(client, task_id)["retryable"] is False


def test_only_finished_failed_pdfs_without_knowledge_can_be_dismissed(tmp_path: Path) -> None:
    client, session_factory = _client(tmp_path)
    csrf = _login(client)
    announce_pdf_parser_worker(session_factory)
    queued = _pdf_upload(client, csrf, "queued-upload")
    knowledge_id = _pdf_knowledge(session_factory, tmp_path, task_state="failed")
    with session_scope(session_factory) as session:
        with_knowledge = str(
            session.execute(
                text(
                    """
                    SELECT t.id FROM pdf_tasks t
                    JOIN content_versions cv ON cv.evidence_object_id = t.evidence_object_id
                    JOIN knowledge_versions kv ON kv.content_version_id = cv.id
                    WHERE kv.knowledge_object_id = :id
                    """
                ),
                {"id": knowledge_id},
            ).scalar_one()
        )

    for task_id, key in ((queued, "dismiss-queued"), (with_knowledge, "dismiss-knowledge")):
        refused = client.post(
            f"/v1/knowledge/pdf-imports/{task_id}/dismiss", headers=_headers(csrf, key)
        )
        assert refused.status_code == 409, refused.text
    missing = client.post(
        "/v1/knowledge/pdf-imports/missing/dismiss", headers=_headers(csrf, "dismiss-missing")
    )
    assert missing.status_code == 404


def test_a_dismissed_pdf_stays_dismissed_even_when_its_parse_job_failed(
    tmp_path: Path,
) -> None:
    client, session_factory = _client(tmp_path)
    csrf = _login(client)
    announce_pdf_parser_worker(session_factory)
    task_id = _pdf_upload(client, csrf, "dismiss-failed-job")
    _set_pdf_state(session_factory, task_id, "failed")
    with session_scope(session_factory) as session:
        session.execute(
            text(
                """
                INSERT INTO jobs (id, job_type, idempotency_key, payload_json, status, attempts)
                VALUES (:id, 'knowledge.parse_pdf', 'failed-parse', :payload, 'failed', 1)
                """
            ),
            {
                "id": new_id(),
                "payload": json_text({"task_id": task_id, "failure_code": "parser_timeout"}),
            },
        )
    client.post(f"/v1/knowledge/pdf-imports/{task_id}/dismiss", headers=_headers(csrf, "d"))

    task = _task(client, task_id)

    assert task["status"] == "dismissed"
    assert task["failure"] is None
    assert task["retryable"] is False
