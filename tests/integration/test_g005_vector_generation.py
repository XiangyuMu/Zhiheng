from __future__ import annotations

import importlib.util
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
from zhiheng.retrieval.vector_index import VectorIndexRepository


def _migrated_session_factory(tmp_path: Path) -> sessionmaker[Session]:
    db_path = tmp_path / "zhiheng.db"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    settings = Settings(environment="test", database_url=f"sqlite:///{db_path}")
    return create_session_factory(create_sqlite_engine(settings))


def _ingest(
    session: Session,
    title: str,
    text_value: str,
    artifacts: StoredTextArtifacts,
) -> str:
    return KnowledgeRepository().ingest_text(
        session,
        TextEvidenceInput(
            title=title,
            primary_domain_id="technology.ai",
            text=text_value,
            source_metadata={"fixture": "synthetic"},
        ),
        user_authority=KnowledgeUserAuthority("synthetic-test-user"),
        stored_artifacts=artifacts,
    ).chunk_id


def test_sqlite_vec_dependency_is_available_for_g005_vector_retrieval() -> None:
    assert importlib.util.find_spec("sqlite_vec") is not None, "sqlite-vec must be installed"


def test_sqlite_vec_generation_rebuild_activate_and_search_from_empty_database(
    tmp_path: Path,
) -> None:
    session_factory = _migrated_session_factory(tmp_path)
    repository = VectorIndexRepository()
    exposure_text = "摄影曝光需要理解光圈、快门和 ISO 的关系。"
    backtest_text = "量化回测需要区分样本内和样本外表现。"
    exposure_artifacts = stored_text_artifacts(tmp_path, exposure_text)
    backtest_artifacts = stored_text_artifacts(tmp_path, backtest_text)

    with session_scope(session_factory) as session:
        chunk_a = _ingest(session, "摄影曝光", exposure_text, exposure_artifacts)
        chunk_b = _ingest(session, "量化回测", backtest_text, backtest_artifacts)

        generation_id = repository.create_generation(
            session,
            model_id="BAAI/bge-m3",
            model_revision="synthetic-revision",
            dimension=3,
        )
        inserted = repository.rebuild_generation(
            session,
            generation_id,
            {chunk_a: [1.0, 0.0, 0.0], chunk_b: [0.0, 1.0, 0.0]},
        )

        assert inserted == 2
        assert repository.search_active(
            session,
            [1.0, 0.0, 0.0],
            model_id="BAAI/bge-m3",
            model_revision="synthetic-revision",
            dimension=3,
        ) == []

        repository.activate_generation(session, generation_id)
        hits = repository.search_active(
            session,
            [1.0, 0.0, 0.0],
            model_id="BAAI/bge-m3",
            model_revision="synthetic-revision",
            dimension=3,
            limit=2,
        )
        generation = session.execute(
            text(
                """
                SELECT physical_index_ref, built_count, source_manifest_hash
                FROM embedding_generations
                WHERE id = :generation_id
                """
            ),
            {"generation_id": generation_id},
        ).mappings().one()

    assert [hit.chunk_id for hit in hits] == [chunk_a, chunk_b]
    assert all(hit.generation == generation_id for hit in hits)
    assert generation["physical_index_ref"].startswith("sqlite_vec:vec_chunks_")
    assert generation["built_count"] == 2
    assert generation["source_manifest_hash"]


def test_building_and_shadow_generations_are_not_served(tmp_path: Path) -> None:
    session_factory = _migrated_session_factory(tmp_path)
    repository = VectorIndexRepository()

    artifacts = stored_text_artifacts(tmp_path, "候选索引不能在激活前被 serving。")
    with session_scope(session_factory) as session:
        chunk_id = _ingest(session, "候选索引", "候选索引不能在激活前被 serving。", artifacts)
        built_generation = repository.create_generation(
            session,
            model_id="BAAI/bge-m3",
            model_revision="built",
            dimension=3,
        )
        shadow_generation = repository.create_generation(
            session,
            model_id="BAAI/bge-m3",
            model_revision="shadow",
            dimension=3,
        )
        repository.rebuild_generation(session, built_generation, {chunk_id: [1.0, 0.0, 0.0]})
        repository.rebuild_generation(session, shadow_generation, {chunk_id: [1.0, 0.0, 0.0]})
        session.execute(
            text(
                """
                UPDATE embedding_generations
                SET index_status = 'shadow'
                WHERE id = :generation_id
                """
            ),
            {"generation_id": shadow_generation},
        )

        assert repository.search_active(
            session,
            [1.0, 0.0, 0.0],
            model_id="BAAI/bge-m3",
            model_revision="built",
            dimension=3,
        ) == []
        assert repository.search_active(
            session,
            [1.0, 0.0, 0.0],
            model_id="BAAI/bge-m3",
            model_revision="shadow",
            dimension=3,
        ) == []

        with pytest.raises(ValueError, match="only built"):
            repository.activate_generation(session, shadow_generation)
