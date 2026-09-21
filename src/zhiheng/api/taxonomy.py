from __future__ import annotations

from collections.abc import Generator
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from zhiheng.api.knowledge import (
    AuthDep,
    WriteDep,
    _begin_api_mutation,
    _complete_operation_receipt,
)
from zhiheng.classification.taxonomy import TaxonomyRepository
from zhiheng.db.session import session_scope

router = APIRouter(tags=["taxonomy"])
repository = TaxonomyRepository()
STATIC_DIR = Path(__file__).parent / "static"


class ReclassificationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    knowledge_object_id: str = Field(min_length=1, max_length=64)
    primary_domain_id: str = Field(min_length=1, max_length=64)
    record_type: str = Field(default="knowledge", min_length=1, max_length=64)
    classification_node_ids: list[str] = Field(default_factory=list, max_length=100)
    reason: str = Field(default="", max_length=2000)


class DomainProposalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: str = Field(pattern="^(add|merge|split)$")
    reason: str = Field(min_length=1, max_length=2000)
    domain: dict[str, Any] | None = None
    source_domain_ids: list[str] = Field(default_factory=list, max_length=100)
    new_domains: list[dict[str, Any]] = Field(default_factory=list, max_length=20)


class LegacyMigrationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mapping: dict[str, str] = Field(default_factory=dict)
    reason: str = Field(default="", max_length=2000)


class DomainMigrationDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_domain_id: str | None = Field(default=None, max_length=64)


def install_taxonomy_routes(app: Any) -> None:
    """Install taxonomy endpoints once; tests and embedders may call this explicitly."""
    marker = "_zhiheng_taxonomy_routes_installed"
    if getattr(app.state, marker, False):
        return
    app.include_router(router)
    setattr(app.state, marker, True)


def _db_session(request: Request) -> Generator[Session, None, None]:
    with session_scope(request.app.state.session_factory) as session:
        yield session


SessionDep = Annotated[Session, Depends(_db_session)]


def _if_match(if_match: str | None = Header(default=None, alias="If-Match")) -> str:
    if if_match is None or not if_match.strip():
        raise HTTPException(status_code=412, detail="missing If-Match")
    return if_match.strip()


IfMatchDep = Annotated[str, Depends(_if_match)]


def _mutation_response(
    session: Session,
    *,
    user_id: str,
    idempotency_key: str,
    operation_type: str,
    payload: dict[str, Any],
    action: Any,
) -> dict[str, Any]:
    receipt_id, replay = _begin_api_mutation(
        session,
        operation_key=f"taxonomy:{operation_type}:{user_id}:{idempotency_key}",
        operation_type=operation_type,
        payload=payload,
    )
    if replay is not None:
        return replay.result
    try:
        result = action()
    except ValueError as exc:
        message = str(exc)
        code = 412 if "stale" in message or "changed after preview" in message else 400
        if "not found" in message:
            code = 404
        raise HTTPException(status_code=code, detail=message) from exc
    response = {"status": "ok", "result": result}
    _complete_operation_receipt(session, receipt_id, status_value="ok", result=response)
    return response


@router.get("/taxonomy-center", include_in_schema=False)
def taxonomy_center(_user_id: AuthDep) -> FileResponse:
    return FileResponse(STATIC_DIR / "taxonomy-center.html", media_type="text/html; charset=utf-8")


@router.get("/taxonomy-center.css", include_in_schema=False)
def taxonomy_center_css(_user_id: AuthDep) -> FileResponse:
    return FileResponse(STATIC_DIR / "taxonomy-center.css", media_type="text/css; charset=utf-8")


@router.get("/taxonomy-center.js", include_in_schema=False)
def taxonomy_center_js(_user_id: AuthDep) -> FileResponse:
    return FileResponse(STATIC_DIR / "taxonomy-center.js", media_type="text/javascript")


@router.get("/v1/taxonomy")
def get_taxonomy(session: SessionDep, _user_id: AuthDep) -> dict[str, Any]:
    return repository.list_taxonomy(session, user_id=_user_id)


@router.get("/v1/taxonomy/proposals")
def list_taxonomy_proposals(
    session: SessionDep, user_id: AuthDep, response: Response
) -> dict[str, Any]:
    items = repository.list_proposals(session, user_id=user_id)
    response.headers["ETag"] = f"taxonomy-list:{len(items)}"
    return {"items": items}


@router.get("/v1/taxonomy/proposals/{proposal_id}")
def taxonomy_proposal(
    proposal_id: str, session: SessionDep, user_id: AuthDep, response: Response
) -> dict[str, Any]:
    result = repository.get_proposal(session, user_id=user_id, proposal_id=proposal_id)
    if result is None:
        raise HTTPException(status_code=404, detail="taxonomy proposal not found")
    response.headers["ETag"] = str(result["etag"])
    return result


@router.post("/v1/taxonomy/proposals/reclassification", status_code=status.HTTP_201_CREATED)
def create_reclassification(
    request: ReclassificationRequest,
    session: SessionDep,
    user_id: AuthDep,
    idempotency_key: WriteDep,
) -> dict[str, Any]:
    payload = request.model_dump(mode="json")
    mutation_payload = dict(payload)
    knowledge_id = payload.pop("knowledge_object_id")
    repository_payload = {**payload, "knowledge_id": knowledge_id}
    return _mutation_response(
        session,
        user_id=user_id,
        idempotency_key=idempotency_key,
        operation_type="reclassification.create",
        payload=mutation_payload,
        action=lambda: repository.create_reclassification_proposal(
            session, user_id=user_id, **repository_payload
        ),
    )


@router.post("/v1/taxonomy/proposals/domain", status_code=status.HTTP_201_CREATED)
def create_domain_proposal(
    request: DomainProposalRequest,
    session: SessionDep,
    user_id: AuthDep,
    idempotency_key: WriteDep,
) -> dict[str, Any]:
    payload = request.model_dump(mode="json")
    return _mutation_response(
        session,
        user_id=user_id,
        idempotency_key=idempotency_key,
        operation_type="domain-proposal.create",
        payload=payload,
        action=lambda: repository.create_domain_proposal(session, user_id=user_id, **payload),
    )


@router.post("/v1/taxonomy/proposals/legacy-migration", status_code=status.HTTP_201_CREATED)
def create_legacy_migration(
    request: LegacyMigrationRequest,
    session: SessionDep,
    user_id: AuthDep,
    idempotency_key: WriteDep,
) -> dict[str, Any]:
    payload = request.model_dump(mode="json")
    return _mutation_response(
        session,
        user_id=user_id,
        idempotency_key=idempotency_key,
        operation_type="legacy-migration.create",
        payload=payload,
        action=lambda: repository.create_legacy_migration_proposal(
            session, user_id=user_id, **payload
        ),
    )


@router.post("/v1/taxonomy/proposals/{proposal_id}/approve")
def approve_taxonomy_proposal(
    proposal_id: str,
    session: SessionDep,
    user_id: AuthDep,
    idempotency_key: WriteDep,
    if_match: IfMatchDep,
    request: Request,
) -> dict[str, Any]:
    payload = {"proposal_id": proposal_id, "if_match": if_match}
    return _mutation_response(
        session,
        user_id=user_id,
        idempotency_key=idempotency_key,
        operation_type="proposal.approve",
        payload=payload,
        action=lambda: repository.approve_proposal(
            session,
            user_id=user_id,
            proposal_id=proposal_id,
            expected_etag=if_match,
            request_id=request.headers.get("X-Request-ID"),
        ),
    )


@router.patch("/v1/taxonomy/proposals/{proposal_id}/items/{knowledge_object_id}")
def update_domain_migration(
    proposal_id: str,
    knowledge_object_id: str,
    request: DomainMigrationDecisionRequest,
    session: SessionDep,
    user_id: AuthDep,
    idempotency_key: WriteDep,
    if_match: IfMatchDep,
) -> dict[str, Any]:
    payload = {
        "proposal_id": proposal_id,
        "knowledge_object_id": knowledge_object_id,
        "target_domain_id": request.target_domain_id,
        "if_match": if_match,
    }
    return _mutation_response(
        session,
        user_id=user_id,
        idempotency_key=idempotency_key,
        operation_type="domain-proposal.item-update",
        payload=payload,
        action=lambda: repository.update_domain_migration(
            session,
            user_id=user_id,
            proposal_id=proposal_id,
            knowledge_id=knowledge_object_id,
            target_domain_id=request.target_domain_id,
            expected_etag=if_match,
        ),
    )


__all__ = ["install_taxonomy_routes", "router"]
