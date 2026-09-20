from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from zhiheng.api.memory import MutationDep, get_db_session, require_user
from zhiheng.conclusions import ConclusionRepository
from zhiheng.knowledge import KnowledgeRepository, KnowledgeUserAuthority, TextEvidenceInput
from zhiheng.knowledge.object_store import knowledge_object_store_for_settings

router = APIRouter(prefix="/v1/conclusions", tags=["conclusions"])
SessionDep = Annotated[Session, Depends(get_db_session)]
AuthDep = Annotated[str, Depends(require_user)]
repo = ConclusionRepository()


class SourcePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=100000)


class ClassificationPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    primary_domain_id: str = Field(min_length=1, max_length=128)
    related_domain_ids: list[str] = Field(default_factory=list)
    record_type: Literal["knowledge", "personal_archive_experience"] = "knowledge"
    explanation: str | None = Field(default=None, max_length=1000)


class DraftPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: str
    title: str = Field(min_length=1, max_length=512)
    claim: str = Field(min_length=1, max_length=10000)
    domain_id: str = Field(min_length=1, max_length=128)
    premises: list[dict[str, Any]] = Field(default_factory=list)
    excerpt: str = Field(min_length=1, max_length=10000)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    classification: ClassificationPayload | None = None
    valid_until: AwareDatetime | None = None


class DraftUpdatePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str | None = Field(default=None, min_length=1, max_length=512)
    claim: str | None = Field(default=None, min_length=1, max_length=10000)
    domain_id: str | None = Field(default=None, min_length=1, max_length=128)
    premises: list[dict[str, Any]] | None = None
    excerpt: str | None = Field(default=None, min_length=1, max_length=10000)
    evidence: list[dict[str, Any]] | None = None
    classification: ClassificationPayload | None = None
    valid_until: AwareDatetime | None = None


@router.post("/sources")
def source(
    payload: SourcePayload, session: SessionDep, user: AuthDep, mutation: MutationDep
) -> dict[str, Any]:
    key, _ = mutation
    return repo.persist_source(session, user, payload.text, key)


@router.post("")
def draft(
    payload: DraftPayload, session: SessionDep, user: AuthDep, mutation: MutationDep
) -> dict[str, Any]:
    key, _ = mutation
    try:
        return repo.create_draft(
            session, user, payload.source_id, payload.model_dump(mode="json"), key
        )
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/context")
@router.get("/context/search")
def context(query: str, session: SessionDep, user: AuthDep) -> dict[str, Any]:
    return {"items": repo.context(session, user, query)}


@router.get("/drafts")
def drafts(session: SessionDep, user: AuthDep, limit: int = 100) -> dict[str, Any]:
    return {"items": repo.list_drafts(session, user, limit=limit)}


@router.patch("/{entry_id}")
def update_draft(
    entry_id: str,
    payload: DraftUpdatePayload,
    session: SessionDep,
    user: AuthDep,
    mutation: MutationDep,
) -> dict[str, Any]:
    key, etag = mutation
    changes = {
        name: value for name, value in payload.model_dump(mode="json").items() if value is not None
    }
    try:
        return repo.update_draft(session, user, entry_id, etag, changes, key)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/{entry_id}")
def get(entry_id: str, session: SessionDep, user: AuthDep) -> dict[str, Any]:
    item = repo.get(session, user, entry_id)
    if item is None:
        raise HTTPException(404, "conclusion not found")
    return item


@router.post("/{entry_id}/approve")
def approve(
    entry_id: str,
    request: Request,
    session: SessionDep,
    user: AuthDep,
    mutation: MutationDep,
) -> dict[str, Any]:
    key, etag = mutation
    try:
        result = repo.approve(session, user, entry_id, etag, key)
        row = session.execute(
            text("SELECT knowledge_id FROM conclusion_entries WHERE id=:id AND owner_user_id=:o"),
            {"id": entry_id, "o": user},
        ).scalar_one_or_none()
        if row is None:
            item = repo.get(session, user, entry_id)
            assert item is not None
            premises = item.get("premises", [])
            condition = "、".join(
                str(p.get("text", "")) for p in premises if not p.get("confirmed", False)
            )
            text_value = f"如果{condition}，则{item['claim']}" if condition else str(item["claim"])
            artifacts = knowledge_object_store_for_settings(
                request.app.state.knowledge_settings
            ).write_text_artifacts(text_value)
            stored = KnowledgeRepository().ingest_text(
                session,
                TextEvidenceInput(
                    title=str(item["title"]),
                    text=text_value,
                    primary_domain_id=str(item["domain_id"]),
                    record_type=str(item.get("record_type", "knowledge")),
                    object_kind="conclusion",
                    source_kind="user_explicit",
                    source_metadata={
                        "conclusion_entry_id": entry_id,
                        "source_id": item["source"]["id"],
                        "excerpt": item.get("excerpt"),
                        "classification": item.get("classification", {}),
                        "related_domain_ids": item.get("classification", {}).get(
                            "related_domain_ids", []
                        ),
                    },
                ),
                user_authority=KnowledgeUserAuthority(user),
                stored_artifacts=artifacts,
            )
            repo.attach_knowledge(session, user, entry_id, stored.knowledge_object_id)
            result["knowledge_id"] = stored.knowledge_object_id
            return result
        result["knowledge_id"] = str(row)
        return result
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/{entry_id}/relations")
def relations(entry_id: str, session: SessionDep, user: AuthDep) -> dict[str, Any]:
    if repo.get(session, user, entry_id) is None:
        raise HTTPException(404, "conclusion not found")
    return {"items": repo.list_relations(session, user, entry_id)}


@router.post("/{entry_id}/relations/suggest")
def suggest_relations(
    entry_id: str,
    session: SessionDep,
    user: AuthDep,
    mutation: MutationDep,
) -> dict[str, Any]:
    key, _ = mutation
    try:
        return {"items": repo.suggest_relations(session, user, entry_id, key=key)}
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


def _decide_relation(
    relation_id: str,
    decision: str,
    session: SessionDep,
    user: AuthDep,
    mutation: MutationDep,
) -> dict[str, Any]:
    key, _ = mutation
    try:
        return repo.decide_relation(session, user, relation_id, decision, key)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/relations/{relation_id}/approve")
def approve_relation(
    relation_id: str, session: SessionDep, user: AuthDep, mutation: MutationDep
) -> dict[str, Any]:
    return _decide_relation(relation_id, "approved", session, user, mutation)


@router.post("/relations/{relation_id}/reject")
def reject_relation(
    relation_id: str, session: SessionDep, user: AuthDep, mutation: MutationDep
) -> dict[str, Any]:
    return _decide_relation(relation_id, "rejected", session, user, mutation)


@router.post("/relations/{relation_id}/defer")
def defer_relation(
    relation_id: str, session: SessionDep, user: AuthDep, mutation: MutationDep
) -> dict[str, Any]:
    return _decide_relation(relation_id, "deferred", session, user, mutation)


def install_conclusion_routes(app: FastAPI) -> None:
    app.include_router(router)
