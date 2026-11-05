from __future__ import annotations

import sys
import types
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any, Literal

import pytest
from sqlalchemy.orm import Session

from tests.query_session_stub import QuerySessionStub
from zhiheng.core.ids import sha256_text
from zhiheng.memory.context import MemoryContextSnapshot
from zhiheng.query import (
    AgenticBudget,
    AnswerClaim,
    BoundedAgenticRagService,
    GeneratedAnswer,
    StopReason,
)
from zhiheng.query.service import QueryAnswerService
from zhiheng.retrieval import QueryRoute, QueryRouter, RetrievalAuthorizer
from zhiheng.retrieval.contracts import (
    AuthorizedChunk,
    AuthorizedContextManifest,
    Citation,
    HybridRetrievalResult,
    RetrievalFilters,
    RetrievalSource,
)
from zhiheng.retrieval.embeddings import (
    BgeM3QueryEmbedder,
    QueryEmbeddingUnavailableError,
)


def _chunk(
    chunk_id: str = "chunk-1",
    text: str = "正式知识库证据。",
    title: str = "正式证据",
) -> AuthorizedChunk:
    return AuthorizedChunk(
        source_type="knowledge_object",
        source_id="knowledge-1",
        source_version_id="version-1",
        chunk_id=chunk_id,
        confirmation_generation=1,
        generation=None,
        title=title,
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
    )


def _manifest(chunk: AuthorizedChunk | None = None) -> AuthorizedContextManifest:
    return RetrievalAuthorizer().seal_manifest(
        query_hash="query-hash",
        chunks=[chunk or _chunk()],
    )


class _Hybrid:
    def __init__(self, manifests: list[Any], after_search: Any | None = None) -> None:
        self.manifests = manifests
        self.calls: list[str] = []
        self.offline_calls: list[str] = []
        self.filters_seen: list[RetrievalFilters | None] = []
        self.after_search = after_search

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
        filters: RetrievalFilters | None = None,
    ) -> HybridRetrievalResult:
        self.calls.append(query)
        self.filters_seen.append(filters)
        manifest = self.manifests[min(len(self.calls) - 1, len(self.manifests) - 1)]
        if self.after_search is not None:
            self.after_search()
        return HybridRetrievalResult(manifest=manifest, degraded_reasons=(), run_id="run-1")

    def search_offline(
        self,
        session: Any,
        query: str,
        *,
        query_embedding: object | None = None,
        vector_generation_id: str | None = None,
        limit: int = 10,
        overfetch_factor: int = 4,
        rrf_k: int | None = None,
        filters: RetrievalFilters | None = None,
    ) -> HybridRetrievalResult:
        self.offline_calls.append(query)
        self.calls.append(query)
        self.filters_seen.append(filters)
        manifest = self.manifests[min(len(self.calls) - 1, len(self.manifests) - 1)]
        if self.after_search is not None:
            self.after_search()
        return HybridRetrievalResult(manifest=manifest, degraded_reasons=(), run_id="run-1")


class _Verifier:
    def __init__(self, valid: bool = True) -> None:
        self.valid = valid
        self.manifests: list[AuthorizedContextManifest] = []

    def validate_manifest(self, session: Any, manifest: AuthorizedContextManifest) -> bool:
        self.manifests.append(manifest)
        return self.valid


class _Model:
    def __init__(
        self,
        citation_id: str | None = None,
        *,
        fail: BaseException | None = None,
        answer: str = "有证据支持的回答",
        insufficiencies: tuple[str, ...] = (),
        answerability: Literal["supported", "mixed", "insufficient"] = "supported",
        include_claims: bool = True,
        extra_claims: tuple[AnswerClaim, ...] = (),
        general_claims_cite_evidence: bool = False,
        output_tokens: int = 7,
    ) -> None:
        self.citation_id = citation_id
        self.fail = fail
        self.answer = answer
        self.insufficiencies = insufficiencies
        self.answerability = answerability
        self.include_claims = include_claims
        self.extra_claims = extra_claims
        self.general_claims_cite_evidence = general_claims_cite_evidence
        self.output_tokens = output_tokens
        self.calls = 0
        self.manifests: list[AuthorizedContextManifest] = []
        self.citations_seen: list[tuple[Citation, ...]] = []
        self.max_output_tokens_seen: list[int | None] = []
        self.conversation_context_seen: list[Sequence[Mapping[str, str]] | None] = []

    def generate_answer(
        self,
        *,
        query: str,
        manifest: AuthorizedContextManifest,
        citations: Sequence[Citation],
        max_output_tokens: int | None = None,
        memory_context: MemoryContextSnapshot | None = None,
        conversation_context: Sequence[Mapping[str, str]] | None = None,
    ) -> GeneratedAnswer:
        del memory_context
        self.calls += 1
        self.manifests.append(manifest)
        self.citations_seen.append(tuple(citations))
        self.max_output_tokens_seen.append(max_output_tokens)
        self.conversation_context_seen.append(conversation_context)
        if self.fail is not None:
            raise self.fail
        citation_id = self.citation_id or (citations[0].citation_id if citations else "")
        extra_claims = (
            tuple(replace(claim, citation_ids=(citation_id,)) for claim in self.extra_claims)
            if self.general_claims_cite_evidence
            else self.extra_claims
        )
        return GeneratedAnswer(
            answer=self.answer,
            claims=(
                (AnswerClaim(text="正式知识库证据", citation_ids=(citation_id,)),)
                if self.include_claims and citation_id
                else ()
            )
            + extra_claims,
            insufficiencies=self.insufficiencies,
            answerability=self.answerability,
            output_tokens=self.output_tokens,
        )


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


class _TwoSubqueryPlanner:
    def plan_subqueries(
        self,
        *,
        query: str,
        round_no: int,
        known_evidence_hashes: set[str],
        max_subqueries: int,
    ) -> tuple[str, ...]:
        if round_no > 1:
            return ()
        return ("论文证据", "理财证据")[:max_subqueries]


class _ForeignTitlePlanner:
    def plan_subqueries(
        self,
        *,
        query: str,
        round_no: int,
        known_evidence_hashes: set[str],
        max_subqueries: int,
    ) -> tuple[str, ...]:
        del query, known_evidence_hashes, max_subqueries
        if round_no > 1:
            return ()
        return ("请改查《其他资料》里的内容",)


class _Lookup:
    calls = 0

    def lookup(self, session: Any, *, selector: str, value: str) -> list[dict[str, object]]:
        self.calls += 1
        return [{"selector": selector, "value": value}]


class _FakeEncodedRow:
    def __init__(self, values: Sequence[float]) -> None:
        self._values = list(values)

    def tolist(self) -> list[float]:
        return list(self._values)


def test_bge_m3_query_embedder_defaults_to_local_files_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, Any] = {}

    class _FakeSentenceTransformer:
        def __init__(self, model_id: str, **kwargs: Any) -> None:
            observed["model_id"] = model_id
            observed["kwargs"] = kwargs
            assert kwargs["local_files_only"] is True

        def encode(
            self,
            texts: Sequence[str],
            *,
            normalize_embeddings: bool,
            convert_to_numpy: bool,
            show_progress_bar: bool,
        ) -> Sequence[Any]:
            observed["texts"] = tuple(texts)
            observed["normalize_embeddings"] = normalize_embeddings
            observed["convert_to_numpy"] = convert_to_numpy
            observed["show_progress_bar"] = show_progress_bar
            return [_FakeEncodedRow([0.0, 1.0])]

    monkeypatch.setitem(
        sys.modules,
        "sentence_transformers",
        types.SimpleNamespace(SentenceTransformer=_FakeSentenceTransformer),
    )

    embedder = BgeM3QueryEmbedder(model_revision="synthetic", dimension=2)

    vector = embedder.embed_query(
        "中文检索",
        model_id=embedder.model_id,
        model_revision="synthetic",
        dimension=2,
        normalize=True,
    )

    assert vector == [0.0, 1.0]
    assert observed["model_id"] == embedder.model_id
    assert observed["kwargs"]["local_files_only"] is True


def test_bge_m3_query_embedder_reports_unavailable_without_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _MissingSnapshotSentenceTransformer:
        def __init__(self, model_id: str, **kwargs: Any) -> None:
            raise OSError("missing snapshot")

    monkeypatch.setitem(
        sys.modules,
        "sentence_transformers",
        types.SimpleNamespace(SentenceTransformer=_MissingSnapshotSentenceTransformer),
    )

    embedder = BgeM3QueryEmbedder(model_revision="synthetic", dimension=2)

    with pytest.raises(QueryEmbeddingUnavailableError, match="downloads are disabled"):
        embedder.embed_query(
            "中文检索",
            model_id=embedder.model_id,
            model_revision="synthetic",
            dimension=2,
            normalize=True,
        )


def test_model_answer_claims_must_reference_sealed_citation_ids() -> None:
    manifest = _manifest()
    forged_model = _Model(citation_id="forged")
    service = BoundedAgenticRagService(
        structured_lookup=_Lookup(),
        hybrid_retrieval=_Hybrid([manifest]),
        evidence_verifier=_Verifier(),
        model_gateway=forged_model,
    )

    answer = service.answer(QuerySessionStub(), "query", route=QueryRoute.HYBRID)  # type: ignore[arg-type]

    assert answer.stop_reason is StopReason.CITATION_VALIDATION_FAILED
    assert answer.claims == ()
    assert answer.citations


def test_current_conversation_context_reaches_answer_model() -> None:
    model = _Model()
    service = BoundedAgenticRagService(
        structured_lookup=_Lookup(),
        hybrid_retrieval=_Hybrid([_manifest()]),
        evidence_verifier=_Verifier(),
        model_gateway=model,
    )

    context = [{"query": "前一问", "answer": "当前会话的前文"}]
    answer = service.answer(
        Session(),
        "当前问题",
        route=QueryRoute.HYBRID,
        conversation_context=context,
    )

    assert answer.stop_reason is StopReason.COMPLETED
    assert model.conversation_context_seen == [context]


def test_hybrid_query_uses_offline_contract_and_preserves_source_title_filter() -> None:
    hybrid = _Hybrid([_manifest(_chunk(title="原始资料"))])
    service = BoundedAgenticRagService(
        structured_lookup=_Lookup(),
        hybrid_retrieval=hybrid,
        evidence_verifier=_Verifier(),
        model_gateway=_Model(),
    )

    answer = service.answer(Session(), "请只根据《原始资料》回答", route=QueryRoute.HYBRID)

    assert answer.stop_reason is StopReason.COMPLETED
    assert hybrid.offline_calls == ["请只根据《原始资料》回答"]
    assert hybrid.filters_seen == [RetrievalFilters(title="原始资料")]


def test_privacy_denied_returns_authorized_evidence_only() -> None:
    service = BoundedAgenticRagService(
        structured_lookup=_Lookup(),
        hybrid_retrieval=_Hybrid([_manifest()]),
        evidence_verifier=_Verifier(),
        model_gateway=_Model(fail=PermissionError("denied")),
    )

    answer = service.answer(QuerySessionStub(), "query", route=QueryRoute.HYBRID)  # type: ignore[arg-type]

    assert answer.stop_reason is StopReason.PRIVACY_DENIED
    assert answer.answer == "仅返回已授权证据，未生成模型答案。"
    assert answer.citations


def test_agentic_rag_clamps_budget_and_stops_on_repeated_query() -> None:
    service = BoundedAgenticRagService(
        structured_lookup=_Lookup(),
        hybrid_retrieval=_Hybrid([_manifest()]),
        evidence_verifier=_Verifier(),
        model_gateway=_Model(),
        planner=_RepeatPlanner(),
        budget=AgenticBudget(max_rounds=99, max_subqueries=99, max_retrieval_calls=99),
    )

    answer = service.answer(QuerySessionStub(), "query", route=QueryRoute.AGENTIC)  # type: ignore[arg-type]

    assert answer.stop_reason is StopReason.REPEATED_QUERY
    assert answer.budget_usage.subqueries == 1
    assert answer.budget_usage.retrieval_calls == 1
    assert answer.budget_usage.rounds <= 3


def test_agentic_rag_revalidates_combined_manifest_before_returning_evidence_only() -> None:
    verifier = _Verifier()

    def invalidate() -> None:
        verifier.valid = False

    service = BoundedAgenticRagService(
        structured_lookup=_Lookup(),
        hybrid_retrieval=_Hybrid([_manifest()], after_search=invalidate),
        evidence_verifier=verifier,
        model_gateway=_Model(),
        planner=_RepeatPlanner(),
    )

    answer = service.answer(QuerySessionStub(), "query", route=QueryRoute.AGENTIC)  # type: ignore[arg-type]

    assert answer.stop_reason is StopReason.CITATION_VALIDATION_FAILED
    assert answer.citations == ()
    assert answer.claims == ()
    assert verifier.manifests


def test_agentic_planner_added_title_cannot_escape_original_source_scope() -> None:
    hybrid = _Hybrid([_manifest(_chunk(title="原始资料"))])
    service = BoundedAgenticRagService(
        structured_lookup=_Lookup(),
        hybrid_retrieval=hybrid,
        evidence_verifier=_Verifier(),
        model_gateway=_Model(),
        planner=_ForeignTitlePlanner(),
    )

    answer = service.answer(
        QuerySessionStub(),  # type: ignore[arg-type]
        "请只根据《原始资料》回答",
        route=QueryRoute.AGENTIC,
    )

    assert answer.stop_reason is StopReason.COMPLETED
    assert hybrid.calls == ["请改查《其他资料》里的内容 《原始资料》"]
    assert hybrid.filters_seen == [RetrievalFilters(title="原始资料")]


def test_agentic_rag_combines_authorized_evidence_from_multiple_subqueries() -> None:
    first = _manifest(_chunk(chunk_id="paper", text="论文证据。"))
    second = _manifest(_chunk(chunk_id="finance", text="理财证据。"))
    verifier = _Verifier()
    model = _Model()
    service = BoundedAgenticRagService(
        structured_lookup=_Lookup(),
        hybrid_retrieval=_Hybrid([first, second]),
        evidence_verifier=verifier,
        model_gateway=model,
        planner=_TwoSubqueryPlanner(),
        budget=AgenticBudget(max_context_chunks=4),
    )

    answer = service.answer(QuerySessionStub(), "query", route=QueryRoute.AGENTIC)  # type: ignore[arg-type]

    assert answer.stop_reason is StopReason.COMPLETED
    assert answer.budget_usage.subqueries == 2
    assert answer.budget_usage.retrieval_calls == 2
    assert answer.budget_usage.context_chunks == 2
    assert model.calls == 1
    assert tuple(chunk.chunk_id for chunk in model.manifests[0].chunks) == ("paper", "finance")
    # Both sources reach the model; only the source used by its claim is
    # presented as an answer citation.
    assert tuple(citation.chunk_id for citation in answer.citations) == ("paper",)
    assert tuple(citation.chunk_id for citation in model.citations_seen[0]) == ("paper", "finance")
    assert verifier.manifests[0].seal == model.manifests[0].seal


def test_structured_query_uses_lookup_without_agentic_or_model() -> None:
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

    result = service.answer(
        QuerySessionStub(),  # type: ignore[arg-type]
        "memory:goal.finance",
    )

    assert result.route is QueryRoute.STRUCTURED
    assert lookup.calls == 1
    assert model.calls == 0


def test_manifest_validation_failure_is_evidence_only() -> None:
    manifest = _manifest()
    service = BoundedAgenticRagService(
        structured_lookup=_Lookup(),
        hybrid_retrieval=_Hybrid([replace(manifest)]),
        evidence_verifier=_Verifier(valid=False),
        model_gateway=_Model(),
    )

    answer = service.answer(QuerySessionStub(), "query", route=QueryRoute.HYBRID)  # type: ignore[arg-type]

    assert answer.stop_reason is StopReason.CITATION_VALIDATION_FAILED
    assert answer.claims == ()


def test_genuine_insufficient_answer_does_not_expose_retrieved_citations() -> None:
    service = BoundedAgenticRagService(
        structured_lookup=_Lookup(),
        hybrid_retrieval=_Hybrid([_manifest()]),
        evidence_verifier=_Verifier(),
        model_gateway=_Model(
            answer="没有足够证据回答。",
            insufficiencies=("证据不足。",),
            include_claims=False,
        ),
    )

    answer = service.answer(QuerySessionStub(), "query", route=QueryRoute.HYBRID)  # type: ignore[arg-type]

    assert answer.stop_reason is StopReason.COMPLETED
    assert answer.claims == ()
    assert answer.citations == ()


def test_supported_answer_survives_negative_evidence_wording() -> None:
    model = _Model()
    service = BoundedAgenticRagService(
        structured_lookup=_Lookup(),
        hybrid_retrieval=_Hybrid([_manifest()]),
        evidence_verifier=_Verifier(),
        model_gateway=model,
    )

    answer = service.answer(
        QuerySessionStub(),  # type: ignore[arg-type]
        "材料有没有作者的银行账户号码？没有证据请明确说明。",
        route=QueryRoute.HYBRID,
    )

    assert answer.stop_reason is StopReason.COMPLETED
    assert answer.claims
    assert answer.citations
    assert model.calls == 1


def test_unsupported_existence_answer_withholds_unrelated_claims() -> None:
    model = _Model(
        answer="没有证据表明材料包含该事实。",
        insufficiencies=("当前材料未提供该事实。",),
        answerability="insufficient",
    )
    service = BoundedAgenticRagService(
        structured_lookup=_Lookup(),
        hybrid_retrieval=_Hybrid([_manifest()]),
        evidence_verifier=_Verifier(),
        model_gateway=model,
    )

    answer = service.answer(
        QuerySessionStub(),  # type: ignore[arg-type]
        "材料是否包含该事实？",
        route=QueryRoute.HYBRID,
    )

    assert answer.stop_reason is StopReason.COMPLETED
    assert answer.claims == ()
    assert answer.citations == ()
    assert answer.insufficiencies == ("当前材料未提供该事实。",)


def test_partial_insufficiency_keeps_supported_claims_and_citations() -> None:
    model = _Model(
        answer="材料支持正式知识库证据；没有足够证据确认另一部分。",
        insufficiencies=("另一部分没有授权证据支持。",),
        answerability="mixed",
    )
    service = BoundedAgenticRagService(
        structured_lookup=_Lookup(),
        hybrid_retrieval=_Hybrid([_manifest()]),
        evidence_verifier=_Verifier(),
        model_gateway=model,
    )

    answer = service.answer(
        QuerySessionStub(),  # type: ignore[arg-type]
        "请回答正式知识库证据，并说明没有证据的部分。",
        route=QueryRoute.HYBRID,
    )

    assert answer.stop_reason is StopReason.COMPLETED
    assert answer.claims
    assert answer.citations
    assert answer.insufficiencies == ("另一部分没有授权证据支持。",)


def _general(text: str = "通用常识") -> AnswerClaim:
    return AnswerClaim(text=text, citation_ids=(), kind="general_knowledge")


def _hybrid_service(model: _Model) -> BoundedAgenticRagService:
    return BoundedAgenticRagService(
        structured_lookup=_Lookup(),
        hybrid_retrieval=_Hybrid([_manifest()]),
        evidence_verifier=_Verifier(),
        model_gateway=model,
    )


def test_insufficient_answer_opens_with_knowledge_base_notice_and_keeps_general_knowledge() -> None:
    model = _Model(
        answer="一般来说可以这样理解。",
        answerability="insufficient",
        extra_claims=(_general(),),
    )

    answer = _hybrid_service(model).answer(
        QuerySessionStub(),  # type: ignore[arg-type]
        "query",
        route=QueryRoute.HYBRID,
    )

    assert answer.stop_reason is StopReason.COMPLETED
    assert answer.answer == "知识库中没有与这个问题相关的内容。一般来说可以这样理解。"
    assert answer.claims == (_general(),)
    assert answer.citations == ()


def test_insufficient_answer_does_not_repeat_knowledge_base_notice() -> None:
    model = _Model(
        answer="知识库中没有与这个问题相关的内容。以下是通用知识。",
        answerability="insufficient",
        include_claims=False,
    )

    answer = _hybrid_service(model).answer(
        QuerySessionStub(),  # type: ignore[arg-type]
        "query",
        route=QueryRoute.HYBRID,
    )

    assert answer.answer == "知识库中没有与这个问题相关的内容。以下是通用知识。"


def test_mixed_answer_publishes_general_knowledge_beside_cited_claims() -> None:
    model = _Model(answerability="mixed", extra_claims=(_general(),))

    answer = _hybrid_service(model).answer(
        QuerySessionStub(),  # type: ignore[arg-type]
        "query",
        route=QueryRoute.HYBRID,
    )

    assert answer.stop_reason is StopReason.COMPLETED
    assert [claim.kind for claim in answer.claims] == ["cited", "general_knowledge"]
    assert {citation.citation_id for citation in answer.citations} == set(
        answer.claims[0].citation_ids
    )
    assert not answer.answer.startswith("知识库中没有")


def test_general_knowledge_claim_with_citations_fails_citation_validation() -> None:
    model = _Model(include_claims=False, general_claims_cite_evidence=True)
    model.extra_claims = (_general("伪装成通用知识"),)

    answer = _hybrid_service(model).answer(
        QuerySessionStub(),  # type: ignore[arg-type]
        "query",
        route=QueryRoute.HYBRID,
    )

    assert answer.stop_reason is StopReason.CITATION_VALIDATION_FAILED
    assert answer.claims == ()


def test_current_conversation_claims_need_conversation_context() -> None:
    claim = AnswerClaim(text="你刚才说预算有限", citation_ids=(), kind="current_conversation")

    with_context = _hybrid_service(_Model(extra_claims=(claim,))).answer(
        QuerySessionStub(),  # type: ignore[arg-type]
        "query",
        route=QueryRoute.HYBRID,
        conversation_context=({"query": "预算多少", "answer": "你说预算有限"},),
    )
    without_context = _hybrid_service(_Model(extra_claims=(claim,))).answer(
        QuerySessionStub(),  # type: ignore[arg-type]
        "query",
        route=QueryRoute.HYBRID,
    )

    assert with_context.stop_reason is StopReason.COMPLETED
    assert [item.kind for item in with_context.claims] == ["cited", "current_conversation"]
    assert without_context.stop_reason is StopReason.CITATION_VALIDATION_FAILED


def test_insufficient_answer_keeps_current_conversation_claims() -> None:
    claim = AnswerClaim(text="你刚才说预算有限", citation_ids=(), kind="current_conversation")
    model = _Model(answerability="insufficient", extra_claims=(claim, _general()))

    answer = _hybrid_service(model).answer(
        QuerySessionStub(),  # type: ignore[arg-type]
        "query",
        route=QueryRoute.HYBRID,
        conversation_context=({"query": "预算多少", "answer": "你说预算有限"},),
    )

    assert answer.claims == (claim, _general())
    assert answer.citations == ()


def _empty_retrieval_service(model: _Model) -> BoundedAgenticRagService:
    return BoundedAgenticRagService(
        structured_lookup=_Lookup(),
        hybrid_retrieval=_Hybrid(
            [RetrievalAuthorizer().seal_manifest(query_hash="query-hash", chunks=[])]
        ),
        evidence_verifier=_Verifier(),
        model_gateway=model,
    )


@pytest.mark.parametrize(
    ("route", "stop_reason"),
    [
        (QueryRoute.HYBRID, StopReason.COMPLETED),
        (QueryRoute.AGENTIC, StopReason.NO_NEW_AUTHORIZED_EVIDENCE),
    ],
)
def test_empty_retrieval_answers_from_general_knowledge_behind_the_notice(
    route: QueryRoute, stop_reason: StopReason
) -> None:
    model = _Model(
        answer="一般来说可以这样理解。",
        answerability="insufficient",
        extra_claims=(_general(),),
    )

    answer = _empty_retrieval_service(model).answer(
        QuerySessionStub(),  # type: ignore[arg-type]
        "知识库里没有的问题",
        route=route,
    )

    assert model.calls == 1
    assert model.manifests[0].chunks == ()
    assert answer.stop_reason is stop_reason
    assert answer.answer == "知识库中没有与这个问题相关的内容。一般来说可以这样理解。"
    assert answer.claims == (_general(),)
    assert answer.citations == ()


@pytest.mark.parametrize("route", [QueryRoute.HYBRID, QueryRoute.AGENTIC])
def test_empty_retrieval_says_so_when_the_model_cannot_answer(route: QueryRoute) -> None:
    service = _empty_retrieval_service(_Model(fail=PermissionError("external models disabled")))

    answer = service.answer(
        QuerySessionStub(),  # type: ignore[arg-type]
        "知识库里没有的问题",
        route=route,
    )

    assert answer.stop_reason is StopReason.PRIVACY_DENIED
    assert answer.answer.startswith("知识库中没有与这个问题相关的内容。")
    assert answer.claims == ()


def test_agentic_service_exposes_no_external_action_ports() -> None:
    public = {name for name in dir(BoundedAgenticRagService) if not name.startswith("_")}

    assert {"browse", "http", "send_message", "trade", "purchase", "publish", "execute"}.isdisjoint(
        public
    )


def test_structured_route_cannot_enter_agentic() -> None:
    service = BoundedAgenticRagService(
        structured_lookup=_Lookup(),
        hybrid_retrieval=_Hybrid([_manifest()]),
        evidence_verifier=_Verifier(),
        model_gateway=_Model(),
    )

    with pytest.raises(ValueError, match="structured"):
        service.answer(QuerySessionStub(), "query", route=QueryRoute.STRUCTURED)  # type: ignore[arg-type]
