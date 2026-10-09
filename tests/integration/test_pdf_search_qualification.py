from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from tests.integration.test_g005_api_impl import _client, _login
from tests.knowledge_helpers import stored_text_artifacts
from zhiheng.core.ids import json_text, new_id, sha256_text
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


def _pdf_knowledge(
    session_factory: sessionmaker[Session],
    tmp_path: Path,
    *,
    task_state: str = "parsed",
    attempt_status: str = "succeeded",
) -> str:
    value = "PDF 解析资格必须绑定 parsed 任务、succeeded 尝试和正式分块。"
    with session_scope(session_factory) as session:
        user_id = str(session.execute(text("SELECT id FROM auth_users LIMIT 1")).scalar_one())
        item = KnowledgeRepository().ingest_text(
            session,
            TextEvidenceInput(
                title="PDF 资格测试资料",
                primary_domain_id="technology.ai",
                text=value,
                media_type="application/pdf",
                object_kind="document",
                source_metadata={"fixture": "issue53-pdf"},
            ),
            user_authority=KnowledgeUserAuthority(user_id),
            stored_artifacts=stored_text_artifacts(tmp_path, value),
        )
        _insert_pdf_lineage(
            session,
            evidence_id=item.evidence_object_id,
            content_version_id=item.content_version_id,
            task_state=task_state,
            attempt_status=attempt_status,
            block_text=value,
        )
        mark_formal_knowledge_indexed(session, item.knowledge_object_id)
        return item.knowledge_object_id


def _insert_pdf_lineage(
    session: Session,
    *,
    evidence_id: str,
    content_version_id: str,
    task_state: str,
    attempt_status: str,
    block_text: str,
) -> None:
    task_id = new_id()
    attempt_id = new_id()
    page_id = new_id()
    session.execute(
        text(
            """
            INSERT INTO pdf_tasks (
              id, evidence_object_id, backend, options_hash, idempotency_key, state
            )
            VALUES (
              :id, :evidence_id, 'deepdoc', :options_hash, :idempotency_key, :state
            )
            """
        ),
        {
            "id": task_id,
            "evidence_id": evidence_id,
            "options_hash": "a" * 64,
            "idempotency_key": f"issue53-pdf:{task_id}",
            "state": task_state,
        },
    )
    session.execute(
        text(
            """
            INSERT INTO pdf_parse_attempts (
              id, task_id, evidence_object_id, backend, attempt_no, status,
              manifest_uri, manifest_sha256
            )
            VALUES (
              :id, :task_id, :evidence_id, 'deepdoc', 1, :status,
              'artifact://pdf-attempts/manifest.json', :manifest_sha256
            )
            """
        ),
        {
            "id": attempt_id,
            "task_id": task_id,
            "evidence_id": evidence_id,
            "status": attempt_status,
            "manifest_sha256": "b" * 64,
        },
    )
    session.execute(
        text(
            """
            INSERT INTO pdf_pages (
              id, attempt_id, page_no, page_width, page_height, rotation,
              crop_box_json, render_uri, render_sha256, status
            )
            VALUES (
              :id, :attempt_id, 1, 612.0, 792.0, 0,
              :crop_box_json, NULL, NULL, 'parsed'
            )
            """
        ),
        {
            "id": page_id,
            "attempt_id": attempt_id,
            "crop_box_json": json_text([0, 0, 612, 792]),
        },
    )
    session.execute(
        text(
            """
            INSERT INTO evidence_blocks (
              id, attempt_id, page_id, content_version_id, block_key, region_type,
              reading_order, bbox_json, raw_bbox_json, transform_version, text,
              text_sha256, confidence, text_source, status
            )
            VALUES (
              :id, :attempt_id, :page_id, :content_version_id, 'block-1', 'paragraph',
              1, :bbox_json, :bbox_json, 'pdf-crop-rotate-v1', :text,
              :text_sha256, 0.99, 'text_layer', 'formal'
            )
            """
        ),
        {
            "id": new_id(),
            "attempt_id": attempt_id,
            "page_id": page_id,
            "content_version_id": content_version_id,
            "bbox_json": json_text([0, 0, 100, 20]),
            "text": block_text,
            "text_sha256": sha256_text(block_text),
        },
    )


def _chunk_id(session: Session, knowledge_id: str) -> str:
    return str(
        session.execute(
            text("SELECT id FROM serving_chunks WHERE source_id = :source_id"),
            {"source_id": knowledge_id},
        ).scalar_one()
    )


def _complete_retrieval_generation(
    session_factory: sessionmaker[Session],
    knowledge_id: str,
    *,
    model_revision: str = "issue53-complete",
) -> str:
    vector = VectorIndexRepository()
    with session_scope(session_factory) as session:
        generation_id = vector.create_generation(
            session,
            model_id="test-embedding",
            model_revision=model_revision,
            dimension=2,
        )
        embeddings_by_chunk = {_chunk_id(session, knowledge_id): [1.0, 0.0]}
        assert vector.rebuild_generation(session, generation_id, embeddings_by_chunk) == 1
        vector.activate_generation(session, generation_id)
        return generation_id


def _assert_not_searchable(client: TestClient, knowledge_id: str) -> None:
    assert client.get("/v1/knowledge/search", params={"q": "向量资格"}).json()["items"] == []
    detail = client.get(f"/v1/knowledge/{knowledge_id}")
    assert detail.status_code == 200
    assert detail.json()["searchable"] is False
    assert detail.json()["retrieval_generation"] is None

    reader = client.get(f"/v1/knowledge/{knowledge_id}/reader")
    assert reader.status_code == 200
    assert reader.json()["searchable"] is False

    processing = client.get(f"/v1/knowledge/{knowledge_id}/processing")
    assert processing.status_code == 200
    assert processing.json()["searchable"] is False


def test_search_and_detail_require_active_generation_and_matching_embeddings(
    tmp_path: Path,
) -> None:
    client, session_factory = _client(tmp_path)
    _login(client)
    knowledge_id = _knowledge(session_factory, tmp_path)

    # A completed lexical index alone is insufficient.
    _assert_not_searchable(client, knowledge_id)

    vector = VectorIndexRepository()
    with session_scope(session_factory) as session:
        generation_id = vector.create_generation(
            session,
            model_id="test-embedding",
            model_revision="issue53-empty",
            dimension=2,
        )
        vector.rebuild_generation(session, generation_id, {})
        vector.activate_generation(session, generation_id)

    # An active physical index with no embedding for the serving chunk is still not searchable.
    _assert_not_searchable(client, knowledge_id)

    generation_id = _complete_retrieval_generation(session_factory, knowledge_id)

    search = client.get("/v1/knowledge/search", params={"q": "向量资格"})
    assert search.status_code == 200
    assert (
        [item["knowledge_object_id"] for item in search.json()["items"]] == [knowledge_id]
    ), search.json()
    detail = client.get(f"/v1/knowledge/{knowledge_id}")
    assert detail.status_code == 200
    detail_payload = detail.json()
    assert detail_payload["searchable"] is True
    assert detail_payload["retrieval_generation"]["id"] == generation_id
    assert detail_payload["retrieval_generation"]["model_id"] == "test-embedding"
    assert detail_payload["retrieval_generation"]["model_revision"] == "issue53-complete"
    assert detail_payload["retrieval_generation"]["dimension"] == 2
    assert detail_payload["retrieval_generation"]["purpose"] == "retrieval"
    assert detail_payload["retrieval_generation"]["index_status"] == "active"
    assert detail_payload["retrieval_generation"]["built_count"] == 1
    assert detail_payload["retrieval_generation"]["physical_index_ref"].startswith(
        "sqlite_vec:vec_chunks_"
    )
    assert len(detail_payload["retrieval_generation"]["source_manifest_hash"]) == 64

    reader = client.get(f"/v1/knowledge/{knowledge_id}/reader")
    assert reader.status_code == 200
    assert reader.json()["searchable"] is True
    assert reader.json()["primary_domain_id"] == "technology.ai"
    assert reader.json()["source_metadata"]["fixture"] == "issue53"
    assert reader.json()["citations"]

    processing = client.get(f"/v1/knowledge/{knowledge_id}/processing")
    assert processing.status_code == 200
    assert processing.json()["public_status"] == "succeeded"
    assert processing.json()["job_status"] == "completed"
    assert processing.json()["searchable"] is True


def _archive_generation(session: Session, knowledge_id: str, generation_id: str) -> None:
    del knowledge_id
    session.execute(
        text("UPDATE embedding_generations SET index_status = 'archived' WHERE id = :id"),
        {"id": generation_id},
    )


def _change_generation_purpose(session: Session, knowledge_id: str, generation_id: str) -> None:
    del knowledge_id
    session.execute(
        text("UPDATE embedding_generations SET purpose = 'classification' WHERE id = :id"),
        {"id": generation_id},
    )


def _remove_physical_index_pointer(
    session: Session,
    knowledge_id: str,
    generation_id: str,
) -> None:
    del knowledge_id
    session.execute(
        text("UPDATE embedding_generations SET physical_index_ref = '' WHERE id = :id"),
        {"id": generation_id},
    )


def _stale_embedding_source_version(
    session: Session,
    knowledge_id: str,
    generation_id: str,
) -> None:
    del knowledge_id
    session.execute(
        text(
            """
            UPDATE chunk_embeddings
            SET source_version_id = 'stale-version'
            WHERE generation_id = :id
            """
        ),
        {"id": generation_id},
    )


def _remove_completed_job(session: Session, knowledge_id: str, generation_id: str) -> None:
    del generation_id
    session.execute(
        text(
            """
            UPDATE jobs
            SET status = 'processing'
            WHERE job_type = 'knowledge.index'
              AND status = 'completed'
              AND json_extract(payload_json, '$.knowledge_object_id') = :knowledge_id
            """
        ),
        {"knowledge_id": knowledge_id},
    )


def _deactivate_evidence(session: Session, knowledge_id: str, generation_id: str) -> None:
    del generation_id
    session.execute(
        text(
            """
            UPDATE evidence_objects
            SET status = 'archived'
            WHERE id = (
              SELECT cv.evidence_object_id
              FROM knowledge_objects ko
              JOIN knowledge_versions kv ON kv.id = ko.current_version_id
              JOIN content_versions cv ON cv.id = kv.content_version_id
              WHERE ko.id = :knowledge_id
            )
            """
        ),
        {"knowledge_id": knowledge_id},
    )


@pytest.mark.parametrize(
    "break_qualification",
    [
        _archive_generation,
        _change_generation_purpose,
        _remove_physical_index_pointer,
        _stale_embedding_source_version,
        _remove_completed_job,
        _deactivate_evidence,
    ],
)
def test_search_detail_reader_and_processing_reject_partial_vector_qualification(
    tmp_path: Path,
    break_qualification: Callable[[Session, str, str], None],
) -> None:
    client, session_factory = _client(tmp_path)
    _login(client)
    knowledge_id = _knowledge(session_factory, tmp_path)
    generation_id = _complete_retrieval_generation(session_factory, knowledge_id)

    with session_scope(session_factory) as session:
        break_qualification(session, knowledge_id, generation_id)

    _assert_not_searchable(client, knowledge_id)


def test_pdf_search_qualification_requires_parsed_task_and_succeeded_attempt(
    tmp_path: Path,
) -> None:
    client, session_factory = _client(tmp_path)
    _login(client)
    knowledge_id = _pdf_knowledge(session_factory, tmp_path)
    generation_id = _complete_retrieval_generation(
        session_factory,
        knowledge_id,
        model_revision="issue53-pdf-complete",
    )

    search = client.get("/v1/knowledge/search", params={"q": "PDF 解析资格"})
    assert search.status_code == 200
    assert [item["knowledge_object_id"] for item in search.json()["items"]] == [knowledge_id]
    detail = client.get(f"/v1/knowledge/{knowledge_id}")
    assert detail.status_code == 200
    assert detail.json()["searchable"] is True
    assert detail.json()["retrieval_generation"]["id"] == generation_id


@pytest.mark.parametrize(
    ("task_state", "attempt_status"),
    [
        ("succeeded", "succeeded"),
        ("parsed", "partial"),
    ],
)
def test_pdf_search_qualification_rejects_wrong_parser_terminal_states(
    tmp_path: Path,
    task_state: str,
    attempt_status: str,
) -> None:
    client, session_factory = _client(tmp_path)
    _login(client)
    knowledge_id = _pdf_knowledge(
        session_factory,
        tmp_path,
        task_state=task_state,
        attempt_status=attempt_status,
    )
    _complete_retrieval_generation(
        session_factory,
        knowledge_id,
        model_revision=f"issue53-pdf-{task_state}-{attempt_status}",
    )

    _assert_not_searchable(client, knowledge_id)
