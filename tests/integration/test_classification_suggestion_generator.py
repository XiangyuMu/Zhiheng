from __future__ import annotations

from pathlib import Path

from sqlalchemy import text

from tests.integration.test_topic_taxonomy import _client, _headers, _login
from zhiheng.classification.suggestions import (
    ClassificationSuggestionService,
    enqueue_classification_suggestion,
)
from zhiheng.db.session import session_scope
from zhiheng.jobs.classification import (
    ClassificationSuggestionJobExecutor,
    process_classification_jobs_once,
)
from zhiheng.jobs.outbox import OutboxRepository


def test_generator_suggests_a_matching_node_for_imported_knowledge(tmp_path: Path) -> None:
    client = _client(tmp_path)
    csrf = _login(client)
    node = client.post(
        "/v1/classifications",
        json={"domain_id": "computing_engineering", "name": "哈希表"},
        headers=_headers(csrf, "node"),
    )
    assert node.status_code == 201, node.text
    imported = client.post(
        "/v1/knowledge/imports",
        json={"title": "哈希表 入门", "text": "哈希表 是一种查找结构。"},
        headers=_headers(csrf, "import"),
    )
    assert imported.status_code == 200, imported.text
    knowledge_id = str(imported.json()["result"]["knowledge_object_id"])
    owner = client.get("/me").json()["user_id"]
    factory = client.app.state.session_factory  # type: ignore[attr-defined]

    with session_scope(factory) as session:
        generated = ClassificationSuggestionService().generate(
            session, knowledge_object_id=knowledge_id, owner_user_id=owner
        )

    assert [s.candidate_node_id for s in generated] == [node.json()["id"]]
    with session_scope(factory) as session:
        stored = (
            session.execute(
                text(
                    "SELECT status FROM classification_suggestions WHERE knowledge_object_id = :id"
                ),
                {"id": knowledge_id},
            )
            .scalars()
            .all()
        )
    assert stored == ["pending"]


def test_queued_suggestion_request_is_turned_into_suggestions_by_the_worker_job(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
    csrf = _login(client)
    node = client.post(
        "/v1/classifications",
        json={"domain_id": "computing_engineering", "name": "哈希表"},
        headers=_headers(csrf, "node"),
    )
    assert node.status_code == 201, node.text
    imported = client.post(
        "/v1/knowledge/imports",
        json={"title": "哈希表 入门", "text": "哈希表 是一种查找结构。"},
        headers=_headers(csrf, "import"),
    )
    knowledge_id = str(imported.json()["result"]["knowledge_object_id"])
    owner = client.get("/me").json()["user_id"]
    factory = client.app.state.session_factory  # type: ignore[attr-defined]
    with session_scope(factory) as session:
        enqueue_classification_suggestion(
            session, knowledge_object_id=knowledge_id, owner_user_id=owner, idempotency_key="k"
        )
        outbox = OutboxRepository()
        outbox.enqueue_jobs_for_events(session, outbox.claim_pending(session))

    completed = process_classification_jobs_once(
        factory, ClassificationSuggestionJobExecutor(), worker_id="test"
    )

    assert completed == 1
    with session_scope(factory) as session:
        stored = (
            session.execute(
                text(
                    "SELECT status FROM classification_suggestions WHERE knowledge_object_id = :id"
                ),
                {"id": knowledge_id},
            )
            .scalars()
            .all()
        )
    assert stored == ["pending"]
