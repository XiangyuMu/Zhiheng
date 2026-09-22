from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from sqlalchemy.orm import Session

from zhiheng.evolution.releases import ReleaseContext
from zhiheng.memory.context import MemoryContextSnapshot
from zhiheng.retrieval.contracts import (
    AuthorizedContextManifest,
    Citation,
    HybridRetrievalResult,
    QueryRoute,
)


class StopReason(StrEnum):
    COMPLETED = "completed"
    EVIDENCE_ONLY = "evidence_only"
    PRIVACY_DENIED = "privacy_denied"
    MODEL_FAILED = "model_failed"
    INVALID_MODEL_OUTPUT = "invalid_model_output"
    CITATION_VALIDATION_FAILED = "citation_validation_failed"
    BUDGET_EXHAUSTED = "budget_exhausted"
    REPEATED_QUERY = "repeated_query"
    NO_NEW_AUTHORIZED_EVIDENCE = "no_new_authorized_evidence"
    RELEASE_UNAVAILABLE = "release_unavailable"
    MEMORY_CONTEXT_CHANGED = "memory_context_changed"


@dataclass(frozen=True)
class BudgetUsage:
    rounds: int = 0
    subqueries: int = 0
    retrieval_calls: int = 0
    model_calls: int = 0
    context_chunks: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    wall_clock_ms: int = 0


@dataclass(frozen=True)
class AgenticBudget:
    max_rounds: int = 3
    max_subqueries: int = 6
    max_retrieval_calls: int = 8
    max_model_calls: int = 3
    max_context_chunks: int = 16
    max_input_tokens: int = 12_000
    max_output_tokens: int = 4_000
    max_wall_clock_ms: int = 10_000

    def clamped(self) -> AgenticBudget:
        return AgenticBudget(
            max_rounds=min(self.max_rounds, 3),
            max_subqueries=min(self.max_subqueries, 6),
            max_retrieval_calls=min(self.max_retrieval_calls, 8),
            max_model_calls=min(self.max_model_calls, 3),
            max_context_chunks=min(self.max_context_chunks, 16),
            max_input_tokens=max(0, self.max_input_tokens),
            max_output_tokens=max(0, self.max_output_tokens),
            max_wall_clock_ms=max(0, self.max_wall_clock_ms),
        )


@dataclass(frozen=True)
class AnswerClaim:
    text: str
    citation_ids: tuple[str, ...]


@dataclass(frozen=True)
class PersonalizationRef:
    formal_memory_id: str
    formal_version_id: str
    confirmation_generation: int
    state_key: str


@dataclass(frozen=True)
class MemoryImpact:
    formal_memory_id: str
    formal_version_id: str
    state_key: str
    effect_type: str
    explanation: str
    used: bool = True


@dataclass(frozen=True)
class AnswerEnvelope:
    answer: str
    claims: tuple[AnswerClaim, ...]
    citations: tuple[Citation, ...]
    conflicts: tuple[str, ...]
    assumptions: tuple[str, ...]
    insufficiencies: tuple[str, ...]
    route: QueryRoute
    stop_reason: StopReason
    budget_usage: BudgetUsage
    release_context: ReleaseContext | None = None
    release_degraded_reasons: tuple[str, ...] = ()
    retrieval_run_ids: tuple[str, ...] = ()
    trajectory_id: str | None = None
    personalization_refs: tuple[PersonalizationRef, ...] = ()
    memory_context_digest: str | None = None
    memory_impacts: tuple[MemoryImpact, ...] = ()


@dataclass(frozen=True)
class GeneratedAnswer:
    answer: str
    claims: tuple[AnswerClaim, ...]
    conflicts: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()
    insufficiencies: tuple[str, ...] = ()
    output_tokens: int = 0
    personalization_refs: tuple[PersonalizationRef, ...] = ()


class HybridRetrievalPort(Protocol):
    def search(
        self,
        session: Session,
        query: str,
        *,
        release_context: ReleaseContext,
        query_embedding: Sequence[float] | None = None,
        vector_generation_id: str | None = None,
        limit: int = 10,
        overfetch_factor: int = 4,
        rrf_k: int | None = None,
    ) -> HybridRetrievalResult: ...


class StructuredLookupPort(Protocol):
    def lookup(self, session: Session, *, selector: str, value: str) -> list[dict[str, object]]: ...


class EvidenceVerifierPort(Protocol):
    def validate_manifest(self, session: Session, manifest: AuthorizedContextManifest) -> bool: ...


class AnswerModelPort(Protocol):
    def generate_answer(
        self,
        *,
        query: str,
        manifest: AuthorizedContextManifest,
        citations: Sequence[Citation],
        max_output_tokens: int | None = None,
        memory_context: MemoryContextSnapshot | None = None,
        conversation_context: Sequence[Mapping[str, str]] | None = None,
    ) -> GeneratedAnswer: ...


@dataclass(frozen=True)
class StructuredLookupResult:
    answer: str
    rows: tuple[Mapping[str, object], ...]
    route: QueryRoute = QueryRoute.STRUCTURED
    stop_reason: StopReason = StopReason.COMPLETED
    budget_usage: BudgetUsage = field(default_factory=BudgetUsage)
    release_context: ReleaseContext | None = None
    release_degraded_reasons: tuple[str, ...] = ()
    trajectory_id: str | None = None


@dataclass(frozen=True)
class ReleaseBehaviorConfig:
    route_override: QueryRoute | None = None
    rrf_k: int | None = None
    overfetch_factor: int = 4


@dataclass(frozen=True)
class ResolvedRelease:
    context: ReleaseContext | None
    behavior: ReleaseBehaviorConfig
    degraded_reasons: tuple[str, ...] = ()

    @property
    def available(self) -> bool:
        return self.context is not None and not self.degraded_reasons


@dataclass(frozen=True)
class ReleasePreview:
    release_id: str | None = None
    synthetic_worker: bool = False
    assignment_scope: Mapping[str, Any] = field(default_factory=dict)
