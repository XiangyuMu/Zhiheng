"""Current-authority proofs for cached citations; cache data never grants access."""

from collections.abc import Sequence
from dataclasses import asdict, replace

from sqlalchemy import text
from sqlalchemy.orm import Session

from zhiheng.core.ids import sha256_json
from zhiheng.retrieval.authorization import RetrievalAuthorizer
from zhiheng.retrieval.citations import CitationBuilder
from zhiheng.retrieval.contracts import Citation, RetrievalCandidate, RetrievalSource


class CitationReplayValidator:
    def digest(self, session: Session, citations: Sequence[Citation]) -> str | None:
        authorizer = RetrievalAuthorizer()
        proofs = []
        for citation in citations:
            generation = session.execute(text(
                "SELECT confirmation_generation FROM serving_chunks WHERE id = :id"
            ), {"id": citation.chunk_id}).scalar_one_or_none()
            if generation is None:
                return None
            chunks = authorizer.authorize_batch(session, [RetrievalCandidate(
                source_type=citation.source_type, source_id=citation.source_id,
                source_version_id=citation.source_version_id, chunk_id=citation.chunk_id,
                confirmation_generation=int(generation), generation=None,
                rank=1, score=1.0, retriever=RetrievalSource.LEXICAL,
            )])
            if len(chunks) != 1:
                return None
            manifest = authorizer.seal_manifest(query_hash="cached-citation", chunks=chunks)
            try:
                current = CitationBuilder().build(
                    manifest, chunk_id=citation.chunk_id,
                    start_offset=citation.offset[0], end_offset=citation.offset[1],
                )
            except ValueError:
                return None
            if replace(current, citation_id=citation.citation_id) != citation:
                return None
            proofs.append({"citation": asdict(citation), "generation": int(generation)})
        return sha256_json({"citations": proofs})
