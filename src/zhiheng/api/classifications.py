from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field

from zhiheng.api.knowledge import (
    AuthDep,
    SessionFactoryDep,
    WriteDep,
    _begin_api_mutation,
    _complete_operation_receipt,
)
from zhiheng.db.session import session_scope
from zhiheng.knowledge.classification import ClassificationRepository

router = APIRouter(tags=["classifications"])
repository = ClassificationRepository()


class ClassificationCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    domain_id: str = Field(min_length=1, max_length=64)
    parent_id: str | None = Field(default=None, max_length=36)
    name: str = Field(min_length=1, max_length=256)
    description: str = Field(default="", max_length=4000)
    sort_order: int = Field(default=0, ge=0, le=2_000_000_000)


class ClassificationUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=256)
    description: str | None = Field(default=None, max_length=4000)
    sort_order: int | None = Field(default=None, ge=0, le=2_000_000_000)
    status: str | None = Field(default=None, pattern="^(active|disabled)$")


class ClassificationAssignmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    primary_domain_id: str = Field(min_length=1, max_length=64)
    node_ids: list[str] = Field(default_factory=list, max_length=100)


def install_classification_routes(app: Any) -> None:
    app.include_router(router)


@router.get("/v1/domains")
def list_domains(
    session_factory: SessionFactoryDep,
    user_id: AuthDep,
) -> dict[str, Any]:
    with session_scope(session_factory) as session:
        return {"items": repository.list_domains(session, user_id=user_id)}


@router.post("/v1/classifications", status_code=status.HTTP_201_CREATED)
def create_classification(
    request: ClassificationCreateRequest,
    session_factory: SessionFactoryDep,
    user_id: AuthDep,
    idempotency_key: WriteDep,
) -> dict[str, Any]:
    with session_scope(session_factory) as session:
        receipt_id, replay = _begin_api_mutation(
            session,
            operation_key=f"classification.create:{user_id}:{idempotency_key}",
            operation_type="classification.create",
            payload=request.model_dump(mode="json"),
        )
        if replay is not None:
            return replay.result
        try:
            result = repository.create_node(
                session, user_id=user_id, **request.model_dump(mode="python")
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _complete_operation_receipt(session, receipt_id, status_value="ok", result=result)
        return result


@router.patch("/v1/classifications/{classification_id}")
def update_classification(
    classification_id: str,
    request: ClassificationUpdateRequest,
    session_factory: SessionFactoryDep,
    user_id: AuthDep,
    idempotency_key: WriteDep,
) -> dict[str, Any]:
    with session_scope(session_factory) as session:
        receipt_id, replay = _begin_api_mutation(
            session,
            operation_key=f"classification.update:{user_id}:{classification_id}:{idempotency_key}",
            operation_type="classification.update",
            payload={"classification_id": classification_id, **request.model_dump(mode="json")},
        )
        if replay is not None:
            return replay.result
        try:
            result = repository.update_node(
                session,
                node_id=classification_id,
                user_id=user_id,
                **request.model_dump(mode="python"),
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=404 if "not found" in str(exc) else 400, detail=str(exc)
            ) from exc
        _complete_operation_receipt(session, receipt_id, status_value="ok", result=result)
        return result


@router.delete("/v1/classifications/{classification_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_classification(
    classification_id: str,
    session_factory: SessionFactoryDep,
    user_id: AuthDep,
    idempotency_key: WriteDep,
) -> None:
    with session_scope(session_factory) as session:
        receipt_id, replay = _begin_api_mutation(
            session,
            operation_key=f"classification.delete:{user_id}:{classification_id}:{idempotency_key}",
            operation_type="classification.delete",
            payload={"classification_id": classification_id},
        )
        if replay is not None:
            return
        try:
            repository.delete_node(session, node_id=classification_id, user_id=user_id)
        except ValueError as exc:
            raise HTTPException(
                status_code=409 if "referenced" in str(exc) else 404, detail=str(exc)
            ) from exc
        _complete_operation_receipt(
            session, receipt_id, status_value="ok", result={"classification_id": classification_id}
        )


@router.get("/v1/classifications/{classification_id}/children")
def list_classification_children(
    classification_id: str,
    session_factory: SessionFactoryDep,
    user_id: AuthDep,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    with session_scope(session_factory) as session:
        try:
            return {
                "items": repository.children(
                    session, node_id=classification_id, user_id=user_id, limit=limit, offset=offset
                )
            }
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/v1/knowledge/{knowledge_object_id}/classifications")
def get_knowledge_classifications(
    knowledge_object_id: str,
    session_factory: SessionFactoryDep,
    user_id: AuthDep,
) -> dict[str, Any]:
    with session_scope(session_factory) as session:
        result = repository.get_assignments(
            session, knowledge_id=knowledge_object_id, user_id=user_id
        )
    if result is None:
        raise HTTPException(status_code=404, detail="knowledge not found")
    return result


@router.put("/v1/knowledge/{knowledge_object_id}/classifications")
def put_knowledge_classifications(
    knowledge_object_id: str,
    request: ClassificationAssignmentRequest,
    request_context: Request,
    session_factory: SessionFactoryDep,
    user_id: AuthDep,
    idempotency_key: WriteDep,
) -> dict[str, Any]:
    with session_scope(session_factory) as session:
        receipt_id, replay = _begin_api_mutation(
            session,
            operation_key=f"classification.assign:{user_id}:{knowledge_object_id}:{idempotency_key}",
            operation_type="classification.assign",
            payload={"knowledge_object_id": knowledge_object_id, **request.model_dump(mode="json")},
        )
        if replay is not None:
            return replay.result
        try:
            result = repository.assign(
                session,
                knowledge_id=knowledge_object_id,
                user_id=user_id,
                primary_domain_id=request.primary_domain_id,
                node_ids=request.node_ids,
                request_id=request_context.headers.get("X-Request-ID"),
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=404 if "not found" in str(exc) else 400, detail=str(exc)
            ) from exc
        _complete_operation_receipt(session, receipt_id, status_value="ok", result=result)
        return result


@router.get("/v1/knowledge/{knowledge_object_id}/classification-history")
def get_classification_history(
    knowledge_object_id: str,
    session_factory: SessionFactoryDep,
    user_id: AuthDep,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    with session_scope(session_factory) as session:
        try:
            items = repository.history(
                session,
                knowledge_id=knowledge_object_id,
                user_id=user_id,
                limit=limit,
                offset=offset,
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"items": items}


@router.get("/v1/knowledge/unclassified")
def list_unclassified(
    session_factory: SessionFactoryDep,
    user_id: AuthDep,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    with session_scope(session_factory) as session:
        return {
            "items": repository.unclassified(session, user_id=user_id, limit=limit, offset=offset)
        }
