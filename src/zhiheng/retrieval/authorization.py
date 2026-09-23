from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace

from sqlalchemy import text
from sqlalchemy.orm import Session

from zhiheng.core.ids import new_id, sha256_json, sha256_text
from zhiheng.retrieval.contracts import (
    AuthorizedChunk,
    AuthorizedContextManifest,
    ManifestSeal,
    RetrievalCandidate,
)


class RetrievalAuthorizer:
    def authorize_batch(
        self,
        session: Session,
        candidates: Iterable[RetrievalCandidate],
    ) -> list[AuthorizedChunk]:
        unique: dict[tuple[str, str, str, str, int, str | None], RetrievalCandidate] = {}
        for candidate in candidates:
            unique.setdefault(candidate.authorization_tuple, candidate)

        authorized: list[AuthorizedChunk] = []
        for candidate in unique.values():
            if candidate.generation is not None:
                vector_row = session.execute(
                    text(
                        """
                        SELECT 1
                        FROM chunk_embeddings ce
                        JOIN embedding_generations eg ON eg.id = ce.generation_id
                        WHERE ce.chunk_id = :chunk_id
                          AND ce.generation_id = :generation_id
                          AND eg.index_status = 'active'
                          AND ce.source_version_id = :source_version_id
                          AND ce.visibility_scope = 'formal'
                          AND ce.confirmation_generation = :confirmation_generation
                        """
                    ),
                    {
                        "chunk_id": candidate.chunk_id,
                        "generation_id": candidate.generation,
                        "source_version_id": candidate.source_version_id,
                        "confirmation_generation": candidate.confirmation_generation,
                    },
                ).first()
                if vector_row is None:
                    continue

            if candidate.source_type == "event_memory":
                event_row = (
                    session.execute(
                        text("""
                  SELECT s.source_type,s.source_id,s.source_version_id,s.id AS chunk_id,
                         s.confirmation_generation,s.title,s.text,s.span_start,s.span_end,
                         s.quote_hash
                  FROM serving_chunks s
                  JOIN event_memories e ON e.id=s.source_id AND e.status='formal_current'
                  JOIN event_memory_versions v ON v.id=s.source_version_id AND v.event_memory_id=e.id
                  JOIN event_memory_evidence ev ON ev.event_version_id=v.id
                  JOIN answer_history h ON h.id=e.source_history_id
                  WHERE s.source_type='event_memory' AND s.source_id=:source_id
                    AND s.source_version_id=:source_version_id AND s.id=:chunk_id
                    AND s.confirmation_generation=:generation
                """),
                        {
                            "source_id": candidate.source_id,
                            "source_version_id": candidate.source_version_id,
                            "chunk_id": candidate.chunk_id,
                            "generation": candidate.confirmation_generation,
                        },
                    )
                    .mappings()
                    .first()
                )
                if event_row is None or str(event_row["quote_hash"]) != sha256_text(
                    str(event_row["text"])
                ):
                    continue
                authorized.append(
                    AuthorizedChunk(
                        source_type="event_memory",
                        source_id=str(event_row["source_id"]),
                        source_version_id=str(event_row["source_version_id"]),
                        chunk_id=str(event_row["chunk_id"]),
                        confirmation_generation=int(event_row["confirmation_generation"]),
                        generation=candidate.generation,
                        title=str(event_row["title"]),
                        text=str(event_row["text"]),
                        span_start=int(event_row["span_start"]),
                        span_end=int(event_row["span_end"]),
                        content_version_id=None,
                        content_span_id=None,
                        evidence_object_id=None,
                        page_no=None,
                        section_path=None,
                        quote_hash=str(event_row["quote_hash"]),
                        score=candidate.score,
                        rank=candidate.rank,
                        retrievers=(candidate.retriever,),
                        component_ranks=candidate.component_ranks
                        or ((candidate.retriever, candidate.rank),),
                    )
                )
                continue
            row = (
                session.execute(
                    text(
                        """
                    SELECT
                      s.source_type,
                      s.source_id,
                      s.source_version_id,
                      s.id AS chunk_id,
                      s.confirmation_generation,
                      s.title,
                      s.text,
                      s.span_start,
                      s.span_end,
                      kv.content_version_id,
                      cs.id AS content_span_id,
                      cs.start_offset AS content_span_start,
                      cs.end_offset AS content_span_end,
                      cs.page_no,
                      cs.section_path,
                      cs.quote_hash,
                      cv.evidence_object_id
                    FROM serving_chunks s
                    JOIN current_formal_knowledge cfk
                      ON cfk.id = s.source_id
                     AND cfk.current_version_id = s.source_version_id
                     AND cfk.confirmation_generation = s.confirmation_generation
                     AND cfk.content_version_id = s.content_version_id
                    JOIN knowledge_versions kv
                      ON kv.id = s.source_version_id
                     AND kv.knowledge_object_id = s.source_id
                     AND kv.content_version_id = s.content_version_id
                    JOIN content_versions cv
                      ON cv.id = kv.content_version_id
                     AND cv.status = 'active'
                    JOIN evidence_objects eo
                      ON eo.id = cv.evidence_object_id
                     AND eo.status = 'active'
                    JOIN content_spans cs
                      ON cs.id = s.content_span_id
                     AND cs.content_version_id = kv.content_version_id
                     AND cs.start_offset = s.span_start
                     AND cs.end_offset = s.span_end
                    WHERE s.source_type = :source_type
                      AND s.source_id = :source_id
                      AND s.source_version_id = :source_version_id
                      AND s.id = :chunk_id
                      AND s.confirmation_generation = :confirmation_generation
                    """
                    ),
                    {
                        "source_type": candidate.source_type,
                        "source_id": candidate.source_id,
                        "source_version_id": candidate.source_version_id,
                        "chunk_id": candidate.chunk_id,
                        "confirmation_generation": candidate.confirmation_generation,
                    },
                )
                .mappings()
                .first()
            )
            if row is None:
                continue
            if row["quote_hash"] != sha256_text(str(row["text"])):
                continue

            authorized.append(
                AuthorizedChunk(
                    source_type=str(row["source_type"]),
                    source_id=str(row["source_id"]),
                    source_version_id=str(row["source_version_id"]),
                    chunk_id=str(row["chunk_id"]),
                    confirmation_generation=int(row["confirmation_generation"]),
                    generation=candidate.generation,
                    title=str(row["title"]) if row["title"] is not None else None,
                    text=str(row["text"]),
                    span_start=int(row["span_start"]),
                    span_end=int(row["span_end"]),
                    content_version_id=(
                        str(row["content_version_id"]) if row["content_version_id"] else None
                    ),
                    content_span_id=str(row["content_span_id"]) if row["content_span_id"] else None,
                    evidence_object_id=(
                        str(row["evidence_object_id"]) if row["evidence_object_id"] else None
                    ),
                    page_no=int(row["page_no"]) if row["page_no"] is not None else None,
                    section_path=str(row["section_path"]) if row["section_path"] else None,
                    quote_hash=str(row["quote_hash"]) if row["quote_hash"] else None,
                    score=candidate.score,
                    rank=candidate.rank,
                    retrievers=(candidate.retriever,),
                    component_ranks=candidate.component_ranks
                    or ((candidate.retriever, candidate.rank),),
                )
            )
        return authorized

    def seal_manifest(
        self,
        *,
        query_hash: str,
        chunks: Iterable[AuthorizedChunk],
    ) -> AuthorizedContextManifest:
        normalized = tuple(chunks)
        payload = {
            "query_hash": query_hash,
            "chunks": [
                {
                    "source_type": chunk.source_type,
                    "source_id": chunk.source_id,
                    "source_version_id": chunk.source_version_id,
                    "chunk_id": chunk.chunk_id,
                    "confirmation_generation": chunk.confirmation_generation,
                    "generation": chunk.generation,
                    "text_hash": sha256_text(chunk.text),
                    "span_start": chunk.span_start,
                    "span_end": chunk.span_end,
                    "content_version_id": chunk.content_version_id,
                    "content_span_id": chunk.content_span_id,
                    "evidence_object_id": chunk.evidence_object_id,
                    "quote_hash": chunk.quote_hash,
                    "retrievers": [retriever.value for retriever in chunk.retrievers],
                    "component_ranks": [
                        {"retriever": retriever.value, "rank": rank}
                        for retriever, rank in chunk.component_ranks
                    ],
                    "rank": chunk.rank,
                }
                for chunk in normalized
            ],
        }
        return AuthorizedContextManifest(
            manifest_id=new_id(),
            seal=ManifestSeal(sha256_json(payload)),
            query_hash=query_hash,
            chunks=normalized,
        )

    def validate_manifest(self, session: Session, manifest: AuthorizedContextManifest) -> bool:
        authorized = self.authorize_batch(
            session,
            [
                RetrievalCandidate(
                    source_type=chunk.source_type,
                    source_id=chunk.source_id,
                    source_version_id=chunk.source_version_id,
                    chunk_id=chunk.chunk_id,
                    confirmation_generation=chunk.confirmation_generation,
                    generation=chunk.generation,
                    rank=chunk.rank,
                    score=chunk.score,
                    retriever=chunk.retrievers[0],
                    component_ranks=chunk.component_ranks,
                )
                for chunk in manifest.chunks
            ],
        )
        if len(authorized) != len(manifest.chunks):
            return False
        normalized_chunks = [
            replace(
                new,
                rank=old.rank,
                score=old.score,
                retrievers=old.retrievers,
                component_ranks=old.component_ranks,
            )
            for old, new in zip(manifest.chunks, authorized, strict=True)
        ]
        resealed = self.seal_manifest(
            query_hash=manifest.query_hash,
            chunks=normalized_chunks,
        )
        return resealed.seal == manifest.seal
