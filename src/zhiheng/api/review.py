from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

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
    PendingItemNotFoundError,
    StalePendingItemError,
)
from zhiheng.pending.inbox import DeferredFilter, SortOrder
from zhiheng.pending.sources import build_inbox

router = APIRouter(prefix="/v1/review", tags=["review"])
STATIC_DIR = Path(__file__).parent / "static"
SessionDep = Annotated[Session, Depends(get_db_session)]
AuthDep = Annotated[str, Depends(require_user)]
inbox = build_inbox()


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
def pending_counts(session: SessionDep, user: AuthDep) -> dict[str, Any]:
    """Undeferred pending items in total and per type; same rules as /items."""
    return inbox.counts(session, user)


@router.post("/items/{item_type}/{item_id}/{action}")
def perform_pending_action(
    item_type: str,
    item_id: str,
    action: str,
    session: SessionDep,
    user: AuthDep,
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
