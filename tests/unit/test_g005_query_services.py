from __future__ import annotations

import sys
import types
from collections.abc import Sequence
from dataclasses import replace
from typing import Any

import pytest

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
    RetrievalSource,
)
from zhiheng.retrieval.embeddings import (
    BgeM3QueryEmbedder,
    QueryEmbeddingUnavailableError,
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
    ) -> HybridRetrievalResult:
        self.calls.append(query)
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
        output_tokens: int = 7,
    ) -> None:
        self.citation_id = citation_id
        self.fail = fail
        self.answer = answer
        self.output_tokens = output_tokens
        self.calls = 0
        self.manifests: list[AuthorizedContextManifest] = []
        self.citations_seen: list[tuple[Citation, ...]] = []
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
        self.manifests.append(manifest)
        self.citations_seen.append(tuple(citations))
        self.max_output_tokens_seen.append(max_output_tokens)
        if self.fail is not None:
            raise self.fail
        citation_id = self.citation_id or citations[0].citation_id
        return GeneratedAnswer(
            answer=self.answer,
            claims=(AnswerClaim(text="正式知识库证据", citation_ids=(citation_id,)),),
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
    assert tuple(citation.chunk_id for citation in answer.citations) == ("paper", "finance")
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
