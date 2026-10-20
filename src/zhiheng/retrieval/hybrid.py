from __future__ import annotations

import json
import time
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import replace
from inspect import Parameter, signature
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.orm import Session

from zhiheng.core.ids import sha256_text
from zhiheng.evolution.releases import ReleaseContext
from zhiheng.retrieval.authorization import RetrievalAuthorizer
from zhiheng.retrieval.contracts import (
    AuthorizedChunk,
    AuthorizedContextManifest,
    HybridRetrievalResult,
    RerankerPort,
    RetrievalCandidate,
    RetrievalContextChangedError,
    RetrievalFilters,
    RetrievalRunRepositoryPort,
    RetrievalSource,
)
from zhiheng.retrieval.qualification import formal_searchable_sql
from zhiheng.retrieval.repository import LexicalRetriever, RetrievalRunRepository, VectorRetriever

_MANIFEST_SEAL_ATTEMPTS = 2


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

        if filters is not None and filters.title:
            # Resolve the explicit title before applying the retrieval limit.
            # Otherwise a source with many similarly named chunks can be
            # absent from the lexical/vector top-k and become impossible to
            # answer even though it is formally searchable.
            # Resolve the title from the chunk projection so event-memory
            # sources remain eligible alongside knowledge objects.  Knowledge
            # objects still need their stricter formal qualification; other
            # source types are passed to the normal authorizer below.
            title_rows = session.execute(
                text(
                    "SELECT DISTINCT source_type, source_id, source_version_id "
                    "FROM chunks WHERE title = :title AND status = 'ready'"
                ),
                {"title": filters.title},
            ).mappings().all()
            identities: set[tuple[str, str, str]] = set()
            for row in title_rows:
                candidate_identity = (
                    str(row["source_type"]),
                    str(row["source_id"]),
                    str(row["source_version_id"]),
                )
                if candidate_identity[0] != "knowledge_object":
                    if candidate_identity[0] == "event_memory":
                        event_ready = session.execute(
                            text(
                                "SELECT 1 FROM event_memories e "
                                "JOIN event_memory_versions v "
                                "ON v.event_memory_id = e.id AND v.id = :source_version_id "
                                "AND v.version_no = (SELECT max(current_v.version_no) "
                                "FROM event_memory_versions current_v "
                                "WHERE current_v.event_memory_id = e.id) "
                                "JOIN event_memory_evidence ev ON ev.event_version_id = v.id "
                                "WHERE e.id = :source_id AND e.status = 'formal_current'"
                            ),
                            {
                                "source_id": candidate_identity[1],
                                "source_version_id": candidate_identity[2],
                            },
                        ).first()
                        if event_ready is None:
                            continue
                    identities.add(candidate_identity)
                    continue
                qualified = session.execute(
                    text(
                        "SELECT 1 FROM knowledge_objects ko "
                        f"WHERE ko.id = :source_id AND ko.current_version_id = :source_version_id "
                        f"AND {formal_searchable_sql('ko')}"
                    ),
                    {
                        "source_id": candidate_identity[1],
                        "source_version_id": candidate_identity[2],
                    },
                ).first()
                if qualified is not None:
                    identities.add(candidate_identity)
            identity: tuple[str, str, str] | None = None
            if len(identities) != 1:
                # Never answer an ambiguous or unavailable title query with a
                # mixture of same-named sources.
                candidates = []
            else:
                identity = next(iter(identities))
            rows: list[dict[str, object]] = []
            if identity is not None:
                candidates = [
                    candidate
                    for candidate in candidates
                    if (candidate.source_type, candidate.source_id, candidate.source_version_id)
                    == identity
                ]
                rows = [
                    dict(row)
                    for row in session.execute(
                        text(
                            "SELECT id, source_type, source_id, source_version_id, "
                            "confirmation_generation FROM chunks WHERE title = :title "
                            "AND source_type = :source_type AND source_id = :source_id "
                            "AND source_version_id = :source_version_id AND status = 'ready'"
                        ),
                        {
                            "title": filters.title,
                            "source_type": identity[0],
                            "source_id": identity[1],
                            "source_version_id": identity[2],
                        },
                    ).mappings()
                ]
            known = {candidate.chunk_id for candidate in candidates}
            for index, db_row in enumerate(rows, start=1):
                chunk_id = str(db_row["id"])
                if chunk_id in known:
                    continue
                candidates.append(
                    RetrievalCandidate(
                        source_type=str(db_row["source_type"]),
                        source_id=str(db_row["source_id"]),
                        source_version_id=str(db_row["source_version_id"]),
                        chunk_id=chunk_id,
                        confirmation_generation=int(str(db_row["confirmation_generation"])),
                        generation=None,
                        rank=index,
                        score=0.0,
                        retriever=RetrievalSource.LEXICAL,
                        component_ranks=((RetrievalSource.LEXICAL, index),),
                    )
                )
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
        # Retrieval reads are not one snapshot: a Worker commit can land between
        # sealing and persisting the run. Reseal from a fresh read once before
        # reporting a retryable context change. Run and citation audit rows are
        # append-only, so a stale run is rolled back to its savepoint, never deleted.
        for _attempt in range(_MANIFEST_SEAL_ATTEMPTS):
            manifest = self._reseal(session, query, authorized)
            if not self._authorizer.validate_manifest(session, manifest):
                continue
            session.execute(text("SAVEPOINT hybrid_manifest_run"))
            run_id = self._run_repository.create_run(
                session,
                raw_query=query,
                route="hybrid",
                started_at=started_at,
                results=manifest.chunks,
                strategy_release_id=strategy_release_id,
                manifest_hash=manifest.seal,
            )
            if self._authorizer.validate_manifest(session, manifest):
                return HybridRetrievalResult(
                    manifest=manifest,
                    degraded_reasons=tuple(degraded),
                    run_id=run_id,
                )
            session.execute(text("ROLLBACK TO SAVEPOINT hybrid_manifest_run"))
            session.execute(text("RELEASE SAVEPOINT hybrid_manifest_run"))
        raise RetrievalContextChangedError(
            "authorized context manifest changed before response"
        )

    def _reseal(
        self,
        session: Session,
        query: str,
        authorized: Sequence[AuthorizedChunk],
    ) -> AuthorizedContextManifest:
        by_tuple = {chunk.authorization_tuple: chunk for chunk in authorized}
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
        return self._authorizer.seal_manifest(
            query_hash=sha256_text(query),
            chunks=[
                replace(
                    chunk,
                    rank=index,
                    retrievers=by_tuple[chunk.authorization_tuple].retrievers,
                    component_ranks=by_tuple[chunk.authorization_tuple].component_ranks,
                )
                for index, chunk in enumerate(resealed_chunks, start=1)
            ],
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
    knowledge_ids = sorted(
        {str(chunk.source_id) for chunk in chunks if chunk.source_type == "knowledge_object"}
    )
    event_pairs = sorted(
        {
            (str(chunk.source_id), str(chunk.source_version_id))
            for chunk in chunks
            if chunk.source_type == "event_memory"
        }
    )
    event_ids = sorted({source_id for source_id, _version_id in event_pairs})
    conditions = ["ko.id IN (SELECT value FROM json_each(:retrieval_ids))"]
    params: dict[str, object] = {
        "retrieval_ids": json.dumps(knowledge_ids, ensure_ascii=False)
    }
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

    allowed_knowledge = (
        {
            str(row[0])
            for row in session.execute(
                text("SELECT ko.id FROM knowledge_objects ko WHERE " + " AND ".join(conditions)),
                {**params, "retrieval_ids": json.dumps(knowledge_ids, ensure_ascii=False)},
            )
        }
        if knowledge_ids
        else set()
    )
    allowed_events: set[tuple[str, str]] = set()
    if event_ids and filters.domain_id is None:
        event_conditions = ["e.id IN (SELECT value FROM json_each(:event_ids))"]
        event_params: dict[str, object] = {
            "event_ids": json.dumps(event_ids, ensure_ascii=False)
        }
        if filters.title is not None:
            event_conditions.append("v.title = :event_title")
            event_params["event_title"] = filters.title
        for name, operator, value in (
            ("created_at", ">=", filters.created_from),
            ("created_at", "<=", filters.created_to),
            ("updated_at", ">=", filters.updated_from),
            ("updated_at", "<=", filters.updated_to),
        ):
            if value is not None:
                key = f"event_{name}_{operator[0]}"
                event_conditions.append(f"e.{name} {operator} :{key}")
                event_params[key] = value.isoformat()
        allowed_events = {
            (str(row[0]), str(row[1]))
            for row in session.execute(
                text(
                    "SELECT e.id, v.id FROM event_memories e "
                    "JOIN event_memory_versions v ON v.event_memory_id = e.id "
                    "AND v.version_no = (SELECT max(current_v.version_no) "
                    "FROM event_memory_versions current_v "
                    "WHERE current_v.event_memory_id = e.id) "
                    "WHERE " + " AND ".join(event_conditions)
                ),
                event_params,
            )
        }
    return [
        chunk
        for chunk in chunks
        if (chunk.source_type == "knowledge_object" and chunk.source_id in allowed_knowledge)
        or (
            chunk.source_type == "event_memory"
            and (chunk.source_id, chunk.source_version_id) in allowed_events
        )
    ]
