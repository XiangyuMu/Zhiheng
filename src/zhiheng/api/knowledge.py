from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from typing import Annotated, Any, Literal, cast

from fastapi import (
    APIRouter,
    Cookie,
    Depends,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import RowMapping, text
from sqlalchemy.orm import Session, sessionmaker

from zhiheng.auth import SessionService
from zhiheng.core.config import Settings
from zhiheng.core.ids import json_text, new_id, sha256_json, sha256_text
from zhiheng.db.session import session_scope
from zhiheng.jobs.knowledge_contract import (
    failure_from_row,
    job_etag,
    project_import_status,
)
from zhiheng.jobs.knowledge_indexing import KnowledgeJobRepository
from zhiheng.knowledge import (
    KnowledgeIngestionService,
    KnowledgeRepository,
    KnowledgeUserAuthority,
    PdfRepository,
    TextEvidenceInput,
)
from zhiheng.knowledge.import_adapters import fetch_web, parse_markdown, parse_ocr
from zhiheng.knowledge.object_store import knowledge_object_store_for_settings

SESSION_COOKIE = "zhiheng_session"

router = APIRouter()
repository = KnowledgeRepository()
session_service = SessionService()
job_repository = KnowledgeJobRepository()
pdf_repository = PdfRepository()


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


class AdapterPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    format: Literal["markdown", "pdf", "web", "ocr"]
    title: str = Field(min_length=1, max_length=512)
    content_base64: str | None = None
    url: str | None = None


class KnowledgeConfirmationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["confirmed"] = "confirmed"
    confirmation_request_id: str = Field(min_length=1, max_length=64)
    expected_content_sha256: str = Field(min_length=64, max_length=64)


class MutationResponse(BaseModel):
    status: str
    result: dict[str, Any]


class KnowledgeItemPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    knowledge_object_id: str
    knowledge_version_id: str
    chunk_id: str
    title: str
    primary_domain_id: str
    media_type: str
    object_kind: str
    lifecycle_status: str
    searchable: bool
    summary: str | None
    is_favorite: bool = False
    is_pinned: bool = False
    pinned_at: str | None = None


class KnowledgeListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[KnowledgeItemPayload]


class KnowledgeDomainPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    domain_id: str
    item_count: int
    latest_import_at: str | None = None


class KnowledgeDomainsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[KnowledgeDomainPayload]


class KnowledgeDetailResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    knowledge_object_id: str
    knowledge_version_id: str
    title: str
    primary_domain_id: str
    media_type: str
    object_kind: str
    lifecycle_status: str
    searchable: bool
    summary: str | None
    source_metadata: dict[str, Any]
    text: str
    citations: list[dict[str, Any]]


class KnowledgeProcessingResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    knowledge_object_id: str
    status: str
    public_status: str
    job_id: str
    job_status: str
    knowledge_lifecycle_status: str | None = None
    searchable: bool
    attempts: int
    max_attempts: int
    etag: str
    failure_code: str | None = None
    failure_stage: str | None = None
    retryable: bool = False
    redacted_summary: str | None = None


class KnowledgeRetryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    result: dict[str, Any]


class KnowledgeFlagRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool


class KnowledgeBulkRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["delete", "restore", "reindex"]
    knowledge_object_ids: list[str] = Field(min_length=1, max_length=100)


class PdfTaskResponse(BaseModel):
    task_id: str
    evidence_object_id: str
    source_sha256: str
    status_url: str
    state: str


class PdfTaskStatusResponse(BaseModel):
    task_id: str
    evidence_object_id: str
    state: str
    backend: str
    attempt_id: str | None = None
    page_count: int
    parsed_page_count: int
    block_count: int
    table_count: int
    image_count: int
    source_sha256: str
    etag: str
    error_code: str | None = None
    redacted_summary: str | None = None
    retryable: bool = False


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


def require_if_match(if_match: str | None = Header(default=None, alias="If-Match")) -> str:
    if if_match is None or not if_match.strip():
        raise HTTPException(
            status_code=status.HTTP_412_PRECONDITION_FAILED,
            detail="missing If-Match",
        )
    return if_match.strip()


IfMatchDep = Annotated[str, Depends(require_if_match)]


@router.post("/v1/knowledge/pdf-imports", response_model=PdfTaskResponse, status_code=202)
async def upload_pdf(
    request: Request,
    session_factory: SessionFactoryDep,
    user_id: AuthDep,
    idempotency_key: WriteDep,
    title: str,
    primary_domain_id: str,
    backend: Literal["deepdoc", "mineru"] | None = None,
) -> PdfTaskResponse | JSONResponse:
    settings: Settings = request.app.state.knowledge_settings
    selected_backend = backend or settings.pdf_parser_backend
    if selected_backend not in {"deepdoc", "mineru"}:
        raise HTTPException(status_code=400, detail="unsupported PDF parser backend")
    if request.headers.get("content-type", "").split(";", 1)[0].lower() != "application/pdf":
        raise HTTPException(status_code=415, detail="content-type must be application/pdf")
    body = await request.body()
    if len(body) == 0:
        raise HTTPException(status_code=400, detail="PDF body cannot be empty")
    if len(body) > 50 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="PDF exceeds 50 MiB limit")
    if not body.startswith(b"%PDF"):
        raise HTTPException(status_code=400, detail="invalid PDF header")
    try:
        from io import BytesIO

        from pypdf import PdfReader

        reader = PdfReader(BytesIO(body), strict=False)
        if reader.is_encrypted:
            raise HTTPException(status_code=400, detail="encrypted PDF is unsupported")
        if len(reader.pages) > 500:
            raise HTTPException(status_code=413, detail="PDF exceeds 500 page limit")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail="malformed PDF") from exc

    source_sha256 = hashlib.sha256(body).hexdigest()
    payload = {
        "title": title,
        "primary_domain_id": primary_domain_id,
        "backend": selected_backend,
        "source_sha256": source_sha256,
        "byte_size": len(body),
    }
    operation_key = f"knowledge.pdf-import:{user_id}:{idempotency_key}"
    with session_scope(session_factory) as session:
        receipt_id, replay = _begin_api_mutation(
            session,
            operation_key=operation_key,
            operation_type="knowledge.pdf-import",
            payload=payload,
        )
    if replay is not None:
        return JSONResponse(status_code=202, content=replay.result)

    artifact = knowledge_object_store_for_settings(settings).write_binary_artifact(
        body, namespace="evidence/pdf"
    )
    evidence_object_id = new_id()
    options_hash = sha256_json(
        {"title": title, "primary_domain_id": primary_domain_id, "backend": backend}
    )
    with session_scope(session_factory) as session:
        created = pdf_repository.create_task(
            session,
            evidence_object_id=evidence_object_id,
            source_uri=artifact.uri,
            source_sha256=artifact.sha256,
            byte_size=artifact.byte_size,
            title=title,
            primary_domain_id=primary_domain_id,
            backend=selected_backend,
            options_hash=options_hash,
            idempotency_key=operation_key,
        )
        result = {
            "task_id": created.task_id,
            "evidence_object_id": created.evidence_object_id,
            "source_sha256": created.source_sha256,
            "status_url": f"/v1/knowledge/pdf-imports/{created.task_id}",
            "state": created.status,
        }
        _complete_operation_receipt(session, receipt_id, status_value="ok", result=result)
    return PdfTaskResponse(**result)


@router.get(
    "/v1/knowledge/pdf-imports/{task_id}",
    response_model=PdfTaskStatusResponse,
)
def get_pdf_task(
    task_id: str,
    session_factory: SessionFactoryDep,
    _user_id: AuthDep,
) -> PdfTaskStatusResponse:
    with session_scope(session_factory) as session:
        row = (
            session.execute(
                text(
                    """
                SELECT
                  t.id AS task_id, t.evidence_object_id, t.state, t.backend,
                  eo.sha256 AS source_sha256,
                  a.id AS attempt_id,
                  a.failure_code,
                  (
                    SELECT json_extract(j.payload_json, '$.failure_code')
                    FROM jobs j
                    WHERE j.job_type = 'knowledge.parse_pdf'
                      AND json_extract(j.payload_json, '$.task_id') = t.id
                    ORDER BY j.updated_at DESC, j.id DESC
                    LIMIT 1
                  ) AS job_failure_code,
                  (
                    SELECT j.status
                    FROM jobs j
                    WHERE j.job_type = 'knowledge.parse_pdf'
                      AND json_extract(j.payload_json, '$.task_id') = t.id
                    ORDER BY j.updated_at DESC, j.id DESC
                    LIMIT 1
                  ) AS parse_job_status,
                  count(DISTINCT p.id) AS page_count,
                  count(DISTINCT CASE WHEN p.status = 'parsed' THEN p.id END)
                    AS parsed_page_count,
                  count(DISTINCT b.id) AS block_count,
                  count(DISTINCT tbl.id) AS table_count,
                  count(DISTINCT img.id) AS image_count
                FROM pdf_tasks t
                JOIN evidence_objects eo ON eo.id = t.evidence_object_id
                LEFT JOIN pdf_parse_attempts a
                  ON a.task_id = t.id
                 AND a.attempt_no = (
                   SELECT max(attempt_no) FROM pdf_parse_attempts WHERE task_id = t.id
                 )
                LEFT JOIN pdf_pages p ON p.attempt_id = a.id
                LEFT JOIN evidence_blocks b ON b.attempt_id = a.id
                LEFT JOIN pdf_tables tbl ON tbl.attempt_id = a.id
                LEFT JOIN pdf_images img ON img.attempt_id = a.id
                WHERE t.id = :task_id
                GROUP BY t.id, eo.sha256, a.id
                """
                ),
                {"task_id": task_id},
            )
            .mappings()
            .first()
        )
    if row is None:
        raise HTTPException(status_code=404, detail="PDF task not found")
    etag = sha256_json(
        {
            "task_id": task_id,
            "state": row["state"],
            "attempt_id": row["attempt_id"],
            "page_count": int(row["page_count"]),
            "block_count": int(row["block_count"]),
        }
    )
    failure_code = row["failure_code"] or row["job_failure_code"] or (
        "unsupported_pdf_parser" if str(row["state"]) == "unsupported" else None
    )
    failure = failure_from_row(
        error_class=str(failure_code) if failure_code else None,
        error_message=(
            "PDF parsing is unsupported: configure a parser service before retrying this import"
            if str(row["state"]) == "unsupported"
            else None
        ),
        payload=(
            {"failure_code": failure_code, "failure_stage": "parse", "retryable": False}
            if failure_code and str(row["state"]) == "unsupported"
            else {
                "failure_code": failure_code,
                "failure_stage": "parse",
                "retryable": str(row["parse_job_status"] or "") != "dead",
            }
            if failure_code
            else None
        ),
        job_status=str(row["state"]),
    )
    return PdfTaskStatusResponse(
        task_id=str(row["task_id"]),
        evidence_object_id=str(row["evidence_object_id"]),
        state=str(row["state"]),
        backend=str(row["backend"]),
        attempt_id=str(row["attempt_id"]) if row["attempt_id"] else None,
        page_count=int(row["page_count"]),
        parsed_page_count=int(row["parsed_page_count"]),
        block_count=int(row["block_count"]),
        table_count=int(row["table_count"]),
        image_count=int(row["image_count"]),
        source_sha256=str(row["source_sha256"]),
        etag=etag,
        error_code=failure.code if failure else None,
        redacted_summary=failure.redacted_summary if failure else None,
        retryable=bool(failure.retryable if failure else str(row["state"]) in {"failed", "dead"}),
    )


@router.post("/v1/knowledge/pdf-imports/{task_id}/retry")
def retry_pdf_task(
    task_id: str,
    session_factory: SessionFactoryDep,
    _user_id: AuthDep,
    if_match: IfMatchDep,
    idempotency_key: WriteDep,
) -> dict[str, Any]:
    with session_scope(session_factory) as session:
        row = session.execute(
            text("SELECT state FROM pdf_tasks WHERE id=:id"), {"id": task_id}
        ).first()
        if row is None:
            raise HTTPException(status_code=404, detail="PDF task not found")
        if str(row[0]) not in {"failed", "partial", "dead"}:
            raise HTTPException(status_code=409, detail="PDF task is not retryable")
        pdf_repository.retry_task(session, task_id)
    return {"task_id": task_id, "state": "queued"}


@router.post("/v1/knowledge/imports/preview")
def preview_import(
    request: AdapterPreviewRequest,
    _user_id: AuthDep,
) -> dict[str, Any]:
    """Parse an import without persisting it; the caller must confirm separately."""
    if request.format == "pdf":
        return {
            "status": "async_required",
            "state": "not_submitted",
            "retryable": False,
            "error": (
                "PDF parsing is asynchronous and preserves page, layout, OCR, and provenance data."
            ),
            "upload_endpoint": "/v1/knowledge/pdf-imports",
        }
    try:
        if request.format == "web":
            if not request.url:
                raise ValueError("parse_failed: url is required")
            result = fetch_web(request.url, title=request.title)
        else:
            if not request.content_base64:
                raise ValueError("parse_failed: content_base64 is required")
            data = base64.b64decode(request.content_base64, validate=True)
            if request.format == "markdown":
                result = parse_markdown(data, title=request.title)
            else:
                result = parse_ocr(data, title=request.title)
    except ValueError as exc:
        message = str(exc)
        code = message.split(":", 1)[0] if ":" in message else "parse_failed"
        return {"status": code, "retryable": False, "error": message}
    return {
        "status": "awaiting_confirmation",
        "title": result.title,
        "text": result.text,
        "media_type": result.media_type,
        "source_metadata": result.source_metadata,
        "requires_confirmation": True,
    }


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


@router.get("/v1/knowledge/items", response_model=KnowledgeListResponse)
def list_user_knowledge(
    session_factory: SessionFactoryDep,
    _user_id: AuthDep,
) -> KnowledgeListResponse:
    with session_scope(session_factory) as session:
        rows = session.execute(
            text(
                """
                SELECT
                  cfk.id AS knowledge_object_id,
                  cfk.current_version_id AS knowledge_version_id,
                  s.id AS chunk_id,
                  cfk.title,
                  cfk.primary_domain_id,
                  eo.media_type,
                  cfk.object_kind,
                  cfk.lifecycle_status,
                  cfk.summary,
                  cfk.is_favorite,
                  cfk.is_pinned,
                  cfk.pinned_at
                FROM current_formal_knowledge cfk
                JOIN serving_chunks s
                  ON s.source_id = cfk.id
                 AND s.source_version_id = cfk.current_version_id
                 AND s.confirmation_generation = cfk.confirmation_generation
                JOIN content_versions cv
                  ON cv.id = cfk.content_version_id
                 AND cv.status = 'active'
                JOIN evidence_objects eo
                  ON eo.id = cv.evidence_object_id
                 AND eo.status = 'active'
                ORDER BY cfk.updated_at DESC, cfk.id DESC
                LIMIT 100
                """
            )
        ).mappings()
        return KnowledgeListResponse(
            items=[
                KnowledgeItemPayload(
                    knowledge_object_id=str(row["knowledge_object_id"]),
                    knowledge_version_id=str(row["knowledge_version_id"]),
                    chunk_id=str(row["chunk_id"]),
                    title=str(row["title"]),
                    primary_domain_id=str(row["primary_domain_id"]),
                    media_type=str(row["media_type"]),
                    object_kind=str(row["object_kind"]),
                    lifecycle_status=str(row["lifecycle_status"]),
                    searchable=True,
                    summary=str(row["summary"]) if row["summary"] is not None else None,
                    is_favorite=bool(row["is_favorite"]),
                    is_pinned=bool(row["is_pinned"]),
                    pinned_at=str(row["pinned_at"]) if row["pinned_at"] else None,
                )
                for row in rows
            ]
        )


@router.get("/v1/knowledge/domains", response_model=KnowledgeDomainsResponse)
def list_knowledge_domains(
    session_factory: SessionFactoryDep,
    _user_id: AuthDep,
) -> KnowledgeDomainsResponse:
    with session_scope(session_factory) as session:
        rows = session.execute(
            text(
                """
                SELECT primary_domain_id AS domain_id, count(*) AS item_count,
                       max(updated_at) AS latest_import_at
                FROM knowledge_objects
                WHERE lifecycle_status <> 'privacy_erased'
                GROUP BY primary_domain_id
                ORDER BY domain_id
                """
            )
        ).mappings()
        return KnowledgeDomainsResponse(
            items=[
                KnowledgeDomainPayload(
                    domain_id=str(row["domain_id"]),
                    item_count=int(row["item_count"]),
                    latest_import_at=(
                        str(row["latest_import_at"])
                        if row["latest_import_at"] is not None
                        else None
                    ),
                )
                for row in rows
            ]
        )


@router.get("/v1/knowledge/search")
def search_knowledge(
    session_factory: SessionFactoryDep,
    user_id: AuthDep,
    q: str | None = Query(default=None, max_length=512),
    domain_id: str | None = Query(default=None, max_length=128),
    tag: str | None = Query(default=None, max_length=128),
    source_type: str | None = Query(default=None, max_length=64),
    media_type: str | None = Query(default=None, max_length=128),
    lifecycle_status: str | None = Query(default=None, max_length=32),
    created_from: str | None = Query(default=None, max_length=64),
    created_to: str | None = Query(default=None, max_length=64),
    favorite: bool | None = None,
    pinned: bool | None = None,
    classification_id: str | None = Query(default=None, max_length=36),
    sort: Literal[
        "relevance",
        "updated_at",
        "created_at",
        "title",
        # Values used by the knowledge workspace controls. Keep these aliases
        # at the API boundary so older clients using the explicit direction
        # names remain compatible with the canonical sort keys above.
        "updated_desc",
        "created_desc",
        "title_asc",
    ] = "relevance",
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=100000),
) -> dict[str, Any]:
    """Search the caller's visible knowledge using FTS and structured filters."""
    from zhiheng.retrieval.tokenizer import segment_for_fts

    conditions = [
        "(ko.owner_user_id = :user_id OR ko.owner_user_id IS NULL)",
        "ko.lifecycle_status <> 'privacy_erased'",
        """
        EXISTS (
          SELECT 1
          FROM serving_chunks eligible_chunk
          WHERE eligible_chunk.source_id = ko.id
            AND EXISTS (
              SELECT 1
              FROM jobs completed_index
              WHERE completed_index.job_type = 'knowledge.index'
                AND completed_index.status = 'completed'
                AND (
                  json_extract(completed_index.payload_json, '$.knowledge_object_id') = ko.id
                  OR json_extract(completed_index.payload_json, '$.aggregate_id') = ko.id
                )
            )
        )
        """,
    ]
    params: dict[str, Any] = {
        "user_id": user_id,
        "limit": limit,
        "offset": offset,
    }
    has_query = bool(q and q.strip())
    if has_query:
        query_text = q
        if query_text is not None:
            params["fts_query"] = segment_for_fts(query_text.strip())
    if domain_id:
        conditions.append("ko.primary_domain_id = :domain_id")
        params["domain_id"] = domain_id
    if tag:
        conditions.append(
            "EXISTS (SELECT 1 FROM knowledge_tags kt "
            "WHERE kt.knowledge_object_id = ko.id AND kt.tag = :tag)"
        )
        params["tag"] = tag
    if classification_id:
        conditions.append(
            "EXISTS (SELECT 1 FROM knowledge_classifications kc "
            "WHERE kc.knowledge_object_id = ko.id "
            "AND kc.classification_node_id = :classification_id "
            "AND kc.confirmation_status = 'confirmed')"
        )
        params["classification_id"] = classification_id
    if source_type:
        conditions.append("eo.source_kind = :source_type")
        params["source_type"] = source_type
    if media_type:
        conditions.append("eo.media_type = :media_type")
        params["media_type"] = media_type
    if lifecycle_status:
        conditions.append("ko.lifecycle_status = :lifecycle_status")
        params["lifecycle_status"] = lifecycle_status
    if created_from:
        conditions.append("ko.created_at >= :created_from")
        params["created_from"] = created_from
    if created_to:
        conditions.append("ko.created_at <= :created_to")
        params["created_to"] = created_to
    if favorite is not None:
        conditions.append("ko.is_favorite = :favorite")
        params["favorite"] = favorite
    if pinned is not None:
        conditions.append("ko.is_pinned = :pinned")
        params["pinned"] = pinned
    sort_key = {
        "updated_desc": "updated_at",
        "created_desc": "created_at",
        "title_asc": "title",
    }.get(sort, sort)
    order_by = {
        "relevance": (
            "hit.rank, ko.updated_at DESC, ko.id DESC"
            if has_query
            else "ko.is_pinned DESC, ko.updated_at DESC, ko.id DESC"
        ),
        "updated_at": "ko.updated_at DESC, ko.id DESC",
        "created_at": "ko.created_at DESC, ko.id DESC",
        "title": "ko.title COLLATE NOCASE ASC, ko.id ASC",
    }[sort_key]
    where = " AND ".join(conditions)
    # Keep one row per knowledge object even when a document has multiple chunks.
    fts_join = (
        """
        JOIN (
          SELECT c.source_id,
                 0 AS rank,
                 min(c.raw_text) AS match_text
          FROM fts_chunks
          JOIN chunks c ON c.rowid = fts_chunks.rowid
          JOIN serving_chunks eligible_chunk ON eligible_chunk.id = c.id
          WHERE fts_chunks MATCH :fts_query
            AND EXISTS (
              SELECT 1
              FROM jobs completed_index
              WHERE completed_index.job_type = 'knowledge.index'
                AND completed_index.status = 'completed'
                AND (
                  json_extract(completed_index.payload_json, '$.knowledge_object_id')
                    = c.source_id
                  OR json_extract(completed_index.payload_json, '$.aggregate_id')
                    = c.source_id
                )
            )
          GROUP BY c.source_id
        ) hit ON hit.source_id = ko.id
        """
        if has_query
        else """
        LEFT JOIN (
          SELECT c.source_id, min(c.rowid) AS rowid
          FROM chunks c
          JOIN serving_chunks eligible_chunk ON eligible_chunk.id = c.id
          WHERE EXISTS (
            SELECT 1
            FROM jobs completed_index
            WHERE completed_index.job_type = 'knowledge.index'
              AND completed_index.status = 'completed'
              AND (
                json_extract(completed_index.payload_json, '$.knowledge_object_id')
                  = c.source_id
                OR json_extract(completed_index.payload_json, '$.aggregate_id')
                  = c.source_id
              )
          )
          GROUP BY c.source_id
        ) hit ON hit.source_id = ko.id
        """
    )
    snippet_select = "hit.match_text" if has_query else "NULL AS match_text"
    query = f"""
        SELECT ko.id AS knowledge_object_id, ko.title, ko.primary_domain_id,
               ko.object_kind, ko.lifecycle_status, ko.created_at, ko.updated_at,
               ko.is_favorite, ko.is_pinned, ko.pinned_at,
               kv.id AS knowledge_version_id, kv.version_no, kv.summary,
               eo.media_type, eo.source_kind, eo.source_metadata_json,
               cv.content_sha256,
               {snippet_select}
        FROM knowledge_objects ko
        JOIN knowledge_versions kv ON kv.id = ko.current_version_id
        JOIN content_versions cv ON cv.id = kv.content_version_id
        JOIN evidence_objects eo ON eo.id = cv.evidence_object_id
        {fts_join}
        WHERE {where}
        GROUP BY ko.id, kv.id, eo.id, cv.id
        ORDER BY {order_by}
        LIMIT :limit OFFSET :offset
    """
    count_query = f"""
        SELECT count(DISTINCT ko.id)
        FROM knowledge_objects ko
        JOIN knowledge_versions kv ON kv.id = ko.current_version_id
        JOIN content_versions cv ON cv.id = kv.content_version_id
        JOIN evidence_objects eo ON eo.id = cv.evidence_object_id
        {fts_join}
        WHERE {where}
    """
    params["has_query"] = 1 if has_query else 0
    with session_scope(session_factory) as session:
        rows = session.execute(text(query), params).mappings().all()
        total = int(session.execute(text(count_query), params).scalar_one())
        items = []
        for row in rows:
            metadata = _json_object(row["source_metadata_json"])
            items.append(
                {
                    "knowledge_object_id": str(row["knowledge_object_id"]),
                    "knowledge_version_id": str(row["knowledge_version_id"]),
                    "version_no": int(row["version_no"]),
                    "title": str(row["title"]),
                    "primary_domain_id": str(row["primary_domain_id"]),
                    "object_kind": str(row["object_kind"]),
                    "lifecycle_status": str(row["lifecycle_status"]),
                    "media_type": str(row["media_type"]),
                    "source_type": str(row["source_kind"]),
                    "source_url": metadata.get("source_url") or metadata.get("url"),
                    "content_sha256": str(row["content_sha256"]),
                    "summary": str(row["summary"]) if row["summary"] is not None else None,
                    "match_snippet": (
                        _highlight_match(str(row["match_text"]), q)
                        if row["match_text"] is not None and q
                        else None
                    ),
                    "is_favorite": bool(row["is_favorite"]),
                    "is_pinned": bool(row["is_pinned"]),
                    "pinned_at": str(row["pinned_at"]) if row["pinned_at"] else None,
                    "created_at": str(row["created_at"]),
                    "updated_at": str(row["updated_at"]),
                }
            )
    return {"items": items, "total": total, "limit": limit, "offset": offset}


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(str(value)) if value is not None else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _highlight_match(value: str, query: str | None) -> str:
    excerpt = value[:500]
    if not query:
        return excerpt
    for token in query.split():
        if token:
            excerpt = re.sub(
                re.escape(token),
                lambda match: f"<mark>{match.group(0)}</mark>",
                excerpt,
                flags=re.IGNORECASE,
            )
    return excerpt


@router.get("/v1/knowledge/{knowledge_object_id}", response_model=KnowledgeDetailResponse)
def get_knowledge_detail(
    knowledge_object_id: str,
    session_factory: SessionFactoryDep,
    _user_id: AuthDep,
) -> KnowledgeDetailResponse:
    with session_scope(session_factory) as session:
        detail = repository.get_detail(session, knowledge_object_id)
    if detail is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="knowledge not found")
    return KnowledgeDetailResponse(**detail.__dict__)


def _assert_owned_knowledge(session: Session, knowledge_object_id: str, user_id: str) -> None:
    exists = session.execute(
        text(
            """
            SELECT 1 FROM knowledge_objects
            WHERE id = :id
              AND (owner_user_id = :user_id OR owner_user_id IS NULL)
              AND lifecycle_status <> 'privacy_erased'
            """
        ),
        {"id": knowledge_object_id, "user_id": user_id},
    ).scalar()
    if exists is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="knowledge not found")


def _set_knowledge_flag(
    *,
    flag: Literal["is_favorite", "is_pinned"],
    knowledge_object_id: str,
    enabled: bool,
    session_factory: sessionmaker[Session],
    user_id: str,
    idempotency_key: str,
) -> MutationResponse:
    operation_key = f"knowledge.{flag}:{user_id}:{knowledge_object_id}:{idempotency_key}"
    payload = {
        "knowledge_object_id": knowledge_object_id,
        "enabled": enabled,
        "flag": flag,
    }
    with session_scope(session_factory) as session:
        receipt_id, replay = _begin_api_mutation(
            session,
            operation_key=operation_key,
            operation_type=f"knowledge.{flag}",
            payload=payload,
        )
    if replay is not None:
        return replay
    with session_scope(session_factory) as session:
        _assert_owned_knowledge(session, knowledge_object_id, user_id)
        if flag == "is_pinned":
            session.execute(
                text(
                    """
                    UPDATE knowledge_objects
                    SET is_pinned=:enabled,
                        pinned_at=CASE WHEN :enabled = 1 THEN CURRENT_TIMESTAMP ELSE NULL END,
                        updated_at=CURRENT_TIMESTAMP
                    WHERE id=:id
                    """
                ),
                {"id": knowledge_object_id, "enabled": enabled},
            )
        else:
            session.execute(
                text(
                    """
                    UPDATE knowledge_objects
                    SET is_favorite=:enabled, updated_at=CURRENT_TIMESTAMP
                    WHERE id=:id
                    """
                ),
                {"id": knowledge_object_id, "enabled": enabled},
            )
        result = {
            "knowledge_object_id": knowledge_object_id,
            "enabled": enabled,
            "flag": flag,
        }
        _complete_operation_receipt(session, receipt_id, status_value="ok", result=result)
    return MutationResponse(status="ok", result=result)


@router.post("/v1/knowledge/{knowledge_object_id}/favorite", response_model=MutationResponse)
def set_knowledge_favorite(
    knowledge_object_id: str,
    request: KnowledgeFlagRequest,
    session_factory: SessionFactoryDep,
    user_id: AuthDep,
    idempotency_key: WriteDep,
) -> MutationResponse:
    return _set_knowledge_flag(
        flag="is_favorite",
        knowledge_object_id=knowledge_object_id,
        enabled=request.enabled,
        session_factory=session_factory,
        user_id=user_id,
        idempotency_key=idempotency_key,
    )


@router.post("/v1/knowledge/{knowledge_object_id}/pin", response_model=MutationResponse)
def set_knowledge_pin(
    knowledge_object_id: str,
    request: KnowledgeFlagRequest,
    session_factory: SessionFactoryDep,
    user_id: AuthDep,
    idempotency_key: WriteDep,
) -> MutationResponse:
    return _set_knowledge_flag(
        flag="is_pinned",
        knowledge_object_id=knowledge_object_id,
        enabled=request.enabled,
        session_factory=session_factory,
        user_id=user_id,
        idempotency_key=idempotency_key,
    )


@router.post("/v1/knowledge/bulk", response_model=dict[str, Any])
def bulk_knowledge_action(
    request: KnowledgeBulkRequest,
    session_factory: SessionFactoryDep,
    user_id: AuthDep,
    idempotency_key: WriteDep,
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> dict[str, Any]:
    ids = list(dict.fromkeys(request.knowledge_object_ids))
    payload = {
        "action": request.action,
        "knowledge_object_ids": ids,
        "if_match": if_match,
    }
    operation_key = f"knowledge.bulk:{user_id}:{idempotency_key}"
    with session_scope(session_factory) as session:
        receipt_id, replay = _begin_api_mutation(
            session,
            operation_key=operation_key,
            operation_type="knowledge.bulk",
            payload=payload,
        )
    if replay is not None:
        return replay.result
    results: list[dict[str, Any]] = []
    with session_scope(session_factory) as session:
        for knowledge_object_id in ids:
            try:
                _assert_owned_knowledge(session, knowledge_object_id, user_id)
                if request.action == "delete":
                    repository.soft_delete_knowledge(session, knowledge_object_id)
                elif request.action == "restore":
                    repository.restore_knowledge(session, knowledge_object_id)
                else:
                    repository.reindex_knowledge(session, knowledge_object_id)
                results.append(
                    {
                        "knowledge_object_id": knowledge_object_id,
                        "status": "ok",
                        "action": request.action,
                    }
                )
            except HTTPException as exc:
                results.append(
                    {
                        "knowledge_object_id": knowledge_object_id,
                        "status": "failed",
                        "reason": str(exc.detail),
                    }
                )
            except ValueError as exc:
                results.append(
                    {
                        "knowledge_object_id": knowledge_object_id,
                        "status": "failed",
                        "reason": str(exc),
                    }
                )
        result = {
            "operation_batch_id": new_id(),
            "action": request.action,
            "results": results,
            "succeeded": sum(item["status"] == "ok" for item in results),
            "failed": sum(item["status"] == "failed" for item in results),
        }
        _complete_operation_receipt(session, receipt_id, status_value="ok", result=result)
    return result


def _knowledge_lifecycle_mutation(
    *,
    action: str,
    knowledge_object_id: str,
    session_factory: sessionmaker[Session],
    user_id: str,
    idempotency_key: str,
) -> MutationResponse:
    payload = {"knowledge_object_id": knowledge_object_id, "action": action}
    operation_key = f"knowledge.{action}:{user_id}:{knowledge_object_id}:{idempotency_key}"
    with session_scope(session_factory) as session:
        receipt_id, replay = _begin_api_mutation(
            session,
            operation_key=operation_key,
            operation_type=f"knowledge.{action}",
            payload=payload,
        )
    if replay is not None:
        return replay
    try:
        with session_scope(session_factory) as session:
            if action == "delete":
                repository.soft_delete_knowledge(session, knowledge_object_id)
            elif action == "restore":
                repository.restore_knowledge(session, knowledge_object_id)
            else:
                repository.reindex_knowledge(session, knowledge_object_id)
            result = {"knowledge_object_id": knowledge_object_id, "action": action}
            _complete_operation_receipt(session, receipt_id, status_value="ok", result=result)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return MutationResponse(status="ok", result=result)


@router.post("/v1/knowledge/{knowledge_object_id}/delete", response_model=MutationResponse)
def delete_knowledge(
    knowledge_object_id: str,
    session_factory: SessionFactoryDep,
    user_id: AuthDep,
    idempotency_key: WriteDep,
) -> MutationResponse:
    return _knowledge_lifecycle_mutation(
        action="delete",
        knowledge_object_id=knowledge_object_id,
        session_factory=session_factory,
        user_id=user_id,
        idempotency_key=idempotency_key,
    )


@router.post("/v1/knowledge/{knowledge_object_id}/restore", response_model=MutationResponse)
def restore_knowledge(
    knowledge_object_id: str,
    session_factory: SessionFactoryDep,
    user_id: AuthDep,
    idempotency_key: WriteDep,
) -> MutationResponse:
    return _knowledge_lifecycle_mutation(
        action="restore",
        knowledge_object_id=knowledge_object_id,
        session_factory=session_factory,
        user_id=user_id,
        idempotency_key=idempotency_key,
    )


@router.post("/v1/knowledge/{knowledge_object_id}/reindex", response_model=MutationResponse)
def reindex_knowledge(
    knowledge_object_id: str,
    session_factory: SessionFactoryDep,
    user_id: AuthDep,
    idempotency_key: WriteDep,
) -> MutationResponse:
    return _knowledge_lifecycle_mutation(
        action="reindex",
        knowledge_object_id=knowledge_object_id,
        session_factory=session_factory,
        user_id=user_id,
        idempotency_key=idempotency_key,
    )


@router.get(
    "/v1/knowledge/{knowledge_object_id}/processing",
    response_model=KnowledgeProcessingResponse,
)
def knowledge_processing_status(
    knowledge_object_id: str,
    response: Response,
    session_factory: SessionFactoryDep,
    _user_id: AuthDep,
) -> KnowledgeProcessingResponse:
    with session_scope(session_factory) as session:
        row = (
            session.execute(
                text("""
                SELECT
                  j.id,
                  j.status,
                  j.attempts,
                  j.max_attempts,
                  j.updated_at,
                  j.payload_json,
                  ja.error_class,
                  ja.error_message,
                  ko.lifecycle_status,
                  CASE WHEN j.status = 'completed' AND EXISTS (
                    SELECT 1 FROM serving_chunks s
                    WHERE s.source_id = :knowledge_object_id
                  ) THEN 1 ELSE 0 END AS searchable
                FROM jobs j
                LEFT JOIN job_attempts ja ON ja.job_id = j.id
                 AND ja.started_at = (
                    SELECT max(started_at) FROM job_attempts WHERE job_id = j.id
                 )
                LEFT JOIN knowledge_objects ko ON ko.id = :knowledge_object_id
                WHERE j.job_type = 'knowledge.index'
                  AND (
                    json_extract(j.payload_json, '$.knowledge_object_id') = :knowledge_object_id
                    OR json_extract(j.payload_json, '$.aggregate_id') = :knowledge_object_id
                  )
                ORDER BY j.created_at DESC LIMIT 1
            """),
                {"knowledge_object_id": knowledge_object_id},
            )
            .mappings()
            .first()
        )
    if row is None:
        with session_scope(session_factory) as session:
            row = (
                session.execute(
                    text(
                        """
                    SELECT
                      oe.id,
                      'pending' AS status,
                      0 AS attempts,
                      3 AS max_attempts,
                      oe.updated_at,
                      oe.payload_json,
                      NULL AS error_class,
                      NULL AS error_message,
                      ko.lifecycle_status,
                      0 AS searchable
                    FROM outbox_events oe
                    LEFT JOIN knowledge_objects ko ON ko.id = :knowledge_object_id
                    WHERE oe.event_type IN (
                      'evidence.ingested',
                      'knowledge.reindex_requested',
                      'knowledge_candidate.created',
                      'knowledge_candidate.confirmed'
                    )
                      AND (
                        oe.aggregate_id = :knowledge_object_id
                        OR json_extract(oe.payload_json, '$.knowledge_object_id')
                           = :knowledge_object_id
                      )
                      AND oe.status IN ('pending', 'processing')
                    ORDER BY oe.created_at DESC, oe.id DESC
                    LIMIT 1
                    """
                    ),
                    {"knowledge_object_id": knowledge_object_id},
                )
                .mappings()
                .first()
            )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="processing job not found",
        )
    row_dict = dict(row)
    etag = job_etag(row_dict)
    payload = _json_result(row["payload_json"])
    failure = failure_from_row(
        error_class=str(row["error_class"]) if row["error_class"] else None,
        error_message=str(row["error_message"]) if row["error_message"] else None,
        payload=payload,
        job_status=str(row["status"]),
    )
    projection = project_import_status(
        job_status=str(row["status"]),
        lifecycle_status=str(row["lifecycle_status"]) if row["lifecycle_status"] else None,
        searchable=bool(row["searchable"]),
        failure=failure,
    )
    response.headers["ETag"] = etag
    return KnowledgeProcessingResponse(
        knowledge_object_id=knowledge_object_id,
        status=projection.public_status.value,
        public_status=projection.public_status.value,
        job_id=str(row["id"]),
        job_status=projection.job_status.value if projection.job_status else str(row["status"]),
        knowledge_lifecycle_status=projection.knowledge_lifecycle_status,
        searchable=projection.searchable,
        attempts=int(row["attempts"]),
        max_attempts=int(row["max_attempts"]),
        etag=etag,
        failure_code=failure.code if failure else None,
        failure_stage=failure.stage if failure else None,
        retryable=bool(failure.retryable if failure else row["status"] in {"failed", "dead"}),
        redacted_summary=failure.redacted_summary if failure else None,
    )


@router.post(
    "/v1/knowledge/{knowledge_object_id}/retry",
    response_model=KnowledgeRetryResponse,
)
def retry_knowledge_processing(
    knowledge_object_id: str,
    session_factory: SessionFactoryDep,
    user_id: AuthDep,
    idempotency_key: WriteDep,
    if_match: IfMatchDep,
) -> KnowledgeRetryResponse:
    payload = {
        "knowledge_object_id": knowledge_object_id,
        "if_match": if_match,
    }
    operation_key = f"knowledge.retry:{user_id}:{knowledge_object_id}:{idempotency_key}"
    with session_scope(session_factory) as session:
        receipt_id, replay = _begin_api_mutation(
            session,
            operation_key=operation_key,
            operation_type="knowledge.retry",
            payload=payload,
        )
    if replay is not None:
        return KnowledgeRetryResponse(status=replay.status, result=replay.result)
    try:
        with session_scope(session_factory) as session:
            job = _latest_processing_job(session, knowledge_object_id)
            if job is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="processing job not found",
                )
            result = job_repository.retry_failed_job(
                session,
                knowledge_object_id=knowledge_object_id,
                expected_job_id=str(job["id"]),
                expected_etag=if_match,
                operation_key=operation_key,
            )
            response_payload = {
                "knowledge_object_id": knowledge_object_id,
                "previous_job_id": result.previous_job_id,
                "job_id": result.job_id,
                "job_status": result.status,
                "public_status": "queued",
                "etag": result.etag,
            }
            _complete_operation_receipt(
                session,
                receipt_id,
                status_value="ok",
                result=response_payload,
            )
    except ValueError as exc:
        message = str(exc)
        if "stale" in message:
            raise HTTPException(
                status_code=status.HTTP_412_PRECONDITION_FAILED,
                detail=message,
            ) from exc
        if "can be retried" in message:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=message,
            ) from exc
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=message) from exc
    return KnowledgeRetryResponse(status="ok", result=response_payload)


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


def _latest_processing_job(session: Session, knowledge_object_id: str) -> RowMapping | None:
    return (
        session.execute(
            text(
                """
            SELECT id, status, attempts, max_attempts, updated_at
            FROM jobs
            WHERE job_type = 'knowledge.index'
              AND (
                json_extract(payload_json, '$.knowledge_object_id') = :knowledge_object_id
                OR json_extract(payload_json, '$.aggregate_id') = :knowledge_object_id
              )
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """
            ),
            {"knowledge_object_id": knowledge_object_id},
        )
        .mappings()
        .first()
    )


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
    row = (
        session.execute(
            text(
                """
            SELECT id, result_json, status, request_hash
            FROM knowledge_operation_receipts
            WHERE operation_key = :operation_key
            """
            ),
            {"operation_key": operation_key},
        )
        .mappings()
        .first()
    )
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
