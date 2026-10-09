from __future__ import annotations

from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from tests.integration.test_g005_api_impl import _client, _login
from tests.knowledge_helpers import stored_text_artifacts
from zhiheng.db.session import session_scope
from zhiheng.evaluation.search_fixtures import mark_formal_knowledge_indexed
from zhiheng.knowledge import KnowledgeRepository, KnowledgeUserAuthority, TextEvidenceInput
from zhiheng.retrieval.vector_index import VectorIndexRepository


def _knowledge(session_factory: sessionmaker[Session], tmp_path: Path) -> str:
    value = "向量资格必须同时具备原文、索引指针和每个分块的嵌入。"
    with session_scope(session_factory) as session:
        user_id = str(session.execute(text("SELECT id FROM auth_users LIMIT 1")).scalar_one())
        item = KnowledgeRepository().ingest_text(
            session,
            TextEvidenceInput(
                title="资格测试资料",
                primary_domain_id="technology.ai",
                text=value,
                source_metadata={"fixture": "issue53"},
            ),
            user_authority=KnowledgeUserAuthority(user_id),
            stored_artifacts=stored_text_artifacts(tmp_path, value),
        )
        mark_formal_knowledge_indexed(session, item.knowledge_object_id)
        return item.knowledge_object_id


def test_search_and_detail_require_active_generation_and_matching_embeddings(
    tmp_path: Path,
) -> None:
    client, session_factory = _client(tmp_path)
    _login(client)
    knowledge_id = _knowledge(session_factory, tmp_path)

    # A completed lexical index alone is insufficient.
    assert client.get("/v1/knowledge/search", params={"q": "向量资格"}).json()["items"] == []
    assert client.get(f"/v1/knowledge/{knowledge_id}").json()["searchable"] is False

    vector = VectorIndexRepository()
    with session_scope(session_factory) as session:
        generation_id = vector.create_generation(
            session,
            model_id="test-embedding",
            model_revision="issue53",
            dimension=2,
        )
        vector.rebuild_generation(session, generation_id, {})
        vector.activate_generation(session, generation_id)

    # An active physical index with no embedding for the serving chunk is still not searchable.
    assert client.get("/v1/knowledge/search", params={"q": "向量资格"}).json()["items"] == []
    assert client.get(f"/v1/knowledge/{knowledge_id}").json()["searchable"] is False

    with session_scope(session_factory) as session:
        generation_id = vector.create_generation(
            session,
            model_id="test-embedding",
            model_revision="issue53-complete",
            dimension=2,
        )
        chunk_id = str(
            session.execute(
                text("SELECT id FROM serving_chunks WHERE source_id = :source_id"),
                {"source_id": knowledge_id},
            ).scalar_one()
        )
        assert vector.rebuild_generation(session, generation_id, {chunk_id: [1.0, 0.0]}) == 1
        vector.activate_generation(session, generation_id)

    search = client.get("/v1/knowledge/search", params={"q": "向量资格"})
    assert search.status_code == 200
    assert (
        [item["knowledge_object_id"] for item in search.json()["items"]] == [knowledge_id]
    ), search.json()
    detail = client.get(f"/v1/knowledge/{knowledge_id}")
    assert detail.status_code == 200
    assert detail.json()["searchable"] is True
