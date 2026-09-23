from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, NewType, Protocol

ManifestSeal = NewType("ManifestSeal", str)


class RetrievalSource(StrEnum):
    STRUCTURED = "structured"
    LEXICAL = "lexical"
    VECTOR = "vector"


@dataclass(frozen=True)
class RetrievalFilters:
    """Structured constraints applied to both lexical and vector candidates."""

    domain_id: str | None = None
    created_from: datetime | None = None
    created_to: datetime | None = None
    updated_from: datetime | None = None
    updated_to: datetime | None = None

    def is_empty(self) -> bool:
        return all(
            value is None
            for value in (
                self.domain_id,
                self.created_from,
                self.created_to,
                self.updated_from,
                self.updated_to,
            )
        )


class QueryRoute(StrEnum):
    STRUCTURED = "structured"
    HYBRID = "hybrid"
    AGENTIC = "agentic"


@dataclass(frozen=True)
class RetrievalCandidate:
    source_type: str
    source_id: str
    source_version_id: str
    chunk_id: str
    confirmation_generation: int
    generation: str | None
    rank: int
    score: float
    retriever: RetrievalSource
    component_ranks: tuple[tuple[RetrievalSource, int], ...] = field(default_factory=tuple)

    @property
    def authorization_tuple(self) -> tuple[str, str, str, str, int, str | None]:
        return (
            self.source_type,
            self.source_id,
            self.source_version_id,
            self.chunk_id,
            self.confirmation_generation,
            self.generation,
        )


@dataclass(frozen=True)
class AuthorizedChunk:
    source_type: str
    source_id: str
    source_version_id: str
    chunk_id: str
    confirmation_generation: int
    generation: str | None
    title: str | None
    text: str
    span_start: int
    span_end: int
    content_version_id: str | None
    content_span_id: str | None
    evidence_object_id: str | None
    page_no: int | None
    section_path: str | None
    quote_hash: str | None
    score: float
    rank: int
    retrievers: tuple[RetrievalSource, ...]
    component_ranks: tuple[tuple[RetrievalSource, int], ...] = field(default_factory=tuple)

    @property
    def authorization_tuple(self) -> tuple[str, str, str, str, int, str | None]:
        return (
            self.source_type,
            self.source_id,
            self.source_version_id,
            self.chunk_id,
            self.confirmation_generation,
            self.generation,
        )


@dataclass(frozen=True)
class AuthorizedContextManifest:
    manifest_id: str
    seal: ManifestSeal
    query_hash: str
    chunks: tuple[AuthorizedChunk, ...]


@dataclass(frozen=True)
class Citation:
    citation_id: str
    source_type: str
    source_id: str
    source_version_id: str
    chunk_id: str
    evidence_object_id: str | None
    content_version_id: str | None
    content_span_id: str | None
    content_span: tuple[int, int]
    offset: tuple[int, int]
    page_no: int | None
    section_path: str | None
    quote_hash: str


@dataclass(frozen=True)
class HybridRetrievalResult:
    manifest: AuthorizedContextManifest
    degraded_reasons: tuple[str, ...]
    run_id: str


class RerankerPort(Protocol):
    def rerank(
        self,
        session: Any,
        query: str,
        candidates: Sequence[RetrievalCandidate],
        *,
        limit: int,
    ) -> Sequence[RetrievalCandidate]: ...


class RetrievalRunRepositoryPort(Protocol):
    def create_run(
        self,
        session: Any,
        *,
        raw_query: str,
        route: str,
        started_at: float,
        results: Sequence[AuthorizedChunk],
        strategy_release_id: str,
        manifest_hash: str | None = None,
    ) -> str: ...


@dataclass(frozen=True)
class RouteDecision:
    route: QueryRoute
    reason_code: str
    structured_selector: str | None = None


class VectorSearchPort(Protocol):
    def search(
        self,
        query_embedding: Sequence[float],
        *,
        generation_id: str,
        limit: int,
    ) -> Sequence[RetrievalCandidate]: ...
