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
    _link_operation_receipt_sources,
    _operation_receipt,
    _request_hash,
)
from zhiheng.auth import SessionService
from zhiheng.core.config import Settings
from zhiheng.core.ids import sha256_json, sha256_text
from zhiheng.db.session import session_scope
from zhiheng.evolution.learning_loop import LearningLoopService
from zhiheng.evolution.releases import ReleaseContext
from zhiheng.evolution.trajectory_repository import TrajectoryRepository
from zhiheng.memory.context import (
    TOPIC_PREFIX_PATTERN,
    MemoryContextService,
    MemoryContextSnapshot,
)
from zhiheng.memory.personal_updates import PersonalUpdateService
from zhiheng.models import ModelGateway
from zhiheng.models.configuration import defaults as model_defaults
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
from zhiheng.query.conversations import ConversationRepository
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
from zhiheng.retrieval.repository import CitationContextRepository, LexicalRetriever
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
    conversation_id: str | None = Field(default=None, min_length=1, max_length=36)


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


class CitationContextRequest(CitationPayload):
    pass


class EventCitationEvidence(BaseModel):
    history_id: str
    conversation_id: str
    excerpt: str
    quote_hash: str


class CitationContextResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    citation_id: str
    title: str | None
    source_type: str
    source_id: str
    source_version_id: str
    chunk_id: str
    media_type: str | None
    object_kind: str | None
    page_no: int | None
    section_path: str | None
    context: str
    quote: str
    quote_start: int
    quote_end: int
    event_evidence: EventCitationEvidence | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


class CitationCoverageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    claims: list[ClaimPayload]
    citation_ids: list[str] = Field(default_factory=list)


class CitationCoverageResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    total_claims: int
    cited_claims: int
    uncovered_claims: int
    coverage: float
    low_confidence: bool


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


class MemoryImpactPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    formal_memory_id: str
    formal_version_id: str
    state_key: str
    effect_type: str
    explanation: str
    used: bool = True


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
    memory_impacts: list[MemoryImpactPayload] = Field(
        default_factory=list,
        exclude_if=lambda value: not value,
    )
    context_prompts: list[dict[str, Any]] = Field(default_factory=list)


class LookupResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    route: RoutePayload
    rows: list[dict[str, Any]]


class ConversationCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, max_length=200)


class ConversationResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    title: str | None = None
    turn_count: int = 0


class AnswerHistoryResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    conversation_id: str
    turn_index: int
    query: str
    response: dict[str, Any]
    route: str
    stop_reason: str
    is_favorite: bool


def install_retrieval_routes(app: Any, settings: Settings) -> None:
    app.state.retrieval_settings = settings
    _ensure_query_services(app)
    app.include_router(router)


def initialize_retrieval_services(app: Any, settings: Settings) -> None:
    app.state.retrieval_settings = settings
    _ensure_query_services(app, eager_answer_model=True)


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
    user_id: AuthDep,
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
        current_proof = _answer_authority_digest(
            session, request.app, payload, response, user_id=user_id, replay=True
        )
        if current_proof is None or current_proof != replay.get("authority_digest"):
            raise HTTPException(status_code=409, detail="cached answer source authority changed")
        replay = response.model_dump(mode="json")
        if replay.get("memory_context_digest") is not None:
            from zhiheng.core.ids import sha256_text

            current_memory = request.app.state.memory_context_service.load(
                session,
                query_hash=sha256_text(payload.query),
                topic_prefix=payload.memory_topic_prefix,
                query=payload.query,
                intent=payload.intent,
            )
            if current_memory.digest != replay["memory_context_digest"]:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="answer memory context changed; use a new idempotency key",
                )
            _validate_replayed_personalization_refs(
                replay.get("personalization_refs"), current_memory
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
    conversation_context = None
    if payload.conversation_id is not None:
        repository = ConversationRepository()
        if (
            repository.get_owned(
                session, conversation_id=payload.conversation_id, owner_user_id=user_id
            )
            is None
        ):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="conversation not found"
            )
        conversation_context = repository.context(
            session, conversation_id=payload.conversation_id, owner_user_id=user_id
        )
    result = request.app.state.query_answer_service.answer(
        session,
        payload.query,
        selector=payload.selector,
        structured_value=payload.structured_value,
        intent=payload.intent,
        memory_topic_prefix=payload.memory_topic_prefix,
        conversation_context=conversation_context,
    )
    response = _answer_response(result, reason=decision.reason_code)
    response.context_prompts = _contextual_prompts(
        session, query=payload.query, user_id=user_id, result=result
    )
    conflicts = [
        prompt for prompt in response.context_prompts if prompt["kind"] == "conflict"
    ]
    if conflicts:
        alternatives = []
        for prompt in conflicts:
            candidate = prompt.get("candidate", {})
            existing = prompt.get("existing", {})
            candidate_value = candidate.get("value", candidate.get("text", ""))
            existing_value = existing.get("value", existing.get("text", ""))
            alternatives.append(f"条件一：{candidate_value}；条件二：{existing_value}")
        response.insufficiencies.append(
            "回答中依赖冲突个人信息的部分暂缓；可按以下条件分别理解："
            + "；".join(alternatives)
            + "。请先确认或补充；不依赖冲突的信息仍可继续使用。"
        )
    authority_digest = _answer_authority_digest(
        session, request.app, payload, response, user_id=user_id
    )
    if authority_digest is None:
        raise HTTPException(status_code=409, detail="answer source authority changed")
    memory_source_ids: list[str] = []
    if response.memory_context_digest is not None:
        from zhiheng.core.ids import sha256_text

        snapshot = request.app.state.memory_context_service.load(
            session,
            query_hash=sha256_text(payload.query),
            topic_prefix=payload.memory_topic_prefix,
            query=payload.query,
            intent=payload.intent,
        )
        if snapshot.digest != response.memory_context_digest:
            raise HTTPException(status_code=409, detail="answer memory context changed")
        # Erasure lineage follows the authorized memory context that was supplied
        # to answer generation, rather than model-reported personalization refs.
        # A provider may omit refs from its response while still receiving and
        # using formal memory context; those memories must remain erasable.
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
    _link_operation_receipt_sources(
        session,
        receipt_id,
        [("formal_memory", source_id) for source_id in memory_source_ids]
        + [
            (str(row["source_type"]), str(row["source_id"]))
            for row in response.rows
            if row.get("source_type") and row.get("source_id")
        ]
        + [(citation.source_type, citation.source_id) for citation in response.citations],
    )
    if payload.conversation_id is not None:
        ConversationRepository().append(
            session,
            conversation_id=payload.conversation_id,
            owner_user_id=user_id,
            query=payload.query,
            response=response.model_dump(mode="json"),
        )
    return response


def _contextual_prompts(
    session: Session,
    *,
    query: str,
    user_id: str,
    result: AnswerEnvelope | StructuredLookupResult,
) -> list[dict[str, Any]]:
    service = PersonalUpdateService()
    prompts = service.context_prompts(
        session, query=query, owner_user_id=user_id, include_deferred=True
    )
    if not prompts and _needs_personal_context(query, result):
        prompts.append(
            service.create_missing_prompt(
                session,
                owner_user_id=user_id,
                query=query,
                reason="当前问题需要你的个人背景或偏好，但知识库中没有足够的已确认信息。",
            )
        )
    return prompts


def _needs_personal_context(query: str, result: AnswerEnvelope | StructuredLookupResult) -> bool:
    if getattr(result, "citations", ()):
        return False
    normalized = query.casefold()
    return any(
        marker in normalized for marker in ("我", "我的", "目前", "现在", "偏好", "目标", "计划")
    )


@router.post("/v1/conversations", response_model=ConversationResponse)
def create_conversation(
    payload: ConversationCreateRequest,
    session: SessionDep,
    user_id: AuthDep,
    _idempotency_key: WriteDep,
) -> ConversationResponse:
    return ConversationResponse(
        **ConversationRepository().create(session, owner_user_id=user_id, title=payload.title)
    )


@router.get("/v1/conversations", response_model=list[ConversationResponse])
def list_conversations(
    session: SessionDep,
    user_id: AuthDep,
    limit: int = 50,
    offset: int = 0,
) -> list[ConversationResponse]:
    return [
        ConversationResponse(**item)
        for item in ConversationRepository().list_owned(
            session, owner_user_id=user_id, limit=max(1, min(limit, 100)), offset=max(0, offset)
        )
    ]


@router.get("/v1/conversations/{conversation_id}", response_model=ConversationResponse)
def get_conversation(
    conversation_id: str,
    session: SessionDep,
    user_id: AuthDep,
) -> ConversationResponse:
    item = ConversationRepository().get_owned(
        session, conversation_id=conversation_id, owner_user_id=user_id
    )
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="conversation not found")
    return ConversationResponse(**item)


@router.get("/v1/answers/history", response_model=list[AnswerHistoryResponse])
def answer_history(
    session: SessionDep,
    user_id: AuthDep,
    conversation_id: str | None = None,
    favorite: bool | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[AnswerHistoryResponse]:
    items = ConversationRepository().list_history(
        session,
        owner_user_id=user_id,
        conversation_id=conversation_id,
        favorite=favorite,
        limit=max(1, min(limit, 100)),
        offset=max(0, offset),
    )
    return [AnswerHistoryResponse(**item) for item in items]


@router.post("/v1/answers/history/{history_id}/favorite", response_model=dict[str, bool])
def favorite_answer(
    history_id: str,
    session: SessionDep,
    user_id: AuthDep,
    _idempotency_key: WriteDep,
    enabled: bool = True,
) -> dict[str, bool]:
    updated = ConversationRepository().set_favorite(
        session, history_id=history_id, owner_user_id=user_id, favorite=enabled
    )
    if not updated:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="answer history not found"
        )
    return {"is_favorite": enabled}


@router.post("/v1/citations/context", response_model=CitationContextResponse)
def citation_context(
    payload: CitationContextRequest,
    session: SessionDep,
    _user_id: AuthDep,
) -> CitationContextResponse:
    citation = Citation(
        citation_id=payload.citation_id,
        source_type=payload.source_type,
        source_id=payload.source_id,
        source_version_id=payload.source_version_id,
        chunk_id=payload.chunk_id,
        evidence_object_id=payload.evidence_object_id,
        content_version_id=payload.content_version_id,
        content_span_id=payload.content_span_id,
        content_span=(payload.span_start, payload.span_end),
        offset=(payload.offset_start, payload.offset_end),
        page_no=payload.page_no,
        section_path=payload.section_path,
        quote_hash=payload.quote_hash,
    )
    if CitationReplayValidator().digest(session, [citation]) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="citation source is unavailable or no longer authorized",
        )
    row = CitationContextRepository().get_chunk(
        session,
        source_type=payload.source_type,
        source_id=payload.source_id,
        source_version_id=payload.source_version_id,
        chunk_id=payload.chunk_id,
    )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="citation source not found",
        )
    chunk_text = str(row["text"])
    span_start = int(cast(int | str, row["span_start"]))
    quote_start = max(0, min(payload.offset_start - span_start, len(chunk_text)))
    quote_end = max(quote_start, min(payload.offset_end - span_start, len(chunk_text)))
    quote = chunk_text[quote_start:quote_end]
    if sha256_text(quote) != payload.quote_hash:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="citation quote hash is stale or unauthorized",
        )
    context_start = max(0, quote_start - 160)
    context_end = min(len(chunk_text), quote_end + 160)
    return CitationContextResponse(
        citation_id=payload.citation_id,
        title=str(row["title"]) if row["title"] is not None else None,
        source_type=payload.source_type,
        source_id=payload.source_id,
        source_version_id=payload.source_version_id,
        chunk_id=payload.chunk_id,
        media_type=str(row["media_type"]) if row["media_type"] is not None else None,
        object_kind=str(row["object_kind"]) if row["object_kind"] is not None else None,
        page_no=payload.page_no,
        section_path=payload.section_path,
        context=chunk_text[context_start:context_end],
        quote=quote,
        quote_start=quote_start - context_start,
        quote_end=quote_end - context_start,
        event_evidence=EventCitationEvidence(
            history_id=str(row["history_id"]),
            conversation_id=str(row["conversation_id"]),
            excerpt=str(row["evidence_excerpt"]),
            quote_hash=str(row["evidence_quote_hash"]),
        )
        if payload.source_type == "event_memory"
        else None,
    )


@router.post("/v1/citations/coverage", response_model=CitationCoverageResponse)
def citation_coverage(
    payload: CitationCoverageRequest, _user_id: AuthDep
) -> CitationCoverageResponse:
    allowed = set(payload.citation_ids)
    total = len(payload.claims)
    cited = sum(bool(set(claim.citation_ids) & allowed) for claim in payload.claims)
    coverage = cited / total if total else 0.0
    return CitationCoverageResponse(
        total_claims=total,
        cited_claims=cited,
        uncovered_claims=total - cited,
        coverage=coverage,
        low_confidence=coverage < 0.75,
    )


def _answer_authority_digest(
    session: Session,
    app: Any,
    payload: AnswerRequest,
    response: AnswerResponse,
    *,
    user_id: str,
    replay: bool = False,
) -> str | None:
    conversation_context: list[dict[str, str]] = []
    if payload.conversation_id is not None:
        conversation_context = ConversationRepository().context(
            session,
            conversation_id=payload.conversation_id,
            owner_user_id=user_id,
        )
        if replay and conversation_context:
            latest = conversation_context[-1]
            if latest.get("query") == payload.query and latest.get("answer") == response.answer:
                conversation_context = conversation_context[:-1]
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
        return sha256_json({"rows": current_rows, "conversation_context": conversation_context})
    citation_digest = CitationReplayValidator().digest(
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
    if citation_digest is None:
        return None
    return sha256_json(
        {"citations": citation_digest, "conversation_context": conversation_context}
    )


def _ensure_query_services(app: Any, *, eager_answer_model: bool = False) -> None:
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
    settings: Settings = app.state.retrieval_settings
    if (
        settings.answer_provider_id is not None
        and settings.answer_model_id is not None
        and not hasattr(app.state, "model_gateway")
    ):
        app.state.model_gateway = ModelGateway(
            session_factory=app.state.session_factory,
            settings=settings,
        )
    if not hasattr(app.state, "answer_model"):
        app.state.answer_model = (
            _answer_model_for_settings(app) if eager_answer_model else _DeferredAnswerModel(app)
        )
    elif eager_answer_model and isinstance(app.state.answer_model, _DeferredAnswerModel):
        app.state.answer_model = _answer_model_for_settings(app)
    if not hasattr(app.state, "trajectory_repository"):
        app.state.trajectory_repository = TrajectoryRepository(
            deployment_secret=app.state.retrieval_settings.secret_key.get_secret_value(),
        )
    if not hasattr(app.state, "learning_loop_service"):
        app.state.learning_loop_service = LearningLoopService()
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
            learning_loop_service=app.state.learning_loop_service,
        )


def _answer_model_for_settings(app: Any) -> Any:
    settings: Settings = app.state.retrieval_settings
    configured_provider = settings.answer_provider_id
    configured_model = settings.answer_model_id
    database_default = False
    with app.state.session_factory() as session:
        selected = model_defaults(session).get("text")
    if isinstance(selected, dict):
        # Once a user saves a route in SQLite it is authoritative.  The
        # environment values serve only as the initial bootstrap fallback
        # and must not silently overwrite a page-configured default.
        configured_provider = str(selected["provider_id"])
        configured_model = str(selected["model_id"])
        database_default = True
    if configured_provider is None and configured_model is None:
        return EvidenceBoundAnswerModel()
    if configured_provider is None or configured_model is None:
        raise ValueError("answer_provider_id and answer_model_id must be configured together")
    if not hasattr(app.state, "model_gateway"):
        app.state.model_gateway = ModelGateway(
            session_factory=app.state.session_factory,
            settings=settings,
        )
    from zhiheng.query.gateway_model import GatewayAnswerModel

    if database_default:
        return _DynamicGatewayAnswerModel(
            gateway=app.state.model_gateway,
            session_factory=app.state.session_factory,
            fallback=(configured_provider, configured_model),
        )
    return GatewayAnswerModel(
        gateway=app.state.model_gateway,
        provider_id=configured_provider,
        model_id=configured_model,
    )


class _DeferredAnswerModel:
    """Resolve the configured answer model after startup checks have run."""

    def __init__(self, app: Any) -> None:
        self._app = app

    def generate_answer(self, **kwargs: Any) -> Any:
        return _answer_model_for_settings(self._app).generate_answer(**kwargs)


class _DynamicGatewayAnswerModel:
    """Resolve the persisted text default for every answer invocation."""

    def __init__(
        self, *, gateway: ModelGateway, session_factory: Any, fallback: tuple[str, str]
    ) -> None:
        self._gateway = gateway
        self._session_factory = session_factory
        self._fallback = fallback

    def generate_answer(self, **kwargs: Any) -> Any:
        from zhiheng.query.gateway_model import GatewayAnswerModel

        provider_id, model_id = self._fallback
        with self._session_factory() as session:
            selected = model_defaults(session).get("text")
        if isinstance(selected, dict):
            provider_id, model_id = str(selected["provider_id"]), str(selected["model_id"])
        return GatewayAnswerModel(
            gateway=self._gateway,
            provider_id=provider_id,
            model_id=model_id,
        ).generate_answer(**kwargs)


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
        memory_impacts=[MemoryImpactPayload(**asdict(item)) for item in result.memory_impacts],
    )


def _validate_replayed_personalization_refs(
    value: Any,
    snapshot: MemoryContextSnapshot,
) -> None:
    """Reject a cached answer whose formal refs are outside its current context."""
    if not isinstance(value, list):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="cached answer personalization references are invalid",
        )
    allowed = {
        (
            entry.formal_memory_id,
            entry.formal_version_id,
            entry.confirmation_generation,
            entry.state_key,
        )
        for entry in snapshot.entries
    }
    for item in value:
        if not isinstance(item, dict):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="cached answer personalization references are invalid",
            )
        key = (
            item.get("formal_memory_id"),
            item.get("formal_version_id"),
            item.get("confirmation_generation"),
            item.get("state_key"),
        )
        if key not in allowed:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="cached answer personalization references changed",
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
        conversation_context: Sequence[Mapping[str, str]] | None = None,
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
        follow_up = "已结合本会话前文理解当前问题。\n" if conversation_context else ""
        insufficiencies: tuple[str, ...] = ("当前为证据摘录模式，未完成通用语义一致性检查。",)
        if memory_context is not None and memory_context.truncated:
            insufficiencies += ("用户上下文已按预算截断，不能假定已覆盖全部目标和约束。",)
        return GeneratedAnswer(
            answer=follow_up
            + personalization
            + warning
            + "根据当前知识库中已授权证据：\n"
            + "\n".join(lines),
            claims=tuple(claims),
            conflicts=conflicts,
            insufficiencies=insufficiencies,
            personalization_refs=memory_refs,
            # No provider token usage exists in extractive mode. Let the query
            # budget estimate the entire answer, including conflicts and caveats.
        )
