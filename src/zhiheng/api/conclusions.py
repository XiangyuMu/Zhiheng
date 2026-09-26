from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from zhiheng.api.memory import MutationDep, get_db_session, require_user
from zhiheng.conclusions import (
    ConclusionRepository,
    get_extraction_review_result,
    list_extraction_review_results,
)
from zhiheng.knowledge import KnowledgeRepository, KnowledgeUserAuthority, TextEvidenceInput
from zhiheng.knowledge.object_store import knowledge_object_store_for_settings

router = APIRouter(prefix="/v1/conclusions", tags=["conclusions"])
SessionDep = Annotated[Session, Depends(get_db_session)]
AuthDep = Annotated[str, Depends(require_user)]
repo = ConclusionRepository()


def _formalize_conclusion(
    request: Request,
    session: Session,
    user: str,
    entry_id: str,
    item: dict[str, Any],
    *,
    relation_kind: str | None = None,
    related_entry_id: str | None = None,
) -> str:
    """Materialize one approved conclusion through the shared knowledge path."""
    existing = session.execute(
        text("SELECT knowledge_id FROM conclusion_entries WHERE id=:id AND owner_user_id=:o"),
        {"id": entry_id, "o": user},
    ).scalar_one_or_none()
    if existing is not None:
        return str(existing)

    if relation_kind == "duplicate" and related_entry_id is not None:
        duplicate_knowledge_id = session.execute(
            text("SELECT knowledge_id FROM conclusion_entries WHERE id=:id AND owner_user_id=:o"),
            {"id": related_entry_id, "o": user},
        ).scalar_one_or_none()
        if duplicate_knowledge_id is not None:
            # Approved relation and immutable source retain duplicate provenance.
            # Do not give a merged duplicate independent serving authority.
            repo.attach_knowledge(session, user, entry_id, str(duplicate_knowledge_id))
            return str(duplicate_knowledge_id)

    premises = item.get("premises", [])
    condition = "、".join(str(p.get("text", "")) for p in premises if not p.get("confirmed", False))
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
                "related_domain_ids": item.get("classification", {}).get("related_domain_ids", []),
                "relation_kind": relation_kind,
                "related_entry_id": related_entry_id,
            },
        ),
        user_authority=KnowledgeUserAuthority(user),
        stored_artifacts=artifacts,
    )
    repo.attach_knowledge(session, user, entry_id, stored.knowledge_object_id)
    if relation_kind == "revision" and related_entry_id is not None:
        old_knowledge_id = session.execute(
            text("SELECT knowledge_id FROM conclusion_entries WHERE id=:id AND owner_user_id=:o"),
            {"id": related_entry_id, "o": user},
        ).scalar_one_or_none()
        if old_knowledge_id is not None:
            KnowledgeRepository().soft_delete_knowledge(session, str(old_knowledge_id))
    return stored.knowledge_object_id


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


class SupplementPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=512)
    claim: str = Field(min_length=1, max_length=10000)
    domain_id: str = Field(min_length=1, max_length=128)
    premises: list[dict[str, Any]] = Field(default_factory=list)
    excerpt: str = Field(min_length=1, max_length=10000)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
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


@router.get("/extraction-runs")
def extraction_runs(session: SessionDep, user: AuthDep, limit: int = 100) -> dict[str, Any]:
    return {"items": list_extraction_review_results(session, user, limit=limit)}


@router.get("/extraction-runs/{run_id}")
def extraction_run(run_id: str, session: SessionDep, user: AuthDep) -> dict[str, Any]:
    item = get_extraction_review_result(session, user, run_id)
    if item is None:
        raise HTTPException(404, "extraction run not found")
    return item


@router.post("/drafts/{run_id}/supplement")
def supplement_draft(
    run_id: str,
    payload: SupplementPayload,
    session: SessionDep,
    user: AuthDep,
    mutation: MutationDep,
) -> dict[str, Any]:
    """Create a normal review draft from an unrecognized extraction result."""
    key, _ = mutation
    run = get_extraction_review_result(session, user, run_id)
    if run is None and run_id.endswith(":manual-supplement"):
        run = get_extraction_review_result(session, user, run_id[: -len(":manual-supplement")])
    if run is None:
        raise HTTPException(404, "extraction run not found")
    try:
        return repo.create_draft(
            session,
            user,
            str(run["source"]["id"]),
            payload.model_dump(mode="json"),
            key,
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/{entry_id}/defer")
def defer_draft(
    entry_id: str, session: SessionDep, user: AuthDep, mutation: MutationDep
) -> dict[str, Any]:
    key, etag = mutation
    try:
        return repo.decide_draft(session, user, entry_id, etag, "deferred", key)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/{entry_id}/reject")
def reject_draft(
    entry_id: str, session: SessionDep, user: AuthDep, mutation: MutationDep
) -> dict[str, Any]:
    key, etag = mutation
    try:
        return repo.decide_draft(session, user, entry_id, etag, "rejected", key)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


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
        item = repo.get(session, user, entry_id)
        assert item is not None
        result["knowledge_id"] = _formalize_conclusion(request, session, user, entry_id, item)
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
    request: Request,
) -> dict[str, Any]:
    key, _ = mutation
    try:
        result = repo.decide_relation(session, user, relation_id, decision, key)
        if decision == "approved":
            item = repo.get(session, user, str(result["left_id"]))
            assert item is not None
            result["knowledge_id"] = _formalize_conclusion(
                request,
                session,
                user,
                str(result["left_id"]),
                item,
                relation_kind=str(result["kind"]),
                related_entry_id=str(result["right_id"]),
            )
        return result
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/relations/{relation_id}/approve")
def approve_relation(
    relation_id: str, request: Request, session: SessionDep, user: AuthDep, mutation: MutationDep
) -> dict[str, Any]:
    return _decide_relation(relation_id, "approved", session, user, mutation, request)


@router.post("/relations/{relation_id}/reject")
def reject_relation(
    relation_id: str, request: Request, session: SessionDep, user: AuthDep, mutation: MutationDep
) -> dict[str, Any]:
    return _decide_relation(relation_id, "rejected", session, user, mutation, request)


@router.post("/relations/{relation_id}/defer")
def defer_relation(
    relation_id: str, request: Request, session: SessionDep, user: AuthDep, mutation: MutationDep
) -> dict[str, Any]:
    return _decide_relation(relation_id, "deferred", session, user, mutation, request)


def install_conclusion_routes(app: FastAPI) -> None:
    app.include_router(router)
