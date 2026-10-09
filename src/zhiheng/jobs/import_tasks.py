from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from zhiheng.core.ids import new_id
from zhiheng.jobs.knowledge_contract import (
    KnowledgeFailure,
    failure_from_row,
    project_import_status,
)
from zhiheng.retrieval.qualification import formal_searchable_sql


@dataclass(frozen=True, slots=True)
class ImportTaskProjection:
    """Stable read model spanning PDF tasks and knowledge index jobs."""

    task_id: str
    task_type: str
    source_id: str
    job_id: str | None
    title: str | None
    status: str
    attempts: int
    max_attempts: int
    progress_completed: int
    progress_total: int | None
    created_at: str
    updated_at: str
    failure: KnowledgeFailure | None

    @property
    def retryable(self) -> bool:
        return bool(self.failure and self.failure.retryable) or self.status in {
            "failed",
            "dead_letter",
            "parse_failed",
            "partial",
        }


_TERMINAL_BATCH_STATES = {
    "succeeded",
    "failed",
    "dead_letter",
    "unsupported",
    "cancelled",
    "duplicate",
}


def sync_import_batches_for_task(
    session: Session,
    task_ids: str | Iterable[str],
) -> int:
    """Project authoritative job/PDF state onto batch items and append events.

    Batch rows are a durable read model.  Jobs remain authoritative, so this
    function is safe to call after every worker transition and also from API
    reads (which repairs batches created before a worker was upgraded).
    """
    ids = [task_ids] if isinstance(task_ids, str) else list(task_ids)
    if not ids:
        return 0
    changed = 0
    placeholders = ", ".join(f":task_{index}" for index in range(len(ids)))
    params = {f"task_{index}": value for index, value in enumerate(ids)}
    item_rows = (
        session.execute(
            text(
                f"""
            SELECT id, batch_id, task_id, source_id, status, stage,
                   progress_completed, progress_total, retry_count,
                   error_stage, error_summary, error_diagnostic_id, retryable,
                   completed_at
            FROM import_batch_items
            WHERE task_id IN ({placeholders}) OR source_id IN ({placeholders})
            """
            ),
            params,
        )
        .mappings()
        .all()
    )
    for item in item_rows:
        projected = _project_batch_item(session, dict(item))
        if _item_changed(item, projected):
            session.execute(
                text(
                    """
                    UPDATE import_batch_items
                    SET status=:status, stage=:stage,
                        progress_completed=:progress_completed,
                        progress_total=:progress_total,
                        error_stage=:error_stage, error_summary=:error_summary,
                        error_diagnostic_id=:error_diagnostic_id,
                        retryable=:retryable, retry_count=:retry_count,
                        completed_at=:completed_at, updated_at=CURRENT_TIMESTAMP
                    WHERE id=:item_id
                    """
                ),
                {"item_id": item["id"], **projected},
            )
            _append_batch_event(
                session,
                batch_id=str(item["batch_id"]),
                item_id=str(item["id"]),
                event_type="item.updated",
                payload={"item_id": str(item["id"]), **projected},
            )
            changed += 1
        changed += _recompute_batch(session, str(item["batch_id"]))
    return changed


def sync_all_import_batches(session: Session) -> int:
    rows = session.execute(text("SELECT DISTINCT task_id FROM import_batch_items")).scalars()
    return sync_import_batches_for_task(session, list(rows))


def _project_batch_item(session: Session, item: dict[str, Any]) -> dict[str, Any]:
    task_id = str(item["task_id"])
    pdf = (
        session.execute(
            text(
                """
            SELECT t.state, t.updated_at,
                   count(DISTINCT p.id) AS progress_total,
                   count(DISTINCT CASE WHEN p.status='parsed' THEN p.id END)
                     AS progress_completed,
                   j.status AS job_status, j.attempts, j.max_attempts,
                   ja.error_class, ja.error_message,
                   a.status AS attempt_status
            FROM pdf_tasks t
            LEFT JOIN pdf_parse_attempts a
              ON a.task_id=t.id
             AND a.attempt_no=(SELECT max(attempt_no) FROM pdf_parse_attempts WHERE task_id=t.id)
            LEFT JOIN pdf_pages p ON p.attempt_id=a.id
            LEFT JOIN jobs j
              ON j.job_type='knowledge.parse_pdf'
             AND json_extract(j.payload_json,'$.task_id')=t.id
             AND j.created_at=(SELECT max(j2.created_at) FROM jobs j2
                               WHERE j2.job_type='knowledge.parse_pdf'
                                 AND json_extract(j2.payload_json,'$.task_id')=t.id)
            LEFT JOIN job_attempts ja
              ON ja.job_id=j.id
             AND ja.started_at=(SELECT max(ja2.started_at) FROM job_attempts ja2
                                WHERE ja2.job_id=j.id)
            WHERE t.id=:task_id
            GROUP BY t.id, j.id, a.id, ja.id
            """
            ),
            {"task_id": task_id},
        )
        .mappings()
        .first()
    )
    if pdf is not None:
        status = _pdf_public_status(pdf)
        error = _error_fields(pdf, status)
        return {
            "status": status,
            "stage": _pdf_stage(status),
            "progress_completed": int(pdf["progress_completed"] or 0),
            "progress_total": int(pdf["progress_total"] or 0),
            "error_stage": error["error_stage"],
            "error_summary": error["error_summary"],
            "error_diagnostic_id": error["error_diagnostic_id"],
            "retryable": error["retryable"],
            "retry_count": int(item.get("retry_count") or 0),
            "completed_at": (
                datetime.utcnow().isoformat(timespec="seconds")
                if status in _TERMINAL_BATCH_STATES
                else None
            ),
        }
    job = (
        session.execute(
            text(
                """
            SELECT j.status, j.attempts, j.max_attempts, j.updated_at,
                   ja.error_class, ja.error_message, j.payload_json
            FROM jobs j
            LEFT JOIN job_attempts ja
              ON ja.job_id=j.id
             AND ja.started_at=(SELECT max(ja2.started_at) FROM job_attempts ja2
                                WHERE ja2.job_id=j.id)
            WHERE j.id=:task_id
               OR json_extract(j.payload_json,'$.knowledge_object_id')=:source_id
            """
            ),
            {"task_id": task_id, "source_id": str(item.get("source_id") or "")},
        )
        .mappings()
        .first()
    )
    if job is None:
        return {
            "status": str(item.get("status") or "queued"),
            "stage": str(item.get("stage") or "queued"),
            "progress_completed": int(item.get("progress_completed") or 0),
            "progress_total": item.get("progress_total"),
            "error_stage": item.get("error_stage"),
            "error_summary": item.get("error_summary"),
            "error_diagnostic_id": item.get("error_diagnostic_id"),
            "retryable": bool(item.get("retryable")),
            "retry_count": int(item.get("retry_count") or 0),
            "completed_at": item.get("completed_at"),
        }
    payload = _json_object(job["payload_json"])
    status = {
        "pending": "queued",
        "processing": "processing",
        "completed": "succeeded",
        "failed": "failed",
        "dead": "dead_letter",
        "unsupported": "unsupported",
    }.get(str(job["status"]), str(job["status"]))
    code = str(payload.get("failure_code") or "").strip() or None
    stage = str(payload.get("failure_stage") or "").strip() or (
        "index" if status in {"failed", "dead_letter"} else status
    )
    retryable = bool(payload.get("retryable", status in {"failed", "dead_letter"}))
    return {
        "status": status,
        "stage": stage,
        "progress_completed": 1 if status == "succeeded" else 0,
        "progress_total": 1,
        "error_stage": stage if code or job["error_message"] else None,
        "error_summary": (
            _redact(str(job["error_message"] or code)) if code or job["error_message"] else None
        ),
        "error_diagnostic_id": (
            str(payload.get("diagnostic_id")) if payload.get("diagnostic_id") else None
        ),
        "retryable": retryable if status in {"failed", "dead_letter"} else False,
        "retry_count": int(item.get("retry_count") or 0),
        "completed_at": (
            datetime.utcnow().isoformat(timespec="seconds")
            if status in _TERMINAL_BATCH_STATES
            else None
        ),
    }


def _pdf_public_status(row: Any) -> str:
    job_status = str(row["job_status"]) if row["job_status"] else None
    source_status = str(row["state"]) if row["state"] else None
    if job_status == "processing":
        return "processing"
    if job_status == "unsupported":
        return "unsupported"
    if job_status == "dead":
        return "dead_letter"
    if job_status == "failed":
        return "failed"
    if source_status == "unsupported":
        return "unsupported"
    if source_status == "parsed":
        # Parsing is not formal retrieval qualification. The indexing job must
        # publish a knowledge object and serving pointer before success.
        return "processing"
    if source_status == "partial":
        return "partial"
    return "queued" if source_status == "queued" else source_status or "queued"


def _pdf_stage(status: str) -> str:
    return {
        "queued": "queued",
        "processing": "parse",
        "partial": "parse",
        "succeeded": "index",
        "failed": "parse",
        "dead_letter": "parse",
    }.get(status, status)


def _error_fields(row: Any, status: str) -> dict[str, Any]:
    if status not in {"failed", "dead_letter", "unsupported"} and not row["error_message"]:
        return {
            "error_stage": None,
            "error_summary": None,
            "error_diagnostic_id": None,
            "retryable": False,
        }
    summary = _redact(str(row["error_message"] or row["attempt_status"] or "导入失败"))
    return {
        "error_stage": "parse",
        "error_summary": summary[:512],
        "error_diagnostic_id": str(row["error_class"]) if row["error_class"] else None,
        "retryable": status == "failed",
    }


def _item_changed(item: Any, projected: dict[str, Any]) -> bool:
    for key in (
        "status",
        "stage",
        "progress_completed",
        "progress_total",
        "error_stage",
        "error_summary",
        "error_diagnostic_id",
        "retryable",
    ):
        if item[key] != projected[key]:
            return True
    return False


def _recompute_batch(session: Session, batch_id: str) -> int:
    rows = (
        session.execute(
            text("SELECT status FROM import_batch_items WHERE batch_id=:batch_id"),
            {"batch_id": batch_id},
        )
        .scalars()
        .all()
    )
    if not rows:
        return 0
    statuses = [str(status) for status in rows]
    if all(status == "succeeded" for status in statuses):
        aggregate = "succeeded"
    elif all(status in _TERMINAL_BATCH_STATES for status in statuses):
        if any(status == "succeeded" for status in statuses):
            aggregate = "partial"
        elif all(status == "unsupported" for status in statuses):
            aggregate = "unsupported"
        else:
            aggregate = "failed"
    elif any(status in {"processing", "partial"} for status in statuses):
        aggregate = "processing"
    else:
        aggregate = "queued"
    current = session.execute(
        text("SELECT status FROM import_batches WHERE id=:batch_id"),
        {"batch_id": batch_id},
    ).scalar()
    if current == aggregate:
        return 0
    session.execute(
        text(
            """
            UPDATE import_batches
            SET status=:status,
                completed_at=CASE WHEN :status IN ('succeeded','partial','failed')
                                  THEN COALESCE(completed_at,CURRENT_TIMESTAMP) ELSE NULL END,
                revision=revision+1, updated_at=CURRENT_TIMESTAMP
            WHERE id=:batch_id
            """
        ),
        {"status": aggregate, "batch_id": batch_id},
    )
    _append_batch_event(
        session,
        batch_id=batch_id,
        item_id=None,
        event_type="batch.updated",
        payload={"batch_id": batch_id, "status": aggregate},
    )
    return 1


def _append_batch_event(
    session: Session,
    *,
    batch_id: str,
    item_id: str | None,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    revision = (
        int(
            session.execute(
                text("SELECT revision FROM import_batches WHERE id=:batch_id"),
                {"batch_id": batch_id},
            ).scalar_one()
        )
        + 1
    )
    session.execute(
        text(
            """
            UPDATE import_batches SET revision=:revision, updated_at=CURRENT_TIMESTAMP
            WHERE id=:batch_id
            """
        ),
        {"revision": revision, "batch_id": batch_id},
    )
    session.execute(
        text(
            """
            INSERT INTO import_batch_events
              (id,batch_id,revision,event_type,item_id,payload_json)
            VALUES (:id,:batch_id,:revision,:event_type,:item_id,:payload_json)
            """
        ),
        {
            "id": new_id(),
            "batch_id": batch_id,
            "revision": revision,
            "event_type": event_type,
            "item_id": item_id,
            "payload_json": json.dumps(payload, ensure_ascii=False, sort_keys=True),
        },
    )


def _redact(value: str) -> str:
    return value.replace("\r", " ").replace("\n", " ").strip()


def list_import_tasks(
    session: Session,
    *,
    status_filter: str | None = None,
    task_type: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[ImportTaskProjection]:
    """Return one deterministic task list without duplicating worker state.

    PDF rows are keyed by ``pdf_tasks.id`` and index rows by ``jobs.id``.
    The source tables remain authoritative; this projection is deliberately
    read-only so retries continue to use the existing job fencing contract.
    """

    rows = session.execute(
        text(
            f"""
            SELECT
              t.id AS task_id,
              'pdf' AS task_type,
              t.id AS source_id,
              j.id AS job_id,
              json_extract(eo.source_metadata_json, '$.title') AS title,
              t.state AS source_status,
              COALESCE(ij.status, j.status) AS job_status,
              t.created_at AS created_at,
              t.updated_at AS updated_at,
              j.attempts AS job_attempts,
              j.max_attempts AS max_attempts,
              (
                SELECT count(*)
                FROM pdf_pages p
                WHERE p.attempt_id = (
                  SELECT a.id
                  FROM pdf_parse_attempts a
                  WHERE a.task_id = t.id
                  ORDER BY a.attempt_no DESC
                  LIMIT 1
                )
                AND p.status = 'parsed'
              ) AS progress_completed,
              (
                SELECT count(*)
                FROM pdf_pages p
                WHERE p.attempt_id = (
                  SELECT a.id
                  FROM pdf_parse_attempts a
                  WHERE a.task_id = t.id
                  ORDER BY a.attempt_no DESC
                  LIMIT 1
                )
              ) AS progress_total,
              ja.error_class AS error_class,
              ja.error_message AS error_message,
              COALESCE(ij.payload_json, j.payload_json) AS payload_json,
              ko.lifecycle_status AS lifecycle_status,
              CASE WHEN ko.id IS NOT NULL AND {formal_searchable_sql("ko")} THEN 1 ELSE 0 END
                AS searchable
            FROM pdf_tasks t
            JOIN evidence_objects eo ON eo.id = t.evidence_object_id
            LEFT JOIN jobs j
              ON j.job_type = 'knowledge.parse_pdf'
             AND json_extract(j.payload_json, '$.task_id') = t.id
             AND j.created_at = (
               SELECT max(j2.created_at)
               FROM jobs j2
               WHERE j2.job_type = 'knowledge.parse_pdf'
                 AND json_extract(j2.payload_json, '$.task_id') = t.id
             )
            LEFT JOIN jobs ij
              ON ij.job_type = 'knowledge.index'
             AND json_extract(ij.payload_json, '$.task_id') = t.id
             AND ij.created_at = (
               SELECT max(ij2.created_at)
               FROM jobs ij2
               WHERE ij2.job_type = 'knowledge.index'
                 AND json_extract(ij2.payload_json, '$.task_id') = t.id
             )
            LEFT JOIN job_attempts ja
              ON ja.job_id = COALESCE(ij.id, j.id)
             AND ja.started_at = (
               SELECT max(ja2.started_at)
               FROM job_attempts ja2
               WHERE ja2.job_id = COALESCE(ij.id, j.id)
             )
            LEFT JOIN knowledge_objects ko
              ON ko.id = COALESCE(
                json_extract(ij.payload_json, '$.knowledge_object_id'),
                json_extract(j.payload_json, '$.knowledge_object_id')
              )

            UNION ALL

            SELECT
              j.id AS task_id,
              'knowledge' AS task_type,
              COALESCE(
                json_extract(j.payload_json, '$.knowledge_object_id'),
                json_extract(j.payload_json, '$.aggregate_id'),
                j.id
              ) AS source_id,
              j.id AS job_id,
              ko.title AS title,
              NULL AS source_status,
              j.status AS job_status,
              j.created_at AS created_at,
              j.updated_at AS updated_at,
              j.attempts AS job_attempts,
              j.max_attempts AS max_attempts,
              CASE WHEN EXISTS (
                SELECT 1
                FROM serving_chunks s
                WHERE s.source_id = COALESCE(
                  json_extract(j.payload_json, '$.knowledge_object_id'),
                  json_extract(j.payload_json, '$.aggregate_id')
                )
              ) THEN 1 ELSE 0 END AS progress_completed,
              1 AS progress_total,
              ja.error_class AS error_class,
              ja.error_message AS error_message,
              j.payload_json AS payload_json,
              ko.lifecycle_status AS lifecycle_status,
              CASE WHEN ko.id IS NOT NULL AND {formal_searchable_sql("ko")} THEN 1 ELSE 0 END
                AS searchable
            FROM jobs j
            LEFT JOIN job_attempts ja
              ON ja.job_id = j.id
             AND ja.started_at = (
               SELECT max(ja2.started_at)
               FROM job_attempts ja2
               WHERE ja2.job_id = j.id
             )
            LEFT JOIN knowledge_objects ko
              ON ko.id = COALESCE(
                json_extract(j.payload_json, '$.knowledge_object_id'),
                json_extract(j.payload_json, '$.aggregate_id')
              )
            WHERE j.job_type = 'knowledge.index'
            ORDER BY created_at DESC, task_id DESC
            """
        ),
    ).mappings()

    projections: list[ImportTaskProjection] = []
    for row in rows:
        payload = _json_object(row["payload_json"])
        failure = failure_from_row(
            error_class=str(row["error_class"]) if row["error_class"] else None,
            error_message=str(row["error_message"]) if row["error_message"] else None,
            payload=payload,
            job_status=str(row["job_status"]) if row["job_status"] else None,
        )
        source_status = str(row["source_status"]) if row["source_status"] else None
        job_status = str(row["job_status"]) if row["job_status"] else source_status
        if str(row["task_type"]) == "pdf":
            public_status = _pdf_status(
                source_status, job_status, bool(row["searchable"]), failure
            )
        else:
            public_status = project_import_status(
                job_status=job_status,
                lifecycle_status=(
                    str(row["lifecycle_status"]) if row["lifecycle_status"] else None
                ),
                searchable=bool(row["searchable"]),
                failure=failure,
            ).public_status.value
        if status_filter and public_status != status_filter:
            continue
        if task_type and str(row["task_type"]) != task_type:
            continue
        projections.append(
            ImportTaskProjection(
                task_id=str(row["task_id"]),
                task_type=str(row["task_type"]),
                source_id=str(row["source_id"]),
                job_id=str(row["job_id"]) if row["job_id"] else None,
                title=str(row["title"]) if row["title"] else None,
                status=public_status,
                attempts=int(row["job_attempts"] or 0),
                max_attempts=int(row["max_attempts"] or 3),
                progress_completed=int(row["progress_completed"] or 0),
                progress_total=(
                    int(row["progress_total"]) if row["progress_total"] is not None else None
                ),
                created_at=str(row["created_at"]),
                updated_at=str(row["updated_at"]),
                failure=failure,
            )
        )
    start = max(offset, 0)
    return projections[start : start + min(max(limit, 1), 100)]


def _pdf_status(
    source_status: str | None,
    job_status: str | None,
    searchable: bool,
    failure: KnowledgeFailure | None,
) -> str:
    if job_status == "processing":
        return "processing"
    if job_status == "unsupported":
        return "unsupported"
    if job_status == "dead":
        return "dead_letter"
    if job_status == "failed":
        return "failed"
    if source_status == "queued":
        return "queued"
    if source_status == "partial":
        return "partial"
    if source_status == "parsed":
        return "succeeded" if searchable and job_status == "completed" else "processing"
    if source_status == "unsupported":
        return "unsupported"
    if failure is not None:
        return failure.code
    return source_status or "failed"


def _json_object(value: Any) -> dict[str, Any]:
    if not value:
        return {}
    if isinstance(value, dict):
        return value
    try:
        decoded = json.loads(str(value))
    except (TypeError, ValueError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


__all__ = ["ImportTaskProjection", "list_import_tasks"]
