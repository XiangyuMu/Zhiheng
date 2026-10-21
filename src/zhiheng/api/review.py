from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from zhiheng.api.conclusions import formalize_conclusion
from zhiheng.api.memory import (
    MutationDep,
    MutationResponse,
    _begin_api_mutation,
    _complete_api_mutation,
    get_db_session,
    require_user,
)
from zhiheng.conclusions import ConclusionRepository, list_extraction_review_results
from zhiheng.memory.personal_updates import PersonalUpdateService
from zhiheng.memory.repository import MemoryRepository
from zhiheng.pending import (
    PENDING_TYPES,
    ActionUnavailableError,
    BatchTarget,
    IndividualApprovalRequiredError,
    PendingItemNotFoundError,
    StalePendingItemError,
)
from zhiheng.pending.inbox import DeferredFilter, PendingInbox, PendingItem, SortOrder
from zhiheng.pending.sources import build_inbox

router = APIRouter(prefix="/v1/review", tags=["review"])
STATIC_DIR = Path(__file__).parent / "static"
SessionDep = Annotated[Session, Depends(get_db_session)]
AuthDep = Annotated[str, Depends(require_user)]


def _inbox(request: Request) -> PendingInbox:
    inbox: PendingInbox = request.app.state.pending_inbox
    return inbox


InboxDep = Annotated[PendingInbox, Depends(_inbox)]


def build_app_inbox(app: FastAPI) -> PendingInbox:
    """The inbox plus the approvals that write formal knowledge through the app's store."""
    inbox = build_inbox()
    repo = ConclusionRepository()

    def approve_draft(session: Session, owner: str, item: PendingItem) -> dict[str, Any]:
        key = f"pending:approve:{item.id}:{item.etag}"
        result = repo.approve(session, owner, item.id, item.etag, key)
        entry = repo.get(session, owner, item.id)
        assert entry is not None
        knowledge_id = formalize_conclusion(
            app.state.knowledge_settings, session, owner, item.id, entry
        )
        return {"status": result["status"], "knowledge_id": knowledge_id}

    def approve_relation(session: Session, owner: str, item: PendingItem) -> dict[str, Any]:
        result = repo.decide_relation(
            session, owner, item.id, "approved", f"pending:approved:{item.id}:{item.etag}"
        )
        left_id = str(result["left_id"])
        entry = repo.get(session, owner, left_id)
        assert entry is not None
        knowledge_id = formalize_conclusion(
            app.state.knowledge_settings,
            session,
            owner,
            left_id,
            entry,
            relation_kind=str(result["kind"]),
            related_entry_id=str(result["right_id"]),
        )
        return {"status": result["status"], "knowledge_id": knowledge_id}

    inbox.register_action("conclusion_draft", "approve", approve_draft)
    inbox.register_action("relation_suggestion", "approve", approve_relation)
    return inbox


def _types(value: str | None) -> list[str] | None:
    if not value:
        return None
    types = [part.strip() for part in value.split(",") if part.strip()]
    unknown = [item for item in types if item not in PENDING_TYPES]
    if unknown:
        raise HTTPException(status_code=400, detail=f"unknown pending type {unknown[0]}")
    return types


@router.get("/items")
def pending_items(
    session: SessionDep,
    user: AuthDep,
    inbox: InboxDep,
    type: str | None = None,
    deferred: DeferredFilter = "exclude",
    order: SortOrder = "oldest",
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    return inbox.list(
        session,
        user,
        types=_types(type),
        deferred=deferred,
        order=order,
        limit=limit,
        offset=offset,
    )


@router.get("/counts")
def pending_counts(session: SessionDep, user: AuthDep, inbox: InboxDep) -> dict[str, Any]:
    """Undeferred pending items in total and per type; same rules as /items."""
    return inbox.counts(session, user)


@router.post("/items/{item_type}/{item_id}/{action}")
def perform_pending_action(
    item_type: str,
    item_id: str,
    action: str,
    session: SessionDep,
    user: AuthDep,
    inbox: InboxDep,
    mutation: MutationDep,
) -> dict[str, Any]:
    key, if_match = mutation
    if item_type not in PENDING_TYPES:
        raise HTTPException(status_code=404, detail="unknown pending type")
    receipt_id, replay = _begin_api_mutation(
        session,
        operation_key=f"api:pending:{user}:{key}",
        operation_type="pending_item_action",
        payload={"type": item_type, "id": item_id, "action": action, "if_match": if_match},
    )
    if replay is not None:
        return replay.model_dump()
    try:
        result = inbox.perform(session, user, item_type, item_id, action, if_match)
    except PendingItemNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except StalePendingItemError as exc:
        raise HTTPException(status_code=412, detail=str(exc)) from exc
    except (ActionUnavailableError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    response = MutationResponse(status="completed", result=result)
    _complete_api_mutation(session, receipt_id, response)
    return response.model_dump()


class BatchItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal[PENDING_TYPES]  # type: ignore[valid-type]
    id: str = Field(min_length=1, max_length=255)
    etag: str = Field(min_length=1, max_length=255)


class BatchPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["approve", "reject", "defer", "restore"]
    items: list[BatchItem] = Field(min_length=1, max_length=500)


@router.post("/batch")
def perform_pending_batch(
    payload: BatchPayload,
    session: SessionDep,
    user: AuthDep,
    inbox: InboxDep,
    mutation: MutationDep,
) -> dict[str, Any]:
    """Apply one action item by item; each item carries its own etag and outcome."""
    key, _ = mutation
    targets = [BatchTarget(item.type, item.id, item.etag) for item in payload.items]
    if len(set(targets)) != len(targets):
        raise HTTPException(status_code=400, detail="duplicate items in batch")
    receipt_id, replay = _begin_api_mutation(
        session,
        operation_key=f"api:pending-batch:{user}:{key}",
        operation_type="pending_batch_action",
        payload=payload.model_dump(mode="json"),
    )
    if replay is not None:
        return replay.model_dump()
    try:
        results = inbox.perform_batch(session, user, payload.action, targets)
    except IndividualApprovalRequiredError as exc:
        # Raising rolls back the whole request, receipt included: no side effects.
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    response = MutationResponse(
        status="completed", result={"action": payload.action, "items": results}
    )
    _complete_api_mutation(session, receipt_id, response)
    return response.model_dump()


@router.get("/summary")
def summary(session: SessionDep, user: AuthDep, limit: int = 100) -> dict[str, Any]:
    bounded = max(1, min(limit, 500))
    conclusions = ConclusionRepository().list_drafts(session, user, limit=bounded)
    extraction_runs = list_extraction_review_results(session, user, limit=bounded)
    conflicts = PersonalUpdateService().list_conflicts(session, limit=bounded)
    memory_repo = MemoryRepository()
    for conflict in conflicts:
        candidate_id = conflict.get("candidate_id")
        if candidate_id:
            conflict["candidate_etag"] = memory_repo.candidate_etag(session, str(candidate_id))
    return {
        "counts": {
            "conclusions": len(conclusions),
            "conflicts": len(conflicts),
            "total": len(conclusions) + len(conflicts),
            "extractions": len(extraction_runs),
        },
        "conclusions": conclusions,
        "conflicts": conflicts,
        "extraction_runs": extraction_runs,
    }


def install_review_routes(app: FastAPI) -> None:
    app.state.pending_inbox = build_app_inbox(app)
    app.include_router(router)

    @app.get("/review-center", include_in_schema=False)
    def review_center() -> FileResponse:
        return FileResponse(
            STATIC_DIR / "review-center.html", media_type="text/html; charset=utf-8"
        )

    @app.get("/review-center.css", include_in_schema=False)
    def review_center_css() -> FileResponse:
        return FileResponse(STATIC_DIR / "review-center.css", media_type="text/css; charset=utf-8")

    @app.get("/review-center.js", include_in_schema=False)
    def review_center_js() -> FileResponse:
        return FileResponse(STATIC_DIR / "review-center.js", media_type="text/javascript")
