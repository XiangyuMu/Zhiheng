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
from zhiheng.retrieval import (
    AuthorizedChunk,
    CitationBuilder,
    HybridRetriever,
    RetrievalAuthorizer,
    RetrievalCandidate,
    RetrievalSource,
    StructuredLookupService,
    VectorIndexRepository,
    VectorRetriever,
)


def _migrated_session_factory(tmp_path: Path) -> sessionmaker[Session]:
    db_path = tmp_path / "zhiheng.db"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    settings = Settings(environment="test", database_url=f"sqlite:///{db_path}")
    return create_session_factory(create_sqlite_engine(settings))


def _ingest(
    session: Session,
    *,
    artifacts: StoredTextArtifacts,
    title: str = "中文检索",
    text_value: str | None = None,
) -> str:
    body = text_value or "中文全文检索必须回查正式视图后，才能进入回答上下文。"
    ingested = KnowledgeRepository().ingest_text(
        session,
        TextEvidenceInput(
            title=title,
            primary_domain_id="technology.ai",
            text=body,
            source_metadata={"fixture": "synthetic"},
            summary="synthetic summary",
        ),
        user_authority=KnowledgeUserAuthority("synthetic-test-user"),
        stored_artifacts=artifacts,
    )
    return ingested.chunk_id


def _candidate_from_chunk(
    session: Session,
    chunk_id: str,
    *,
    generation: str | None = None,
) -> RetrievalCandidate:
    row = (
        session.execute(
            text(
                """
            SELECT source_type, source_id, source_version_id, confirmation_generation
            FROM chunks
            WHERE id = :chunk_id
            """
            ),
            {"chunk_id": chunk_id},
        )
        .mappings()
        .one()
    )
    return RetrievalCandidate(
        source_type=str(row["source_type"]),
        source_id=str(row["source_id"]),
        source_version_id=str(row["source_version_id"]),
        chunk_id=chunk_id,
        confirmation_generation=int(row["confirmation_generation"]),
        generation=generation,
        rank=1,
        score=1.0,
        retriever=RetrievalSource.VECTOR if generation else RetrievalSource.LEXICAL,
    )


def test_authorizer_rejects_stale_deleted_and_wrong_generation_candidates(tmp_path: Path) -> None:
    session_factory = _migrated_session_factory(tmp_path)
    knowledge_repository = KnowledgeRepository()
    artifacts = stored_text_artifacts(
        tmp_path,
        "中文全文检索必须回查正式视图后，才能进入回答上下文。",
    )

    with session_scope(session_factory) as session:
        chunk_id = _ingest(session, artifacts=artifacts)
        candidate = _candidate_from_chunk(session, chunk_id)
        assert RetrievalAuthorizer().authorize_batch(session, [candidate])

        stale = RetrievalCandidate(
            source_type=candidate.source_type,
            source_id=candidate.source_id,
            source_version_id=candidate.source_version_id,
            chunk_id=candidate.chunk_id,
            confirmation_generation=99,
            generation=None,
            rank=1,
            score=1.0,
            retriever=RetrievalSource.LEXICAL,
        )
        wrong_generation = RetrievalCandidate(
            source_type=candidate.source_type,
            source_id=candidate.source_id,
            source_version_id=candidate.source_version_id,
            chunk_id=candidate.chunk_id,
            confirmation_generation=candidate.confirmation_generation,
            generation="missing-generation",
            rank=1,
            score=1.0,
            retriever=RetrievalSource.VECTOR,
        )
        assert RetrievalAuthorizer().authorize_batch(session, [stale, wrong_generation]) == []

        knowledge_repository.soft_delete_knowledge(session, candidate.source_id)
        assert RetrievalAuthorizer().authorize_batch(session, [candidate]) == []


def test_manifest_validation_rejects_source_changed_after_load(tmp_path: Path) -> None:
    session_factory = _migrated_session_factory(tmp_path)
    artifacts = stored_text_artifacts(
        tmp_path,
        "中文全文检索必须回查正式视图后，才能进入回答上下文。",
    )

    with session_scope(session_factory) as session:
        chunk_id = _ingest(session, artifacts=artifacts)
        candidate = _candidate_from_chunk(session, chunk_id)
        authorizer = RetrievalAuthorizer()
        chunks = authorizer.authorize_batch(session, [candidate])
        manifest = authorizer.seal_manifest(query_hash="hash", chunks=chunks)
        session.execute(
            text(
                """
                UPDATE knowledge_objects
                SET confirmation_generation = confirmation_generation + 1
                WHERE id = :source_id
                """
            ),
            {"source_id": candidate.source_id},
        )

        assert not authorizer.validate_manifest(session, manifest)


def test_lexical_vector_hybrid_only_store_final_authorized_results_and_query_hash(
    tmp_path: Path,
) -> None:
    session_factory = _migrated_session_factory(tmp_path)
    raw_query = "中文 检索 私密查询"
    artifacts = stored_text_artifacts(
        tmp_path,
        "中文全文检索必须回查正式视图后，才能进入回答上下文。",
    )

    with session_scope(session_factory) as session:
        chunk_id = _ingest(session, artifacts=artifacts)
        vector_repository = VectorIndexRepository()
        generation_id = vector_repository.create_generation(
            session,
            model_id="BAAI/bge-m3",
            model_revision="synthetic",
            dimension=3,
        )
        vector_repository.rebuild_generation(session, generation_id, {chunk_id: [1.0, 0.0, 0.0]})
        vector_repository.activate_generation(session, generation_id)

        result = HybridRetriever().search_offline(
            session,
            raw_query,
            query_embedding=[1.0, 0.0, 0.0],
            vector_generation_id=generation_id,
            limit=5,
        )

        assert result.manifest.chunks
        citation = CitationBuilder().build(
            result.manifest,
            chunk_id=chunk_id,
            start_offset=0,
            end_offset=2,
        )
        assert citation.content_version_id is not None

        run = (
            session.execute(
                text(
                    """
                SELECT query_hash, result_count, manifest_hash
                FROM retrieval_runs
                WHERE id = :run_id
                """
                ),
                {"run_id": result.run_id},
            )
            .mappings()
            .one()
        )
        rows = (
            session.execute(
                text(
                    """
                SELECT
                  rr.chunk_id,
                  rr.content_version_id,
                  rr.content_span_id,
                  rr.retriever_ranks_json,
                  rr.citation_record_id,
                  rr.retrieval_generation_id,
                  cr.source_version_id AS citation_source_version_id,
                  cr.content_version_id AS citation_content_version_id,
                  cr.content_span_id AS citation_content_span_id,
                  cr.span_start,
                  cr.span_end,
                  cr.quote_hash AS citation_quote_hash
                FROM retrieval_results rr
                JOIN citation_records cr ON cr.id = rr.citation_record_id
                WHERE rr.run_id = :run_id
                ORDER BY rr.rank
                """
                ),
                {"run_id": result.run_id},
            )
            .mappings()
            .all()
        )
        event_types = (
            session.execute(
                text(
                    """
                SELECT DISTINCT event_type
                FROM retrieval_authorization_events
                WHERE run_id = :run_id
                ORDER BY event_type
                """
                ),
                {"run_id": result.run_id},
            )
            .scalars()
            .all()
        )

    assert run["query_hash"] != raw_query
    assert run["result_count"] == len(result.manifest.chunks)
    assert run["manifest_hash"] == result.manifest.seal
    assert len(rows) == len(result.manifest.chunks)
    assert raw_query not in repr(rows)
    first = rows[0]
    first_chunk = result.manifest.chunks[0]
    assert first["chunk_id"] == first_chunk.chunk_id
    assert first["content_version_id"] == first_chunk.content_version_id
    assert first["content_span_id"] == first_chunk.content_span_id
    assert first["retriever_ranks_json"]
    assert first["citation_record_id"]
    assert first["retrieval_generation_id"] == generation_id
    assert first["citation_source_version_id"] == first_chunk.source_version_id
    assert first["citation_content_version_id"] == citation.content_version_id
    assert first["citation_content_span_id"] == citation.content_span_id
    assert (first["span_start"], first["span_end"]) == (
        first_chunk.span_start,
        first_chunk.span_end,
    )
    assert first["citation_quote_hash"] == first_chunk.quote_hash
    assert citation.source_version_id == first_chunk.source_version_id
    assert citation.content_span == (first_chunk.span_start, first_chunk.span_end)
    assert citation.quote_hash
    assert event_types == ["after_evidence_load", "before_context", "before_response"]


def test_structured_lookup_uses_only_current_formal_views(tmp_path: Path) -> None:
    session_factory = _migrated_session_factory(tmp_path)
    artifacts = stored_text_artifacts(
        tmp_path,
        "中文全文检索必须回查正式视图后，才能进入回答上下文。",
    )

    with session_scope(session_factory) as session:
        chunk_id = _ingest(session, artifacts=artifacts)
        source_id = _candidate_from_chunk(session, chunk_id).source_id
        lookup = StructuredLookupService()

        assert lookup.lookup(session, selector="knowledge.id", value=source_id)
        with pytest.raises(ValueError, match="unsupported"):
            lookup.lookup(session, selector="knowledge.raw_sql", value=source_id)


class _EmptyLexicalRetriever:
    def search(self, session: Session, query: str, *, limit: int = 10) -> list[RetrievalCandidate]:
        return []


class _ExplodingVectorRetriever:
    def search(
        self,
        session: Session,
        query_embedding: Sequence[float],
        *,
        generation_id: str,
        limit: int,
    ) -> list[RetrievalCandidate]:
        raise AssertionError("vector should not run without an embedding and generation")


class _CandidateLexicalRetriever:
    def __init__(self, candidate: RetrievalCandidate) -> None:
        self._candidate = candidate

    def search(self, session: Session, query: str, *, limit: int = 10) -> list[RetrievalCandidate]:
        return [self._candidate]


def test_hybrid_degrades_without_raw_table_scan_fallback(tmp_path: Path) -> None:
    session_factory = _migrated_session_factory(tmp_path)
    artifacts = stored_text_artifacts(
        tmp_path,
        "中文全文检索必须回查正式视图后，才能进入回答上下文。",
    )

    with session_scope(session_factory) as session:
        _ingest(session, artifacts=artifacts)
        result = HybridRetriever(
            lexical=_EmptyLexicalRetriever(),
            vector=_ExplodingVectorRetriever(),
        ).search_offline(session, "中文 检索", limit=5)
        stored_count = session.execute(text("SELECT count(*) FROM retrieval_results")).scalar_one()

    assert result.degraded_reasons == ("vector_unavailable",)
    assert result.manifest.chunks == ()
    assert stored_count == 0


class _MutatingRunRepository:
    def create_run(
        self,
        session: Session,
        *,
        raw_query: str,
        route: str,
        started_at: float,
        results: Sequence[AuthorizedChunk],
        strategy_release_id: str | None = None,
        manifest_hash: str | None = None,
    ) -> str:
        session.execute(
            text(
                """
                UPDATE knowledge_objects
                SET confirmation_generation = confirmation_generation + 1
                WHERE id = (
                  SELECT source_id
                  FROM serving_chunks
                  LIMIT 1
                )
                """
            )
        )
        return "mutated-run"


def test_hybrid_rejects_source_changed_after_persistence_before_response(tmp_path: Path) -> None:
    session_factory = _migrated_session_factory(tmp_path)
    artifacts = stored_text_artifacts(
        tmp_path,
        "中文全文检索必须回查正式视图后，才能进入回答上下文。",
    )

    with session_scope(session_factory) as session:
        chunk_id = _ingest(session, artifacts=artifacts)
        candidate = _candidate_from_chunk(session, chunk_id)

        with pytest.raises(ValueError, match="changed before response"):
            HybridRetriever(
                lexical=_CandidateLexicalRetriever(candidate),
                run_repository=_MutatingRunRepository(),
            ).search_offline(
                session,
                "中文 检索",
                limit=5,
            )


def test_vector_retriever_filters_against_serving_view(tmp_path: Path) -> None:
    session_factory = _migrated_session_factory(tmp_path)
    knowledge_repository = KnowledgeRepository()
    kept_text = "中文全文检索必须回查正式视图后，才能进入回答上下文。"
    deleted_text = "中文全文检索必须回查正式视图后，才能进入回答上下文。"
    kept_artifacts = stored_text_artifacts(tmp_path, kept_text)
    deleted_artifacts = stored_text_artifacts(tmp_path, deleted_text)

    with session_scope(session_factory) as session:
        kept_chunk = _ingest(
            session,
            title="保留",
            text_value=kept_text,
            artifacts=kept_artifacts,
        )
        deleted_chunk = _ingest(
            session,
            title="删除",
            text_value=deleted_text,
            artifacts=deleted_artifacts,
        )
        deleted_source = _candidate_from_chunk(session, deleted_chunk).source_id
        knowledge_repository.soft_delete_knowledge(session, deleted_source)
        vector_repository = VectorIndexRepository()
        generation_id = vector_repository.create_generation(
            session,
            model_id="BAAI/bge-m3",
            model_revision="synthetic",
            dimension=3,
        )
        vector_repository.rebuild_generation(
            session,
            generation_id,
            {kept_chunk: [1.0, 0.0, 0.0], deleted_chunk: [1.0, 0.0, 0.0]},
        )
        vector_repository.activate_generation(session, generation_id)

        hits = VectorRetriever().search(
            session,
            [1.0, 0.0, 0.0],
            generation_id=generation_id,
            limit=10,
        )

    assert [hit.chunk_id for hit in hits] == [kept_chunk]
