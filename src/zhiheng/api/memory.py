from __future__ import annotations

import hmac
import json
from collections.abc import Generator
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Request, status
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import RowMapping, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from zhiheng.auth import SessionService
from zhiheng.core.config import Settings
from zhiheng.core.ids import json_text, new_id, sha256_json
from zhiheng.db.session import session_scope
from zhiheng.memory import MemoryCandidateInput, MemoryRepository, MemoryValue
from zhiheng.memory.repository import (
    OperationConflictError,
    StaleConfirmationError,
    StaleMemoryStateError,
    formal_memory_etag,
)

SESSION_COOKIE = "zhiheng_session"
CSRF_COOKIE = "zhiheng_csrf"

router = APIRouter()
repository = MemoryRepository()
session_service = SessionService()
STATIC_DIR = Path(__file__).parent / "static"


class MemoryValuePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    memory_type: str = Field(min_length=1, max_length=64)
    state_key: str = Field(min_length=1, max_length=128)
    value: dict[str, Any]
    sensitivity_level: str = Field(default="private", max_length=32)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    evidence_refs: list[dict[str, Any]] = Field(default_factory=list)


class CandidatePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_type: Literal["explicit_extracted", "inferred"]
    memory_type: str = Field(min_length=1, max_length=64)
    state_key: str = Field(min_length=1, max_length=128)
    proposed_value: dict[str, Any]
    rationale: str = Field(min_length=1)
    source_kind: str = Field(min_length=1, max_length=64)
    confidence: float = Field(ge=0.0, le=1.0)
    sensitivity_level: str = Field(default="private", max_length=32)
    evidence_refs: list[dict[str, Any]] = Field(default_factory=list)

    @field_validator("source_kind")
    @classmethod
    def require_protected_source_kind(cls, value: str) -> str:
        if value in {"user_explicit", "user"}:
            raise ValueError(
                "candidate source must be extracted or inferred, not direct user input"
            )
        return value


class EditPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: dict[str, Any] | str
    change_reason: str = Field(default="user edit", max_length=512)

    def value_dict(self) -> dict[str, Any]:
        if isinstance(self.value, str):
            return {"text": self.value}
        return self.value


class ConfirmPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["confirmed"] = "confirmed"


class RejectPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["rejected"] = "rejected"


class TransitionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(default="user action", max_length=512)


class RollbackPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version_id: str
    version_no: int | None = None
    reason: str = Field(default="user rollback", max_length=512)


class BatchItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    etag: str


class BatchPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["confirm", "reject", "delete", "restore"]
    view: Literal["candidates", "formal", "history", "trash"]
    ids: list[str]
    items: list[BatchItem]

    @field_validator("items")
    @classmethod
    def require_items(cls, value: list[BatchItem]) -> list[BatchItem]:
        if not value:
            raise ValueError("batch requires at least one item")
        return value


class ItemsResponse(BaseModel):
    items: list[dict[str, Any]]


class MutationResponse(BaseModel):
    status: str
    result: dict[str, Any] | list[dict[str, Any]] | None = None


def install_memory_routes(app: Any, settings: Settings) -> None:
    app.state.memory_settings = settings
    app.add_exception_handler(StaleConfirmationError, _stale_confirmation_error)
    app.add_exception_handler(OperationConflictError, _operation_conflict_error)
    app.add_exception_handler(StaleMemoryStateError, _stale_memory_state_error)
    app.add_exception_handler(IntegrityError, _integrity_error)
    app.include_router(router)


def get_db_session(request: Request) -> Generator[Session, None, None]:
    factory = request.app.state.session_factory
    with session_scope(factory) as session:
        yield session


SessionDep = Annotated[Session, Depends(get_db_session)]


def require_user(
    request: Request,
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


def require_mutation_headers(
    request: Request,
    _user_id: AuthDep,
    csrf_header: str | None = Header(default=None, alias="X-CSRF-Token"),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    if_match: str | None = Header(default=None, alias="If-Match"),
    session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE),
) -> tuple[str, str]:
    if session_token is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing session")
    settings: Settings = request.app.state.memory_settings
    expected_csrf = _csrf_token(settings, session_token)
    if csrf_header is None or not hmac.compare_digest(csrf_header, expected_csrf):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="invalid csrf token")
    if idempotency_key is None or not idempotency_key.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="missing idempotency key",
        )
    if len(idempotency_key.strip()) > 64:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="idempotency key is too long",
        )
    if if_match is None or not if_match.strip():
        raise HTTPException(
            status_code=status.HTTP_412_PRECONDITION_FAILED,
            detail="missing If-Match",
        )
    return idempotency_key.strip(), if_match.strip()


MutationDep = Annotated[tuple[str, str], Depends(require_mutation_headers)]


@router.get("/memory-center", include_in_schema=False)
def memory_center(_user_id: AuthDep) -> FileResponse:
    return _static_file("memory-center.html", media_type="text/html; charset=utf-8", csp=True)


@router.get("/memory-center.css", include_in_schema=False)
def memory_center_css(_user_id: AuthDep) -> FileResponse:
    return _static_file("memory-center.css", media_type="text/css; charset=utf-8")


@router.get("/memory-center.js", include_in_schema=False)
def memory_center_js(_user_id: AuthDep) -> FileResponse:
    return _static_file("memory-center.js", media_type="text/javascript; charset=utf-8")


@router.get("/v1/memory/candidates", response_model=ItemsResponse)
def list_candidates(session: SessionDep, _user_id: AuthDep, limit: int = 100) -> ItemsResponse:
    return ItemsResponse(items=_list_candidates(session, limit=_limit(limit)))


@router.get("/v1/memory/formal", response_model=ItemsResponse)
def list_formal(session: SessionDep, _user_id: AuthDep, limit: int = 100) -> ItemsResponse:
    return ItemsResponse(items=_list_formal(session, limit=_limit(limit)))


@router.get("/v1/memory/history", response_model=ItemsResponse)
def list_history(session: SessionDep, _user_id: AuthDep, limit: int = 100) -> ItemsResponse:
    return ItemsResponse(items=_list_history(session, limit=_limit(limit)))


@router.get("/v1/memory/trash", response_model=ItemsResponse)
def list_trash(session: SessionDep, _user_id: AuthDep, limit: int = 100) -> ItemsResponse:
    return ItemsResponse(items=_list_trash(session, limit=_limit(limit)))


@router.get("/v1/memory/context/l0")
def l0_context(session: SessionDep, _user_id: AuthDep) -> dict[str, dict[str, Any]]:
    return repository.l0_context(session)


@router.get("/v1/memory/context/l1")
def l1_context(session: SessionDep, _user_id: AuthDep, prefix: str) -> dict[str, dict[str, Any]]:
    return repository.l1_context(session, prefix=prefix)


@router.post("/v1/memory/formal", response_model=MutationResponse)
def create_formal(
    payload: MemoryValuePayload,
    session: SessionDep,
    mutation: MutationDep,
) -> MutationResponse:
    idempotency_key, if_match = mutation
    _require_create_match(if_match)
    result = repository.commit_explicit_memory(
        session,
        MemoryValue(
            memory_type=payload.memory_type,
            state_key=payload.state_key,
            value=payload.value,
            sensitivity_level=payload.sensitivity_level,
            confidence=payload.confidence,
        ),
        operation_key=f"api:formal:create:{idempotency_key}",
        evidence_refs=payload.evidence_refs,
    )
    return _mutation_result(result)


@router.post("/v1/memory/candidates", response_model=MutationResponse)
def create_candidate(
    payload: CandidatePayload,
    session: SessionDep,
    mutation: MutationDep,
) -> MutationResponse:
    idempotency_key, if_match = mutation
    _require_create_match(if_match)
    request_hash = _request_hash(
        operation_type="create_memory_candidate",
        payload=payload.model_dump(mode="json"),
    )
    operation_key = f"api:candidate:create:{idempotency_key}"
    existing = _operation_receipt(session, operation_key, request_hash=request_hash)
    if existing is not None:
        return MutationResponse(
            status=str(existing["status"]),
            result=_json_dict(existing["result_json"]),
        )
    receipt_id = _insert_operation_receipt(
        session, operation_key, "create_memory_candidate", request_hash
    )
    candidate_id = repository.propose_candidate(
        session,
        MemoryCandidateInput(
            candidate_type=payload.candidate_type,
            memory_type=payload.memory_type,
            state_key=payload.state_key,
            proposed_value=payload.proposed_value,
            rationale=payload.rationale,
            source_kind=payload.source_kind,
            confidence=payload.confidence,
            sensitivity_level=payload.sensitivity_level,
            evidence_refs=payload.evidence_refs,
        ),
    )
    request_id = repository.request_confirmation(session, candidate_id=candidate_id)
    result = {
        "receipt_id": receipt_id,
        "candidate_id": candidate_id,
        "request_id": request_id,
        "etag": _candidate_etag(session, candidate_id),
        "status": "created",
    }
    _complete_operation_receipt(session, receipt_id, status_value="created", result=result)
    return MutationResponse(status="created", result=result)


@router.patch("/v1/memory/candidates/{candidate_id}", response_model=MutationResponse)
def edit_candidate(
    candidate_id: str,
    payload: EditPayload,
    session: SessionDep,
    mutation: MutationDep,
) -> MutationResponse:
    idempotency_key, if_match = mutation
    receipt_id, replay = _begin_api_mutation(
        session,
        operation_key=f"api:candidate:edit:{candidate_id}:{idempotency_key}",
        operation_type="edit_memory_candidate",
        payload={
            "candidate_id": candidate_id,
            "if_match": if_match,
            "body": payload.model_dump(mode="json"),
        },
    )
    if replay is not None:
        return replay
    _candidate_cas(session, candidate_id, if_match)
    version_id = repository.edit_candidate(
        session,
        candidate_id=candidate_id,
        new_value=payload.value_dict(),
        change_reason=payload.change_reason,
    )
    request_id = repository.request_confirmation(session, candidate_id=candidate_id)
    response = MutationResponse(
        status="completed",
        result={
            "receipt_id": receipt_id,
            "candidate_id": candidate_id,
            "version_id": version_id,
            "request_id": request_id,
        },
    )
    _complete_api_mutation(session, receipt_id, response)
    return response


@router.post("/v1/memory/candidates/{candidate_id}/confirm", response_model=MutationResponse)
def confirm_candidate(
    candidate_id: str,
    payload: ConfirmPayload,
    session: SessionDep,
    mutation: MutationDep,
) -> MutationResponse:
    idempotency_key, if_match = mutation
    receipt_id, replay = _begin_api_mutation(
        session,
        operation_key=f"api:candidate:confirm:{candidate_id}:{idempotency_key}",
        operation_type="confirm_memory_candidate",
        payload={
            "candidate_id": candidate_id,
            "if_match": if_match,
            "body": payload.model_dump(mode="json"),
        },
    )
    if replay is not None:
        return replay
    _candidate_cas(session, candidate_id, if_match)
    request_id = _pending_or_new_request(session, candidate_id)
    response = _mutation_result(
        repository.confirm_request(
            session,
            request_id=request_id,
            operation_key=f"domain:candidate:confirm:{receipt_id}",
        )
    )
    _complete_api_mutation(session, receipt_id, response)
    return response


@router.post("/v1/memory/candidates/{candidate_id}/reject", response_model=MutationResponse)
def reject_candidate(
    candidate_id: str,
    payload: RejectPayload,
    session: SessionDep,
    mutation: MutationDep,
) -> MutationResponse:
    idempotency_key, if_match = mutation
    receipt_id, replay = _begin_api_mutation(
        session,
        operation_key=f"api:candidate:reject:{candidate_id}:{idempotency_key}",
        operation_type="reject_memory_candidate",
        payload={
            "candidate_id": candidate_id,
            "if_match": if_match,
            "body": payload.model_dump(mode="json"),
        },
    )
    if replay is not None:
        return replay
    _candidate_cas(session, candidate_id, if_match)
    request_id = _pending_or_new_request(session, candidate_id)
    response = _mutation_result(
        repository.reject_request(
            session,
            request_id=request_id,
            operation_key=f"domain:candidate:reject:{receipt_id}",
        )
    )
    _complete_api_mutation(session, receipt_id, response)
    return response


@router.patch("/v1/memory/items/{formal_memory_id}", response_model=MutationResponse)
def patch_formal(
    formal_memory_id: str,
    payload: EditPayload,
    session: SessionDep,
    mutation: MutationDep,
) -> MutationResponse:
    idempotency_key, if_match = mutation
    receipt_id, replay = _begin_api_mutation(
        session,
        operation_key=f"api:formal:patch:{formal_memory_id}:{idempotency_key}",
        operation_type="append_formal_memory_version",
        payload={
            "formal_memory_id": formal_memory_id,
            "if_match": if_match,
            "body": payload.model_dump(mode="json"),
        },
    )
    if replay is not None:
        return replay
    _formal_cas(session, formal_memory_id, if_match)
    response = _mutation_result(
        repository.append_formal_version(
            session,
            formal_memory_id=formal_memory_id,
            value=payload.value_dict(),
            operation_key=f"domain:formal:patch:{receipt_id}",
            change_reason=payload.change_reason,
            expected_etag=if_match,
        )
    )
    _complete_api_mutation(session, receipt_id, response)
    return response


@router.delete("/v1/memory/items/{formal_memory_id}", response_model=MutationResponse)
def delete_formal(
    formal_memory_id: str,
    payload: TransitionPayload,
    session: SessionDep,
    mutation: MutationDep,
) -> MutationResponse:
    idempotency_key, if_match = mutation
    receipt_id, replay = _begin_api_mutation(
        session,
        operation_key=f"api:formal:delete:{formal_memory_id}:{idempotency_key}",
        operation_type="delete_formal_memory",
        payload={
            "formal_memory_id": formal_memory_id,
            "if_match": if_match,
            "body": payload.model_dump(mode="json"),
        },
    )
    if replay is not None:
        return replay
    _formal_cas(session, formal_memory_id, if_match)
    response = _mutation_result(
        repository.soft_delete(
            session,
            formal_memory_id=formal_memory_id,
            operation_key=f"domain:formal:delete:{receipt_id}",
            reason=payload.reason,
            expected_etag=if_match,
        )
    )
    _complete_api_mutation(session, receipt_id, response)
    return response


@router.post("/v1/memory/items/{formal_memory_id}/restore", response_model=MutationResponse)
def restore_formal(
    formal_memory_id: str,
    payload: TransitionPayload,
    session: SessionDep,
    mutation: MutationDep,
) -> MutationResponse:
    idempotency_key, if_match = mutation
    receipt_id, replay = _begin_api_mutation(
        session,
        operation_key=f"api:formal:restore:{formal_memory_id}:{idempotency_key}",
        operation_type="restore_formal_memory",
        payload={
            "formal_memory_id": formal_memory_id,
            "if_match": if_match,
            "body": payload.model_dump(mode="json"),
        },
    )
    if replay is not None:
        return replay
    _formal_cas(session, formal_memory_id, if_match)
    response = _mutation_result(
        repository.restore(
            session,
            formal_memory_id=formal_memory_id,
            operation_key=f"domain:formal:restore:{receipt_id}",
            reason=payload.reason,
            expected_etag=if_match,
        )
    )
    _complete_api_mutation(session, receipt_id, response)
    return response


@router.post("/v1/memory/items/{formal_memory_id}/rollback", response_model=MutationResponse)
def rollback_formal(
    formal_memory_id: str,
    payload: RollbackPayload,
    session: SessionDep,
    mutation: MutationDep,
) -> MutationResponse:
    idempotency_key, if_match = mutation
    receipt_id, replay = _begin_api_mutation(
        session,
        operation_key=f"api:formal:rollback:{formal_memory_id}:{idempotency_key}",
        operation_type="rollback_formal_memory",
        payload={
            "formal_memory_id": formal_memory_id,
            "if_match": if_match,
            "body": payload.model_dump(mode="json"),
        },
    )
    if replay is not None:
        return replay
    _formal_cas(session, formal_memory_id, if_match)
    response = _mutation_result(
        repository.rollback(
            session,
            formal_memory_id=formal_memory_id,
            target_version_id=payload.version_id,
            operation_key=f"domain:formal:rollback:{receipt_id}",
            reason=payload.reason,
            expected_etag=if_match,
        )
    )
    _complete_api_mutation(session, receipt_id, response)
    return response


@router.post("/v1/memory/bulk", response_model=MutationResponse)
def bulk_memory(
    payload: BatchPayload,
    session: SessionDep,
    mutation: MutationDep,
) -> MutationResponse:
    idempotency_key, if_match = mutation
    receipt_id, replay = _begin_api_mutation(
        session,
        operation_key=f"api:bulk:{idempotency_key}",
        operation_type="bulk_memory_mutation",
        payload={"if_match": if_match, "body": payload.model_dump(mode="json")},
    )
    if replay is not None:
        return replay
    _validate_batch_action(payload)
    _require_bulk_match(if_match)
    etags = {item.id: item.etag for item in payload.items}
    if set(payload.ids) != set(etags):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="ids and items differ")
    _validate_batch_items(session, payload.action, payload.ids, etags)

    savepoint = session.begin_nested()
    try:
        results = [
            _apply_batch_item(session, payload.action, item_id, etags[item_id], receipt_id)
            for item_id in payload.ids
        ]
    except Exception:
        savepoint.rollback()
        raise
    else:
        savepoint.commit()
    response = MutationResponse(
        status="completed", result=[_result_dict(result) for result in results]
    )
    _complete_api_mutation(session, receipt_id, response)
    return response


def _apply_batch_item(
    session: Session, action: str, item_id: str, etag: str, receipt_id: str
) -> Any:
    if action in {"confirm", "reject"}:
        _candidate_cas(session, item_id, etag)
        request_id = _pending_or_new_request(session, item_id)
        if action == "confirm":
            return repository.confirm_request(
                session,
                request_id=request_id,
                operation_key=f"domain:bulk:confirm:{receipt_id}:{item_id}",
            )
        return repository.reject_request(
            session,
            request_id=request_id,
            operation_key=f"domain:bulk:reject:{receipt_id}:{item_id}",
        )
    if action == "delete":
        _formal_cas(session, item_id, etag)
        return repository.soft_delete(
            session,
            formal_memory_id=item_id,
            operation_key=f"domain:bulk:delete:{receipt_id}:{item_id}",
            reason="bulk delete",
            expected_etag=etag,
        )
    if action == "restore":
        _formal_cas(session, item_id, etag)
        return repository.restore(
            session,
            formal_memory_id=item_id,
            operation_key=f"domain:bulk:restore:{receipt_id}:{item_id}",
            reason="bulk restore",
            expected_etag=etag,
        )
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="unsupported action")


def _validate_batch_items(
    session: Session, action: str, item_ids: list[str], etags: dict[str, str]
) -> None:
    for item_id in item_ids:
        if action in {"confirm", "reject"}:
            _candidate_cas(session, item_id, etags[item_id])
            if _latest_pending_request_id(session, item_id) is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="candidate has no pending confirmation request",
                )
            continue
        if action in {"delete", "restore"}:
            _formal_cas(session, item_id, etags[item_id])
            continue
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="unsupported action")


def _validate_batch_action(payload: BatchPayload) -> None:
    allowed = {
        "candidates": {"confirm", "reject"},
        "formal": {"delete"},
        "trash": {"restore"},
        "history": set(),
    }
    if payload.action not in allowed[payload.view]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="batch action is not valid for view",
        )


def _pending_or_new_request(session: Session, candidate_id: str) -> str:
    request_id = _latest_pending_request_id(session, candidate_id)
    if request_id is not None:
        return request_id
    try:
        return repository.request_confirmation(session, candidate_id=candidate_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


def _candidate_cas(session: Session, candidate_id: str, etag: str) -> None:
    current = _candidate_etag(session, candidate_id)
    if current is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="candidate not found")
    if current != etag:
        raise HTTPException(
            status_code=status.HTTP_412_PRECONDITION_FAILED,
            detail="candidate version changed",
        )


def _formal_cas(session: Session, formal_memory_id: str, etag: str) -> None:
    current = _formal_etag(session, formal_memory_id)
    if current is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="formal memory not found")
    if current != etag:
        raise HTTPException(
            status_code=status.HTTP_412_PRECONDITION_FAILED,
            detail="formal memory version changed",
        )


def _list_candidates(session: Session, *, limit: int) -> list[dict[str, Any]]:
    rows = session.execute(
        text(
            """
            SELECT mc.*, mcv.version_no, mcv.value_json, mcv.change_reason,
                   mcv.created_by_role, mcv.created_at AS version_created_at
            FROM memory_candidates mc
            JOIN memory_candidate_versions mcv ON mcv.id = mc.current_version_id
            WHERE mc.status IN ('pending_confirmation', 'edited')
            ORDER BY mc.updated_at DESC, mc.created_at DESC
            LIMIT :limit
            """
        ),
        {"limit": limit},
    ).mappings()
    items = [_candidate_row(row) for row in rows]
    return _decorate_memory_items(session, items, target_type="memory_candidate")


def _list_formal(session: Session, *, limit: int) -> list[dict[str, Any]]:
    rows = session.execute(
        text(
            """
            SELECT cfm.*, fmv.version_no, fmv.change_reason, fmv.created_by_role,
                   fmv.source_kind, fmv.source_candidate_id, fmv.source_decision_id,
                   (
                     SELECT mge.reason FROM memory_generation_events mge
                     WHERE mge.state_key = cfm.state_key
                       AND mge.generation = cfm.current_generation
                   ) AS lifecycle_reason,
                   fmv.created_at AS version_created_at
            FROM current_formal_memory cfm
            JOIN formal_memory_versions fmv ON fmv.id = cfm.current_version_id
            ORDER BY cfm.updated_at DESC, cfm.created_at DESC
            LIMIT :limit
            """
        ),
        {"limit": limit},
    ).mappings()
    items = [_formal_row(row) for row in rows]
    return _decorate_memory_items(session, items, target_type="formal_memory")


def _list_history(session: Session, *, limit: int) -> list[dict[str, Any]]:
    rows = session.execute(
        text(
            """
            SELECT fm.id AS formal_memory_id, fm.memory_type, fm.state_key, fm.status,
                   fm.current_version_id, fm.current_generation, fm.sensitivity_level,
                   fm.confidence, fm.created_at, fm.updated_at,
                   fmv.id AS version_id, fmv.version_no, fmv.value_json,
                   fmv.change_reason, fmv.created_by_role, fmv.generation,
                   fmv.source_kind, fmv.source_candidate_id, fmv.source_decision_id,
                   (
                     SELECT mge.reason FROM memory_generation_events mge
                     WHERE mge.state_key = fm.state_key
                       AND mge.generation = fmv.generation
                   ) AS lifecycle_reason,
                   fmv.status AS version_status, fmv.created_at AS version_created_at
            FROM formal_memory_versions fmv
            JOIN formal_memories fm ON fm.id = fmv.formal_memory_id
            WHERE fm.status <> 'privacy_erased'
            ORDER BY fmv.created_at DESC
            LIMIT :limit
            """
        ),
        {"limit": limit},
    ).mappings()
    items = [_history_row(row) for row in rows]
    return _decorate_memory_items(session, items, target_type="formal_memory")


def _list_trash(session: Session, *, limit: int) -> list[dict[str, Any]]:
    rows = session.execute(
        text(
            """
            SELECT fm.*, fmv.version_no, fmv.value_json, fmv.change_reason,
                   fmv.created_by_role, fmv.source_kind, fmv.source_candidate_id,
                   fmv.source_decision_id,
                   (
                     SELECT mge.reason FROM memory_generation_events mge
                     WHERE mge.state_key = fm.state_key
                       AND mge.generation = fm.current_generation
                   ) AS lifecycle_reason,
                   fmv.created_at AS version_created_at
            FROM formal_memories fm
            JOIN formal_memory_versions fmv ON fmv.id = fm.current_version_id
            WHERE fm.status = 'deleted'
            ORDER BY fm.deleted_at DESC, fm.updated_at DESC
            LIMIT :limit
            """
        ),
        {"limit": limit},
    ).mappings()
    items = [_formal_row(row) for row in rows]
    return _decorate_memory_items(session, items, target_type="formal_memory")


def _latest_pending_request_id(session: Session, candidate_id: str) -> str | None:
    row = session.execute(
        text(
            """
            SELECT id
            FROM memory_confirmation_requests
            WHERE candidate_id = :candidate_id
              AND status = 'pending'
            ORDER BY created_at DESC
            LIMIT 1
            """
        ),
        {"candidate_id": candidate_id},
    ).mappings().first()
    return None if row is None else str(row["id"])


def _candidate_etag(session: Session, candidate_id: str) -> str | None:
    row = session.execute(
        text("SELECT id, current_version_id, status FROM memory_candidates WHERE id = :id"),
        {"id": candidate_id},
    ).mappings().first()
    return None if row is None else _candidate_etag_value(row)


def _formal_etag(session: Session, formal_memory_id: str) -> str | None:
    return repository.formal_etag(session, formal_memory_id)


def _operation_receipt(
    session: Session, operation_key: str, *, request_hash: str
) -> RowMapping | None:
    row = session.execute(
        text(
            """
            SELECT id, result_json, status, request_hash
            FROM memory_operation_receipts
            WHERE operation_key = :operation_key
            """
        ),
        {"operation_key": operation_key},
    ).mappings().first()
    if row is None:
        return None
    if row["request_hash"] != request_hash:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="idempotency key was reused with different payload",
        )
    return row


def _begin_api_mutation(
    session: Session,
    *,
    operation_key: str,
    operation_type: str,
    payload: dict[str, Any],
) -> tuple[str, MutationResponse | None]:
    request_hash = _request_hash(operation_type=operation_type, payload=payload)
    existing = _operation_receipt(session, operation_key, request_hash=request_hash)
    if existing is not None:
        return "", MutationResponse(
            status=str(existing["status"]),
            result=_json_result(existing["result_json"]),
        )
    return (
        _insert_operation_receipt(session, operation_key, operation_type, request_hash),
        None,
    )


def _insert_operation_receipt(
    session: Session, operation_key: str, operation_type: str, request_hash: str
) -> str:
    receipt_id = new_id()
    session.execute(
        text(
            """
            INSERT INTO memory_operation_receipts (
              id, operation_key, operation_type, request_hash, status, result_json
            )
            VALUES (:id, :operation_key, :operation_type, :request_hash, 'started', '{}')
            """
        ),
        {
            "id": receipt_id,
            "operation_key": operation_key,
            "operation_type": operation_type,
            "request_hash": request_hash,
        },
    )
    return receipt_id


def _complete_operation_receipt(
    session: Session,
    receipt_id: str,
    *,
    status_value: str,
    result: dict[str, Any] | list[dict[str, Any]],
) -> None:
    session.execute(
        text(
            """
            UPDATE memory_operation_receipts
            SET status = :status, result_json = :result_json, completed_at = CURRENT_TIMESTAMP
            WHERE id = :id
            """
        ),
        {
            "id": receipt_id,
            "status": status_value,
            "result_json": json_text(result),
        },
    )


def _complete_api_mutation(
    session: Session, receipt_id: str, response: MutationResponse
) -> None:
    if response.result is None:
        raise ValueError("mutation receipt requires a result")
    _complete_operation_receipt(
        session,
        receipt_id,
        status_value=response.status,
        result=response.result,
    )


def _json_dict(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        decoded = json.loads(value)
        if isinstance(decoded, dict):
            return decoded
    raise TypeError("expected JSON object")


def _json_result(value: object) -> dict[str, Any] | list[dict[str, Any]] | None:
    decoded = json.loads(value) if isinstance(value, str) else value
    if decoded is None or isinstance(decoded, dict):
        return decoded
    if isinstance(decoded, list) and all(isinstance(item, dict) for item in decoded):
        return decoded
    raise TypeError("expected JSON mutation result")


def _candidate_row(row: RowMapping) -> dict[str, Any]:
    item = dict(row)
    item["value"] = _json_dict(item.pop("value_json"))
    item["reason"] = item.get("change_reason")
    item["etag"] = _candidate_etag_value(item)
    item["version_id"] = str(item["current_version_id"])
    return item


def _formal_row(row: RowMapping) -> dict[str, Any]:
    item = dict(row)
    item["value"] = _json_dict(item.pop("value_json"))
    item["etag"] = _formal_etag_value(item)
    item["version_id"] = str(item["current_version_id"])
    item["confirmation_generation"] = item["current_generation"]
    return item


def _history_row(row: RowMapping) -> dict[str, Any]:
    item = dict(row)
    item["id"] = str(item["version_id"])
    item["memory_item_id"] = str(item["formal_memory_id"])
    item["value"] = _json_dict(item.pop("value_json"))
    item["etag"] = _formal_etag_value(
        {
            "id": item["formal_memory_id"],
            "current_version_id": item["current_version_id"],
            "current_generation": item["current_generation"],
            "status": item["status"],
        }
    )
    item["confirmation_generation"] = item["generation"]
    return item


def _candidate_etag_value(item: RowMapping | dict[str, Any]) -> str:
    return sha256_json(
        {
            "id": str(item["id"]),
            "current_version_id": str(item["current_version_id"]),
            "status": str(item["status"]),
        }
    )


def _formal_etag_value(item: RowMapping | dict[str, Any]) -> str:
    return formal_memory_etag(item)


def _decorate_memory_items(
    session: Session,
    items: list[dict[str, Any]],
    *,
    target_type: str,
) -> list[dict[str, Any]]:
    for item in items:
        target_id = (
            str(item["formal_memory_id"])
            if target_type == "formal_memory" and "formal_memory_id" in item
            else str(item["id"])
        )
        version_id = str(item["version_id"])
        refs = session.execute(
            text(
                """
                SELECT evidence_object_id, content_span_id, trajectory_id, support_type
                FROM memory_evidence_refs
                WHERE target_type = :target_type
                  AND target_id = :target_id
                  AND target_version_id = :target_version_id
                ORDER BY support_type, evidence_object_id, content_span_id, trajectory_id
                """
            ),
            {
                "target_type": target_type,
                "target_id": target_id,
                "target_version_id": version_id,
            },
        ).mappings()
        item["version_evidence_refs"] = [dict(ref) for ref in refs]
        if target_type == "memory_candidate":
            item["hypothetical_impact"] = (
                f"确认后可能影响与 {item['state_key']} 相关的回答、检索规划和推荐。"
            )
        else:
            item["hypothetical_impact"] = (
                f"可能影响与 {item['state_key']} 相关的回答、检索规划和推荐；"
                "重要判断仍需版本绑定的 L2 证据。"
            )
    return items


def _limit(limit: int) -> int:
    if limit < 1 or limit > 500:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="limit must be 1..500")
    return limit


def _require_create_match(if_match: str) -> None:
    if if_match != "*":
        raise HTTPException(
            status_code=status.HTTP_412_PRECONDITION_FAILED,
            detail="If-Match must be *",
        )


def _require_bulk_match(if_match: str) -> None:
    if if_match != "bulk-selection":
        raise HTTPException(
            status_code=status.HTTP_412_PRECONDITION_FAILED,
            detail="If-Match must be bulk-selection",
        )


def _request_hash(*, operation_type: str, payload: dict[str, Any]) -> str:
    return sha256_json({"operation_type": operation_type, "payload": payload})


def _mutation_result(result: Any) -> MutationResponse:
    return MutationResponse(status=str(result.status), result=_result_dict(result))


def _result_dict(result: Any) -> dict[str, Any]:
    return {
        "receipt_id": result.receipt_id,
        "decision_id": result.decision_id,
        "formal_memory_id": result.formal_memory_id,
        "formal_version_id": result.formal_version_id,
        "generation": result.generation,
        "status": result.status,
    }


def _static_file(name: str, *, media_type: str, csp: bool = False) -> FileResponse:
    path = STATIC_DIR / name
    response = FileResponse(path, media_type=media_type)
    response.headers["X-Content-Type-Options"] = "nosniff"
    if csp:
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; "
            "frame-ancestors 'none'"
        )
    return response


def _csrf_token(settings: Settings, session_token: str) -> str:
    from zhiheng.api.main import _csrf_token as main_csrf_token

    return main_csrf_token(settings, session_token)


def _stale_confirmation_error(_request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content={"detail": str(exc)},
    )


def _operation_conflict_error(_request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content={"detail": str(exc)},
    )


def _stale_memory_state_error(_request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_412_PRECONDITION_FAILED,
        content={"detail": str(exc)},
    )


def _integrity_error(_request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content={"detail": str(exc)},
    )
