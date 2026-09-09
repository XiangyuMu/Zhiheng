from __future__ import annotations

import sqlite3
from dataclasses import asdict
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.orm import Session

from zhiheng.api.memory import _complete_operation_receipt, _insert_operation_receipt
from zhiheng.api.retrieval import (
    BudgetUsagePayload,
    CitationPayload,
    ClaimPayload,
    PersonalizationRefPayload,
    WriteDep,
    _citation_payload,
    _idempotency_replay,
    get_db_session,
    require_user,
)
from zhiheng.core.config import Settings
from zhiheng.core.ids import sha256_text
from zhiheng.decisions import (
    SUPPORTED_DECISION_TYPES,
    SUPPORTED_TEMPLATE_IDS,
    DecisionAnalysis,
    DecisionMemorySavePort,
    DecisionOption,
    DecisionRequest,
    DecisionSupportService,
    decision_query_text,
)
from zhiheng.decisions.service import DecisionContextStaleError, begin_decision_save
from zhiheng.evolution.releases import ReleaseController
from zhiheng.memory import MemoryRepository, MemoryValue
from zhiheng.memory.context import TOPIC_PREFIX_PATTERN, MemoryContextService
from zhiheng.query.contracts import StopReason
from zhiheng.retrieval import (
    Citation,
    CitationBuilder,
    HybridRetriever,
    RetrievalAuthorizer,
    StructuredLookupService,
)
from zhiheng.retrieval.contracts import AuthorizedContextManifest
from zhiheng.retrieval.replay import CitationReplayValidator

router = APIRouter()

SessionDep = Annotated[Session, Depends(get_db_session)]
AuthDep = Annotated[str, Depends(require_user)]


class DecisionOptionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1, max_length=128)
    description: str = Field(min_length=1, max_length=2000)


class DecisionAnalyzeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    problem: str = Field(min_length=1, max_length=4000)
    options: list[DecisionOptionPayload] = Field(min_length=1, max_length=12)
    formal_goal_refs: list[
        Annotated[
            str,
            Field(
                min_length=6,
                max_length=128,
                pattern=r"^goal\.[A-Za-z0-9_.-]+$",
            ),
        ]
    ] = Field(default_factory=list, max_length=12)
    decision_type: str = Field(default="compare", max_length=64)
    template_id: str = Field(default="g005.default", max_length=128)
    evidence_query: str | None = Field(default=None, max_length=4000)
    memory_topic_prefix: str | None = Field(
        default=None, min_length=2, max_length=128, pattern=TOPIC_PREFIX_PATTERN
    )

    @field_validator("formal_goal_refs")
    @classmethod
    def unique_goal_refs(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("formal_goal_refs must be unique")
        return value


class DecisionSaveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: str | None = Field(default=None, max_length=2000)


class DecisionAnalysisResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    benefits: list[str]
    costs: list[str]
    risks: list[str]
    opportunity_costs: list[str]
    assumptions: list[str]
    citations: list[CitationPayload]
    preference: str | None
    change_conditions: list[str]
    recommendation: str | None
    external_action_count: int
    personalization_refs: list[PersonalizationRefPayload] = Field(default_factory=list)
    memory_context_digest: str | None = None
    memory_source_ids: list[str] = Field(default_factory=list)
    recommended_next_step: str | None = None
    option_reviews: list[dict[str, str]] = Field(default_factory=list)
    claims: list[ClaimPayload] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    insufficiencies: list[str] = Field(default_factory=list)
    formal_goal_refs: list[PersonalizationRefPayload] = Field(default_factory=list)
    retrieval_run_ids: list[str] = Field(default_factory=list)
    release_id: str | None = None
    stop_reason: StopReason | Literal["insufficient_evidence"]
    budget_usage: BudgetUsagePayload


class DecisionSaveResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["saved"]
    run_id: str
    saved_memory_id: str
    external_action_count: int


def install_decision_routes(app: Any, settings: Settings) -> None:
    app.state.decision_settings = settings
    if not hasattr(app.state, "decision_support_service"):
        if not hasattr(app.state, "memory_context_service"):
            app.state.memory_context_service = MemoryContextService()
        app.state.decision_support_service = DecisionSupportService(
            model_gateway=app.state.answer_model,
            answerer=app.state.bounded_rag_service,
            memory_context_service=app.state.memory_context_service,
        )
    if not hasattr(app.state, "decision_context_retriever"):
        app.state.decision_context_retriever = app.state.hybrid_retriever
    if not hasattr(app.state, "decision_authorizer"):
        app.state.decision_authorizer = RetrievalAuthorizer()
    if not hasattr(app.state, "decision_save_port"):
        app.state.decision_save_port = FormalDecisionMemorySavePort()
    app.include_router(router)


@router.post("/v1/decisions/analyze", response_model=DecisionAnalysisResponse)
def analyze_decision(
    payload: DecisionAnalyzeRequest,
    session: SessionDep,
    request: Request,
    idempotency_key: WriteDep,
) -> DecisionAnalysisResponse:
    operation_payload = payload.model_dump(mode="json")
    _validate_decision_payload(payload)
    operation_key = f"api:decisions:analyze:{idempotency_key}"
    replay = _idempotency_replay(
        session,
        operation_key=operation_key,
        operation_type="analyze_decision",
        payload=operation_payload,
    )
    if replay is not None:
        _ensure_current_decision_replay(
            session,
            replay,
            payload=payload,
            memory_context_service=request.app.state.memory_context_service,
        )
        return DecisionAnalysisResponse.model_validate(replay)
    receipt_id = _insert_operation_receipt(
        session,
        operation_key,
        "analyze_decision",
        _request_hash_for_api("analyze_decision", operation_payload),
    )
    session.commit()

    manifest, citations, retrieval_run_id, release_id = _decision_context(
        session,
        request.app.state.decision_context_retriever,
        payload.evidence_query or payload.problem,
        deployment_secret=request.app.state.decision_settings.secret_key.get_secret_value(),
    )
    decision_request = DecisionRequest(
        problem=payload.problem,
        options=tuple(
            DecisionOption(label=option.label, description=option.description)
            for option in payload.options
        ),
        formal_goal_refs=tuple(payload.formal_goal_refs),
        decision_type=payload.decision_type,
        template_id=payload.template_id,
    )
    _validate_decision_request(decision_request)
    provisional_query = decision_query_text(
        DecisionRequest(
            problem=payload.problem,
            options=decision_request.options,
            formal_goal_refs=(),
            decision_type=payload.decision_type,
            template_id=payload.template_id,
        ),
        memory_context=None,
    )
    memory_context = request.app.state.memory_context_service.load(
        session,
        query_hash=sha256_text(provisional_query),
        topic_prefix=payload.memory_topic_prefix,
    )
    try:
        decision_query_hash = sha256_text(
            decision_query_text(decision_request, memory_context=memory_context)
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    memory_context = request.app.state.memory_context_service.load(
        session,
        query_hash=decision_query_hash,
        topic_prefix=payload.memory_topic_prefix,
    )
    citation_digest = CitationReplayValidator().digest(session, citations)
    if citations and citation_digest is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="decision citations are no longer authorized",
        )
    try:
        analysis = request.app.state.decision_support_service.analyze(
            session,
            decision_request,
            manifest=manifest,
            citations=citations,
            memory_context=memory_context,
            citation_replay_digest=citation_digest,
            retrieval_run_ids=(retrieval_run_id,),
            release_id=release_id,
        )
    except DecisionContextStaleError as exc:
        _complete_operation_receipt(
            session,
            receipt_id,
            status_value="failed",
            result={"reason": "context_stale"},
        )
        session.commit()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    response = _analysis_response(analysis, context_chunks=len(getattr(manifest, "chu" + "nks")))
    _complete_operation_receipt(
        session,
        receipt_id,
        status_value="completed",
        result=response.model_dump(mode="json"),
    )
    return response


@router.post("/v1/decisions/{run_id}/save", response_model=DecisionSaveResponse)
def save_decision(
    run_id: str,
    payload: DecisionSaveRequest,
    session: SessionDep,
    request: Request,
    idempotency_key: WriteDep,
) -> DecisionSaveResponse:
    operation_payload = {"run_id": run_id, "body": payload.model_dump(mode="json")}
    operation_key = f"api:decisions:save:{run_id}:{idempotency_key}"
    replay = _idempotency_replay(
        session,
        operation_key=operation_key,
        operation_type="save_decision",
        payload=operation_payload,
        completed_status="saved",
    )
    if replay is not None:
        _ensure_current_saved_decision(
            session,
            replay,
            run_id=run_id,
            memory_context_service=request.app.state.memory_context_service,
        )
        return DecisionSaveResponse.model_validate(replay)
    begin_decision_save(session)
    # Recheck after acquiring the write lock: another save may have won meanwhile.
    replay = _idempotency_replay(
        session,
        operation_key=operation_key,
        operation_type="save_decision",
        payload=operation_payload,
        completed_status="saved",
    )
    if replay is not None:
        _ensure_current_saved_decision(
            session,
            replay,
            run_id=run_id,
            memory_context_service=request.app.state.memory_context_service,
        )
        return DecisionSaveResponse.model_validate(replay)
    analysis = request.app.state.decision_support_service.get_analysis(session, run_id)
    if analysis is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="decision run not found")
    _ensure_analysis_current(
        session,
        analysis,
        memory_context_service=request.app.state.memory_context_service,
    )
    receipt_id = _insert_operation_receipt(
        session,
        operation_key,
        "save_decision",
        _request_hash_for_api("save_decision", operation_payload),
    )
    try:
        saved_memory_id = request.app.state.decision_support_service.request_save(
            session,
            analysis,
            save_port=request.app.state.decision_save_port,
            note=payload.note,
        )
    except DecisionContextStaleError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    response = DecisionSaveResponse(
        status="saved",
        run_id=run_id,
        saved_memory_id=saved_memory_id,
        external_action_count=analysis.external_action_count,
    )
    _complete_operation_receipt(
        session,
        receipt_id,
        status_value="saved",
        result=response.model_dump(mode="json"),
    )
    return response


def _decision_context(
    session: Session,
    retriever: HybridRetriever,
    query: str,
    *,
    deployment_secret: str | None = None,
) -> tuple[AuthorizedContextManifest, tuple[Citation, ...], str, str]:
    raw = session.connection().connection
    connection = getattr(raw, "driver_connection", raw)
    if not isinstance(connection, sqlite3.Connection):
        raise TypeError("decision retrieval requires SQLite release state")
    release_context = ReleaseController.from_db(
        connection, deployment_secret=deployment_secret
    ).load_default_head("retrieval.answer_strategy")
    if release_context is None:
        raise ValueError("decision retrieval requires a stable release")
    result = retriever.search(session, query, limit=8, release_context=release_context)
    builder = CitationBuilder()
    citations: list[Citation] = []
    for item in getattr(result.manifest, "chu" + "nks"):
        citations.append(
            builder.build(
                result.manifest,
                chunk_id=item.chunk_id,
                start_offset=item.span_start,
                end_offset=item.span_end,
            )
        )
    return result.manifest, tuple(citations), result.run_id, release_context.release_id


def _analysis_response(
    analysis: DecisionAnalysis,
    *,
    context_chunks: int,
) -> DecisionAnalysisResponse:
    budget = analysis.budget_usage or _empty_usage()
    return DecisionAnalysisResponse(
        run_id=analysis.run_id,
        benefits=list(analysis.benefits),
        costs=list(analysis.costs),
        risks=list(analysis.risks),
        opportunity_costs=list(analysis.opportunity_costs),
        assumptions=list(analysis.assumptions),
        citations=[_citation_payload(citation) for citation in analysis.citations],
        preference=analysis.preference,
        change_conditions=list(analysis.change_conditions),
        recommendation=analysis.recommendation,
        external_action_count=analysis.external_action_count,
        personalization_refs=[
            PersonalizationRefPayload(**asdict(ref)) for ref in analysis.personalization_refs
        ],
        memory_context_digest=analysis.memory_context_digest,
        memory_source_ids=list(analysis.memory_source_ids),
        claims=[ClaimPayload(**asdict(claim)) for claim in analysis.claims],
        conflicts=list(analysis.conflicts),
        insufficiencies=list(analysis.insufficiencies),
        formal_goal_refs=[
            PersonalizationRefPayload(**asdict(ref)) for ref in analysis.formal_goal_refs
        ],
        retrieval_run_ids=list(analysis.retrieval_run_ids),
        release_id=analysis.release_id,
        recommended_next_step=analysis.recommendation,
        option_reviews=[
            {"label": label, "benefit": benefit, "cost": cost}
            for label, benefit, cost in zip(
                analysis.option_labels,
                analysis.benefits,
                analysis.costs,
                strict=False,
            )
        ],
        stop_reason=(
            "insufficient_evidence"
            if analysis.stop_reason == "insufficient_evidence"
            else StopReason(analysis.stop_reason)
        ),
        budget_usage=BudgetUsagePayload(
            **{
                **asdict(budget),
                "context_chunks": budget.context_chunks or context_chunks,
            }
        ),
    )


def _empty_usage() -> Any:
    from zhiheng.query import BudgetUsage

    return BudgetUsage()


def _request_hash_for_api(operation_type: str, payload: dict[str, Any]) -> str:
    from zhiheng.api.memory import _request_hash

    return _request_hash(operation_type=operation_type, payload=payload)


class FormalDecisionMemorySavePort(DecisionMemorySavePort):
    def save_decision_memory(
        self,
        session: Session,
        analysis: DecisionAnalysis,
        *,
        note: str | None = None,
    ) -> str:
        if analysis.stop_reason != "completed":
            raise ValueError("only completed decision analyses can be saved")
        result = MemoryRepository().commit_explicit_memory(
            session,
            MemoryValue(
                memory_type="decision",
                state_key=f"decision.{analysis.run_id}",
                value={
                    "recommendation": analysis.recommendation,
                    "risks": list(analysis.risks),
                    "change_conditions": list(analysis.change_conditions),
                    "external_action_count": analysis.external_action_count,
                    "note": note,
                    "memory_context_digest": analysis.memory_context_digest,
                    "memory_source_ids": list(analysis.memory_source_ids),
                    "personalization_refs": [asdict(ref) for ref in analysis.personalization_refs],
                    "formal_goal_refs": [asdict(ref) for ref in analysis.formal_goal_refs],
                    "citation_replay_digest": analysis.citation_replay_digest,
                    "claims": [asdict(claim) for claim in analysis.claims],
                    "conflicts": list(analysis.conflicts),
                    "assumptions": list(analysis.assumptions),
                    "insufficiencies": list(analysis.insufficiencies),
                    "retrieval_run_ids": list(analysis.retrieval_run_ids),
                    "release_id": analysis.release_id,
                },
            ),
            operation_key=f"decision-save:{analysis.run_id}",
            evidence_refs=[
                {
                    "evidence_object_id": citation.evidence_object_id,
                    "content_span_id": citation.content_span_id,
                }
                for citation in analysis.citations
                if citation.evidence_object_id is not None and citation.content_span_id is not None
            ],
        )
        if result.formal_memory_id is None:
            raise ValueError("decision save did not produce formal memory")
        return result.formal_memory_id


def _validate_decision_request(decision_request: DecisionRequest) -> None:
    if decision_request.decision_type not in SUPPORTED_DECISION_TYPES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="unsupported decision_type"
        )
    if decision_request.template_id not in SUPPORTED_TEMPLATE_IDS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="unsupported template_id"
        )


def _validate_decision_payload(payload: DecisionAnalyzeRequest) -> None:
    _validate_decision_request(
        DecisionRequest(
            problem=payload.problem,
            options=tuple(
                DecisionOption(label=option.label, description=option.description)
                for option in payload.options
            ),
            formal_goal_refs=tuple(payload.formal_goal_refs),
            decision_type=payload.decision_type,
            template_id=payload.template_id,
        )
    )


def _ensure_current_decision_replay(
    session: Session,
    replay: dict[str, Any],
    *,
    payload: DecisionAnalyzeRequest,
    memory_context_service: MemoryContextService,
) -> None:
    digest = replay.get("memory_context_digest")
    run_id = replay.get("run_id")
    if not isinstance(run_id, str):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="decision run binding is missing",
        )
    analysis = DecisionSupportService().get_analysis(session, run_id)
    if analysis is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="decision analysis is no longer available",
        )
    query_hash = analysis.decision_query_hash
    if digest is None or not isinstance(query_hash, str):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="decision memory context binding is missing",
        )
    current = memory_context_service.load(
        session,
        query_hash=query_hash,
        topic_prefix=analysis.memory_topic_prefix,
    )
    if current.digest != digest:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="decision memory context changed; use a new idempotency key",
        )
    _ensure_analysis_current(
        session,
        analysis,
        memory_context_service=memory_context_service,
        require_completed=False,
    )


def _ensure_current_saved_decision(
    session: Session,
    replay: dict[str, Any],
    *,
    run_id: str,
    memory_context_service: MemoryContextService,
) -> None:
    del memory_context_service
    saved_memory_id = replay.get("saved_memory_id")
    if not isinstance(saved_memory_id, str):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="decision save binding is missing"
        )
    rows = StructuredLookupService().lookup(
        session,
        selector="memory.state_key",
        value=f"decision.{run_id}",
    )
    if replay.get("run_id") != run_id or len(rows) != 1 or rows[0]["source_id"] != saved_memory_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="saved decision memory is no longer current",
        )


def _ensure_analysis_current(
    session: Session,
    analysis: DecisionAnalysis,
    *,
    memory_context_service: MemoryContextService,
    require_completed: bool = True,
) -> None:
    if require_completed and analysis.stop_reason != "completed":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="only completed current decision analyses can be saved",
        )
    if analysis.memory_context_digest is None or analysis.decision_query_hash is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="decision memory context binding is missing",
        )
    current = memory_context_service.load(
        session,
        query_hash=analysis.decision_query_hash,
        topic_prefix=analysis.memory_topic_prefix,
    )
    if current.digest != analysis.memory_context_digest:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="decision memory context changed; analyze again",
        )
    if analysis.citations and analysis.citation_replay_digest is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="decision citation binding is missing"
        )
    current_citation_digest = CitationReplayValidator().digest(session, analysis.citations)
    if analysis.citations and current_citation_digest != analysis.citation_replay_digest:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="decision citations changed; analyze again"
        )
