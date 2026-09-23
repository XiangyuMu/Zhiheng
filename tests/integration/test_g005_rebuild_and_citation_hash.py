from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from tests.knowledge_helpers import stored_text_artifacts
from zhiheng.core.config import Settings
from zhiheng.core.ids import new_id, sha256_text
from zhiheng.db.session import create_session_factory, create_sqlite_engine, session_scope
from zhiheng.knowledge import KnowledgeRepository, KnowledgeUserAuthority, TextEvidenceInput
from zhiheng.knowledge.object_store import StoredTextArtifacts
from zhiheng.retrieval import (
    CitationBuilder,
    RetrievalAuthorizer,
    RetrievalCandidate,
    RetrievalSource,
)
from zhiheng.retrieval.tokenizer import segment_for_fts


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
    return (
        KnowledgeRepository()
        .ingest_text(
            session,
            TextEvidenceInput(
                title=title,
                primary_domain_id="technology.ai",
                text=text_value,
                source_metadata={"fixture": "g005-rebuild-citation-hash"},
                summary="synthetic summary",
            ),
            user_authority=KnowledgeUserAuthority("synthetic-test-user"),
            stored_artifacts=artifacts,
        )
        .chunk_id
    )


def _candidate_from_chunk(session: Session, chunk_id: str) -> RetrievalCandidate:
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
        generation=None,
        rank=1,
        score=1.0,
        retriever=RetrievalSource.LEXICAL,
    )


def test_rebuild_fts_index_uses_only_exact_current_serving_chunks(tmp_path: Path) -> None:
    session_factory = _migrated_session_factory(tmp_path)
    texts = {
        title: f"{title} 只用于检索授权过滤测试。"
        for title in (
            "kept",
            "candidate",
            "deleted",
            "erased",
            "stale",
            "wrong-generation",
        )
    }
    artifacts = {
        title: stored_text_artifacts(tmp_path, text_value) for title, text_value in texts.items()
    }

    with session_scope(session_factory) as session:
        kept = _ingest(session, "kept", texts["kept"], artifacts["kept"])
        candidate = _ingest(session, "candidate", texts["candidate"], artifacts["candidate"])
        deleted = _ingest(session, "deleted", texts["deleted"], artifacts["deleted"])
        erased = _ingest(session, "erased", texts["erased"], artifacts["erased"])
        stale = _ingest(session, "stale", texts["stale"], artifacts["stale"])
        wrong_generation = _ingest(
            session,
            "wrong-generation",
            texts["wrong-generation"],
            artifacts["wrong-generation"],
        )

        session.execute(
            text("UPDATE chunks SET visibility_scope = 'candidate' WHERE id = :chunk_id"),
            {"chunk_id": candidate},
        )
        KnowledgeRepository().soft_delete_knowledge(
            session,
            _candidate_from_chunk(session, deleted).source_id,
        )
        session.execute(
            text(
                """
                UPDATE evidence_objects
                SET status = 'privacy_erased'
                WHERE id = (
                  SELECT cv.evidence_object_id
                  FROM chunks c
                  JOIN knowledge_versions kv ON kv.id = c.source_version_id
                  JOIN content_versions cv ON cv.id = kv.content_version_id
                  WHERE c.id = :chunk_id
                )
                """
            ),
            {"chunk_id": erased},
        )
        stale_row = (
            session.execute(
                text(
                    """
                SELECT c.source_id, c.content_version_id
                FROM chunks c
                WHERE c.id = :chunk_id
                """
                ),
                {"chunk_id": stale},
            )
            .mappings()
            .one()
        )
        new_version_id = new_id()
        session.execute(
            text(
                """
                INSERT INTO knowledge_versions (
                  id, knowledge_object_id, version_no, content_version_id, markdown_uri,
                  summary, source_quality
                )
                VALUES (
                  :id, :knowledge_object_id, 2, :content_version_id,
                  :markdown_uri, 'replacement', 'user_provided'
                )
                """
            ),
            {
                "id": new_version_id,
                "knowledge_object_id": stale_row["source_id"],
                "content_version_id": stale_row["content_version_id"],
                "markdown_uri": f"artifact://knowledge/{new_version_id}.md",
            },
        )
        session.execute(
            text("UPDATE knowledge_objects SET current_version_id = :version_id WHERE id = :id"),
            {"version_id": new_version_id, "id": stale_row["source_id"]},
        )
        session.execute(
            text(
                """
                UPDATE knowledge_objects
                SET confirmation_generation = confirmation_generation + 1
                WHERE id = (SELECT source_id FROM chunks WHERE id = :chunk_id)
                """
            ),
            {"chunk_id": wrong_generation},
        )

        rebuilt = KnowledgeRepository().rebuild_fts_index(session)
        indexed = {
            marker: session.execute(
                text(
                    """
                    SELECT c.id
                    FROM fts_chunks
                    JOIN chunks c ON c.rowid = fts_chunks.rowid
                    WHERE fts_chunks MATCH :query
                    ORDER BY c.id
                    """
                ),
                {"query": marker},
            )
            .scalars()
            .all()
            for marker in (
                "kept",
                "candidate",
                "deleted",
                "erased",
                "stale",
                "wrong",
            )
        }

    assert rebuilt == 1
    assert indexed == {
        "kept": [kept],
        "candidate": [],
        "deleted": [],
        "erased": [],
        "stale": [],
        "wrong": [],
    }


def test_citations_hash_exact_offset_quote_and_fail_closed_on_tamper(tmp_path: Path) -> None:
    session_factory = _migrated_session_factory(tmp_path)
    artifacts = stored_text_artifacts(tmp_path, "中文知识图谱")

    with session_scope(session_factory) as session:
        chunk_id = _ingest(session, "subspan", "中文知识图谱", artifacts)
        row = (
            session.execute(
                text(
                    """
                SELECT source_version_id, content_version_id
                FROM chunks
                WHERE id = :chunk_id
                """
                ),
                {"chunk_id": chunk_id},
            )
            .mappings()
            .one()
        )
        span_id = new_id()
        session.execute(
            text(
                """
                INSERT INTO content_spans (
                  id, content_version_id, span_kind, start_offset, end_offset,
                  page_no, section_path, quote_hash
                )
                VALUES (
                  :id, :content_version_id, 'body', 2, 4,
                  NULL, 'subspan', :quote_hash
                )
                """
            ),
            {
                "id": span_id,
                "content_version_id": row["content_version_id"],
                "quote_hash": sha256_text("知识"),
            },
        )
        session.execute(
            text(
                """
                UPDATE chunks
                SET text = '知识',
                    raw_text = '知识',
                    segmented_text = :segmented_text,
                    span_start = 2,
                    span_end = 4,
                    content_span_id = :span_id
                WHERE id = :chunk_id
                """
            ),
            {
                "chunk_id": chunk_id,
                "span_id": span_id,
                "segmented_text": segment_for_fts("知识"),
            },
        )

        authorizer = RetrievalAuthorizer()
        candidate = _candidate_from_chunk(session, chunk_id)
        chunks = authorizer.authorize_batch(session, [candidate])
        manifest = authorizer.seal_manifest(query_hash="hash", chunks=chunks)
        subspan_citation = CitationBuilder().build(
            manifest,
            chunk_id=chunk_id,
            start_offset=2,
            end_offset=3,
        )
        full_span_citation = CitationBuilder().build(
            manifest,
            chunk_id=chunk_id,
            start_offset=2,
            end_offset=4,
        )

        session.execute(
            text("UPDATE content_spans SET quote_hash = :quote_hash WHERE id = :span_id"),
            {"span_id": span_id, "quote_hash": sha256_text("篡改")},
        )
        tampered = authorizer.authorize_batch(session, [candidate])

    assert subspan_citation.quote_hash == sha256_text("知")
    assert full_span_citation.quote_hash == sha256_text("知识")
    assert tampered == []


def test_authorizer_rejects_serving_chunk_with_erased_underlying_evidence(
    tmp_path: Path,
) -> None:
    session_factory = _migrated_session_factory(tmp_path)
    artifacts = stored_text_artifacts(tmp_path, "synthetic evidence for authorization")

    with session_scope(session_factory) as session:
        chunk_id = _ingest(
            session, "erased-underlying", "synthetic evidence for authorization", artifacts
        )
        candidate = _candidate_from_chunk(session, chunk_id)
        assert session.execute(text("SELECT count(*) FROM serving_chunks")).scalar_one() == 1
        session.execute(
            text(
                """
                UPDATE evidence_objects
                SET status = 'privacy_erased'
                WHERE id = (
                  SELECT cv.evidence_object_id
                  FROM chunks c
                  JOIN knowledge_versions kv ON kv.id = c.source_version_id
                  JOIN content_versions cv ON cv.id = kv.content_version_id
                  WHERE c.id = :chunk_id
                )
                """
            ),
            {"chunk_id": chunk_id},
        )

        assert session.execute(text("SELECT count(*) FROM serving_chunks")).scalar_one() == 1
        authorized = RetrievalAuthorizer().authorize_batch(session, [candidate])

    assert authorized == []
