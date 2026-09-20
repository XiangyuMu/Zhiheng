from __future__ import annotations

import json
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from zhiheng.api.memory import MutationDep, get_db_session, require_user
from zhiheng.core.ids import json_text, new_id, sha256_json
from zhiheng.memory import MemoryRepository
from zhiheng.memory.personal_updates import PersonalUpdate, PersonalUpdateService

router = APIRouter(prefix="/v1/personal-updates", tags=["personal-updates"])
SessionDep = Annotated[Session, Depends(get_db_session)]
AuthDep = Annotated[str, Depends(require_user)]
service = PersonalUpdateService()
memory_repository = MemoryRepository()


class PersonalUpdatePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    memory_type: str = Field(min_length=1, max_length=64)
    state_key: str = Field(min_length=1, max_length=128)
    value: dict[str, Any]
    source_kind: str = Field(min_length=1, max_length=64)
    evidence_refs: list[dict[str, Any]] = Field(default_factory=list)
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    time_sensitivity: str = Field(default="persistent", max_length=32)
    temporal_change: bool = False
    quoted: bool = False
    hypothetical: bool = False
    joke: bool = False
    inferred: bool = False
    rationale: str = Field(default="personal update", max_length=512)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


class DecisionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["confirm", "reject", "defer", "skip"]


class ContextPromptDecisionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["confirm", "reject", "supplement", "defer", "skip"]
    candidate_etag: str | None = None
    value: dict[str, Any] | None = None
    state_key: str | None = Field(default=None, min_length=1, max_length=128)
    memory_type: str = Field(default="profile", min_length=1, max_length=64)
    rationale: str = Field(default="user supplied missing context", max_length=512)


@router.post("")
def create_update(
    payload: PersonalUpdatePayload,
    session: SessionDep,
    _user_id: AuthDep,
    mutation: MutationDep,
) -> dict[str, Any]:
    operation_key, if_match = mutation
    if if_match != "*":
        raise HTTPException(status_code=412, detail="If-Match must be *")
    try:
        result = service.apply(
            session,
            PersonalUpdate(
                memory_type=payload.memory_type,
                state_key=payload.state_key,
                value=payload.value,
                source_kind=payload.source_kind,
                evidence_refs=payload.evidence_refs,
                valid_from=payload.valid_from,
                valid_to=payload.valid_to,
                time_sensitivity=payload.time_sensitivity,
                temporal_change=payload.temporal_change,
                quoted=payload.quoted,
                hypothetical=payload.hypothetical,
                joke=payload.joke,
                inferred=payload.inferred,
                rationale=payload.rationale,
                confidence=payload.confidence,
            ),
            operation_key=f"api:personal-update:{operation_key}",
            owner_user_id=_user_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"status": "completed", "result": result}


@router.get("/triage")
def list_triage(session: SessionDep, _user_id: AuthDep, limit: int = 100) -> dict[str, Any]:
    return {"items": service.list_triage(session, limit=max(1, min(limit, 500)))}


@router.get("/conflicts")
def list_conflicts(session: SessionDep, _user_id: AuthDep, limit: int = 100) -> dict[str, Any]:
    return {"items": service.list_conflicts(session, limit=max(1, min(limit, 500)))}


@router.get("/context-prompts")
def list_context_prompts(
    query: str,
    session: SessionDep,
    _user_id: AuthDep,
    limit: int = 20,
) -> dict[str, Any]:
    return {
        "items": service.context_prompts(
            session, query=query, owner_user_id=_user_id, limit=max(1, min(limit, 100))
        )
    }


@router.post("/context-prompts/{prompt_id}/decision")
def decide_context_prompt(
    prompt_id: str,
    payload: ContextPromptDecisionPayload,
    session: SessionDep,
    _user_id: AuthDep,
    mutation: MutationDep,
) -> dict[str, Any]:
    operation_key, if_match = mutation
    conflict = session.execute(
        text(
            "SELECT id FROM memory_conflicts WHERE id=:id AND status='pending'"
        ),
        {"id": prompt_id},
    ).first()
    if conflict is not None:
        if payload.decision in {"defer", "skip"}:
            result = (
                service.defer_conflict(session, prompt_id, owner_user_id=_user_id)
                if payload.decision == "defer"
                else service.skip_conflict(session, prompt_id, owner_user_id=_user_id)
            )
            return {"status": "completed", "result": result}
        if payload.decision == "supplement":
            if payload.value is None:
                raise HTTPException(status_code=400, detail="supplement requires value")
            state_key = payload.state_key
            if not state_key:
                state_row = session.execute(
                    text("SELECT state_key FROM personal_conflicts WHERE id=:id"),
                    {"id": prompt_id},
                ).first()
                state_key = str(state_row[0]) if state_row is not None else None
            if not state_key:
                raise HTTPException(status_code=409, detail="conflict state key is missing")
            service.resolve_conflict(session, prompt_id, "supplemented", owner_user_id=_user_id)
            result = service.apply(
                session,
                PersonalUpdate(
                    memory_type=payload.memory_type,
                    state_key=state_key,
                    value=payload.value,
                    source_kind="user_explicit",
                    temporal_change=True,
                    rationale=payload.rationale,
                ),
                operation_key=f"api:context-prompt:supplement:{operation_key}",
                owner_user_id=_user_id,
            )
            return {"status": "completed", "result": {"status": "supplemented", "update": result}}
        candidate = session.execute(
            text("SELECT candidate_id FROM memory_conflicts WHERE id=:id"), {"id": prompt_id}
        ).first()
        if candidate is None or not payload.candidate_etag:
            raise HTTPException(status_code=412, detail="candidate_etag is required")
        candidate_id = str(candidate[0])
        current_etag = memory_repository.candidate_etag(session, candidate_id)
        if current_etag != payload.candidate_etag:
            raise HTTPException(status_code=412, detail="candidate version changed")
        request_id = memory_repository.pending_request_id(session, candidate_id)
        if request_id is None:
            raise HTTPException(status_code=409, detail="candidate has no pending request")
        if payload.decision == "confirm":
            confirmation_result = memory_repository.confirm_request(
                session, request_id=request_id, operation_key=f"api:context-prompt:{operation_key}"
            )
            service.resolve_conflict(session, prompt_id, "confirmed", owner_user_id=_user_id)
            return {
                "status": "completed",
                "result": {
                    "status": "confirmed",
                    "formal_memory_id": confirmation_result.formal_memory_id,
                },
            }
        rejection_result = memory_repository.reject_request(
            session, request_id=request_id, operation_key=f"api:context-prompt:{operation_key}"
        )
        service.resolve_conflict(session, prompt_id, "rejected", owner_user_id=_user_id)
        return {
            "status": "completed",
            "result": {"status": "rejected", "decision_id": rejection_result.decision_id},
        }

    row = session.execute(
        text(
            """
            SELECT state_key FROM personal_prompts
            WHERE id=:id AND owner_user_id=:owner AND prompt_kind='missing' AND status='pending'
            """
        ),
        {"id": prompt_id, "owner": _user_id},
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="context prompt not found")
    if payload.decision in {"defer", "skip"}:
        return {
            "status": "completed",
            "result": service.decide_missing_prompt(session, prompt_id, payload.decision),
        }
    if payload.decision != "supplement" or payload.value is None:
        raise HTTPException(status_code=400, detail="supplement requires value")
    state_key = payload.state_key or str(row[0])
    result = service.apply(
        session,
        PersonalUpdate(
            memory_type=payload.memory_type,
            state_key=state_key,
            value=payload.value,
            source_kind="user_explicit",
            rationale=payload.rationale,
        ),
        operation_key=f"api:context-prompt:supplement:{operation_key}",
        owner_user_id=_user_id,
    )
    session.execute(
        text(
            "UPDATE personal_prompts SET status='supplemented', "
            "resolved_at=CURRENT_TIMESTAMP WHERE id=:id"
        ),
        {"id": prompt_id},
    )
    return {"status": "completed", "result": {"status": "supplemented", "update": result}}


@router.post("/conflicts/{conflict_id}/decision")
def decide_conflict(
    conflict_id: str,
    payload: DecisionPayload,
    session: SessionDep,
    _user_id: AuthDep,
    mutation: MutationDep,
) -> dict[str, Any]:
    operation_key, if_match = mutation
    request_hash = sha256_json(
        {
            "operation": "personal_conflict_decision",
            "conflict_id": conflict_id,
            "decision": payload.decision,
            "if_match": if_match,
        }
    )
    replay = _replay_decision(session, operation_key, request_hash)
    if replay is not None:
        return replay
    row = session.execute(
        text(
            """
            SELECT candidate_id FROM memory_conflicts
            WHERE id = :id AND status = 'pending'
            """
        ),
        {"id": conflict_id},
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="conflict not found")
    if payload.decision in {"defer", "skip"}:
        if if_match != "*":
            raise HTTPException(status_code=412, detail="If-Match must be * for defer")
        response = {
            "status": "completed",
            "result": (
                service.defer_conflict(session, conflict_id, owner_user_id=_user_id)
                if payload.decision == "defer"
                else service.skip_conflict(session, conflict_id, owner_user_id=_user_id)
            ),
        }
        _record_decision(session, operation_key, request_hash, response)
        return response
    if payload.decision not in {"confirm", "reject"}:
        raise HTTPException(status_code=400, detail="unsupported conflict decision")
    candidate_id = str(row[0])
    current_etag = memory_repository.candidate_etag(session, candidate_id)
    if current_etag is None:
        raise HTTPException(status_code=404, detail="candidate not found")
    if current_etag != if_match:
        raise HTTPException(status_code=412, detail="candidate version changed")
    request_id = memory_repository.pending_request_id(session, candidate_id)
    if request_id is None:
        raise HTTPException(status_code=409, detail="candidate has no pending request")
    if payload.decision == "confirm":
        result = memory_repository.confirm_request(
            session,
            request_id=request_id,
            operation_key=f"api:personal-conflict:confirm:{operation_key}",
        )
        service.resolve_conflict(
            session, conflict_id, "confirmed", owner_user_id=_user_id
        )
        response = {
            "status": "completed",
            "result": {"status": "confirmed", "formal_memory_id": result.formal_memory_id},
        }
        _record_decision(session, operation_key, request_hash, response)
        return response
    result = memory_repository.reject_request(
        session,
        request_id=request_id,
        operation_key=f"api:personal-conflict:reject:{operation_key}",
    )
    service.resolve_conflict(
        session, conflict_id, "rejected", owner_user_id=_user_id
    )
    response = {
        "status": "completed",
        "result": {"status": "rejected", "decision_id": result.decision_id},
    }
    _record_decision(session, operation_key, request_hash, response)
    return response


def _replay_decision(
    session: Session, operation_key: str, request_hash: str
) -> dict[str, Any] | None:
    row = session.execute(
        text(
            """
            SELECT request_hash, status, result_json
            FROM memory_operation_receipts
            WHERE operation_key = :operation_key
            """
        ),
        {"operation_key": f"api:personal-conflict:{operation_key}"},
    ).mappings().first()
    if row is None:
        return None
    if row["request_hash"] != request_hash:
        raise HTTPException(
            status_code=409, detail="idempotency key was reused with different payload"
        )
    result = json.loads(row["result_json"])
    return {"status": str(row["status"]), "result": result}


def _record_decision(
    session: Session,
    operation_key: str,
    request_hash: str,
    response: dict[str, Any],
) -> None:
    session.execute(
        text(
            """
            INSERT INTO memory_operation_receipts
              (id, operation_key, operation_type, request_hash, status, result_json,
               completed_at)
            VALUES (:id, :operation_key, 'personal_conflict_decision', :request_hash,
                    :status, :result_json, CURRENT_TIMESTAMP)
            """
        ),
        {
            "id": new_id(),
            "operation_key": f"api:personal-conflict:{operation_key}",
            "request_hash": request_hash,
            "status": response["status"],
            "result_json": json_text(response["result"]),
        },
    )


def install_personal_update_routes(app: FastAPI) -> None:
    app.include_router(router)
