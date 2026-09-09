"""Exercise the real answer dispatcher with code-owned candidate routing parameters.

The migrated serving baseline remains intact. This offline probe injects only
the candidate's declared router knob, not a forged approved release or score.
"""

from typing import Any, Never

from sqlalchemy.orm import Session

from zhiheng.evolution.releases import ReleaseContext
from zhiheng.memory.context import MemoryContextSnapshot
from zhiheng.query.agentic import BoundedAgenticRagService
from zhiheng.query.contracts import ReleaseBehaviorConfig, StructuredLookupResult
from zhiheng.query.service import QueryAnswerService
from zhiheng.retrieval import QueryRoute, QueryRouter, StructuredLookupService
from zhiheng.retrieval.contracts import RouteDecision


class _CandidateRouter(QueryRouter):
    def __init__(self, override: QueryRoute | None) -> None:
        self._override = override

    def route(
        self, query: str, *, selector: str | None = None, intent: str | None = None,
        route_override: QueryRoute | None = None,
    ) -> RouteDecision:
        return super().route(
            query, selector=selector, intent=intent, route_override=self._override,
        )


class _UnexpectedRagCall(RuntimeError):
    pass


class _CountingRagBoundary(BoundedAgenticRagService):
    def __init__(self) -> None:
        # This port cannot dispatch a retrieval/model request; any entry fails.
        self.calls = 0

    def answer(
        self, session: Session, query: str, *, route: QueryRoute,
        release_context: ReleaseContext | None = None,
        behavior: ReleaseBehaviorConfig | None = None,
        memory_context: MemoryContextSnapshot | None = None,
    ) -> Never:
        self.calls += 1
        raise _UnexpectedRagCall("structured retention entered the RAG boundary")


def execute_memory_answer_probe(
    session: Session, *, route_override: QueryRoute | None,
) -> dict[str, Any]:
    rag = _CountingRagBoundary()
    service = QueryAnswerService(
        router=_CandidateRouter(route_override), structured_lookup=StructuredLookupService(),
        rag=rag,
    )
    responses: list[StructuredLookupResult] = []
    rejected = False
    try:
        for query in ("memory:goal.fixed", "memory:goal.unconfirmed"):
            result = service.answer(session, query)
            if isinstance(result, StructuredLookupResult):
                responses.append(result)
    except _UnexpectedRagCall:
        rejected = True
    return {
        "query_dispatch_count": len(responses),
        "rag_entry_calls": rag.calls,
        "rag_entry_rejected": rejected,
        "formal_answer_rows": list(responses[0].rows) if responses else [],
        "candidate_answer_rows": list(responses[1].rows) if len(responses) == 2 else [],
        "serving_release_available": (
            len(responses) == 2
            and all(response.release_context is not None for response in responses)
            and all(not response.release_degraded_reasons for response in responses)
        ),
        "execution_profile": "real_answer_dispatch_offline_candidate_router",
    }
