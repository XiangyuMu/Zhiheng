from __future__ import annotations

import hmac
import json
from typing import Annotated, Any, Literal, cast

from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import RowMapping, text
from sqlalchemy.orm import Session, sessionmaker

from zhiheng.auth import SessionService
from zhiheng.core.config import Settings
from zhiheng.core.ids import json_text, new_id, sha256_json, sha256_text
from zhiheng.db.session import session_scope
from zhiheng.knowledge import (
    KnowledgeIngestionService,
    KnowledgeRepository,
    KnowledgeUserAuthority,
    TextEvidenceInput,
)

SESSION_COOKIE = "zhiheng_session"

router = APIRouter()
repository = KnowledgeRepository()
session_service = SessionService()


class UserKnowledgeImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=512)
    text: str = Field(min_length=1)
    primary_domain_id: str = Field(min_length=1, max_length=128)
    media_type: str = Field(default="text/markdown", max_length=128)
    object_kind: str = Field(default="note", max_length=64)
    sensitivity_level: str = Field(default="private", max_length=32)
    source_metadata: dict[str, Any] = Field(default_factory=dict)
    summary: str | None = Field(default=None, max_length=2000)
    erasable: bool = True


class KnowledgeConfirmationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["confirmed"] = "confirmed"
    confirmation_request_id: str = Field(min_length=1, max_length=64)
    expected_content_sha256: str = Field(min_length=64, max_length=64)


class MutationResponse(BaseModel):
    status: str
    result: dict[str, Any]


def install_knowledge_routes(app: Any, settings: Settings) -> None:
    app.state.knowledge_settings = settings
    app.include_router(router)


def get_session_factory(request: Request) -> sessionmaker[Session]:
    return cast(sessionmaker[Session], request.app.state.session_factory)


SessionFactoryDep = Annotated[sessionmaker[Session], Depends(get_session_factory)]


def require_user(
    session_factory: SessionFactoryDep,
    session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE),
) -> str:
    if session_token is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing session")
    try:
        with session_scope(session_factory) as session:
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
    settings: Settings = request.app.state.knowledge_settings
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


@router.post("/v1/knowledge/imports", response_model=MutationResponse)
def import_user_knowledge(
    request: UserKnowledgeImportRequest,
    request_context: Request,
    session_factory: SessionFactoryDep,
    user_id: AuthDep,
    idempotency_key: WriteDep,
) -> MutationResponse:
    payload = request.model_dump(mode="json")
    operation_key = f"knowledge.import:{user_id}:{idempotency_key}"
    with session_scope(session_factory) as session:
        receipt_id, replay = _begin_api_mutation(
            session,
            operation_key=operation_key,
            operation_type="knowledge.import",
            payload=payload,
        )
    if replay is not None:
        return replay
    item = TextEvidenceInput(
        title=request.title,
        text=request.text,
        primary_domain_id=request.primary_domain_id,
        source_kind="user_explicit",
        media_type=request.media_type,
        object_kind=request.object_kind,
        sensitivity_level=request.sensitivity_level,
        source_metadata=request.source_metadata,
        summary=request.summary,
        erasable=request.erasable,
    )
    service = _knowledge_ingestion_service(request_context)
    stored_artifacts = service.prepare_text_artifacts(item.text)
    service.verify_prepared_text_artifacts(stored_artifacts)
    with session_scope(session_factory) as session:
        ingested = service.ingest_prepared_user_text(
            session,
            item,
            user_authority=KnowledgeUserAuthority(user_id=user_id),
            stored_artifacts=stored_artifacts,
        )
        result = {
            "knowledge_object_id": ingested.knowledge_object_id,
            "knowledge_version_id": ingested.knowledge_version_id,
            "chunk_id": ingested.chunk_id,
        }
        _complete_operation_receipt(session, receipt_id, status_value="ok", result=result)
    return MutationResponse(status="ok", result=result)


@router.post(
    "/v1/knowledge/external-candidates/{knowledge_object_id}/confirmation",
    response_model=MutationResponse,
)
def confirm_external_candidate(
    knowledge_object_id: str,
    request: KnowledgeConfirmationRequest,
    session_factory: SessionFactoryDep,
    user_id: AuthDep,
    idempotency_key: WriteDep,
) -> MutationResponse:
    payload = {
        **request.model_dump(mode="json"),
        "knowledge_object_id": knowledge_object_id,
    }
    with session_scope(session_factory) as session:
        receipt_id, replay = _begin_api_mutation(
            session,
            operation_key=f"knowledge.confirm:{user_id}:{idempotency_key}",
            operation_type="knowledge.confirm_external_candidate",
            payload=payload,
        )
    if replay is not None:
        return replay
    try:
        with session_scope(session_factory) as session:
            result = repository.confirm_external_candidate(
                session,
                knowledge_object_id,
                confirmation_request_id=request.confirmation_request_id,
                expected_content_sha256=request.expected_content_sha256,
                user_authority=KnowledgeUserAuthority(user_id=user_id),
            )
            response_payload = {
                "knowledge_object_id": result.knowledge_object_id,
                "knowledge_version_id": result.knowledge_version_id,
                "chunk_id": result.chunk_id,
                "confirmation_generation": result.confirmation_generation,
                "confirmation_request_id": result.confirmation_request_id,
                "confirmation_decision_id": result.confirmation_decision_id,
            }
            _complete_operation_receipt(
                session,
                receipt_id,
                status_value="ok",
                result=response_payload,
            )
    except (PermissionError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return MutationResponse(status="ok", result=response_payload)


def _knowledge_ingestion_service(request: Request) -> KnowledgeIngestionService:
    settings: Settings = request.app.state.knowledge_settings
    object_store = getattr(request.app.state, "knowledge_object_store", None)
    return KnowledgeIngestionService(settings=settings, object_store=object_store)


def _csrf_token(settings: Settings, session_token: str) -> str:
    return sha256_text(f"{settings.secret_key.get_secret_value()}:{session_token}")


def _begin_api_mutation(
    session: Session,
    *,
    operation_key: str,
    operation_type: str,
    payload: dict[str, Any],
) -> tuple[str, MutationResponse | None]:
    request_hash = sha256_json({"operation_type": operation_type, "payload": payload})
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


def _operation_receipt(
    session: Session,
    operation_key: str,
    *,
    request_hash: str,
) -> RowMapping | None:
    row = session.execute(
        text(
            """
            SELECT id, result_json, status, request_hash
            FROM knowledge_operation_receipts
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


def _insert_operation_receipt(
    session: Session,
    operation_key: str,
    operation_type: str,
    request_hash: str,
) -> str:
    receipt_id = new_id()
    session.execute(
        text(
            """
            INSERT INTO knowledge_operation_receipts (
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
    result: dict[str, Any],
) -> None:
    session.execute(
        text(
            """
            UPDATE knowledge_operation_receipts
            SET status = :status, result_json = :result_json, completed_at = CURRENT_TIMESTAMP
            WHERE id = :id
            """
        ),
        {"id": receipt_id, "status": status_value, "result_json": json_text(result)},
    )


def _json_result(value: Any) -> dict[str, Any]:
    decoded = json.loads(value) if isinstance(value, str) else value
    if isinstance(decoded, dict):
        return decoded
    return {}
