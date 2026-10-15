"""The library lists knowledge that is still processing or failed to index (issue #58).

Formal retrieval qualification is unchanged: unready knowledge stays out of
full-text search and answers, but it is visible, openable and deletable in the
library so the user can manage it.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from tests.integration.test_g005_api_impl import _client, _headers, _login
from tests.knowledge_helpers import stored_text_artifacts
from zhiheng.core.ids import json_text, new_id
from zhiheng.db.session import session_scope
from zhiheng.evaluation.search_fixtures import mark_formal_knowledge_indexed
from zhiheng.knowledge import KnowledgeRepository, KnowledgeUserAuthority, TextEvidenceInput

BODY = "未就绪资料不能进入正式检索，也不能进入回答上下文。"


def _ingest(session_factory: sessionmaker[Session], tmp_path: Path, title: str) -> str:
    with session_scope(session_factory) as session:
        user_id = str(session.execute(text("SELECT id FROM auth_users LIMIT 1")).scalar_one())
        item = KnowledgeRepository().ingest_text(
            session,
            TextEvidenceInput(
                title=title,
                primary_domain_id="technology.ai",
                text=BODY,
                source_metadata={"fixture": "issue58"},
            ),
            user_authority=KnowledgeUserAuthority(user_id),
            stored_artifacts=stored_text_artifacts(tmp_path / title, BODY),
        )
        return item.knowledge_object_id


def _ready(session_factory: sessionmaker[Session], tmp_path: Path) -> str:
    knowledge_id = _ingest(session_factory, tmp_path, "就绪资料")
    with session_scope(session_factory) as session:
        _consume_outbox(session, knowledge_id)
        mark_formal_knowledge_indexed(session, knowledge_id)
    return knowledge_id


def _failed(session_factory: sessionmaker[Session], tmp_path: Path) -> str:
    knowledge_id = _ingest(session_factory, tmp_path, "失败资料")
    job_id = new_id()
    with session_scope(session_factory) as session:
        _consume_outbox(session, knowledge_id)
        session.execute(
            text(
                """
                INSERT INTO jobs (id, job_type, idempotency_key, payload_json, status, attempts)
                VALUES (:id, 'knowledge.index', :key, :payload, 'dead', 3)
                """
            ),
            {
                "id": job_id,
                "key": f"issue58-failed:{knowledge_id}",
                "payload": json_text({"knowledge_object_id": knowledge_id}),
            },
        )
        session.execute(
            text(
                """
                INSERT INTO job_attempts (id, job_id, status, error_class, error_message)
                VALUES (:id, :job_id, 'failed', 'IndexError', 'embedding service unavailable')
                """
            ),
            {"id": new_id(), "job_id": job_id},
        )
    return knowledge_id


def _consume_outbox(session: Session, knowledge_id: str) -> None:
    session.execute(
        text("UPDATE outbox_events SET status = 'processed' WHERE aggregate_id = :id"),
        {"id": knowledge_id},
    )


def _library(client: TestClient, **params: str) -> dict:
    response = client.get("/v1/knowledge/search", params=params)
    assert response.status_code == 200
    return response.json()


def _by_id(body: dict) -> dict[str, dict]:
    return {item["knowledge_object_id"]: item for item in body["items"]}


def test_library_lists_processing_knowledge_as_processing(tmp_path: Path) -> None:
    client, session_factory = _client(tmp_path)
    _login(client)
    processing_id = _ingest(session_factory, tmp_path, "处理中资料")

    items = _by_id(_library(client))

    assert processing_id in items
    assert items[processing_id]["processing_status"] == "processing"
    assert items[processing_id]["searchable"] is False


def test_library_lists_failed_knowledge_with_readable_reason(tmp_path: Path) -> None:
    client, session_factory = _client(tmp_path)
    _login(client)
    failed_id = _failed(session_factory, tmp_path)

    item = _by_id(_library(client))[failed_id]

    assert item["processing_status"] == "failed"
    assert item["searchable"] is False
    assert item["failure_summary"] == "embedding service unavailable"


def test_library_status_filters_partition_items_and_counts_match(tmp_path: Path) -> None:
    client, session_factory = _client(tmp_path)
    _login(client)
    ready_id = _ready(session_factory, tmp_path)
    processing_id = _ingest(session_factory, tmp_path, "处理中资料")
    failed_id = _failed(session_factory, tmp_path)

    expected = {
        None: {ready_id, processing_id, failed_id},
        "active": {ready_id},
        "processing": {processing_id},
        "failed": {failed_id},
    }
    for status, ids in expected.items():
        body = _library(client, **({"status": status} if status else {}))
        assert set(_by_id(body)) == ids, status
        assert body["total"] == len(ids), status


def test_unready_knowledge_can_be_opened_deleted_restored_and_reprocessed(
    tmp_path: Path,
) -> None:
    client, session_factory = _client(tmp_path)
    csrf = _login(client)
    failed_id = _failed(session_factory, tmp_path)

    reader = client.get(f"/v1/knowledge/{failed_id}/reader")
    assert reader.status_code == 200
    assert reader.json()["searchable"] is False

    deleted = client.post(
        f"/v1/knowledge/{failed_id}/delete", headers=_headers(csrf, "issue58-delete")
    )
    assert deleted.status_code == 200
    assert failed_id not in _by_id(_library(client))
    trash = client.get("/v1/knowledge/trash").json()["items"]
    assert failed_id in {item["knowledge_object_id"] for item in trash}

    restored = client.post(
        f"/v1/knowledge/{failed_id}/restore", headers=_headers(csrf, "issue58-restore")
    )
    assert restored.status_code == 200
    assert failed_id in _by_id(_library(client))

    reprocessed = client.post(
        f"/v1/knowledge/{failed_id}/reindex", headers=_headers(csrf, "issue58-reindex")
    )
    assert reprocessed.status_code == 200
    assert _by_id(_library(client))[failed_id]["processing_status"] == "processing"


def test_search_and_answers_only_include_formally_qualified_knowledge(
    tmp_path: Path,
) -> None:
    client, session_factory = _client(tmp_path)
    csrf = _login(client)
    ready_id = _ready(session_factory, tmp_path)
    processing_id = _ingest(session_factory, tmp_path, "处理中资料")
    failed_id = _failed(session_factory, tmp_path)

    searched = _by_id(_library(client, q="正式检索"))
    assert set(searched) == {ready_id}

    answered = client.post(
        "/v1/answers",
        json={"query": "未就绪 资料 正式 检索"},
        headers=_headers(csrf, "issue58-answer"),
    )
    assert answered.status_code == 200
    assert ready_id in answered.text
    assert processing_id not in answered.text
    assert failed_id not in answered.text
