from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from tests.query_session_stub import QuerySessionStub
from zhiheng.core.ids import sha256_text
from zhiheng.decisions import DecisionOption, DecisionRequest, DecisionSupportService
from zhiheng.gaps import FormalGoalRef, GapReasonCode, KnowledgeGapService
from zhiheng.memory.context import MemoryContextSnapshot
from zhiheng.query import (
    AgenticBudget,
    AnswerClaim,
    BoundedAgenticRagService,
    GeneratedAnswer,
    StopReason,
)
from zhiheng.query.service import QueryAnswerService, route_query
from zhiheng.retrieval import QueryRoute, QueryRouter, RetrievalAuthorizer, RetrievalSource
from zhiheng.retrieval.contracts import (
    AuthorizedChunk,
    AuthorizedContextManifest,
    Citation,
    HybridRetrievalResult,
)


def _chunk(chunk_id: str = "chunk-1", text: str = "正式知识库证据。") -> AuthorizedChunk:
    return AuthorizedChunk(
        source_type="knowledge_object",
        source_id="knowledge-1",
        source_version_id="version-1",
        chunk_id=chunk_id,
        confirmation_generation=1,
        generation=None,
        title="正式证据",
        text=text,
        span_start=0,
        span_end=len(text),
        content_version_id="content-1",
        content_span_id="span-1",
        evidence_object_id="evidence-1",
        page_no=None,
        section_path=None,
        quote_hash=sha256_text(text),
        score=1.0,
        rank=1,
        retrievers=(RetrievalSource.LEXICAL,),
        component_ranks=((RetrievalSource.LEXICAL, 1),),
    )


def _manifest(chunk: AuthorizedChunk | None = None) -> AuthorizedContextManifest:
    return RetrievalAuthorizer().seal_manifest(query_hash="query-hash", chunks=[chunk or _chunk()])


class _Lookup:
    def __init__(self) -> None:
        self.calls = 0

    def lookup(self, session: Any, *, selector: str, value: str) -> list[dict[str, object]]:
        self.calls += 1
        return [{"selector": selector, "value": value}]


class _Verifier:
    def validate_manifest(self, session: Any, manifest: AuthorizedContextManifest) -> bool:
        return True


class _Model:
    def __init__(
        self,
        *,
        answer: str = "有证据支持的回答",
        output_tokens: int = 3,
    ) -> None:
        self.answer = answer
        self.output_tokens = output_tokens
        self.calls = 0
        self.max_output_tokens_seen: list[int | None] = []

    def generate_answer(
        self,
        *,
        query: str,
        manifest: AuthorizedContextManifest,
        citations: Sequence[Citation],
        max_output_tokens: int | None = None,
        memory_context: MemoryContextSnapshot | None = None,
    ) -> GeneratedAnswer:
        del memory_context
        self.calls += 1
        self.max_output_tokens_seen.append(max_output_tokens)
        return GeneratedAnswer(
            answer=self.answer,
            claims=(AnswerClaim("正式知识库证据", (citations[0].citation_id,)),),
            output_tokens=self.output_tokens,
        )


class _Hybrid:
    def __init__(self, manifests: Sequence[AuthorizedContextManifest]) -> None:
        self._manifests = tuple(manifests)
        self.calls: list[tuple[str, int]] = []

    def search(
        self,
        session: Any,
        query: str,
        *,
        release_context: object | None = None,
        query_embedding: object | None = None,
        vector_generation_id: str | None = None,
        limit: int = 10,
        overfetch_factor: int = 4,
        rrf_k: int | None = None,
    ) -> HybridRetrievalResult:
        self.calls.append((query, limit))
        manifest = self._manifests[min(len(self.calls) - 1, len(self._manifests) - 1)]
        return HybridRetrievalResult(manifest=manifest, degraded_reasons=(), run_id="run-1")


class _BudgetMaxPlanner:
    def plan_subqueries(
        self,
        *,
        query: str,
        round_no: int,
        known_evidence_hashes: set[str],
        max_subqueries: int,
    ) -> tuple[str, ...]:
        return tuple(f"{query}-{round_no}-{index}" for index in range(max_subqueries))


class _NoNewEvidencePlanner:
    def plan_subqueries(
        self,
        *,
        query: str,
        round_no: int,
        known_evidence_hashes: set[str],
        max_subqueries: int,
    ) -> tuple[str, ...]:
        return (f"{query}-{round_no}",)


class _RepeatPlanner:
    def plan_subqueries(
        self,
        *,
        query: str,
        round_no: int,
        known_evidence_hashes: set[str],
        max_subqueries: int,
    ) -> tuple[str, ...]:
        return ("repeat", "repeat")


@dataclass
class _Result:
    value: object | None = None

    def first(self) -> object | None:
        return self.value


class _RecordingSession:
    def __init__(self, *, current_goal: bool = False) -> None:
        self.current_goal = current_goal
        self.statements: list[str] = []

    def in_transaction(self) -> bool:
        return False

    def execute(self, statement: object, params: object | None = None) -> _Result:
        text_value = str(statement)
        self.statements.append(text_value)
        if "FROM current_formal_memory" in text_value and self.current_goal:
            return _Result((1,))
        return _Result()


def test_structured_lookup_route_does_not_call_model_for_current_state() -> None:
    lookup = _Lookup()
    model = _Model()
    service = QueryAnswerService(
        router=QueryRouter(),
        structured_lookup=lookup,
        rag=BoundedAgenticRagService(
            structured_lookup=lookup,
            hybrid_retrieval=_Hybrid([_manifest()]),
            evidence_verifier=_Verifier(),
            model_gateway=model,
        ),
    )

    result = service.answer(QuerySessionStub(), "memory:goal.finance")  # type: ignore[arg-type]

    assert route_query("memory:goal.finance") is QueryRoute.STRUCTURED
    assert result.route is QueryRoute.STRUCTURED
    assert lookup.calls == 1
    assert model.calls == 0


def test_agentic_rag_enforces_round_tool_candidate_model_and_time_budgets() -> None:
    model = _Model()
    hybrid = _Hybrid([_manifest(_chunk(f"chunk-{index}")) for index in range(20)])
    service = BoundedAgenticRagService(
        structured_lookup=_Lookup(),
        hybrid_retrieval=hybrid,
        evidence_verifier=_Verifier(),
        model_gateway=model,
        planner=_BudgetMaxPlanner(),
        budget=AgenticBudget(
            max_rounds=99,
            max_subqueries=99,
            max_retrieval_calls=99,
            max_model_calls=99,
            max_context_chunks=99,
            max_wall_clock_ms=1_000,
        ),
    )

    answer = service.answer(QuerySessionStub(), "预算测试", route=QueryRoute.AGENTIC)  # type: ignore[arg-type]

    assert answer.budget_usage.rounds <= 3
    assert answer.budget_usage.subqueries <= 6
    assert answer.budget_usage.retrieval_calls <= 8
    assert answer.budget_usage.model_calls <= 3
    assert answer.budget_usage.context_chunks <= 16
    assert answer.budget_usage.wall_clock_ms <= 1_000
    assert all(limit <= 16 for _query, limit in hybrid.calls)
    assert model.calls <= 3


def test_agentic_rag_input_token_budget_blocks_model_call() -> None:
    model = _Model()
    service = BoundedAgenticRagService(
        structured_lookup=_Lookup(),
        hybrid_retrieval=_Hybrid([_manifest(_chunk(text="x" * 200))]),
        evidence_verifier=_Verifier(),
        model_gateway=model,
        budget=AgenticBudget(max_input_tokens=8),
    )

    answer = service.answer(QuerySessionStub(), "预算测试", route=QueryRoute.AGENTIC)  # type: ignore[arg-type]

    assert answer.stop_reason is StopReason.BUDGET_EXHAUSTED
    assert answer.answer == "仅返回已授权证据，未生成模型答案。"
    assert answer.citations
    assert answer.budget_usage.input_tokens > 8
    assert answer.budget_usage.model_calls == 0
    assert model.calls == 0


def test_agentic_rag_output_token_budget_discards_generated_prose() -> None:
    model = _Model(output_tokens=9)
    service = BoundedAgenticRagService(
        structured_lookup=_Lookup(),
        hybrid_retrieval=_Hybrid([_manifest()]),
        evidence_verifier=_Verifier(),
        model_gateway=model,
        budget=AgenticBudget(max_output_tokens=4),
    )

    answer = service.answer(QuerySessionStub(), "预算测试", route=QueryRoute.AGENTIC)  # type: ignore[arg-type]

    assert answer.stop_reason is StopReason.BUDGET_EXHAUSTED
    assert answer.answer == "仅返回已授权证据，未生成模型答案。"
    assert answer.claims == ()
    assert answer.citations
    assert answer.budget_usage.model_calls == 1
    assert answer.budget_usage.output_tokens == 9
    assert model.max_output_tokens_seen == [4]


def test_agentic_rag_stops_when_queries_repeat_without_new_evidence() -> None:
    repeated = BoundedAgenticRagService(
        structured_lookup=_Lookup(),
        hybrid_retrieval=_Hybrid([_manifest()]),
        evidence_verifier=_Verifier(),
        model_gateway=_Model(),
        planner=_RepeatPlanner(),
    )
    repeated_answer = repeated.answer(QuerySessionStub(), "重复测试", route=QueryRoute.AGENTIC)  # type: ignore[arg-type]

    empty_manifest = RetrievalAuthorizer().seal_manifest(query_hash="query-hash", chunks=[])
    no_new = BoundedAgenticRagService(
        structured_lookup=_Lookup(),
        hybrid_retrieval=_Hybrid([empty_manifest]),
        evidence_verifier=_Verifier(),
        model_gateway=_Model(),
        planner=_NoNewEvidencePlanner(),
    )
    no_new_answer = no_new.answer(QuerySessionStub(), "空证据测试", route=QueryRoute.AGENTIC)  # type: ignore[arg-type]

    assert repeated_answer.stop_reason is StopReason.REPEATED_QUERY
    assert repeated_answer.budget_usage.subqueries == 1
    assert no_new_answer.stop_reason is StopReason.NO_NEW_AUTHORIZED_EVIDENCE
    assert no_new_answer.budget_usage.retrieval_calls == 1


def test_decision_support_exposes_no_external_action_capability() -> None:
    session = _RecordingSession()
    analysis = DecisionSupportService().analyze(
        session,  # type: ignore[arg-type]
        DecisionRequest(
            problem="是否学习量化",
            options=(DecisionOption(label="learn", description="start"),),
            formal_goal_refs=(),
        ),
        manifest=_manifest(),
        citations=(),
    )

    assert analysis.external_action_count == 0
    assert {"browse", "http", "send_message", "trade", "purchase", "publish", "execute"}.isdisjoint(
        {name for name in dir(DecisionSupportService) if not name.startswith("_")}
    )


def test_gap_recommendations_use_only_current_formal_goals() -> None:
    stale_goal = FormalGoalRef(
        formal_memory_id="memory-1",
        formal_version_id="version-1",
        state_key="goal.finance",
        effective_generation=1,
    )
    session = _RecordingSession(current_goal=False)

    recommendations = KnowledgeGapService().recommend(
        session,  # type: ignore[arg-type]
        goal=stale_goal,
        domain_id="finance.quant",
        reason_code=GapReasonCode.MISSING_MATERIAL,
        missing_coverage=("risk control",),
    )

    assert recommendations == ()
    assert any("FROM current_formal_memory" in statement for statement in session.statements)


def test_gap_recommendations_do_not_claim_the_user_lacks_ability() -> None:
    service = KnowledgeGapService()

    service._assert_non_deficit_language(  # noqa: SLF001
        "知识库覆盖不足：风险控制。",
        "补充资料可以让后续回答更有证据基础。",
    )

    for forbidden in ("你不会", "用户不会", "你缺乏能力", "能力不足", "用户能力不足"):
        try:
            service._assert_non_deficit_language(forbidden)  # noqa: SLF001
        except ValueError as exc:
            assert "knowledge-base coverage" in str(exc)
        else:
            raise AssertionError(f"forbidden deficit language was accepted: {forbidden}")
