from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
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
from zhiheng.retrieval.contracts import RetrievalContextChangedError, RetrievalFilters
from zhiheng.retrieval.repository import RetrievalRunRepository


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
    mark_formal_knowledge_indexed(session, ingested.knowledge_object_id)
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
    def search(
        self,
        session: Session,
        query: str,
        *,
        limit: int = 10,
        filters: RetrievalFilters | None = None,
    ) -> list[RetrievalCandidate]:
        return []


class _ExplodingVectorRetriever:
    def search(
        self,
        session: Session,
        query_embedding: Sequence[float],
        *,
        generation_id: str,
        limit: int,
        filters: RetrievalFilters | None = None,
    ) -> list[RetrievalCandidate]:
        raise AssertionError("vector should not run without an embedding and generation")


class _CandidateLexicalRetriever:
    def __init__(self, candidate: RetrievalCandidate) -> None:
        self._candidate = candidate

    def search(
        self,
        session: Session,
        query: str,
        *,
        limit: int = 10,
        filters: RetrievalFilters | None = None,
    ) -> list[RetrievalCandidate]:
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
    """Changes the source from the request's own session on every persistence."""

    def __init__(self) -> None:
        self.calls = 0

    def create_run(
        self,
        session: Session,
        *,
        raw_query: str,
        route: str,
        started_at: float,
        results: Sequence[AuthorizedChunk],
        strategy_release_id: str,
        manifest_hash: str | None = None,
    ) -> str:
        self.calls += 1
        run_id = RetrievalRunRepository().create_run(
            session,
            raw_query=raw_query,
            route=route,
            started_at=started_at,
            results=results,
            strategy_release_id=strategy_release_id,
            manifest_hash=manifest_hash,
        )
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
        return run_id


def test_hybrid_rejects_source_changed_after_persistence_before_response(tmp_path: Path) -> None:
    session_factory = _migrated_session_factory(tmp_path)
    artifacts = stored_text_artifacts(
        tmp_path,
        "中文全文检索必须回查正式视图后，才能进入回答上下文。",
    )
    run_repository = _MutatingRunRepository()

    with session_scope(session_factory) as session:
        chunk_id = _ingest(session, artifacts=artifacts)
        candidate = _candidate_from_chunk(session, chunk_id)

        with pytest.raises(RetrievalContextChangedError, match="changed before response"):
            HybridRetriever(
                lexical=_CandidateLexicalRetriever(candidate),
                run_repository=run_repository,
            ).search_offline(
                session,
                "中文 检索",
                limit=5,
            )
        # Retried once; both stale runs were rolled back rather than left behind.
        assert run_repository.calls == 2
        assert session.execute(text("SELECT count(*) FROM retrieval_runs")).scalar_one() == 0
        assert session.execute(text("SELECT count(*) FROM citation_records")).scalar_one() == 0


class _ConcurrentWorkerRunRepository:
    """A Worker commit lands just before the first run is persisted.

    This is the window a reindex used to hit: the manifest had passed its first
    validation, so the post-persistence check failed and the answer returned 500.
    """

    def __init__(self, worker_factory: sessionmaker[Session], source_id: str) -> None:
        self._worker_factory = worker_factory
        self._source_id = source_id
        self.calls = 0

    def create_run(
        self,
        session: Session,
        *,
        raw_query: str,
        route: str,
        started_at: float,
        results: Sequence[AuthorizedChunk],
        strategy_release_id: str,
        manifest_hash: str | None = None,
    ) -> str:
        self.calls += 1
        if self.calls == 1:
            with session_scope(self._worker_factory) as worker_session:
                _reconfirm_knowledge(worker_session, self._source_id)
        return RetrievalRunRepository().create_run(
            session,
            raw_query=raw_query,
            route=route,
            started_at=started_at,
            results=results,
            strategy_release_id=strategy_release_id,
            manifest_hash=manifest_hash,
        )


def _reconfirm_knowledge(session: Session, source_id: str) -> None:
    session.execute(
        text(
            """
            UPDATE knowledge_objects
            SET confirmation_generation = confirmation_generation + 1
            WHERE id = :source_id
            """
        ),
        {"source_id": source_id},
    )


def _impatient_worker_factory(tmp_path: Path) -> sessionmaker[Session]:
    # A short busy timeout makes any write lock held by the request fail fast.
    return create_session_factory(
        create_sqlite_engine(
            Settings(
                environment="test",
                database_url=f"sqlite:///{tmp_path / 'zhiheng.db'}",
                sqlite_busy_timeout_ms=50,
            )
        )
    )


class _ListLexicalRetriever:
    def __init__(self, candidates: Sequence[RetrievalCandidate]) -> None:
        self._candidates = list(candidates)

    def search(
        self,
        session: Session,
        query: str,
        *,
        limit: int = 10,
        filters: RetrievalFilters | None = None,
    ) -> list[RetrievalCandidate]:
        return self._candidates


def test_hybrid_reseals_once_when_worker_commits_mid_request(tmp_path: Path) -> None:
    session_factory = _migrated_session_factory(tmp_path)
    with session_scope(session_factory) as session:
        changed_chunk = _ingest(
            session,
            artifacts=stored_text_artifacts(tmp_path, "中文全文检索第一份资料。"),
            title="第一份",
            text_value="中文全文检索第一份资料。",
        )
        kept_chunk = _ingest(
            session,
            artifacts=stored_text_artifacts(tmp_path, "中文全文检索第二份资料。"),
            title="第二份",
            text_value="中文全文检索第二份资料。",
        )
        candidates = [
            replace(_candidate_from_chunk(session, chunk_id), rank=rank)
            for rank, chunk_id in enumerate((changed_chunk, kept_chunk), start=1)
        ]
    run_repository = _ConcurrentWorkerRunRepository(
        _impatient_worker_factory(tmp_path), candidates[0].source_id
    )

    with session_scope(session_factory) as session:
        result = HybridRetriever(
            lexical=_ListLexicalRetriever(candidates),
            run_repository=run_repository,
        ).search_offline(session, "中文 检索", limit=5)
        assert RetrievalAuthorizer().validate_manifest(session, result.manifest)

    assert run_repository.calls == 2
    assert [chunk.chunk_id for chunk in result.manifest.chunks] == [kept_chunk]
    with session_scope(session_factory) as session:
        runs = session.execute(text("SELECT id, manifest_hash FROM retrieval_runs")).all()
        cited = session.execute(text("SELECT chunk_id FROM citation_records")).scalars().all()
    assert [tuple(row) for row in runs] == [(result.run_id, str(result.manifest.seal))]
    assert cited == [kept_chunk]


def test_hybrid_retrieval_leaves_no_write_lock_once_committed(tmp_path: Path) -> None:
    session_factory = _migrated_session_factory(tmp_path)
    artifacts = stored_text_artifacts(
        tmp_path,
        "中文全文检索必须回查正式视图后，才能进入回答上下文。",
    )
    with session_scope(session_factory) as session:
        candidate = _candidate_from_chunk(session, _ingest(session, artifacts=artifacts))
    worker_factory = _impatient_worker_factory(tmp_path)

    session = session_factory()
    try:
        result = HybridRetriever(lexical=_CandidateLexicalRetriever(candidate)).search_offline(
            session, "中文 检索", limit=5
        )
        # Answer paths commit the run before dispatching to a model; the still
        # open session must not keep the Worker from writing during that call.
        session.commit()
        with session_scope(worker_factory) as worker_session:
            _reconfirm_knowledge(worker_session, candidate.source_id)
        assert not RetrievalAuthorizer().validate_manifest(session, result.manifest)
    finally:
        session.close()


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


class _FixedLexical:
    def __init__(self, candidate: RetrievalCandidate) -> None:
        self._candidate = candidate

    def search(
        self,
        session: Session,
        query: str,
        *,
        limit: int,
        filters: RetrievalFilters | None = None,
    ) -> list[RetrievalCandidate]:
        del session, query, limit, filters
        return [self._candidate]


class _EmptyVector:
    def search(
        self,
        session: Session,
        query_embedding: Sequence[float],
        *,
        generation_id: str,
        limit: int,
        filters: RetrievalFilters | None = None,
    ) -> list[RetrievalCandidate]:
        del session, query_embedding, generation_id, limit, filters
        return []


def test_title_filter_expands_before_top_k_and_finds_formal_source(tmp_path: Path) -> None:
    """A titled source remains findable when lexical top-k excludes it."""
    session_factory = _migrated_session_factory(tmp_path)
    artifacts = stored_text_artifacts(tmp_path, "标题限定的正式内容应当可检索。")

    with session_scope(session_factory) as session:
        decoy_chunk = _ingest(
            session,
            artifacts=stored_text_artifacts(tmp_path / "decoy", "无关内容。"),
            title="无关资料",
            text_value="无关内容。",
        )
        target_chunk = _ingest(
            session,
            artifacts=artifacts,
            title="目标资料",
            text_value="标题限定的正式内容应当可检索。",
        )
        decoy = _candidate_from_chunk(session, decoy_chunk)
        retriever = HybridRetriever(lexical=_FixedLexical(decoy), vector=_EmptyVector())

        result = retriever.search_offline(
            session,
            "目标资料",
            limit=1,
            filters=RetrievalFilters(title="目标资料"),
        )

        assert [chunk.chunk_id for chunk in result.manifest.chunks] == [target_chunk]


def test_title_filter_rejects_ambiguous_duplicate_titles(tmp_path: Path) -> None:
    session_factory = _migrated_session_factory(tmp_path)

    with session_scope(session_factory) as session:
        _ingest(
            session,
            artifacts=stored_text_artifacts(tmp_path / "first", "第一份同名资料。"),
            title="重复标题",
            text_value="第一份同名资料。",
        )
        _ingest(
            session,
            artifacts=stored_text_artifacts(tmp_path / "second", "第二份同名资料。"),
            title="重复标题",
            text_value="第二份同名资料。",
        )

        result = HybridRetriever(vector=_EmptyVector()).search_offline(
            session,
            "重复标题",
            limit=5,
            filters=RetrievalFilters(title="重复标题"),
        )

        assert result.manifest.chunks == ()
