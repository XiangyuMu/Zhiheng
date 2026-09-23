from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Annotated, Literal, cast

from fastapi import APIRouter, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text

from zhiheng.api.knowledge import AuthDep, SessionFactoryDep
from zhiheng.core.ids import new_id
from zhiheng.db.session import session_scope
from zhiheng.jobs.import_tasks import (
    ImportTaskProjection,
    _append_batch_event,
    list_import_tasks,
    sync_all_import_batches,
)

router = APIRouter()


class BatchItemRequest(BaseModel):
    source_id: str
    task_id: str | None = None
    title: str | None = None
    source_type: str | None = None
    filename: str | None = None
    media_type: str | None = None
    content_sha256: str | None = None


class BatchCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[BatchItemRequest]
    source_type: str | None = None


class BatchRetryRequest(BaseModel):
    item_ids: list[str] | None = None


@router.post("/v1/knowledge/import-batches", status_code=202)
def create_import_batch(
    payload: BatchCreateRequest,
    session_factory: SessionFactoryDep,
    user_id: AuthDep,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
) -> dict[str, object]:
    if not payload.items or len(payload.items) > 100:
        raise HTTPException(status_code=422, detail="items must contain 1 to 100 entries")
    with session_scope(session_factory) as session:
        existing = session.execute(
            text("SELECT id FROM import_batches WHERE idempotency_key=:k AND user_id=:u"),
            {"k": idempotency_key, "u": user_id},
        ).scalar()
        if existing:
            return {"batch_id": str(existing), "replayed": True}
        batch_id = new_id()
        session.execute(
            text(
                "INSERT INTO import_batches "
                "(id,idempotency_key,user_id,source_type,status) "
                "VALUES (:id,:k,:u,:source_type,'queued')"
            ),
            {
                "id": batch_id,
                "k": idempotency_key,
                "u": user_id,
                "source_type": payload.source_type,
            },
        )
        item_ids = []
        for item in payload.items:
            item_id = item.task_id or new_id()
            session.execute(
                text(
                    "INSERT INTO import_batch_items "
                    "(id,batch_id,task_id,source_id,source_type,filename,media_type,"
                    "content_sha256,status,stage) "
                    "VALUES (:id,:b,:t,:source_id,:source_type,:filename,:media_type,"
                    ":content_sha256,'queued','queued')"
                ),
                {
                    "id": new_id(),
                    "b": batch_id,
                    "t": item_id,
                    "source_id": item.source_id,
                    "source_type": item.source_type or payload.source_type,
                    "filename": item.filename or item.title,
                    "media_type": item.media_type,
                    "content_sha256": item.content_sha256,
                },
            )
            item_ids.append(item_id)
        _append_batch_event(
            session,
            batch_id=batch_id,
            item_id=None,
            event_type="batch.created",
            payload={"batch_id": batch_id, "item_task_ids": item_ids, "status": "queued"},
        )
    return {"batch_id": batch_id, "item_task_ids": item_ids, "status": "queued", "replayed": False}


@router.get("/v1/knowledge/import-batches/{batch_id}")
def get_import_batch(
    batch_id: str, session_factory: SessionFactoryDep, user_id: AuthDep
) -> dict[str, object]:
    with session_scope(session_factory) as session:
        sync_all_import_batches(session)
        batch = (
            session.execute(
                text(
                    "SELECT id,status,source_type,revision,created_at,updated_at,completed_at "
                    "FROM import_batches WHERE id=:b AND user_id=:u"
                ),
                {"b": batch_id, "u": user_id},
            )
            .mappings()
            .first()
        )
        rows = (
            session.execute(
                text(
                    "SELECT id,task_id,source_id,source_type,filename,media_type,status,stage,"
                    "progress_completed,progress_total,error_stage,error_summary,"
                    "error_diagnostic_id,retryable,retry_count,version_id,completed_at "
                    "FROM import_batch_items WHERE batch_id=:b ORDER BY id"
                ),
                {"b": batch_id},
            )
            .mappings()
            .all()
        )
        if batch is None:
            raise HTTPException(status_code=404, detail="batch not found")
    if not rows:
        raise HTTPException(status_code=404, detail="batch not found")
    states = [str(r["status"]) for r in rows]
    total = len(states)
    completed = sum(
        s
        in {"succeeded", "failed", "dead_letter", "unsupported", "duplicate", "cancelled"}
        for s in states
    )
    progress_completed = sum(int(r["progress_completed"] or 0) for r in rows)
    progress_total = sum(
        int(r["progress_total"] or 0) for r in rows if r["progress_total"] is not None
    )
    return {
        "batch_id": batch_id,
        "status": str(batch["status"]),
        "source_type": batch["source_type"],
        "revision": int(batch["revision"] or 0),
        "etag": f'W/"{batch_id}:{int(batch["revision"] or 0)}"',
        "created_at": str(batch["created_at"]),
        "updated_at": str(batch["updated_at"]),
        "completed_at": str(batch["completed_at"]) if batch["completed_at"] else None,
        "summary": {
            "total": total,
            "completed": completed,
            "succeeded": states.count("succeeded"),
            "failed": states.count("failed") + states.count("dead_letter"),
            "unsupported": states.count("unsupported"),
            "duplicate": states.count("duplicate"),
            "processing": states.count("processing"),
            "queued": states.count("queued"),
            "progress_completed": progress_completed,
            "progress_total": progress_total or None,
        },
        "items": [dict(r) for r in rows],
    }


@router.post("/v1/knowledge/import-batches/{batch_id}/retry")
def retry_import_batch(
    batch_id: str,
    payload: BatchRetryRequest,
    session_factory: SessionFactoryDep,
    user_id: AuthDep,
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> dict[str, object]:
    with session_scope(session_factory) as session:
        sync_all_import_batches(session)
        batch = session.execute(
            text("SELECT revision FROM import_batches WHERE id=:b AND user_id=:u"),
            {"b": batch_id, "u": user_id},
        ).scalar()
        if batch is None:
            raise HTTPException(status_code=404, detail="batch not found")
        if if_match is not None and if_match != f'W/"{batch_id}:{int(batch)}"':
            raise HTTPException(status_code=412, detail="stale batch etag")
        rows = (
            session.execute(
                text(
                    "SELECT id,task_id,status,retry_count FROM import_batch_items WHERE batch_id=:b"
                ),
                {"b": batch_id},
            )
            .mappings()
            .all()
        )
        if not rows:
            raise HTTPException(status_code=404, detail="batch not found")
        chosen = set(payload.item_ids or [str(r["task_id"]) for r in rows])
        retried = []
        for row in rows:
            if str(row["task_id"]) in chosen and str(row["status"]) in {
                "failed",
                "dead",
                "retryable_failed",
                "dead_letter",
            }:
                session.execute(
                    text(
                        "UPDATE import_batch_items SET status='queued', stage='queued', "
                        "error_stage=NULL,error_summary=NULL,error_diagnostic_id=NULL,"
                        "retryable=0,retry_count=retry_count+1,completed_at=NULL "
                        "WHERE id=:id"
                    ),
                    {"id": row["id"]},
                )
                job = (
                    session.execute(
                        text(
                            "SELECT id,job_type,idempotency_key,payload_json,max_attempts "
                            "FROM jobs WHERE id=:id AND status IN ('failed','dead')"
                        ),
                        {"id": row["task_id"]},
                    )
                    .mappings()
                    .first()
                )
                if job is not None:
                    retry_key = (
                        f"batch-retry:{batch_id}:{row['task_id']}:"
                        f"{int(row['retry_count'] or 0) + 1}"
                    )
                    retry_payload = json.loads(str(job["payload_json"]))
                    retry_payload.update(
                        {
                            "retry_of_job_id": str(job["id"]),
                            "retry_operation_key": retry_key,
                        }
                    )
                    new_job_id = new_id()
                    session.execute(
                        text(
                            """
                            INSERT INTO jobs
                              (id,job_type,idempotency_key,payload_json,status,attempts,max_attempts)
                            VALUES (:id,:job_type,:key,:payload,'pending',0,:max_attempts)
                            """
                        ),
                        {
                            "id": new_job_id,
                            "job_type": job["job_type"],
                            "key": retry_key,
                            "payload": json.dumps(
                                retry_payload, ensure_ascii=False, sort_keys=True
                            ),
                            "max_attempts": int(job["max_attempts"] or 3),
                        },
                    )
                    session.execute(
                        text("UPDATE import_batch_items SET task_id=:new_task WHERE id=:item_id"),
                        {"new_task": new_job_id, "item_id": row["id"]},
                    )
                retried.append(str(row["task_id"]))
        if retried:
            _append_batch_event(
                session,
                batch_id=batch_id,
                item_id=None,
                event_type="batch.retry_requested",
                payload={"batch_id": batch_id, "task_ids": retried},
            )
            session.execute(
                text("UPDATE import_batches SET status='queued', completed_at=NULL WHERE id=:b"),
                {"b": batch_id},
            )
    return {
        "batch_id": batch_id,
        "status": "queued",
        "retried_task_ids": retried,
        "etag": f'W/"{batch_id}"',
    }


@router.get("/v1/knowledge/import-batches/{batch_id}/events")
def import_batch_events(
    batch_id: str,
    request: Request,
    session_factory: SessionFactoryDep,
    user_id: AuthDep,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    after: int | None = Query(default=None, ge=0),
) -> StreamingResponse:
    """Return durable SSE events; clients can resume with an event revision."""
    try:
        cursor = max(int(last_event_id or after or 0), 0)
    except ValueError:
        cursor = 0
    with session_scope(session_factory) as session:
        sync_all_import_batches(session)
        owned = session.execute(
            text("SELECT 1 FROM import_batches WHERE id=:b AND user_id=:u"),
            {"b": batch_id, "u": user_id},
        ).first()
        if owned is None:
            raise HTTPException(status_code=404, detail="batch not found")
        events = (
            session.execute(
                text(
                    "SELECT revision,event_type,payload_json FROM import_batch_events "
                    "WHERE batch_id=:b AND revision>:cursor ORDER BY revision"
                ),
                {"b": batch_id, "cursor": cursor},
            )
            .mappings()
            .all()
        )

    def stream() -> Iterator[str]:
        for event in events:
            yield (
                f"id: {event['revision']}\n"
                f"event: {event['event_type']}\n"
                f"data: {event['payload_json']}\n\n"
            )
        if not events:
            yield ": keep-alive\n\n"

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/v1/knowledge/import-batches")
def list_import_batches(
    session_factory: SessionFactoryDep,
    user_id: AuthDep,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    source_type: str | None = None,
    created_from: str | None = None,
    created_to: str | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> dict[str, object]:
    with session_scope(session_factory) as session:
        sync_all_import_batches(session)
        conditions = ["user_id=:user_id"]
        params: dict[str, object] = {"user_id": user_id, "limit": limit, "offset": offset}
        if status_filter:
            conditions.append("status=:status")
            params["status"] = status_filter
        if source_type:
            conditions.append("source_type=:source_type")
            params["source_type"] = source_type
        if created_from:
            conditions.append("created_at>=:created_from")
            params["created_from"] = created_from
        if created_to:
            conditions.append("created_at<:created_to")
            params["created_to"] = created_to
        where = " AND ".join(conditions)
        total = int(
            session.execute(
                text(f"SELECT count(*) FROM import_batches WHERE {where}"), params
            ).scalar_one()
        )
        rows = (
            session.execute(
                text(
                    f"""
                SELECT b.id,b.status,b.source_type,b.revision,b.created_at,b.updated_at,
                       b.completed_at,
                       count(i.id) AS item_count,
                       sum(CASE WHEN i.status='succeeded' THEN 1 ELSE 0 END) AS succeeded_count,
                       sum(CASE WHEN i.status IN ('failed','dead_letter')
                                THEN 1 ELSE 0 END) AS failed_count
                       ,sum(CASE WHEN i.status='unsupported'
                                 THEN 1 ELSE 0 END) AS unsupported_count
                FROM import_batches b
                LEFT JOIN import_batch_items i ON i.batch_id=b.id
                WHERE {where}
                GROUP BY b.id
                ORDER BY b.created_at DESC,b.id DESC
                LIMIT :limit OFFSET :offset
                """
                ),
                params,
            )
            .mappings()
            .all()
        )
    return {
        "items": [
            {
                "batch_id": str(row["id"]),
                "status": str(row["status"]),
                "source_type": row["source_type"],
                "revision": int(row["revision"] or 0),
                "created_at": str(row["created_at"]),
                "updated_at": str(row["updated_at"]),
                "completed_at": str(row["completed_at"]) if row["completed_at"] else None,
                "item_count": int(row["item_count"] or 0),
                "succeeded_count": int(row["succeeded_count"] or 0),
                "failed_count": int(row["failed_count"] or 0),
                "unsupported_count": int(row["unsupported_count"] or 0),
            }
            for row in rows
        ],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


class ImportTaskFailurePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    stage: str
    retryable: bool
    redacted_summary: str


class ImportTaskPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str
    task_type: Literal["pdf", "knowledge"]
    source_id: str
    job_id: str | None
    title: str | None
    status: str
    attempts: int
    max_attempts: int
    progress_completed: int
    progress_total: int | None
    retryable: bool
    failure: ImportTaskFailurePayload | None
    created_at: str
    updated_at: str


class ImportTaskListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[ImportTaskPayload]
    limit: int
    offset: int


def install_import_task_routes(app: FastAPI) -> None:
    app.include_router(router)


@router.get("/v1/knowledge/import-tasks", response_model=ImportTaskListResponse)
def list_import_task_status(
    session_factory: SessionFactoryDep,
    _user_id: AuthDep,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    task_type: Literal["pdf", "knowledge"] | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> ImportTaskListResponse:
    with session_scope(session_factory) as session:
        tasks = list_import_tasks(
            session,
            status_filter=status_filter,
            task_type=task_type,
            limit=limit,
            offset=offset,
        )
    return ImportTaskListResponse(
        items=[_payload(task) for task in tasks],
        limit=limit,
        offset=offset,
    )


def _payload(task: ImportTaskProjection) -> ImportTaskPayload:
    failure = (
        ImportTaskFailurePayload(
            code=task.failure.code,
            stage=task.failure.stage,
            retryable=task.failure.retryable,
            redacted_summary=task.failure.redacted_summary,
        )
        if task.failure
        else None
    )
    return ImportTaskPayload(
        task_id=task.task_id,
        task_type=cast(Literal["pdf", "knowledge"], task.task_type),
        source_id=task.source_id,
        job_id=task.job_id,
        title=task.title,
        status=task.status,
        attempts=task.attempts,
        max_attempts=task.max_attempts,
        progress_completed=task.progress_completed,
        progress_total=task.progress_total,
        retryable=task.retryable,
        failure=failure,
        created_at=task.created_at,
        updated_at=task.updated_at,
    )


__all__ = ["install_import_task_routes"]
