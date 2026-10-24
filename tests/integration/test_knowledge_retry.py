from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from tests.integration.test_g005_api_impl import _client, _headers, _login
from zhiheng.core.ids import json_text, new_id
from zhiheng.db.session import session_scope


def _import(client: TestClient, csrf: str) -> str:
    imported = client.post(
        "/v1/knowledge/imports",
        json={"title": "粘贴资料", "text": "这份资料建索引失败过。"},
        headers=_headers(csrf, "retry-import"),
    )
    assert imported.status_code == 200, imported.text
    return str(imported.json()["result"]["knowledge_object_id"])


def _dead_index_job(session_factory: sessionmaker[Session], knowledge_id: str, id_key: str) -> str:
    job_id = new_id()
    with session_scope(session_factory) as session:
        # Clear the import's own index work so the dead job is the item's latest job.
        session.execute(text("DELETE FROM jobs WHERE job_type = 'knowledge.index'"))
        session.execute(text("UPDATE outbox_events SET status = 'processed'"))
        session.execute(
            text(
                """
                INSERT INTO jobs (id, job_type, idempotency_key, payload_json, status, attempts)
                VALUES (:id, 'knowledge.index', :key, :payload, 'dead', 3)
                """
            ),
            {
                "id": job_id,
                "key": f"dead-{id_key}",
                "payload": json_text({id_key: knowledge_id, "aggregate_type": "knowledge_object"}),
            },
        )
        session.execute(
            text(
                """
                INSERT INTO job_attempts (id, job_id, status, error_class, error_message)
                VALUES (:id, :job_id, 'failed', 'QueryEmbeddingUnavailableError', 'no model')
                """
            ),
            {"id": new_id(), "job_id": job_id},
        )
    return job_id


# Index jobs created before knowledge_object_id was added carry only aggregate_id.
@pytest.mark.parametrize("id_key", ["aggregate_id", "knowledge_object_id"])
def test_a_dead_index_job_can_be_retried_whichever_key_holds_the_item(
    tmp_path: Path, id_key: str
) -> None:
    client, session_factory = _client(tmp_path)
    csrf = _login(client)
    knowledge_id = _import(client, csrf)
    dead_job = _dead_index_job(session_factory, knowledge_id, id_key)
    processing = client.get(f"/v1/knowledge/{knowledge_id}/processing")
    assert processing.status_code == 200, processing.text

    retried = client.post(
        f"/v1/knowledge/{knowledge_id}/retry",
        headers={**_headers(csrf, f"retry-{id_key}"), "If-Match": processing.json()["etag"]},
    )

    assert retried.status_code == 200, retried.text
    result = retried.json()["result"]
    assert result["previous_job_id"] == dead_job
    assert result["job_status"] == "pending"
