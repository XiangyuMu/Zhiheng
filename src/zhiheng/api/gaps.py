from __future__ import annotations

from dataclasses import asdict
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from zhiheng.api.memory import _complete_operation_receipt, _insert_operation_receipt
from zhiheng.api.retrieval import WriteDep, _idempotency_replay, get_db_session, require_user
from zhiheng.core.config import Settings
from zhiheng.gaps import (
    FormalGoalRef,
    GapReasonCode,
    KnowledgeGapRecommendation,
    KnowledgeGapService,
)

router = APIRouter()

SessionDep = Annotated[Session, Depends(get_db_session)]
AuthDep = Annotated[str, Depends(require_user)]


class FormalGoalPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    formal_memory_id: str = Field(min_length=1, max_length=64)
    formal_version_id: str = Field(min_length=1, max_length=64)
    state_key: str = Field(min_length=1, max_length=128)
    effective_generation: int = Field(ge=0)


class GapRefreshRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal: FormalGoalPayload
    domain_id: str = Field(min_length=1, max_length=128)
    reason_code: GapReasonCode
    missing_coverage: list[str] = Field(min_length=1, max_length=12)
    evidence: list[str] = Field(default_factory=list, max_length=12)
    suggested_search_terms: list[str] = Field(default_factory=list, max_length=12)
    source_types: list[str] = Field(default_factory=lambda: ["paper", "book", "official_doc"])
    priority: int = Field(default=3, ge=1, le=5)
    learning_cost: Literal["low", "medium", "high"] = "medium"


class GapDismissRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(default="user dismissed", max_length=512)


class GapRecommendationPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    goal: FormalGoalPayload
    domain_id: str
    reason_code: str
    why: str
    benefit: str
    evidence: list[str]
    missing_coverage: list[str]
    suggested_search_terms: list[str]
    source_types: list[str]
    priority: int
    learning_cost: str
    requirement_key: str
    status: Literal["serving", "dismissed"] = "serving"


class GapListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[GapRecommendationPayload]


class GapRefreshResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["completed"]
    items: list[GapRecommendationPayload]


class GapDismissResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["dismissed"]
    id: str


def install_gap_routes(app: Any, settings: Settings) -> None:
    app.state.gap_settings = settings
    if not hasattr(app.state, "knowledge_gap_service"):
        app.state.knowledge_gap_service = KnowledgeGapService()
    app.include_router(router)


@router.post("/v1/knowledge-gaps/refresh", response_model=GapRefreshResponse)
def refresh_gaps(
    payload: GapRefreshRequest,
    session: SessionDep,
    request: Request,
    idempotency_key: WriteDep,
) -> GapRefreshResponse:
    operation_payload = payload.model_dump(mode="json")
    operation_key = f"api:knowledge-gaps:refresh:{idempotency_key}"
    replay = _idempotency_replay(
        session,
        operation_key=operation_key,
        operation_type="refresh_knowledge_gaps",
        payload=operation_payload,
    )
    if replay is not None:
        return GapRefreshResponse.model_validate(replay)
    receipt_id = _insert_operation_receipt(
        session,
        operation_key,
        "refresh_knowledge_gaps",
        _request_hash_for_api("refresh_knowledge_gaps", operation_payload),
    )
    recommendations = request.app.state.knowledge_gap_service.recommend(
        session,
        goal=_goal_ref(payload.goal),
        domain_id=payload.domain_id,
        reason_code=payload.reason_code,
        missing_coverage=tuple(payload.missing_coverage),
        evidence=tuple(payload.evidence),
        suggested_search_terms=tuple(payload.suggested_search_terms),
        source_types=tuple(payload.source_types),
        priority=payload.priority,
        learning_cost=payload.learning_cost,
    )
    items = [_gap_payload(item) for item in recommendations]
    response = GapRefreshResponse(status="completed", items=items)
    _complete_operation_receipt(
        session,
        receipt_id,
        status_value="completed",
        result=response.model_dump(mode="json"),
    )
    return response


@router.get("/v1/knowledge-gaps", response_model=GapListResponse)
def list_gaps(
    session: SessionDep,
    request: Request,
    _user_id: AuthDep,
    goal_id: str | None = None,
) -> GapListResponse:
    items = request.app.state.knowledge_gap_service.list_recommendations(
        session,
        goal_id=goal_id,
    )
    return GapListResponse(items=[_gap_payload(item) for item in items])


@router.post("/v1/knowledge-gaps/{gap_id}/dismiss", response_model=GapDismissResponse)
def dismiss_gap(
    gap_id: str,
    payload: GapDismissRequest,
    session: SessionDep,
    request: Request,
    idempotency_key: WriteDep,
) -> GapDismissResponse:
    operation_payload = {"gap_id": gap_id, "reason": payload.reason}
    operation_key = f"api:knowledge-gaps:dismiss:{gap_id}:{idempotency_key}"
    replay = _idempotency_replay(
        session,
        operation_key=operation_key,
        operation_type="dismiss_knowledge_gap",
        payload=operation_payload,
        completed_status="dismissed",
    )
    if replay is not None:
        return GapDismissResponse.model_validate(replay)
    dismissed = request.app.state.knowledge_gap_service.dismiss(
        session,
        gap_id,
        reason=payload.reason,
    )
    if not dismissed:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="gap not found")
    receipt_id = _insert_operation_receipt(
        session,
        operation_key,
        "dismiss_knowledge_gap",
        _request_hash_for_api("dismiss_knowledge_gap", operation_payload),
    )
    response = GapDismissResponse(status="dismissed", id=gap_id)
    _complete_operation_receipt(
        session,
        receipt_id,
        status_value="dismissed",
        result=response.model_dump(mode="json"),
    )
    return response


def _goal_ref(payload: FormalGoalPayload) -> FormalGoalRef:
    return FormalGoalRef(
        formal_memory_id=payload.formal_memory_id,
        formal_version_id=payload.formal_version_id,
        state_key=payload.state_key,
        effective_generation=payload.effective_generation,
    )


def _gap_payload(recommendation: KnowledgeGapRecommendation) -> GapRecommendationPayload:
    raw = asdict(recommendation)
    goal = raw.pop("goal")
    raw.pop("reason_code")
    return GapRecommendationPayload(
        **raw,
        goal=FormalGoalPayload(**goal),
        reason_code=recommendation.reason_code.value,
    )


def _request_hash_for_api(operation_type: str, payload: dict[str, Any]) -> str:
    from zhiheng.api.memory import _request_hash

    return _request_hash(operation_type=operation_type, payload=payload)
