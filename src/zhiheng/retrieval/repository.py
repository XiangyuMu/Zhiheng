from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.orm import Session

from zhiheng.core.ids import json_text, new_id, sha256_text
from zhiheng.retrieval.contracts import (
    AuthorizedChunk,
    RetrievalCandidate,
    RetrievalFilters,
    RetrievalSource,
)
from zhiheng.retrieval.tokenizer import DEFAULT_TOKENIZER, Tokenizer
from zhiheng.retrieval.vector_index import VectorIndexRepository


class CitationContextRepository:
    """Read an authorized serving chunk for citation replay."""

    def get_chunk(
        self,
        session: Session,
        *,
        source_type: str,
        source_id: str,
        source_version_id: str,
        chunk_id: str,
    ) -> dict[str, object] | None:
        row = (
            session.execute(
                text(
                    """
                SELECT s.title, s.text, s.span_start, s.span_end,
                       ko.object_kind, eo.media_type
                FROM serving_chunks s
                JOIN current_formal_knowledge cfk
                  ON cfk.id=s.source_id AND cfk.current_version_id=s.source_version_id
                JOIN knowledge_objects ko ON ko.id=cfk.id
                JOIN content_versions cv
                  ON cv.id=s.content_version_id AND cv.status='active'
                JOIN evidence_objects eo
                  ON eo.id=cv.evidence_object_id AND eo.status='active'
                WHERE s.source_type=:source_type AND s.source_id=:source_id
                  AND s.source_version_id=:source_version_id AND s.id=:chunk_id
                """
                ),
                {
                    "source_type": source_type,
                    "source_id": source_id,
                    "source_version_id": source_version_id,
                    "chunk_id": chunk_id,
                },
            )
            .mappings()
            .first()
        )
        return dict(row) if row is not None else None


class VectorIndexSearchPort(Protocol):
    def active_generation_id(
        self,
        session: Session,
        *,
        model_id: str,
        model_revision: str,
        dimension: int,
        purpose: str = "retrieval",
    ) -> str | None: ...

    def search_active(
        self,
        session: Session,
        query_embedding: Sequence[float],
        *,
        model_id: str,
        model_revision: str,
        dimension: int,
        purpose: str = "retrieval",
        limit: int = 10,
    ) -> list[RetrievalCandidate]: ...


@dataclass(frozen=True)
class VectorGenerationMetadata:
    model_id: str
    model_revision: str
    dimension: int
    purpose: str
    index_status: str


class StructuredLookupService:
    _allowed_selectors = {
        "memory.state_key": (
            """
            SELECT
              'formal_memory' AS source_type,
              id AS source_id,
              current_version_id AS source_version_id,
              state_key AS selector,
              value_json AS value_json,
              effective_generation AS confirmation_generation
            FROM current_formal_memory
            WHERE state_key = :value
            """
        ),
        "knowledge.id": (
            """
            SELECT
              'knowledge_object' AS source_type,
              id AS source_id,
              current_version_id AS source_version_id,
              title AS selector,
              summary AS value_json,
              confirmation_generation
            FROM current_formal_knowledge
            WHERE id = :value
            """
        ),
    }

    def lookup(self, session: Session, *, selector: str, value: str) -> list[dict[str, object]]:
        statement = self._allowed_selectors.get(selector)
        if statement is None:
            raise ValueError("unsupported structured selector")
        rows = session.execute(text(statement), {"value": value}).mappings()
        return [dict(row) for row in rows]


class LexicalRetriever:
    def __init__(self, tokenizer: Tokenizer = DEFAULT_TOKENIZER) -> None:
        self._tokenizer = tokenizer

    def search(
        self,
        session: Session,
        query: str,
        *,
        limit: int = 10,
        filters: RetrievalFilters | None = None,
    ) -> list[RetrievalCandidate]:
        del filters
        segmented_query = self._tokenizer.segment(query)
        rows = session.execute(
            text(
                """
                SELECT
                  s.source_type,
                  s.source_id,
                  s.source_version_id,
                  s.id AS chunk_id,
                  s.confirmation_generation,
                  bm25(fts_chunks) AS score
                FROM fts_chunks
                JOIN chunks c ON c.rowid = fts_chunks.rowid
                JOIN serving_chunks s ON s.id = c.id
                WHERE fts_chunks MATCH :query
                  AND (
                    s.source_type <> 'knowledge_object'
                    OR EXISTS (
                    SELECT 1
                    FROM jobs completed_index
                    WHERE completed_index.job_type = 'knowledge.index'
                      AND completed_index.status = 'completed'
                      AND (
                        json_extract(completed_index.payload_json, '$.knowledge_object_id')
                          = s.source_id
                        OR json_extract(completed_index.payload_json, '$.aggregate_id')
                          = s.source_id
                      )
                    )
                  )
                  AND (
                    s.source_type <> 'knowledge_object'
                    OR EXISTS (
                      SELECT 1
                      FROM knowledge_objects ko
                      JOIN knowledge_versions kv ON kv.id = ko.current_version_id
                      JOIN content_versions cv ON cv.id = kv.content_version_id
                      JOIN evidence_objects eo ON eo.id = cv.evidence_object_id
                      WHERE ko.id = s.source_id
                        AND ko.current_version_id = s.source_version_id
                        AND cv.status = 'active'
                        AND eo.status = 'active'
                    )
                  )
                ORDER BY score
                LIMIT :limit
                """
            ),
            {"query": segmented_query, "limit": limit},
        ).mappings()
        return [
            RetrievalCandidate(
                source_type=str(row["source_type"]),
                source_id=str(row["source_id"]),
                source_version_id=str(row["source_version_id"]),
                chunk_id=str(row["chunk_id"]),
                confirmation_generation=int(row["confirmation_generation"]),
                generation=None,
                rank=index,
                score=float(row["score"]),
                retriever=RetrievalSource.LEXICAL,
                component_ranks=((RetrievalSource.LEXICAL, index),),
            )
            for index, row in enumerate(rows, start=1)
        ]


class VectorRetriever:
    def __init__(self, index: VectorIndexSearchPort | None = None) -> None:
        self._index = index or VectorIndexRepository()

    def active_generation_id(
        self,
        session: Session,
        *,
        model_id: str,
        model_revision: str,
        dimension: int,
        purpose: str = "retrieval",
    ) -> str | None:
        return self._index.active_generation_id(
            session,
            model_id=model_id,
            model_revision=model_revision,
            dimension=dimension,
            purpose=purpose,
        )

    def generation_metadata(
        self,
        session: Session,
        *,
        generation_id: str,
    ) -> VectorGenerationMetadata | None:
        row = (
            session.execute(
                text(
                    """
                SELECT model_id, model_revision, dimension, purpose, index_status
                FROM embedding_generations
                WHERE id = :generation_id
                """
                ),
                {"generation_id": generation_id},
            )
            .mappings()
            .first()
        )
        if row is None:
            return None
        return VectorGenerationMetadata(
            model_id=str(row["model_id"]),
            model_revision=str(row["model_revision"]),
            dimension=int(row["dimension"]),
            purpose=str(row["purpose"]),
            index_status=str(row["index_status"]),
        )

    def search(
        self,
        session: Session,
        query_embedding: Sequence[float],
        *,
        generation_id: str,
        limit: int = 10,
        filters: RetrievalFilters | None = None,
    ) -> list[RetrievalCandidate]:
        del filters
        if not query_embedding:
            raise ValueError("query_embedding cannot be empty")
        generation = self.generation_metadata(session, generation_id=generation_id)
        if generation is None or generation.index_status != "active":
            return []
        if generation.dimension != len(query_embedding):
            raise ValueError("query embedding dimension does not match generation")
        active_id = self.active_generation_id(
            session,
            model_id=generation.model_id,
            model_revision=generation.model_revision,
            dimension=generation.dimension,
            purpose=generation.purpose,
        )
        if active_id != generation_id:
            return []
        return [
            RetrievalCandidate(
                source_type=candidate.source_type,
                source_id=candidate.source_id,
                source_version_id=candidate.source_version_id,
                chunk_id=candidate.chunk_id,
                confirmation_generation=candidate.confirmation_generation,
                generation=candidate.generation,
                rank=candidate.rank,
                score=candidate.score,
                retriever=candidate.retriever,
                component_ranks=candidate.component_ranks
                or ((RetrievalSource.VECTOR, candidate.rank),),
            )
            for candidate in self._index.search_active(
                session,
                query_embedding,
                model_id=generation.model_id,
                model_revision=generation.model_revision,
                dimension=generation.dimension,
                purpose=generation.purpose,
                limit=limit,
            )
        ]


class RetrievalRunRepository:
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
        if not strategy_release_id:
            raise ValueError("retrieval run requires a strategy_release_id")
        run_id = new_id()
        latency_ms = max(0, int((time.monotonic() - started_at) * 1000))
        session.execute(
            text(
                """
                INSERT INTO retrieval_runs (
                  id, query_hash, route, strategy_release_id, latency_ms, result_count,
                  manifest_hash
                )
                VALUES (
                  :id, :query_hash, :route, :strategy_release_id, :latency_ms, :result_count,
                  :manifest_hash
                )
                """
            ),
            {
                "id": run_id,
                "query_hash": sha256_text(raw_query),
                "route": route,
                "strategy_release_id": strategy_release_id,
                "latency_ms": latency_ms,
                "result_count": len(results),
                "manifest_hash": manifest_hash,
            },
        )
        for index, result in enumerate(results, start=1):
            citation_record_id = self._create_citation_record(
                session,
                run_id=run_id,
                result=result,
            )
            retriever_ranks = [
                {"retriever": retriever.value, "rank": rank}
                for retriever, rank in result.component_ranks
            ]
            session.execute(
                text(
                    """
                    INSERT INTO retrieval_results (
                      run_id, rank, source_type, source_id, source_version_id,
                      chunk_id, content_version_id, content_span_id, retriever_ranks_json,
                      citation_record_id, retrieval_generation_id, score_json
                    )
                    VALUES (
                      :run_id, :rank, :source_type, :source_id, :source_version_id,
                      :chunk_id, :content_version_id, :content_span_id, :retriever_ranks_json,
                      :citation_record_id, :retrieval_generation_id, :score_json
                    )
                    """
                ),
                {
                    "run_id": run_id,
                    "rank": index,
                    "source_type": result.source_type,
                    "source_id": result.source_id,
                    "source_version_id": result.source_version_id,
                    "chunk_id": result.chunk_id,
                    "content_version_id": result.content_version_id,
                    "content_span_id": result.content_span_id,
                    "retriever_ranks_json": json_text(retriever_ranks),
                    "citation_record_id": citation_record_id,
                    "retrieval_generation_id": result.generation,
                    "score_json": json_text(
                        {
                            "score": result.score,
                            "retrievers": [retriever.value for retriever in result.retrievers],
                        }
                    ),
                },
            )
            for event_type in (
                "before_context",
                "after_evidence_load",
                "before_response",
            ):
                self._create_authorization_event(
                    session,
                    run_id=run_id,
                    event_type=event_type,
                    result=result,
                    manifest_hash=manifest_hash,
                )
        return run_id

    def _create_citation_record(
        self,
        session: Session,
        *,
        run_id: str,
        result: AuthorizedChunk,
    ) -> str:
        citation_record_id = new_id()
        session.execute(
            text(
                """
                INSERT INTO citation_records (
                  id, run_id, source_type, source_id, source_version_id, chunk_id,
                  content_version_id, content_span_id, span_start, span_end, quote_hash,
                  evidence_object_id, citation_payload_json
                )
                VALUES (
                  :id, :run_id, :source_type, :source_id, :source_version_id, :chunk_id,
                  :content_version_id, :content_span_id, :span_start, :span_end, :quote_hash,
                  :evidence_object_id, :citation_payload_json
                )
                """
            ),
            {
                "id": citation_record_id,
                "run_id": run_id,
                "source_type": result.source_type,
                "source_id": result.source_id,
                "source_version_id": result.source_version_id,
                "chunk_id": result.chunk_id,
                "content_version_id": result.content_version_id,
                "content_span_id": result.content_span_id,
                "span_start": result.span_start,
                "span_end": result.span_end,
                "quote_hash": result.quote_hash,
                "evidence_object_id": result.evidence_object_id,
                "citation_payload_json": json_text(
                    {
                        "page_no": result.page_no,
                        "section_path": result.section_path,
                        "confirmation_generation": result.confirmation_generation,
                    }
                ),
            },
        )
        return citation_record_id

    def _create_authorization_event(
        self,
        session: Session,
        *,
        run_id: str,
        event_type: str,
        result: AuthorizedChunk,
        manifest_hash: str | None,
    ) -> None:
        session.execute(
            text(
                """
                INSERT INTO retrieval_authorization_events (
                  id, run_id, event_type, source_type, source_id, source_version_id,
                  chunk_id, confirmation_generation, retrieval_generation_id, authorized,
                  reason, manifest_hash, payload_json
                )
                VALUES (
                  :id, :run_id, :event_type, :source_type, :source_id, :source_version_id,
                  :chunk_id, :confirmation_generation, :retrieval_generation_id, :authorized,
                  :reason, :manifest_hash, :payload_json
                )
                """
            ),
            {
                "id": new_id(),
                "run_id": run_id,
                "event_type": event_type,
                "source_type": result.source_type,
                "source_id": result.source_id,
                "source_version_id": result.source_version_id,
                "chunk_id": result.chunk_id,
                "confirmation_generation": result.confirmation_generation,
                "retrieval_generation_id": result.generation,
                "authorized": True,
                "reason": "current formal source",
                "manifest_hash": manifest_hash,
                "payload_json": json_text(
                    {
                        "content_version_id": result.content_version_id,
                        "content_span_id": result.content_span_id,
                        "quote_hash": result.quote_hash,
                    }
                ),
            },
        )
