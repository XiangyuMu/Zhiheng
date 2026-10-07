from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from tests.knowledge_helpers import stored_text_artifacts
from zhiheng.core.config import Settings
from zhiheng.core.ids import json_text, new_id
from zhiheng.db.session import create_session_factory, create_sqlite_engine, session_scope
from zhiheng.jobs.knowledge_indexing import (
    KnowledgeIndexJobExecutor,
    process_knowledge_jobs_once,
)
from zhiheng.knowledge import KnowledgeRepository, KnowledgeUserAuthority, TextEvidenceInput
from zhiheng.knowledge.object_store import StoredTextArtifacts
from zhiheng.worker.main import process_outbox_once, run_once


class _FakeEmbedder:
    def __init__(self, on_call: Callable[[], None] | None = None) -> None:
        self._on_call = on_call
        self.calls: list[str] = []

    def embed_text(
        self,
        text_value: str,
        *,
        model_id: str,
        model_revision: str,
        dimension: int,
        normalize: bool,
    ) -> Sequence[float]:
        assert model_id == "BAAI/bge-m3"
        assert model_revision == "synthetic-worker-revision"
        assert dimension == 3
        assert normalize is True
        self.calls.append(text_value)
        if self._on_call is not None:
            self._on_call()
        return [1.0, 0.0, 0.0]


def _migrated(tmp_path: Path) -> tuple[Settings, sessionmaker[Session]]:
    db_path = tmp_path / "zhiheng.db"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    settings = Settings(
        environment="test",
        database_url=f"sqlite:///{db_path}",
        embedding_model_revision="synthetic-worker-revision",
        embedding_dimension=3,
    )
    engine = create_sqlite_engine(settings)
    return settings, create_session_factory(engine)


def _ingest(
    session: Session,
    artifacts: StoredTextArtifacts,
    text_value: str = "知识索引 worker 应生成正式检索索引。",
) -> str:
    ingested = KnowledgeRepository().ingest_text(
        session,
        TextEvidenceInput(
            title="知识索引",
            primary_domain_id="technology.ai",
            text=text_value,
            source_metadata={"fixture": "synthetic"},
        ),
        user_authority=KnowledgeUserAuthority(user_id="test-user"),
        stored_artifacts=artifacts,
    )
    return ingested.knowledge_object_id


def test_outbox_enqueues_supported_knowledge_event_without_using_evolution_claim(
    tmp_path: Path,
) -> None:
    settings, session_factory = _migrated(tmp_path)
    text_value = "知识索引 worker 应生成正式检索索引。"
    artifacts = stored_text_artifacts(tmp_path, text_value)

    with session_scope(session_factory) as session:
        _ingest(session, artifacts, text_value)

    assert process_outbox_once(settings) == 1

    with session_scope(session_factory) as session:
        rows = [
            tuple(row)
            for row in session.execute(
                text("SELECT job_type, status FROM jobs ORDER BY created_at, id")
            ).all()
        ]
        outbox_status = session.execute(text("SELECT status FROM outbox_events")).scalar_one()

    assert rows == [("knowledge.index", "pending")]
    assert outbox_status == "processed"


def test_knowledge_index_job_rebuilds_fts_and_active_vector_generation(
    tmp_path: Path,
) -> None:
    settings, session_factory = _migrated(tmp_path)
    fake_embedder = _FakeEmbedder()
    text_value = "知识索引 worker 应生成正式检索索引。"
    artifacts = stored_text_artifacts(tmp_path, text_value)

    with session_scope(session_factory) as session:
        _ingest(session, artifacts, text_value)

    assert process_outbox_once(settings) == 1
    completed = process_knowledge_jobs_once(
        session_factory,
        KnowledgeIndexJobExecutor(settings, embedder_factory=lambda: fake_embedder),
        worker_id="knowledge-worker",
    )

    with session_scope(session_factory) as session:
        job_status = session.execute(text("SELECT status FROM jobs")).scalar_one()
        active_generation = (
            session.execute(
                text(
                    """
                SELECT id, built_count, index_status
                FROM embedding_generations
                WHERE index_status = 'active'
                """
                )
            )
            .mappings()
            .one()
        )
        embedding_count = session.execute(
            text("SELECT count(*) FROM chunk_embeddings")
        ).scalar_one()
        hits = KnowledgeRepository().search_formal_fts(session, "知识 索引")

    assert completed == 1
    assert fake_embedder.calls == ["知识索引 worker 应生成正式检索索引。"]
    assert job_status == "completed"
    assert active_generation["built_count"] == 1
    assert embedding_count == 1
    assert hits


def test_index_job_marks_unsupported_without_confirmed_embedding_route(tmp_path: Path) -> None:
    settings, session_factory = _migrated(tmp_path)
    production = settings.model_copy(update={"environment": "production"})
    text_value = "全文检索在缺少向量模型时仍应可用。"
    artifacts = stored_text_artifacts(tmp_path, text_value)

    with session_scope(session_factory) as session:
        _ingest(session, artifacts, text_value)

    assert process_outbox_once(production) == 1
    completed = process_knowledge_jobs_once(
        session_factory,
        KnowledgeIndexJobExecutor(production),
        worker_id="knowledge-worker",
    )

    with session_scope(session_factory) as session:
        row = session.execute(
            text("SELECT status, payload_json FROM jobs")
        ).mappings().one()
        failure = session.execute(
            text("SELECT error_message FROM job_attempts ORDER BY started_at DESC LIMIT 1")
        ).scalar_one()
        serving_chunks = session.execute(
            text("SELECT count(*) FROM serving_chunks")
        ).scalar_one()
        vectors = session.execute(
            text("SELECT count(*) FROM embedding_generations WHERE index_status='active'")
        ).scalar_one()

    payload = json.loads(str(row["payload_json"]))
    assert completed == 1
    assert row["status"] == "unsupported"
    assert payload["failure_code"] == "embedding_model_unavailable"
    assert "Embedding" in str(failure)
    assert serving_chunks == 1
    assert vectors == 0


def test_unknown_outbox_event_fails_explicitly_without_completed_job(tmp_path: Path) -> None:
    settings, session_factory = _migrated(tmp_path)

    with session_scope(session_factory) as session:
        session.execute(
            text(
                """
                INSERT INTO outbox_events (
                  id, event_type, aggregate_type, aggregate_id, payload_json, status
                )
                VALUES (
                  :id, 'unknown.event', 'unknown', :aggregate_id, :payload_json, 'pending'
                )
                """
            ),
            {
                "id": new_id(),
                "aggregate_id": new_id(),
                "payload_json": json_text({"fixture": "synthetic"}),
            },
        )

    assert process_outbox_once(settings) == 0

    with session_scope(session_factory) as session:
        outbox_status = session.execute(text("SELECT status FROM outbox_events")).scalar_one()
        job_count = session.execute(text("SELECT count(*) FROM jobs")).scalar_one()

    assert outbox_status == "failed"
    assert job_count == 0


def test_ui_audit_outbox_event_is_processed_without_background_job(tmp_path: Path) -> None:
    settings, session_factory = _migrated(tmp_path)

    with session_scope(session_factory) as session:
        session.execute(
            text(
                """
                INSERT INTO outbox_events (
                  id, event_type, aggregate_type, aggregate_id, payload_json, status
                )
                VALUES (
                  :id, 'evolution.proposal.user_decision', 'evolution_proposal',
                  :aggregate_id, :payload_json, 'pending'
                )
                """
            ),
            {
                "id": new_id(),
                "aggregate_id": new_id(),
                "payload_json": json_text({"fixture": "synthetic"}),
            },
        )

    assert process_outbox_once(settings) == 0

    with session_scope(session_factory) as session:
        outbox_status = session.execute(text("SELECT status FROM outbox_events")).scalar_one()
        job_count = session.execute(text("SELECT count(*) FROM jobs")).scalar_one()

    assert outbox_status == "processed"
    assert job_count == 0


def test_publisher_run_once_executes_knowledge_jobs_without_publisher_capability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, session_factory = _migrated(tmp_path)
    fake_embedder = _FakeEmbedder()
    text_value = "publisher 部署入口也必须执行知识索引。"
    artifacts = stored_text_artifacts(tmp_path, text_value)

    with session_scope(session_factory) as session:
        _ingest(session, artifacts, text_value)

    monkeypatch.setenv("ZHIHENG_WORKER_ROLE", "publisher")
    completed = run_once(
        settings,
        knowledge_executor_factory=lambda _: KnowledgeIndexJobExecutor(
            settings,
            embedder_factory=lambda: fake_embedder,
        ),
    )

    with session_scope(session_factory) as session:
        job_status = session.execute(text("SELECT status FROM jobs")).scalar_one()
        active_count = session.execute(
            text("SELECT count(*) FROM embedding_generations WHERE index_status = 'active'")
        ).scalar_one()
        embedding_count = session.execute(
            text("SELECT count(*) FROM chunk_embeddings")
        ).scalar_one()
        outbox_status = session.execute(text("SELECT status FROM outbox_events")).scalar_one()

    assert completed == 1
    assert fake_embedder.calls == ["publisher 部署入口也必须执行知识索引。"]
    assert outbox_status == "processed"
    assert job_status == "completed"
    assert active_count == 1
    assert embedding_count == 1


def test_vector_generation_is_not_activated_when_serving_chunk_changes_after_embedding(
    tmp_path: Path,
) -> None:
    settings, session_factory = _migrated(tmp_path)
    deleted = False
    text_value = "激活向量代次前必须复核正式 serving chunk。"
    artifacts = stored_text_artifacts(tmp_path, text_value)

    def delete_after_embedding() -> None:
        nonlocal deleted
        if deleted:
            return
        deleted = True
        with session_scope(session_factory) as session:
            knowledge_id = session.execute(
                text("SELECT id FROM current_formal_knowledge")
            ).scalar_one()
            KnowledgeRepository().soft_delete_knowledge(session, str(knowledge_id))

    fake_embedder = _FakeEmbedder(on_call=delete_after_embedding)

    with session_scope(session_factory) as session:
        _ingest(session, artifacts, text_value)

    assert process_outbox_once(settings) == 1
    completed = process_knowledge_jobs_once(
        session_factory,
        KnowledgeIndexJobExecutor(settings, embedder_factory=lambda: fake_embedder),
        worker_id="knowledge-worker",
    )

    with session_scope(session_factory) as session:
        job_status = session.execute(text("SELECT status FROM jobs")).scalar_one()
        active_count = session.execute(
            text("SELECT count(*) FROM embedding_generations WHERE index_status = 'active'")
        ).scalar_one()
        attempt = session.execute(
            text("SELECT status, error_class, error_message FROM job_attempts")
        ).one()

    assert completed == 0
    assert job_status == "pending"
    assert active_count == 0
    assert attempt[0] == "failed"
    assert attempt[1] == "ValueError"
    assert "serving chunk set changed" in attempt[2]
