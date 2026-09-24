from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from tests.knowledge_helpers import stored_text_artifacts
from zhiheng.core.config import Settings
from zhiheng.db.session import create_session_factory, create_sqlite_engine, session_scope
from zhiheng.evaluation.search_fixtures import mark_formal_knowledge_indexed
from zhiheng.knowledge import KnowledgeRepository, KnowledgeUserAuthority, TextEvidenceInput
from zhiheng.knowledge.object_store import StoredTextArtifacts
from zhiheng.privacy.erase import PrivacyEraseService
from zhiheng.retrieval import VectorIndexRepository
from zhiheng.worker.main import process_outbox_once


def _migrated_session_factory(tmp_path: Path) -> tuple[Settings, sessionmaker[Session]]:
    db_path = tmp_path / "zhiheng.db"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")

    settings = Settings(environment="test", database_url=f"sqlite:///{db_path}")
    engine = create_sqlite_engine(settings)
    return settings, create_session_factory(engine)


DEMO_KNOWLEDGE_TEXT = "中文全文检索必须回查正式视图后，才能进入回答上下文。"


def _ingest_demo_knowledge(
    session: Session,
    artifacts: StoredTextArtifacts,
    title: str = "中文检索",
) -> str:
    repository = KnowledgeRepository()
    ingested = repository.ingest_text(
        session,
        TextEvidenceInput(
            title=title,
            primary_domain_id="technology.ai",
            text=DEMO_KNOWLEDGE_TEXT,
            source_metadata={"fixture": "synthetic"},
            summary="FTS must pass formal authorization.",
        ),
        user_authority=KnowledgeUserAuthority("synthetic-test-user"),
        stored_artifacts=artifacts,
    )
    return ingested.knowledge_object_id


def test_ingest_text_creates_authoritative_rows_fts_and_outbox(tmp_path: Path) -> None:
    _, session_factory = _migrated_session_factory(tmp_path)
    repository = KnowledgeRepository()
    text_value = "中文全文检索使用分词字段，同时原始文本用于展示和引用。"
    artifacts = stored_text_artifacts(tmp_path, text_value)

    with session_scope(session_factory) as session:
        ingested = repository.ingest_text(
            session,
            TextEvidenceInput(
                title="中文全文检索",
                primary_domain_id="technology.ai",
                text=text_value,
                source_metadata={"fixture": "synthetic"},
            ),
            user_authority=KnowledgeUserAuthority("synthetic-test-user"),
            stored_artifacts=artifacts,
        )
        mark_formal_knowledge_indexed(session, ingested.knowledge_object_id)
        hits = repository.search_formal_fts(session, "中文 检索")
        outbox_count = session.execute(
            text("SELECT count(*) FROM outbox_events WHERE status = 'pending'")
        ).scalar_one()
        current_count = session.execute(
            text("SELECT count(*) FROM current_formal_knowledge")
        ).scalar_one()

    assert hits[0].source_id == ingested.knowledge_object_id
    assert outbox_count == 1
    assert current_count == 1


def test_soft_delete_removes_knowledge_from_serving_search(tmp_path: Path) -> None:
    _, session_factory = _migrated_session_factory(tmp_path)
    repository = KnowledgeRepository()
    artifacts = stored_text_artifacts(tmp_path, DEMO_KNOWLEDGE_TEXT)

    with session_scope(session_factory) as session:
        knowledge_object_id = _ingest_demo_knowledge(session, artifacts)
        mark_formal_knowledge_indexed(session, knowledge_object_id)
        assert repository.search_formal_fts(session, "中文 检索")
        repository.soft_delete_knowledge(session, knowledge_object_id)
        hits_after_delete = repository.search_formal_fts(session, "中文 检索")
        serving_count = session.execute(text("SELECT count(*) FROM serving_chunks")).scalar_one()

    assert hits_after_delete == []
    assert serving_count == 0


def test_outbox_worker_turns_pending_events_into_idempotent_jobs(tmp_path: Path) -> None:
    settings, session_factory = _migrated_session_factory(tmp_path)
    artifacts = stored_text_artifacts(tmp_path, DEMO_KNOWLEDGE_TEXT)

    with session_scope(session_factory) as session:
        _ingest_demo_knowledge(session, artifacts)

    assert process_outbox_once(settings) == 1
    assert process_outbox_once(settings) == 0

    with session_scope(session_factory) as session:
        event_statuses = session.execute(text("SELECT status FROM outbox_events")).scalars().all()
        job_count = session.execute(text("SELECT count(*) FROM jobs")).scalar_one()

    assert event_statuses == ["processed"]
    assert job_count == 1


def test_vector_generation_rebuilds_from_serving_chunks_only(tmp_path: Path) -> None:
    _, session_factory = _migrated_session_factory(tmp_path)
    knowledge_repository = KnowledgeRepository()
    vector_repository = VectorIndexRepository()
    kept_artifacts = stored_text_artifacts(tmp_path, DEMO_KNOWLEDGE_TEXT)
    deleted_artifacts = stored_text_artifacts(tmp_path, DEMO_KNOWLEDGE_TEXT)

    with session_scope(session_factory) as session:
        kept_id = _ingest_demo_knowledge(session, kept_artifacts, "保留知识")
        deleted_id = _ingest_demo_knowledge(session, deleted_artifacts, "删除知识")
        knowledge_repository.soft_delete_knowledge(session, deleted_id)
        chunk_ids = (
            session.execute(text("SELECT id, source_id FROM chunks ORDER BY source_id"))
            .mappings()
            .all()
        )
        generation_id = vector_repository.create_generation(
            session,
            model_id="BAAI/bge-m3",
            model_revision="synthetic-revision",
            dimension=3,
        )
        inserted = vector_repository.rebuild_generation(
            session,
            generation_id,
            {str(row["id"]): [1.0, 0.0, 0.0] for row in chunk_ids},
        )
        vector_repository.activate_generation(session, generation_id)
        indexed_sources = (
            session.execute(
                text(
                    """
                SELECT c.source_id
                FROM chunk_embeddings ce
                JOIN chunks c ON c.id = ce.chunk_id
                """
                )
            )
            .scalars()
            .all()
        )

    assert inserted == 1
    assert indexed_sources == [kept_id]
    assert deleted_id not in indexed_sources


def test_vector_generation_rejects_dimension_mismatch(tmp_path: Path) -> None:
    _, session_factory = _migrated_session_factory(tmp_path)
    vector_repository = VectorIndexRepository()
    artifacts = stored_text_artifacts(tmp_path, DEMO_KNOWLEDGE_TEXT)

    with session_scope(session_factory) as session:
        _ingest_demo_knowledge(session, artifacts)
        chunk_id = str(session.execute(text("SELECT id FROM serving_chunks")).scalar_one())
        generation_id = vector_repository.create_generation(
            session,
            model_id="BAAI/bge-m3",
            model_revision="synthetic-revision",
            dimension=3,
        )
        with pytest.raises(ValueError, match="dimension"):
            vector_repository.rebuild_generation(session, generation_id, {chunk_id: [1.0, 0.0]})


def test_privacy_erase_requires_write_ahead_ledger_and_removes_serving_rows(
    tmp_path: Path,
) -> None:
    _, session_factory = _migrated_session_factory(tmp_path)
    knowledge_repository = KnowledgeRepository()
    erase_service = PrivacyEraseService()
    artifacts = stored_text_artifacts(tmp_path, DEMO_KNOWLEDGE_TEXT)

    with session_scope(session_factory) as session:
        knowledge_object_id = _ingest_demo_knowledge(session, artifacts)
        mark_formal_knowledge_indexed(session, knowledge_object_id)
        with pytest.raises(ValueError, match="write-ahead intent"):
            erase_service.execute_knowledge_erase(
                session,
                request_id="missing-request",
                knowledge_object_id=knowledge_object_id,
            )

    with session_scope(session_factory) as session:
        intent = erase_service.request_erase(
            session,
            target_type="knowledge_object",
            target_id=knowledge_object_id,
            requester="user",
            reason="synthetic privacy erase test",
        )
        ledger_status = session.execute(
            text(
                """
                SELECT status
                FROM privacy_erase_ledger
                WHERE id = :ledger_id
                """
            ),
            {"ledger_id": intent.ledger_id},
        ).scalar_one()
        evidence_status_before = session.execute(
            text("SELECT status FROM evidence_objects")
        ).scalar_one()

    assert ledger_status == "pending"
    assert evidence_status_before == "active"

    with session_scope(session_factory) as session:
        erase_service.execute_knowledge_erase(
            session,
            request_id=intent.request_id,
            knowledge_object_id=knowledge_object_id,
        )
        assert knowledge_repository.search_formal_fts(session, "中文 检索") == []
        statuses = session.execute(
            text(
                """
                SELECT ko.lifecycle_status, eo.status
                FROM knowledge_objects ko
                JOIN knowledge_versions kv ON kv.knowledge_object_id = ko.id
                JOIN content_versions cv ON cv.id = kv.content_version_id
                JOIN evidence_objects eo ON eo.id = cv.evidence_object_id
                """
            )
        ).one()
        ledger_phases = (
            session.execute(
                text(
                    """
                SELECT phase
                FROM privacy_erase_ledger
                WHERE erase_request_id = :request_id
                ORDER BY completed_at NULLS FIRST, phase
                """
                ),
                {"request_id": intent.request_id},
            )
            .scalars()
            .all()
        )

    assert statuses == ("privacy_erased", "privacy_erased")
    assert ledger_phases == ["authoritative_rows_erased", "intent", "physical_objects_erased"]
