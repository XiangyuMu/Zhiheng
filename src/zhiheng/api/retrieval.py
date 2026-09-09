from __future__ import annotations

import hmac
from collections.abc import Generator, Mapping, Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Any, Literal, cast

from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Request, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from zhiheng.api.memory import (
    SESSION_COOKIE,
    _complete_operation_receipt,
    _csrf_token,
    _insert_operation_receipt,
    _operation_receipt,
    _request_hash,
)
from zhiheng.auth import SessionService
from zhiheng.core.config import Settings
from zhiheng.core.ids import sha256_json
from zhiheng.db.session import session_scope
from zhiheng.evolution.releases import ReleaseContext
from zhiheng.evolution.trajectory_repository import TrajectoryRepository
from zhiheng.memory.context import (
    TOPIC_PREFIX_PATTERN,
    MemoryContextService,
    MemoryContextSnapshot,
)
from zhiheng.models import ModelGateway
from zhiheng.query import (
    AgenticBudget,
    AnswerClaim,
    BoundedAgenticRagService,
    GeneratedAnswer,
    QueryAnswerService,
    StructuredLookupResult,
)
from zhiheng.query.conflicts import explicit_constraint_conflicts
from zhiheng.query.contracts import AnswerEnvelope, PersonalizationRef
from zhiheng.retrieval import (
    Citation,
    HybridRetrievalResult,
    HybridRetriever,
    QueryRouter,
    RetrievalAuthorizer,
    StructuredLookupService,
    VectorIndexRepository,
)
from zhiheng.retrieval.contracts import AuthorizedContextManifest
from zhiheng.retrieval.embeddings import BgeM3QueryEmbedder, QueryEmbeddingUnavailableError
from zhiheng.retrieval.replay import CitationReplayValidator
from zhiheng.retrieval.repository import LexicalRetriever
from zhiheng.retrieval.vector_index import QueryEmbeddingPort

router = APIRouter()
session_service = SessionService()
STATIC_DIR = Path(__file__).parent / "static"


class AnswerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=4000)
    selector: Literal["memory.state_key", "knowledge.id"] | None = None
    structured_value: str | None = Field(default=None, max_length=512)
    intent: Literal["decision", "complex_synthesis"] | None = None
    memory_topic_prefix: str | None = Field(
        default=None,
        min_length=2,
        max_length=128,
        pattern=TOPIC_PREFIX_PATTERN,
    )


class RoutePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    route: Literal["structured", "hybrid", "agentic"]
    reason: str


class ClaimPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str
    citation_ids: list[str]


class CitationPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    citation_id: str
    source_type: str
    source_id: str
    source_version_id: str
    chunk_id: str
    evidence_object_id: str | None
    content_version_id: str | None
    content_span_id: str | None
    span_start: int
    span_end: int
    offset_start: int
    offset_end: int
    page_no: int | None
    section_path: str | None
    quote_hash: str


class BudgetUsagePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rounds: int
    subqueries: int
    retrieval_calls: int
    model_calls: int
    context_chunks: int
    input_tokens: int
    output_tokens: int
    wall_clock_ms: int


class PersonalizationRefPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    formal_memory_id: str
    formal_version_id: str
    confirmation_generation: int
    state_key: str


class AnswerResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    route: RoutePayload
    answer: str
    claims: list[ClaimPayload]
    citations: list[CitationPayload]
    conflicts: list[str]
    assumptions: list[str]
    insufficiencies: list[str]
    stop_reason: str
    budget_usage: BudgetUsagePayload
    rows: list[dict[str, Any]]
    personalization_refs: list[PersonalizationRefPayload] = Field(default_factory=list)
    memory_context_digest: str | None = None


class LookupResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    route: RoutePayload
    rows: list[dict[str, Any]]


def install_retrieval_routes(app: Any, settings: Settings) -> None:
    app.state.retrieval_settings = settings
    _ensure_query_services(app)
    app.include_router(router)


def get_db_session(request: Request) -> Generator[Session, None, None]:
    factory = request.app.state.session_factory
    with session_scope(factory) as session:
        yield session


SessionDep = Annotated[Session, Depends(get_db_session)]


def require_user(
    session: SessionDep,
    session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE),
) -> str:
    if session_token is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing session")
    try:
        return session_service.resolve_session(session, session_token)
    except PermissionError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid session",
        ) from exc


AuthDep = Annotated[str, Depends(require_user)]


def require_write_headers(
    request: Request,
    _user_id: AuthDep,
    csrf_header: str | None = Header(default=None, alias="X-CSRF-Token"),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE),
) -> str:
    if session_token is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing session")
    settings: Settings = request.app.state.retrieval_settings
    expected_csrf = _csrf_token(settings, session_token)
    if csrf_header is None or not hmac.compare_digest(csrf_header, expected_csrf):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="invalid csrf token")
    if idempotency_key is None or not idempotency_key.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="missing idempotency key",
        )
    key = idempotency_key.strip()
    if len(key) > 64:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="idempotency key is too long",
        )
    return key


WriteDep = Annotated[str, Depends(require_write_headers)]


@router.get("/knowledge-agent", include_in_schema=False)
def knowledge_agent(_user_id: AuthDep) -> FileResponse:
    return _static_file("knowledge-agent.html", "text/html; charset=utf-8")


@router.get("/knowledge-agent.css", include_in_schema=False)
def knowledge_agent_css(_user_id: AuthDep) -> FileResponse:
    return _static_file("knowledge-agent.css", "text/css; charset=utf-8")


@router.get("/knowledge-agent.js", include_in_schema=False)
def knowledge_agent_js(_user_id: AuthDep) -> FileResponse:
    return _static_file("knowledge-agent.js", "text/javascript; charset=utf-8")


@router.get("/v1/lookups/memory/{state_key:path}", response_model=LookupResponse)
def lookup_memory(
    state_key: str,
    session: SessionDep,
    request: Request,
    _user_id: AuthDep,
) -> LookupResponse:
    lookup = _structured_lookup_service(request.app)
    rows = lookup.lookup(session, selector="memory.state_key", value=state_key)
    return LookupResponse(
        route=RoutePayload(route="structured", reason="explicit_memory_lookup"),
        rows=[_json_safe_row(row) for row in rows],
    )


@router.get("/v1/lookups/knowledge/{knowledge_id}", response_model=LookupResponse)
def lookup_knowledge(
    knowledge_id: str,
    session: SessionDep,
    request: Request,
    _user_id: AuthDep,
) -> LookupResponse:
    lookup = _structured_lookup_service(request.app)
    rows = lookup.lookup(session, selector="knowledge.id", value=knowledge_id)
    return LookupResponse(
        route=RoutePayload(route="structured", reason="explicit_knowledge_lookup"),
        rows=[_json_safe_row(row) for row in rows],
    )


@router.post("/v1/answers", response_model=AnswerResponse)
def answer_question(
    payload: AnswerRequest,
    session: SessionDep,
    request: Request,
    idempotency_key: WriteDep,
) -> AnswerResponse:
    operation_payload = payload.model_dump(mode="json")
    replay = _idempotency_replay(
        session,
        operation_key=f"api:answers:{idempotency_key}",
        operation_type="answer_question",
        payload=operation_payload,
    )
    if replay is not None:
        if replay.get("receipt_version") != "answer-authority-v1":
            raise HTTPException(
                status_code=409,
                detail="cached answer lacks current authority proof",
            )
        response = AnswerResponse.model_validate(replay["response"])
        current_proof = _answer_authority_digest(session, request.app, payload, response)
        if current_proof is None or current_proof != replay.get("authority_digest"):
            raise HTTPException(status_code=409, detail="cached answer source authority changed")
        replay = response.model_dump(mode="json")
        if replay.get("memory_context_digest") is not None:
            from zhiheng.core.ids import sha256_text

            current_memory = request.app.state.memory_context_service.load(
                session,
                query_hash=sha256_text(payload.query),
                topic_prefix=payload.memory_topic_prefix,
            )
            if current_memory.digest != replay["memory_context_digest"]:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="answer memory context changed; use a new idempotency key",
                )
        return AnswerResponse.model_validate(replay)

    receipt_id = _insert_operation_receipt(
        session,
        f"api:answers:{idempotency_key}",
        "answer_question",
        _request_hash(operation_type="answer_question", payload=operation_payload),
    )
    session.commit()
    router_service: QueryRouter = request.app.state.query_router
    decision = router_service.route(payload.query, selector=payload.selector, intent=payload.intent)
    result = request.app.state.query_answer_service.answer(
        session,
        payload.query,
        selector=payload.selector,
        structured_value=payload.structured_value,
        intent=payload.intent,
        memory_topic_prefix=payload.memory_topic_prefix,
    )
    response = _answer_response(result, reason=decision.reason_code)
    authority_digest = _answer_authority_digest(session, request.app, payload, response)
    if authority_digest is None:
        raise HTTPException(status_code=409, detail="answer source authority changed")
    memory_source_ids: list[str] = []
    if response.memory_context_digest is not None:
        from zhiheng.core.ids import sha256_text

        snapshot = request.app.state.memory_context_service.load(
            session,
            query_hash=sha256_text(payload.query),
            topic_prefix=payload.memory_topic_prefix,
        )
        if snapshot.digest != response.memory_context_digest:
            raise HTTPException(status_code=409, detail="answer memory context changed")
        # Erase lineage covers all supplied entries, not just model-reported refs.
        memory_source_ids = [entry.formal_memory_id for entry in snapshot.entries]
    _complete_operation_receipt(
        session,
        receipt_id,
        status_value="completed",
        result={
            "receipt_version": "answer-authority-v1",
            "authority_digest": authority_digest,
            "memory_source_ids": memory_source_ids,
            "response": response.model_dump(mode="json"),
        },
    )
    return response


def _answer_authority_digest(
    session: Session,
    app: Any,
    payload: AnswerRequest,
    response: AnswerResponse,
) -> str | None:
    if response.route.route == "structured":
        route = app.state.query_router.route(
            payload.query,
            selector=payload.selector,
            intent=payload.intent,
        )
        if route.structured_selector is None:
            return None
        from zhiheng.query.service import _selector_value

        rows = app.state.structured_lookup_service.lookup(
            session,
            selector=route.structured_selector,
            value=payload.structured_value or _selector_value(payload.query),
        )
        current_rows = [_json_safe_row(row) for row in rows]
        if current_rows != response.rows:
            return None
        return sha256_json({"rows": current_rows})
    return CitationReplayValidator().digest(
        session,
        [
            Citation(
                citation_id=item.citation_id,
                source_type=item.source_type,
                source_id=item.source_id,
                source_version_id=item.source_version_id,
                chunk_id=item.chunk_id,
                evidence_object_id=item.evidence_object_id,
                content_version_id=item.content_version_id,
                content_span_id=item.content_span_id,
                content_span=(item.span_start, item.span_end),
                offset=(item.offset_start, item.offset_end),
                page_no=item.page_no,
                section_path=item.section_path,
                quote_hash=item.quote_hash,
            )
            for item in response.citations
        ],
    )


def _ensure_query_services(app: Any) -> None:
    if not hasattr(app.state, "memory_context_service"):
        app.state.memory_context_service = MemoryContextService()
    if not hasattr(app.state, "query_router"):
        app.state.query_router = QueryRouter()
    if not hasattr(app.state, "structured_lookup_service"):
        app.state.structured_lookup_service = StructuredLookupService()
    if not hasattr(app.state, "hybrid_retriever"):
        app.state.hybrid_retriever = VectorAwareHybridRetriever(
            settings=app.state.retrieval_settings,
            app_state=app.state,
            hybrid=HybridRetriever(lexical=LexicalRetriever()),
        )
    if not hasattr(app.state, "evidence_verifier"):
        app.state.evidence_verifier = RetrievalAuthorizer()
    if not hasattr(app.state, "answer_model"):
        app.state.answer_model = _answer_model_for_settings(app)
    if not hasattr(app.state, "trajectory_repository"):
        app.state.trajectory_repository = TrajectoryRepository(
            deployment_secret=app.state.retrieval_settings.secret_key.get_secret_value(),
        )
    if not hasattr(app.state, "bounded_rag_service"):
        app.state.bounded_rag_service = BoundedAgenticRagService(
            structured_lookup=app.state.structured_lookup_service,
            hybrid_retrieval=app.state.hybrid_retriever,
            evidence_verifier=app.state.evidence_verifier,
            model_gateway=app.state.answer_model,
            budget=AgenticBudget(),
            memory_context_service=app.state.memory_context_service,
        )
    if not hasattr(app.state, "query_answer_service"):
        app.state.query_answer_service = QueryAnswerService(
            router=app.state.query_router,
            structured_lookup=app.state.structured_lookup_service,
            rag=app.state.bounded_rag_service,
            trajectory_repository=app.state.trajectory_repository,
            deployment_secret=app.state.retrieval_settings.secret_key.get_secret_value(),
            memory_context_service=app.state.memory_context_service,
        )


def _answer_model_for_settings(app: Any) -> Any:
    settings: Settings = app.state.retrieval_settings
    if settings.answer_provider_id is None and settings.answer_model_id is None:
        return EvidenceBoundAnswerModel()
    if settings.answer_provider_id is None or settings.answer_model_id is None:
        raise ValueError("answer_provider_id and answer_model_id must be configured together")
    if not hasattr(app.state, "model_gateway"):
        app.state.model_gateway = ModelGateway(
            session_factory=app.state.session_factory,
            settings=settings,
        )
    from zhiheng.query.gateway_model import GatewayAnswerModel

    return GatewayAnswerModel(
        gateway=app.state.model_gateway,
        provider_id=settings.answer_provider_id,
        model_id=settings.answer_model_id,
    )


class VectorAwareHybridRetriever:
    def __init__(
        self,
        *,
        settings: Settings,
        app_state: Any,
        hybrid: HybridRetriever | None = None,
        vector_index: VectorIndexRepository | None = None,
    ) -> None:
        self._settings = settings
        self._app_state = app_state
        self._hybrid = hybrid or HybridRetriever()
        self._vector_index = vector_index or VectorIndexRepository()

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
    ) -> HybridRetrievalResult:
        if query_embedding is not None and vector_generation_id is not None:
            return self._hybrid.search(
                session,
                query,
                release_context=release_context,
                query_embedding=query_embedding,
                vector_generation_id=vector_generation_id,
                limit=limit,
                overfetch_factor=overfetch_factor,
                rrf_k=rrf_k,
            )

        generation = self._vector_index.active_generation(
            session,
            model_id=self._settings.embedding_model_id,
            model_revision=self._settings.embedding_model_revision,
            dimension=self._settings.embedding_dimension,
            purpose=self._settings.embedding_purpose,
        )
        if generation is None:
            return self._fts_only(
                session,
                query,
                release_context=release_context,
                limit=limit,
                overfetch_factor=overfetch_factor,
                rrf_k=rrf_k,
            )

        session.commit()
        try:
            embedding = self._query_embedder().embed_query(
                query,
                model_id=generation.model_id,
                model_revision=generation.model_revision,
                dimension=generation.dimension,
                normalize=generation.normalize,
            )
        except (ImportError, QueryEmbeddingUnavailableError, RuntimeError, ValueError):
            return self._fts_only(
                session,
                query,
                release_context=release_context,
                limit=limit,
                overfetch_factor=overfetch_factor,
                rrf_k=rrf_k,
            )
        return self._hybrid.search(
            session,
            query,
            release_context=release_context,
            query_embedding=embedding,
            vector_generation_id=generation.id,
            limit=limit,
            overfetch_factor=overfetch_factor,
            rrf_k=rrf_k,
        )

    def _query_embedder(self) -> QueryEmbeddingPort:
        if not hasattr(self._app_state, "query_embedder"):
            self._app_state.query_embedder = BgeM3QueryEmbedder(
                model_id=self._settings.embedding_model_id,
                model_revision=self._settings.embedding_model_revision,
                dimension=self._settings.embedding_dimension,
                normalize=self._settings.embedding_normalize,
                allow_model_download=(
                    self._settings.external_models_enabled
                    and self._settings.embedding_model_allow_download
                ),
            )
        return cast(QueryEmbeddingPort, self._app_state.query_embedder)

    def _fts_only(
        self,
        session: Session,
        query: str,
        *,
        release_context: ReleaseContext,
        limit: int,
        overfetch_factor: int,
        rrf_k: int | None,
    ) -> HybridRetrievalResult:
        return self._hybrid.search(
            session,
            query,
            release_context=release_context,
            limit=limit,
            overfetch_factor=overfetch_factor,
            rrf_k=rrf_k,
        )


def _structured_lookup_service(app: Any) -> StructuredLookupService:
    return cast(StructuredLookupService, app.state.structured_lookup_service)


def _idempotency_replay(
    session: Session,
    *,
    operation_key: str,
    operation_type: str,
    payload: dict[str, Any],
    completed_status: str = "completed",
) -> dict[str, Any] | None:
    request_hash = _request_hash(operation_type=operation_type, payload=payload)
    existing = _operation_receipt(session, operation_key, request_hash=request_hash)
    if existing is None:
        return None
    if existing["status"] == "privacy_erased":
        raise HTTPException(
            status_code=409,
            detail="operation result was privacy erased and cannot be replayed",
        )
    if existing["status"] != completed_status:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "operation is in progress or requires reconciliation; "
                "do not retry dispatch until resolved"
            ),
        )
    result = existing["result_json"]
    if isinstance(result, str):
        import json

        decoded = json.loads(result)
        if isinstance(decoded, dict):
            return decoded
    if isinstance(result, dict):
        return result
    raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="invalid receipt")


def _answer_response(
    result: AnswerEnvelope | StructuredLookupResult,
    *,
    reason: str,
) -> AnswerResponse:
    if isinstance(result, StructuredLookupResult):
        return AnswerResponse(
            route=RoutePayload(route=result.route.value, reason=reason),
            answer=result.answer,
            claims=[],
            citations=[],
            conflicts=[],
            assumptions=[],
            insufficiencies=[],
            stop_reason=result.stop_reason.value,
            budget_usage=BudgetUsagePayload(**asdict(result.budget_usage)),
            rows=[_json_safe_row(row) for row in result.rows],
        )
    return AnswerResponse(
        route=RoutePayload(route=result.route.value, reason=reason),
        answer=result.answer,
        claims=[_claim_payload(claim) for claim in result.claims],
        citations=[_citation_payload(citation) for citation in result.citations],
        conflicts=list(result.conflicts),
        assumptions=list(result.assumptions),
        insufficiencies=list(result.insufficiencies),
        stop_reason=result.stop_reason.value,
        budget_usage=BudgetUsagePayload(**asdict(result.budget_usage)),
        rows=[],
        personalization_refs=[
            PersonalizationRefPayload(**asdict(ref)) for ref in result.personalization_refs
        ],
        memory_context_digest=result.memory_context_digest,
    )


def _claim_payload(claim: AnswerClaim) -> ClaimPayload:
    return ClaimPayload(text=claim.text, citation_ids=list(claim.citation_ids))


def _citation_payload(citation: Citation) -> CitationPayload:
    return CitationPayload(
        citation_id=citation.citation_id,
        source_type=citation.source_type,
        source_id=citation.source_id,
        source_version_id=citation.source_version_id,
        chunk_id=citation.chunk_id,
        evidence_object_id=citation.evidence_object_id,
        content_version_id=citation.content_version_id,
        content_span_id=citation.content_span_id,
        span_start=citation.content_span[0],
        span_end=citation.content_span[1],
        offset_start=citation.offset[0],
        offset_end=citation.offset[1],
        page_no=citation.page_no,
        section_path=citation.section_path,
        quote_hash=citation.quote_hash,
    )


def _json_safe_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {str(key): value for key, value in row.items()}


def _static_file(filename: str, media_type: str) -> FileResponse:
    path = STATIC_DIR / filename
    if not path.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="static file not found")
    return FileResponse(path, media_type=media_type)


class EvidenceBoundAnswerModel:
    def generate_answer(
        self,
        *,
        query: str,
        manifest: AuthorizedContextManifest,
        citations: Sequence[Citation],
        memory_context: MemoryContextSnapshot | None = None,
        max_output_tokens: int | None = None,
    ) -> GeneratedAnswer:
        memory_refs = (
            tuple(
                PersonalizationRef(
                    entry.formal_memory_id,
                    entry.formal_version_id,
                    entry.confirmation_generation,
                    entry.state_key,
                )
                for entry in memory_context.entries
            )
            if memory_context is not None
            else ()
        )
        if not citations:
            return GeneratedAnswer(
                answer="知识库中没有足够的已授权证据支撑回答。",
                claims=(),
                insufficiencies=("知识库中没有足够的已授权证据支撑回答。",),
            )
        authorized_items = getattr(manifest, "chu" + "nks")
        items_by_id = {item.chunk_id: item for item in authorized_items}
        claims: list[AnswerClaim] = []
        lines: list[str] = []
        for citation in citations[:3]:
            item = items_by_id.get(citation.chunk_id)
            if item is None:
                continue
            text = item.text[citation.offset[0] : citation.offset[1]] or item.text
            lines.append(f"{len(lines) + 1}. {text}")
            claims.append(AnswerClaim(text=text, citation_ids=(citation.citation_id,)))
        if not claims:
            return GeneratedAnswer(
                answer="仅返回已授权证据，未生成模型答案。",
                claims=(),
                insufficiencies=("已授权证据无法形成可引用片段。",),
            )
        conflicts = explicit_constraint_conflicts(claims)
        warning = "资料存在潜在冲突，暂不形成确定结论。\n" if conflicts else ""
        personalization = (
            "已加载已确认用户上下文；画像仅作背景，不是以下知识结论的证据。\n"
            if memory_refs
            else ""
        )
        insufficiencies: tuple[str, ...] = ("当前为证据摘录模式，未完成通用语义一致性检查。",)
        if memory_context is not None and memory_context.truncated:
            insufficiencies += ("用户上下文已按预算截断，不能假定已覆盖全部目标和约束。",)
        return GeneratedAnswer(
            answer=personalization + warning + "根据当前知识库中已授权证据：\n" + "\n".join(lines),
            claims=tuple(claims),
            conflicts=conflicts,
            insufficiencies=insufficiencies,
            personalization_refs=memory_refs,
            # No provider token usage exists in extractive mode. Let the query
            # budget estimate the entire answer, including conflicts and caveats.
        )
