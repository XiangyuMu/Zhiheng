from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from tests.knowledge_helpers import stored_text_artifacts
from zhiheng.core.config import Settings
from zhiheng.db.session import create_session_factory, create_sqlite_engine, session_scope
from zhiheng.knowledge import KnowledgeRepository, KnowledgeUserAuthority, TextEvidenceInput
from zhiheng.knowledge.object_store import StoredTextArtifacts
from zhiheng.retrieval.contracts import RetrievalSource
from zhiheng.retrieval.vector_index import (
    QueryEmbeddingPort,
    SqliteVecAdapter,
    SqliteVecUnavailableError,
    VectorIndexRepository,
)


class _UnavailableAdapter(SqliteVecAdapter):
    def create_empty_index(self, session: Session, *, generation_id: str, dimension: int) -> str:
        raise SqliteVecUnavailableError("sqlite-vec unavailable")


class _FakeAdapter(SqliteVecAdapter):
    def __init__(self) -> None:
        self.rows: dict[str, list[tuple[int, Sequence[float]]]] = {}

    def create_empty_index(self, session: Session, *, generation_id: str, dimension: int) -> str:
        ref = f"fake:{generation_id}:{dimension}"
        self.rows[ref] = []
        return ref

    def insert_embeddings(
        self,
        session: Session,
        *,
        physical_index_ref: str,
        rows: Sequence[tuple[int, Sequence[float]]],
    ) -> None:
        self.rows[physical_index_ref] = list(rows)

    def search(
        self,
        session: Session,
        *,
        physical_index_ref: str,
        query_embedding: Sequence[float],
        limit: int,
    ) -> list[tuple[int, float]]:
        scored = []
        for rowid, embedding in self.rows[physical_index_ref]:
            distance = sum(
                (left - right) ** 2
                for left, right in zip(query_embedding, embedding, strict=True)
            )
            scored.append((rowid, distance))
        return sorted(scored, key=lambda item: item[1])[:limit]


class _FakeEmbedder(QueryEmbeddingPort):
    def embed_query(
        self,
        query: str,
        *,
        model_id: str,
        model_revision: str,
        dimension: int,
        normalize: bool,
    ) -> Sequence[float]:
        assert model_id == "BAAI/bge-m3"
        assert model_revision == "synthetic-revision"
        assert dimension == 3
        assert normalize is True
        return [1.0, 0.0, 0.0]


def _migrated_session_factory(tmp_path: Path) -> sessionmaker[Session]:
    db_path = tmp_path / "zhiheng.db"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    settings = Settings(environment="test", database_url=f"sqlite:///{db_path}")
    return create_session_factory(create_sqlite_engine(settings))


def _text_for_title(title: str) -> str:
    return f"{title} 的向量检索候选必须等待最终授权后才能进入回答上下文。"


def _ingest(session: Session, title: str, artifacts: StoredTextArtifacts) -> str:
    return KnowledgeRepository().ingest_text(
        session,
        TextEvidenceInput(
            title=title,
            primary_domain_id="technology.ai",
            text=_text_for_title(title),
            source_metadata={"fixture": "synthetic"},
        ),
        user_authority=KnowledgeUserAuthority("synthetic-test-user"),
        stored_artifacts=artifacts,
    ).chunk_id


def test_sqlite_vec_unavailable_fails_closed_without_scan(tmp_path: Path) -> None:
    session_factory = _migrated_session_factory(tmp_path)
    repository = VectorIndexRepository(adapter=_UnavailableAdapter())
    artifacts = stored_text_artifacts(tmp_path, _text_for_title("不可用扩展"))

    with session_scope(session_factory) as session:
        chunk_id = _ingest(session, "不可用扩展", artifacts)
        generation_id = repository.create_generation(
            session,
            model_id="BAAI/bge-m3",
            model_revision="synthetic-revision",
            dimension=3,
        )
        with pytest.raises(SqliteVecUnavailableError, match="unavailable"):
            repository.rebuild_generation(session, generation_id, {chunk_id: [1.0, 0.0, 0.0]})


def test_active_generation_requires_exact_model_revision_dimension_and_purpose(
    tmp_path: Path,
) -> None:
    session_factory = _migrated_session_factory(tmp_path)
    adapter = _FakeAdapter()
    repository = VectorIndexRepository(adapter=adapter)
    artifacts = stored_text_artifacts(tmp_path, _text_for_title("精确匹配"))

    with session_scope(session_factory) as session:
        chunk_id = _ingest(session, "精确匹配", artifacts)
        generation_id = repository.create_generation(
            session,
            model_id="BAAI/bge-m3",
            model_revision="synthetic-revision",
            dimension=3,
            purpose="retrieval",
        )
        repository.rebuild_generation(session, generation_id, {chunk_id: [1.0, 0.0, 0.0]})

        assert (
            repository.active_generation_id(
                session,
                model_id="BAAI/bge-m3",
                model_revision="synthetic-revision",
                dimension=3,
            )
            is None
        )
        assert repository.search_active(
            session,
            [1.0, 0.0, 0.0],
            model_id="BAAI/bge-m3",
            model_revision="synthetic-revision",
            dimension=3,
        ) == []

        repository.activate_generation(session, generation_id)

        assert repository.active_generation_id(
            session,
            model_id="BAAI/bge-m3",
            model_revision="synthetic-revision",
            dimension=3,
        ) == generation_id
        assert repository.active_generation_id(
            session,
            model_id="BAAI/bge-m3",
            model_revision="other",
            dimension=3,
        ) is None
        assert repository.active_generation_id(
            session,
            model_id="BAAI/bge-m3",
            model_revision="synthetic-revision",
            dimension=2,
        ) is None
        assert repository.active_generation_id(
            session,
            model_id="BAAI/bge-m3",
            model_revision="synthetic-revision",
            dimension=3,
            purpose="rerank",
        ) is None

        hits = repository.search_query(
            session,
            "精确匹配",
            embedder=_FakeEmbedder(),
            model_id="BAAI/bge-m3",
            model_revision="synthetic-revision",
            dimension=3,
        )

    assert [hit.chunk_id for hit in hits] == [chunk_id]
    assert hits[0].generation == generation_id
    assert hits[0].retriever is RetrievalSource.VECTOR


def test_activation_archives_only_same_model_and_purpose(tmp_path: Path) -> None:
    session_factory = _migrated_session_factory(tmp_path)
    repository = VectorIndexRepository(adapter=_FakeAdapter())
    artifacts = stored_text_artifacts(tmp_path, _text_for_title("激活切换"))

    with session_scope(session_factory) as session:
        chunk_id = _ingest(session, "激活切换", artifacts)
        first = repository.create_generation(
            session,
            model_id="BAAI/bge-m3",
            model_revision="rev-1",
            dimension=3,
        )
        second = repository.create_generation(
            session,
            model_id="BAAI/bge-m3",
            model_revision="rev-2",
            dimension=3,
        )
        separate_purpose = repository.create_generation(
            session,
            model_id="BAAI/bge-m3",
            model_revision="rev-rerank",
            dimension=3,
            purpose="rerank",
        )
        for generation_id in (first, second, separate_purpose):
            repository.rebuild_generation(session, generation_id, {chunk_id: [1.0, 0.0, 0.0]})

        repository.activate_generation(session, first)
        repository.activate_generation(session, separate_purpose)
        repository.activate_generation(session, second)

        statuses = {
            str(row["id"]): str(row["index_status"])
            for row in session.execute(
                text("SELECT id, index_status FROM embedding_generations")
            ).mappings()
        }

    assert statuses[first] == "archived"
    assert statuses[second] == "active"
    assert statuses[separate_purpose] == "active"
