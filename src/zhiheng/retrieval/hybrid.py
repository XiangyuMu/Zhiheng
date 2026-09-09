from __future__ import annotations

import time
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import replace
from typing import Protocol

from sqlalchemy.orm import Session

from zhiheng.core.ids import sha256_text
from zhiheng.evolution.releases import ReleaseContext
from zhiheng.retrieval.authorization import RetrievalAuthorizer
from zhiheng.retrieval.contracts import (
    HybridRetrievalResult,
    RetrievalCandidate,
    RetrievalRunRepositoryPort,
    RetrievalSource,
)
from zhiheng.retrieval.repository import LexicalRetriever, RetrievalRunRepository, VectorRetriever


class LexicalSearchPort(Protocol):
    def search(self, session: Session, query: str, *, limit: int) -> list[RetrievalCandidate]: ...


class VectorSearchPort(Protocol):
    def search(
        self,
        session: Session,
        query_embedding: Sequence[float],
        *,
        generation_id: str,
        limit: int,
    ) -> list[RetrievalCandidate]: ...


class HybridRetriever:
    def __init__(
        self,
        *,
        lexical: LexicalSearchPort | None = None,
        vector: VectorSearchPort | None = None,
        authorizer: RetrievalAuthorizer | None = None,
        run_repository: RetrievalRunRepositoryPort | None = None,
        rrf_k: int = 60,
    ) -> None:
        self._lexical = lexical or LexicalRetriever()
        self._vector = vector or VectorRetriever()
        self._authorizer = authorizer or RetrievalAuthorizer()
        self._run_repository = run_repository or RetrievalRunRepository()
        self._rrf_k = rrf_k

    def search(
        self,
        session: Session,
        query: str,
        *,
        query_embedding: Sequence[float] | None = None,
        vector_generation_id: str | None = None,
        limit: int = 10,
        overfetch_factor: int = 4,
        release_context: ReleaseContext,
        rrf_k: int | None = None,
    ) -> HybridRetrievalResult:
        return self._search(
            session,
            query,
            query_embedding=query_embedding,
            vector_generation_id=vector_generation_id,
            limit=limit,
            overfetch_factor=overfetch_factor,
            strategy_release_id=release_context.release_id,
            rrf_k=rrf_k,
        )

    def search_offline(
        self,
        session: Session,
        query: str,
        *,
        query_embedding: Sequence[float] | None = None,
        vector_generation_id: str | None = None,
        limit: int = 10,
        overfetch_factor: int = 4,
        rrf_k: int | None = None,
    ) -> HybridRetrievalResult:
        """Explicit non-serving entry point for fixed-set evaluation and tests."""
        return self._search(
            session,
            query,
            query_embedding=query_embedding,
            vector_generation_id=vector_generation_id,
            limit=limit,
            overfetch_factor=overfetch_factor,
            strategy_release_id="offline-evaluation",
            rrf_k=rrf_k,
        )

    def _search(
        self,
        session: Session,
        query: str,
        *,
        query_embedding: Sequence[float] | None,
        vector_generation_id: str | None,
        limit: int,
        overfetch_factor: int,
        strategy_release_id: str,
        rrf_k: int | None,
    ) -> HybridRetrievalResult:
        started_at = time.monotonic()
        overfetch_limit = max(limit, limit * overfetch_factor)
        degraded: list[str] = []
        candidates: list[RetrievalCandidate] = []

        try:
            candidates.extend(self._lexical.search(session, query, limit=overfetch_limit))
        except ValueError:
            degraded.append("lexical_no_indexable_tokens")

        if query_embedding is None or vector_generation_id is None:
            degraded.append("vector_unavailable")
        else:
            try:
                vector_hits = self._vector.search(
                    session,
                    query_embedding,
                    generation_id=vector_generation_id,
                    limit=overfetch_limit,
                )
            except ValueError:
                vector_hits = []
                degraded.append("vector_mismatch")
            except RuntimeError:
                vector_hits = []
                degraded.append("vector_unavailable")
            if not vector_hits and not any(
                reason in degraded for reason in {"vector_mismatch", "vector_unavailable"}
            ):
                degraded.append("vector_empty")
            candidates.extend(vector_hits)

        fused = self._rrf(candidates, rrf_k=rrf_k)[:overfetch_limit]
        retrievers_by_tuple = {
            candidate.authorization_tuple: tuple(
                retriever for retriever, _rank in candidate.component_ranks
            )
            for candidate in fused
        }
        ranks_by_tuple = {
            candidate.authorization_tuple: candidate.component_ranks for candidate in fused
        }
        authorized = [
            replace(
                chunk,
                retrievers=retrievers_by_tuple.get(chunk.authorization_tuple, chunk.retrievers),
                component_ranks=ranks_by_tuple.get(chunk.authorization_tuple, ()),
            )
            for chunk in self._authorizer.authorize_batch(session, fused)
        ]
        authorized = sorted(authorized, key=lambda chunk: (-chunk.score, chunk.chunk_id))[:limit]
        resealed_chunks = self._authorizer.authorize_batch(
            session,
            [
                RetrievalCandidate(
                    source_type=chunk.source_type,
                    source_id=chunk.source_id,
                    source_version_id=chunk.source_version_id,
                    chunk_id=chunk.chunk_id,
                    confirmation_generation=chunk.confirmation_generation,
                    generation=chunk.generation,
                    rank=index,
                    score=chunk.score,
                    retriever=chunk.retrievers[0],
                    component_ranks=chunk.component_ranks,
                )
                for index, chunk in enumerate(authorized, start=1)
            ],
        )
        resealed_chunks = [
            replace(
                chunk,
                rank=index,
                retrievers=authorized[index - 1].retrievers,
                component_ranks=authorized[index - 1].component_ranks,
            )
            for index, chunk in enumerate(resealed_chunks, start=1)
        ]
        manifest = self._authorizer.seal_manifest(
            query_hash=sha256_text(query),
            chunks=resealed_chunks,
        )
        if not self._authorizer.validate_manifest(session, manifest):
            raise ValueError("authorized context manifest failed validation")

        run_id = self._run_repository.create_run(
            session,
            raw_query=query,
            route="hybrid",
            started_at=started_at,
            results=manifest.chunks,
            strategy_release_id=strategy_release_id,
            manifest_hash=manifest.seal,
        )
        if not self._authorizer.validate_manifest(session, manifest):
            raise ValueError("authorized context manifest changed before response")
        return HybridRetrievalResult(
            manifest=manifest,
            degraded_reasons=tuple(degraded),
            run_id=run_id,
        )

    def _rrf(
        self,
        candidates: Sequence[RetrievalCandidate],
        *,
        rrf_k: int | None = None,
    ) -> list[RetrievalCandidate]:
        selected_rrf_k = self._rrf_k if rrf_k is None else max(1, rrf_k)
        scores: dict[tuple[str, str, str, str, int], float] = defaultdict(float)
        first: dict[tuple[str, str, str, str, int], RetrievalCandidate] = {}
        generations: dict[tuple[str, str, str, str, int], str | None] = {}
        component_ranks: dict[
            tuple[str, str, str, str, int],
            dict[RetrievalSource, int],
        ] = defaultdict(dict)
        retrievers: dict[
            tuple[str, str, str, str, int],
            set[RetrievalSource],
        ] = defaultdict(set)
        for candidate in candidates:
            key = (
                candidate.source_type,
                candidate.source_id,
                candidate.source_version_id,
                candidate.chunk_id,
                candidate.confirmation_generation,
            )
            scores[key] += 1.0 / (selected_rrf_k + candidate.rank)
            first.setdefault(key, candidate)
            if candidate.generation is not None:
                generations[key] = candidate.generation
            else:
                generations.setdefault(key, None)
            retrievers[key].add(candidate.retriever)
            component_ranks[key].setdefault(candidate.retriever, candidate.rank)

        ranked_keys = sorted(scores, key=lambda key: (-scores[key], first[key].chunk_id))
        return [
            RetrievalCandidate(
                source_type=first[key].source_type,
                source_id=first[key].source_id,
                source_version_id=first[key].source_version_id,
                chunk_id=first[key].chunk_id,
                confirmation_generation=first[key].confirmation_generation,
                generation=generations[key],
                rank=index,
                score=scores[key],
                retriever=sorted(retrievers[key], key=lambda item: item.value)[0],
                component_ranks=tuple(
                    sorted(component_ranks[key].items(), key=lambda item: item[0].value)
                ),
            )
            for index, key in enumerate(ranked_keys, start=1)
        ]
