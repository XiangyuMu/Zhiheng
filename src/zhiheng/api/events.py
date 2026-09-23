from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from zhiheng.api.memory import MutationDep, get_db_session, require_user
from zhiheng.events import EventRepository

router = APIRouter(prefix="/v1/events", tags=["events"])
SessionDep = Annotated[Session, Depends(get_db_session)]
AuthDep = Annotated[str, Depends(require_user)]
repo = EventRepository()


class EventEditPayload(BaseModel):
    title: str = Field(min_length=1, max_length=512)
    summary: str = Field(min_length=1, max_length=12000)


class EventConfirmPayload(BaseModel):
    decision: str = "confirmed"
    confirmation_request_id: str | None = None
    expected_version_id: str | None = None


@router.get("/candidates")
def candidates(session: SessionDep, user: AuthDep) -> dict[str, Any]:
    return {"items": repo.list_candidates(session, user)}


@router.get("/{event_id}")
def get_event(event_id: str, session: SessionDep, user: AuthDep) -> dict[str, Any]:
    item = repo.get(session, user, event_id)
    if item is None:
        raise HTTPException(404, "event not found")
    item["etag"] = repo.etag(item)
    return item


@router.patch("/{event_id}")
def edit_event(
    event_id: str,
    payload: EventEditPayload,
    session: SessionDep,
    user: AuthDep,
    mutation: MutationDep,
) -> dict[str, Any]:
    key, _if_match = mutation
    try:
        return repo.edit(
            session,
            user,
            event_id,
            title=payload.title,
            summary=payload.summary,
            operation_key=key,
            if_match=_if_match,
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/{event_id}/confirmation")
def confirm(
    event_id: str,
    payload: EventConfirmPayload,
    session: SessionDep,
    user: AuthDep,
    mutation: MutationDep,
) -> dict[str, Any]:
    decision = payload.decision
    if decision not in {"confirmed", "rejected"}:
        raise HTTPException(422, "invalid decision")
    idempotency_key, _if_match = mutation
    try:
        return repo.decide(
            session,
            user,
            event_id,
            decision,
            idempotency_key,
            if_match=_if_match,
            request_id=payload.confirmation_request_id,
            expected_version_id=payload.expected_version_id,
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


def install_event_routes(app: FastAPI) -> None:
    app.include_router(router)
