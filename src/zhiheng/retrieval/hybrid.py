from __future__ import annotations

import json
import time
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import replace
from inspect import Parameter, signature
from typing import Protocol

from sqlalchemy.orm import Session

from zhiheng.core.ids import sha256_text
from zhiheng.evolution.releases import ReleaseContext
from zhiheng.retrieval.authorization import RetrievalAuthorizer
from zhiheng.retrieval.contracts import (
    AuthorizedChunk,
    HybridRetrievalResult,
    RerankerPort,
    RetrievalCandidate,
    RetrievalFilters,
    RetrievalRunRepositoryPort,
    RetrievalSource,
)
from zhiheng.retrieval.repository import LexicalRetriever, RetrievalRunRepository, VectorRetriever


class LexicalSearchPort(Protocol):
    def search(
        self,
        session: Session,
        query: str,
        *,
        limit: int,
        filters: RetrievalFilters | None = None,
    ) -> list[RetrievalCandidate]: ...


class VectorSearchPort(Protocol):
    def search(
        self,
        session: Session,
        query_embedding: Sequence[float],
        *,
        generation_id: str,
        limit: int,
        filters: RetrievalFilters | None = None,
    ) -> list[RetrievalCandidate]: ...


class DeterministicReranker:
    """Stable final pass that rewards multi-retriever agreement.

    A model reranker can be injected later without changing the retrieval
    contract. The default remains local, deterministic, and explainable.
    """

    def rerank(
        self,
        session: Session,
        query: str,
        candidates: Sequence[RetrievalCandidate],
        *,
        limit: int,
    ) -> Sequence[RetrievalCandidate]:
        del session, query
        ranked = sorted(
            candidates,
            key=lambda candidate: (
                -candidate.score,
                -len(candidate.component_ranks),
                candidate.chunk_id,
            ),
        )
        return [
            replace(candidate, rank=index)
            for index, candidate in enumerate(ranked[:limit], start=1)
        ]


class HybridRetriever:
    def __init__(
        self,
        *,
        lexical: LexicalSearchPort | None = None,
        vector: VectorSearchPort | None = None,
        authorizer: RetrievalAuthorizer | None = None,
        run_repository: RetrievalRunRepositoryPort | None = None,
        reranker: RerankerPort | None = None,
        rrf_k: int = 60,
    ) -> None:
        self._lexical = lexical or LexicalRetriever()
        self._vector = vector or VectorRetriever()
        self._authorizer = authorizer or RetrievalAuthorizer()
        self._run_repository = run_repository or RetrievalRunRepository()
        self._reranker = reranker or DeterministicReranker()
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
        filters: RetrievalFilters | None = None,
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
            filters=filters,
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
        filters: RetrievalFilters | None = None,
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
            filters=filters,
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
        filters: RetrievalFilters | None,
    ) -> HybridRetrievalResult:
        started_at = time.monotonic()
        overfetch_limit = max(limit, limit * overfetch_factor)
        degraded: list[str] = []
        candidates: list[RetrievalCandidate] = []

        try:
            candidates.extend(
                _search_with_optional_filters(
                    self._lexical,
                    session,
                    query,
                    limit=overfetch_limit,
                    filters=filters,
                )
            )
        except ValueError:
            degraded.append("lexical_no_indexable_tokens")

        if query_embedding is None or vector_generation_id is None:
            degraded.append("vector_unavailable")
        else:
            try:
                vector_hits = _vector_search_with_optional_filters(
                    self._vector,
                    session,
                    query_embedding,
                    generation_id=vector_generation_id,
                    limit=overfetch_limit,
                    filters=filters,
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
        try:
            reranked = self._reranker.rerank(
                session,
                query,
                fused,
                limit=overfetch_limit,
            )
            fused = list(reranked)[:overfetch_limit]
        except (RuntimeError, ValueError, TypeError):
            degraded.append("reranker_unavailable")
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
        authorized = _filter_authorized_chunks(session, authorized, filters)
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


def _supports_keyword(parameters: Mapping[str, Parameter], name: str) -> bool:
    return name in parameters or any(
        parameter.kind is Parameter.VAR_KEYWORD for parameter in parameters.values()
    )


def _search_with_optional_filters(
    retriever: LexicalSearchPort,
    session: Session,
    query: str,
    *,
    limit: int,
    filters: RetrievalFilters | None,
) -> list[RetrievalCandidate]:
    parameters = signature(retriever.search).parameters
    kwargs: dict[str, object] = {"limit": limit}
    if _supports_keyword(parameters, "filters"):
        kwargs["filters"] = filters
    return retriever.search(session, query, **kwargs)  # type: ignore[arg-type]


def _vector_search_with_optional_filters(
    retriever: VectorSearchPort,
    session: Session,
    query_embedding: Sequence[float],
    *,
    generation_id: str,
    limit: int,
    filters: RetrievalFilters | None,
) -> list[RetrievalCandidate]:
    parameters = signature(retriever.search).parameters
    kwargs: dict[str, object] = {"generation_id": generation_id, "limit": limit}
    if _supports_keyword(parameters, "filters"):
        kwargs["filters"] = filters
    return retriever.search(session, query_embedding, **kwargs)  # type: ignore[arg-type]


def _filter_authorized_chunks(
    session: Session,
    chunks: Sequence[AuthorizedChunk],
    filters: RetrievalFilters | None,
) -> list[AuthorizedChunk]:
    if not chunks or filters is None or filters.is_empty():
        return list(chunks)
    ids = sorted({str(chunk.source_id) for chunk in chunks})
    conditions = ["ko.id IN (SELECT value FROM json_each(:retrieval_ids))"]
    params: dict[str, object] = {"retrieval_ids": json.dumps(ids, ensure_ascii=False)}
    if filters.domain_id is not None:
        conditions.append("ko.primary_domain_id = :retrieval_domain_id")
        params["retrieval_domain_id"] = filters.domain_id
    for name, operator, value in (
        ("created_at", ">=", filters.created_from),
        ("created_at", "<=", filters.created_to),
        ("updated_at", ">=", filters.updated_from),
        ("updated_at", "<=", filters.updated_to),
    ):
        if value is not None:
            key = f"retrieval_{name}_{operator[0]}"
            conditions.append(f"ko.{name} {operator} :{key}")
            params[key] = value.isoformat()
    from sqlalchemy import text

    allowed = {
        str(row[0])
        for row in session.execute(
            text("SELECT ko.id FROM knowledge_objects ko WHERE " + " AND ".join(conditions)),
            params,
        )
    }
    return [chunk for chunk in chunks if chunk.source_id in allowed]
