from __future__ import annotations

import json
import re
from collections.abc import Iterable
from typing import Any, Literal, cast

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text

from zhiheng.api.knowledge import (
    AuthDep,
    SessionFactoryDep,
    WriteDep,
    _begin_api_mutation,
    _complete_operation_receipt,
)
from zhiheng.core.ids import json_text, new_id, sha256_text
from zhiheng.db.session import session_scope
from zhiheng.knowledge.object_store import (
    StoredBinaryArtifact,
    knowledge_object_store_for_settings,
)
from zhiheng.retrieval.qualification import formal_searchable_sql

router = APIRouter(tags=["knowledge-workspace"])


class MergeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    primary_knowledge_object_id: str = Field(min_length=1, max_length=36)
    duplicate_knowledge_object_ids: list[str] = Field(min_length=1, max_length=100)
    preserve_source_ids: list[str] = Field(default_factory=list, max_length=100)


class BulkExportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    knowledge_object_ids: list[str] = Field(min_length=1, max_length=100)
    format: Literal["markdown", "original"] = "markdown"


def install_knowledge_workspace_routes(app: Any) -> None:
    app.include_router(router)


def _owned_clause(alias: str = "ko") -> str:
    return f"({alias}.owner_user_id = :user_id OR {alias}.owner_user_id IS NULL)"


def _version_row(
    session: Any,
    *,
    knowledge_object_id: str,
    version_id: str | None,
    user_id: str,
) -> Any:
    version_filter = (
        "AND kv.id = :version_id" if version_id else "AND kv.id = ko.current_version_id"
    )
    row = (
        session.execute(
            text(
                f"""
            SELECT ko.id AS knowledge_object_id, ko.title, ko.primary_domain_id,
                   ko.object_kind, ko.lifecycle_status, ko.owner_user_id,
                   kv.id AS knowledge_version_id, kv.version_no, kv.summary,
                   kv.markdown_uri, kv.content_version_id AS content_version_id,
                   cv.text_artifact_uri,
                   cv.content_sha256, eo.object_uri, eo.sha256 AS evidence_sha256,
                   eo.media_type, eo.byte_size, eo.source_kind,
                   eo.source_metadata_json,
                   (kv.id = ko.current_version_id AND ({formal_searchable_sql("ko")}))
                     AS searchable
            FROM knowledge_objects ko
            JOIN knowledge_versions kv ON kv.knowledge_object_id = ko.id
            LEFT JOIN content_versions cv ON cv.id = kv.content_version_id
            LEFT JOIN evidence_objects eo ON eo.id = cv.evidence_object_id
            WHERE ko.id = :knowledge_object_id
              {version_filter}
              AND ko.lifecycle_status <> 'privacy_erased'
              AND {_owned_clause()}
            """
            ),
            {
                "knowledge_object_id": knowledge_object_id,
                "version_id": version_id,
                "user_id": user_id,
            },
        )
        .mappings()
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="knowledge version not found")
    return row


def _json(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _read_text(store: Any, uri: str | None) -> str:
    if not uri:
        return ""
    try:
        return cast(str, store.read_bytes(str(uri)).decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return ""


def _reader_payload(session: Any, row: Any, store: Any) -> dict[str, Any]:
    chunks = (
        session.execute(
            text(
                """
            SELECT id, chunk_no, title, raw_text, span_start, span_end, status
            FROM chunks
            WHERE source_id=:object_id AND source_version_id=:version_id
            ORDER BY chunk_no, id
            """
            ),
            {
                "object_id": row["knowledge_object_id"],
                "version_id": row["knowledge_version_id"],
            },
        )
        .mappings()
        .all()
    )
    body = "\n\n".join(str(item["raw_text"] or "") for item in chunks)
    if not body:
        body = _read_text(store, row["text_artifact_uri"])
    spans = [
        dict(item)
        for item in session.execute(
            text(
                """
                SELECT id, span_kind, start_offset, end_offset, page_no, section_path, quote_hash
                FROM content_spans WHERE content_version_id=:content_version_id
                ORDER BY start_offset, id
                """
            ),
            {"content_version_id": row["content_version_id"]},
        ).mappings()
    ]
    pdf = _pdf_resources(session, row["evidence_sha256"])
    return {
        "knowledge_object_id": str(row["knowledge_object_id"]),
        "knowledge_version_id": str(row["knowledge_version_id"]),
        "version_no": int(row["version_no"]),
        "title": str(row["title"]),
        "summary": str(row["summary"]) if row["summary"] is not None else None,
        "media_type": str(row["media_type"]) if row["media_type"] else None,
        "object_kind": str(row["object_kind"]),
        "lifecycle_status": str(row["lifecycle_status"]),
        "primary_domain_id": str(row["primary_domain_id"]),
        "searchable": bool(row["searchable"]),
        "source_metadata": _json(row["source_metadata_json"], {}),
        "citations": spans,
        "source": {
            "kind": str(row["source_kind"]) if row["source_kind"] else None,
            "metadata": _json(row["source_metadata_json"], {}),
            "media_type": str(row["media_type"]) if row["media_type"] else None,
            "byte_size": int(row["byte_size"]) if row["byte_size"] is not None else None,
            "sha256": str(row["evidence_sha256"]) if row["evidence_sha256"] else None,
        },
        "text": body,
        "chunks": [dict(item) for item in chunks],
        "spans": spans,
        "pdf": pdf,
    }


def _pdf_resources(session: Any, evidence_sha256: str | None) -> dict[str, Any] | None:
    if not evidence_sha256:
        return None
    attempt = (
        session.execute(
            text(
                """
            SELECT a.id, a.manifest_uri, a.manifest_sha256, a.backend
            FROM pdf_parse_attempts a
            JOIN evidence_objects eo ON eo.id=a.evidence_object_id
            WHERE eo.sha256=:sha AND a.status IN ('succeeded','published','completed')
            ORDER BY a.attempt_no DESC LIMIT 1
            """
            ),
            {"sha": evidence_sha256},
        )
        .mappings()
        .first()
    )
    if attempt is None:
        return None
    pages = [
        dict(item)
        for item in session.execute(
            text(
                """
                SELECT id, page_no, page_width, page_height, rotation, crop_box_json,
                       render_uri, render_sha256, status
                FROM pdf_pages WHERE attempt_id=:attempt_id ORDER BY page_no
                """
            ),
            {"attempt_id": attempt["id"]},
        ).mappings()
    ]
    blocks = [
        dict(item)
        for item in session.execute(
            text(
                """
                SELECT id, page_id, block_key, region_type, reading_order, bbox_json,
                       text, text_sha256, confidence, text_source, status
                FROM evidence_blocks WHERE attempt_id=:attempt_id ORDER BY reading_order, id
                """
            ),
            {"attempt_id": attempt["id"]},
        ).mappings()
    ]
    tables = [
        dict(item)
        for item in session.execute(
            text(
                """
                SELECT t.id, t.page_id, t.block_id, t.row_count, t.column_count,
                       t.linear_text, t.structure_sha256,
                       (SELECT json_group_array(json_object(
                          'id', c.id, 'row_no', c.row_no, 'column_no', c.column_no,
                          'rowspan', c.rowspan, 'colspan', c.colspan,
                          'bbox', c.bbox_json, 'text', c.text, 'is_header', c.is_header
                       )) FROM pdf_table_cells c WHERE c.table_id=t.id) AS cells_json
                FROM pdf_tables t WHERE t.attempt_id=:attempt_id ORDER BY t.id
                """
            ),
            {"attempt_id": attempt["id"]},
        ).mappings()
    ]
    images = [
        dict(item)
        for item in session.execute(
            text(
                """
                SELECT id, page_id, block_id, artifact_uri, sha256, media_type,
                       bbox_json, caption, description, description_status
                FROM pdf_images WHERE attempt_id=:attempt_id ORDER BY id
                """
            ),
            {"attempt_id": attempt["id"]},
        ).mappings()
    ]
    for table in tables:
        table["cells"] = _json(table.pop("cells_json"), [])
    return {
        "attempt_id": str(attempt["id"]),
        "backend": str(attempt["backend"]),
        "manifest_uri": str(attempt["manifest_uri"]) if attempt["manifest_uri"] else None,
        "manifest_sha256": str(attempt["manifest_sha256"]) if attempt["manifest_sha256"] else None,
        "pages": pages,
        "blocks": blocks,
        "tables": tables,
        "images": images,
    }


@router.get("/v1/knowledge/{knowledge_object_id}/reader")
def read_current_knowledge(
    knowledge_object_id: str,
    session_factory: SessionFactoryDep,
    user_id: AuthDep,
    request: Request,
) -> dict[str, Any]:
    with session_scope(session_factory) as session:
        row = _version_row(
            session,
            knowledge_object_id=knowledge_object_id,
            version_id=None,
            user_id=user_id,
        )
        return _reader_payload(
            session, row, knowledge_object_store_for_settings(request.app.state.knowledge_settings)
        )


@router.get("/v1/knowledge/{knowledge_object_id}/versions/{version_id}/reader")
def read_knowledge_version(
    knowledge_object_id: str,
    version_id: str,
    session_factory: SessionFactoryDep,
    user_id: AuthDep,
    request: Request,
) -> dict[str, Any]:
    with session_scope(session_factory) as session:
        row = _version_row(
            session,
            knowledge_object_id=knowledge_object_id,
            version_id=version_id,
            user_id=user_id,
        )
        return _reader_payload(
            session, row, knowledge_object_store_for_settings(request.app.state.knowledge_settings)
        )


@router.get("/v1/citations/{citation_id}/location")
def citation_location(
    citation_id: str,
    session_factory: SessionFactoryDep,
    user_id: AuthDep,
) -> dict[str, Any]:
    with session_scope(session_factory) as session:
        row = (
            session.execute(
                text(
                    f"""
                SELECT cr.*, ko.title, ko.owner_user_id, c.text, c.raw_text,
                       cs.page_no, cs.section_path, cs.quote_hash AS span_quote_hash
                FROM citation_records cr
                JOIN knowledge_objects ko ON ko.id=cr.source_id
                LEFT JOIN chunks c ON c.id=cr.chunk_id
                LEFT JOIN content_spans cs ON cs.id=cr.content_span_id
                WHERE cr.id=:citation_id AND cr.source_type='knowledge_object'
                  AND {_owned_clause()}
                """,
                ),
                {"citation_id": citation_id, "user_id": user_id},
            )
            .mappings()
            .first()
        )
        if row is None:
            raise HTTPException(status_code=404, detail="citation not found")
        start, end = int(row["span_start"]), int(row["span_end"])
        source = str(row["raw_text"] or row["text"] or "")
        quote = source[start:end] if 0 <= start <= end <= len(source) else ""
        pdf_block = None
        if row["page_no"] is not None:
            pdf_block = (
                session.execute(
                    text(
                        """
                    SELECT eb.page_id, eb.bbox_json, eb.block_key
                    FROM evidence_blocks eb
                    JOIN content_versions cv ON cv.id=eb.content_version_id
                    JOIN knowledge_versions kv ON kv.content_version_id=cv.id
                    WHERE kv.id=:version_id AND eb.text_sha256=:quote_hash
                    ORDER BY eb.reading_order LIMIT 1
                    """
                    ),
                    {"version_id": row["source_version_id"], "quote_hash": row["span_quote_hash"]},
                )
                .mappings()
                .first()
            )
        return {
            "citation_id": citation_id,
            "knowledge_object_id": str(row["source_id"]),
            "knowledge_version_id": str(row["source_version_id"]),
            "title": str(row["title"]),
            "quote": quote,
            "span": {"start": start, "end": end, "quote_hash": row["quote_hash"]},
            "location": {
                "page_no": int(row["page_no"]) if row["page_no"] is not None else None,
                "section_path": row["section_path"],
                "bbox": _json(pdf_block["bbox_json"], None) if pdf_block else None,
                "page_id": str(pdf_block["page_id"]) if pdf_block else None,
                "fallback": pdf_block is None and row["page_no"] is None,
            },
        }


@router.get("/v1/knowledge/{knowledge_object_id}/export")
def export_knowledge(
    knowledge_object_id: str,
    request: Request,
    session_factory: SessionFactoryDep,
    user_id: AuthDep,
    format: Literal["markdown", "original"] = Query(default="markdown"),
    disposition: Literal["attachment", "inline"] = "attachment",
    version_id: str | None = None,
) -> Response:
    with session_scope(session_factory) as session:
        row = _version_row(
            session,
            knowledge_object_id=knowledge_object_id,
            version_id=version_id,
            user_id=user_id,
        )
        if disposition == "inline" and (
            format != "original" or row["media_type"] != "application/pdf"
        ):
            raise HTTPException(status_code=400, detail="inline preview requires an original PDF")
        store = knowledge_object_store_for_settings(request.app.state.knowledge_settings)
        if format == "original":
            try:
                body = store.read_bytes(str(row["object_uri"]))
                store.verify_binary_artifact(
                    StoredBinaryArtifact(
                        uri=str(row["object_uri"]),
                        sha256=str(row["evidence_sha256"]),
                        byte_size=int(row["byte_size"]),
                    )
                )
            except (OSError, ValueError, TypeError) as exc:
                raise HTTPException(status_code=404, detail="original file unavailable") from exc
            media_type = str(row["media_type"] or "application/octet-stream")
            filename = _safe_filename(str(row["title"]), media_type)
        else:
            body = _markdown_export(session, row, store).encode("utf-8")
            media_type = "text/markdown; charset=utf-8"
            filename = _safe_filename(str(row["title"]), "text/markdown")
        return Response(
            content=body,
            media_type=media_type,
            headers={
                "Content-Disposition": f'{disposition}; filename="{filename}"',
                "X-Content-SHA256": sha256_text(body.decode("utf-8"))
                if format == "markdown"
                else str(row["evidence_sha256"]),
            },
        )


@router.post("/v1/knowledge/exports", status_code=202)
def export_knowledge_batch(
    export_request: BulkExportRequest,
    session_factory: SessionFactoryDep,
    user_id: AuthDep,
    idempotency_key: WriteDep,
) -> dict[str, Any]:
    """Return a durable, permission-checked download manifest for a batch.

    Individual downloads remain independently authorized and hash-validated by
    the single-object endpoint. This keeps the response small and lets a
    client retry a failed item without rebuilding an archive.
    """

    ids = list(dict.fromkeys(export_request.knowledge_object_ids))
    payload = {**export_request.model_dump(mode="json"), "knowledge_object_ids": ids}
    operation_key = f"knowledge.export-batch:{user_id}:{idempotency_key}"
    with session_scope(session_factory) as session:
        receipt_id, replay = _begin_api_mutation(
            session,
            operation_key=operation_key,
            operation_type="knowledge.export-batch",
            payload=payload,
        )
        if replay is not None:
            return replay.result
        rows = (
            session.execute(
                text(
                    f"""
                SELECT ko.id
                FROM knowledge_objects ko
                WHERE id IN ({",".join(f":id_{index}" for index in range(len(ids)))})
                  AND lifecycle_status NOT IN ('privacy_erased', 'merged')
                  AND {_owned_clause()}
                """
                ),
                {**{f"id_{index}": value for index, value in enumerate(ids)}, "user_id": user_id},
            )
            .scalars()
            .all()
        )
        visible = {str(value) for value in rows}
        items = [
            {
                "knowledge_object_id": item_id,
                "status": "ready" if item_id in visible else "forbidden_or_missing",
                "download_url": (
                    f"/v1/knowledge/{item_id}/export?format={export_request.format}"
                    if item_id in visible
                    else None
                ),
            }
            for item_id in ids
        ]
        result = {
            "status": "ready",
            "format": export_request.format,
            "items": items,
            "succeeded": sum(item["status"] == "ready" for item in items),
            "failed": sum(item["status"] != "ready" for item in items),
        }
        _complete_operation_receipt(session, receipt_id, status_value="ok", result=result)
        return result


def _safe_filename(title: str, media_type: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", title).strip("._") or "knowledge"
    extension = (
        ".md"
        if media_type.startswith("text/markdown")
        else {
            "application/pdf": ".pdf",
            "text/plain": ".txt",
        }.get(media_type, ".bin")
    )
    return stem if stem.lower().endswith(extension) else stem + extension


def _markdown_export(session: Any, row: Any, store: Any) -> str:
    body = _read_text(store, row["text_artifact_uri"])
    tags = [
        str(item)
        for item in session.execute(
            text("SELECT tag FROM knowledge_tags WHERE knowledge_object_id=:id ORDER BY tag"),
            {"id": row["knowledge_object_id"]},
        ).scalars()
    ]
    metadata = _json(row["source_metadata_json"], {})
    lines = [
        f"# {row['title']}",
        "",
        body,
        "",
        "---",
        f"- knowledge_object_id: {row['knowledge_object_id']}",
        f"- knowledge_version_id: {row['knowledge_version_id']}",
        f"- version: {row['version_no']}",
        f"- primary_domain_id: {row['primary_domain_id']}",
        f"- media_type: {row['media_type']}",
    ]
    if tags:
        lines.append(f"- tags: {', '.join(tags)}")
    if metadata.get("source_url") or metadata.get("url"):
        lines.append(f"- source_url: {metadata.get('source_url') or metadata.get('url')}")
    return "\n".join(lines) + "\n"


@router.get("/v1/knowledge/{knowledge_object_id}/similar")
def similar_knowledge(
    knowledge_object_id: str,
    session_factory: SessionFactoryDep,
    user_id: AuthDep,
    limit: int = Query(default=10, ge=1, le=50),
) -> dict[str, Any]:
    with session_scope(session_factory) as session:
        source = _version_row(
            session,
            knowledge_object_id=knowledge_object_id,
            version_id=None,
            user_id=user_id,
        )
        source_chunk = session.execute(
            text(
                "SELECT raw_text FROM chunks WHERE source_id=:id AND source_version_id=:version "
                "ORDER BY chunk_no LIMIT 1"
            ),
            {"id": knowledge_object_id, "version": source["knowledge_version_id"]},
        ).scalar()
        source_text = str(source_chunk or "")
        source_tokens = _tokens(source_text)
        rows = session.execute(
            text(
                f"""
                SELECT ko.id, ko.title, ko.primary_domain_id, ko.lifecycle_status,
                       kv.id AS version_id, kv.version_no, cv.content_sha256,
                       eo.source_metadata_json
                FROM knowledge_objects ko
                JOIN knowledge_versions kv ON kv.id=ko.current_version_id
                JOIN content_versions cv ON cv.id=kv.content_version_id
                JOIN evidence_objects eo ON eo.id=cv.evidence_object_id
                WHERE ko.id<>:id AND ko.lifecycle_status='formal_current'
                  AND {_owned_clause()}
                """
            ),
            {"id": knowledge_object_id, "user_id": user_id},
        ).mappings()
        candidates: list[dict[str, Any]] = []
        for row in rows:
            other = session.execute(
                text(
                    "SELECT raw_text FROM chunks WHERE source_id=:id AND source_version_id=:version "
                    "ORDER BY chunk_no LIMIT 1"
                ),
                {"id": row["id"], "version": row["version_id"]},
            ).scalar()
            other_text = str(other or "")
            tokens = _tokens(other_text)
            score = _jaccard(source_tokens, tokens)
            exact = bool(
                source["content_sha256"] and source["content_sha256"] == row["content_sha256"]
            )
            if exact:
                score = 1.0
            if score <= 0:
                continue
            candidates.append(
                {
                    "knowledge_object_id": str(row["id"]),
                    "knowledge_version_id": str(row["version_id"]),
                    "version_no": int(row["version_no"]),
                    "title": str(row["title"]),
                    "primary_domain_id": str(row["primary_domain_id"]),
                    "similarity": round(score, 6),
                    "match_reason": "exact_content_hash" if exact else "normalized_token_overlap",
                    "source_metadata": _json(row["source_metadata_json"], {}),
                }
            )
        candidates.sort(key=lambda item: (-item["similarity"], item["knowledge_object_id"]))
        return {"items": candidates[:limit]}


def _tokens(value: str) -> set[str]:
    return {token.lower() for token in re.findall(r"[\w\u4e00-\u9fff]+", value) if token}


def _jaccard(left: Iterable[str], right: Iterable[str]) -> float:
    a, b = set(left), set(right)
    return len(a & b) / len(a | b) if a and b else 0.0


@router.get("/v1/knowledge/{knowledge_object_id}/merge-preview")
def merge_preview(
    knowledge_object_id: str,
    session_factory: SessionFactoryDep,
    user_id: AuthDep,
    duplicate_ids: str = Query(..., description="comma-separated duplicate object IDs"),
) -> dict[str, Any]:
    ids = [value.strip() for value in duplicate_ids.split(",") if value.strip()]
    if not ids:
        raise HTTPException(status_code=400, detail="duplicate_ids is required")
    with session_scope(session_factory) as session:
        primary = _version_row(
            session,
            knowledge_object_id=knowledge_object_id,
            version_id=None,
            user_id=user_id,
        )
        duplicates = [
            _version_row(
                session,
                knowledge_object_id=item,
                version_id=None,
                user_id=user_id,
            )
            for item in ids
            if item != knowledge_object_id
        ]
        if len(duplicates) != len(set(ids) - {knowledge_object_id}):
            raise HTTPException(status_code=404, detail="duplicate knowledge not found")
        primary_text = str(
            session.execute(
                text(
                    "SELECT raw_text FROM chunks WHERE source_id=:id AND source_version_id=:version "
                    "ORDER BY chunk_no LIMIT 1"
                ),
                {"id": knowledge_object_id, "version": primary["knowledge_version_id"]},
            ).scalar()
            or ""
        )
        return {
            "primary": _merge_doc(primary, primary_text),
            "duplicates": [
                _merge_doc(
                    item,
                    str(
                        session.execute(
                            text(
                                "SELECT raw_text FROM chunks WHERE source_id=:id AND source_version_id=:version "
                                "ORDER BY chunk_no LIMIT 1"
                            ),
                            {
                                "id": item["knowledge_object_id"],
                                "version": item["knowledge_version_id"],
                            },
                        ).scalar()
                        or ""
                    ),
                )
                for item in duplicates
            ],
            "requires_confirmation": True,
        }


def _merge_doc(row: Any, body: str) -> dict[str, Any]:
    return {
        "knowledge_object_id": str(row["knowledge_object_id"]),
        "knowledge_version_id": str(row["knowledge_version_id"]),
        "title": str(row["title"]),
        "version_no": int(row["version_no"]),
        "content_sha256": str(row["content_sha256"]),
        "text_preview": body[:500],
        "source_metadata": _json(row["source_metadata_json"], {}),
    }


@router.post("/v1/knowledge/merge")
def merge_knowledge(
    request: MergeRequest,
    session_factory: SessionFactoryDep,
    user_id: AuthDep,
    idempotency_key: WriteDep,
) -> dict[str, Any]:
    ids = list(dict.fromkeys(request.duplicate_knowledge_object_ids))
    if request.primary_knowledge_object_id in ids:
        raise HTTPException(status_code=400, detail="primary cannot be a duplicate")
    operation_key = f"knowledge.merge:{user_id}:{idempotency_key}"
    payload = request.model_dump(mode="json")
    with session_scope(session_factory) as session:
        receipt_id, replay = _begin_api_mutation(
            session,
            operation_key=operation_key,
            operation_type="knowledge.merge",
            payload=payload,
        )
        if replay is not None:
            return replay.result
        primary = _version_row(
            session,
            knowledge_object_id=request.primary_knowledge_object_id,
            version_id=None,
            user_id=user_id,
        )
        if str(primary["lifecycle_status"]) != "formal_current":
            raise HTTPException(status_code=409, detail="primary knowledge must be active")
        for duplicate_id in ids:
            duplicate = _version_row(
                session,
                knowledge_object_id=duplicate_id,
                version_id=None,
                user_id=user_id,
            )
            if str(duplicate["lifecycle_status"]) != "formal_current":
                raise HTTPException(status_code=409, detail="duplicate knowledge is not active")
        event_id = new_id()
        session.execute(
            text(
                """
                INSERT INTO knowledge_merge_events
                  (id, primary_knowledge_object_id, requested_by_user_id,
                   source_object_ids_json, payload_json)
                VALUES (:id,:primary,:user_id,:sources,:payload)
                """
            ),
            {
                "id": event_id,
                "primary": request.primary_knowledge_object_id,
                "user_id": user_id,
                "sources": json_text(ids),
                "payload": json_text(
                    {
                        **payload,
                        "preserve_source_ids": request.preserve_source_ids,
                    }
                ),
            },
        )
        for duplicate_id in ids:
            session.execute(
                text(
                    "UPDATE knowledge_objects SET lifecycle_status='merged', updated_at=CURRENT_TIMESTAMP "
                    "WHERE id=:id AND lifecycle_status='formal_current'"
                ),
                {"id": duplicate_id},
            )
            # Tags and user-facing flags are metadata of the logical document.
            # Preserve them on the surviving object while retaining the source.
            session.execute(
                text(
                    """
                    INSERT OR IGNORE INTO knowledge_tags (knowledge_object_id, tag, tag_kind)
                    SELECT :primary, tag, tag_kind
                    FROM knowledge_tags
                    WHERE knowledge_object_id=:source
                    """
                ),
                {"primary": request.primary_knowledge_object_id, "source": duplicate_id},
            )
            session.execute(
                text(
                    "UPDATE chunks SET status='superseded', updated_at=CURRENT_TIMESTAMP "
                    "WHERE source_id=:id AND status='ready'"
                ),
                {"id": duplicate_id},
            )
            session.execute(
                text(
                    """
                    INSERT INTO knowledge_merge_sources
                      (merge_event_id, source_knowledge_object_id, primary_knowledge_object_id, source_version_id)
                    VALUES (:event,:source,:primary,
                      (SELECT current_version_id FROM knowledge_objects WHERE id=:source))
                    """
                ),
                {
                    "event": event_id,
                    "source": duplicate_id,
                    "primary": request.primary_knowledge_object_id,
                },
            )
        result = {
            "merge_event_id": event_id,
            "primary_knowledge_object_id": request.primary_knowledge_object_id,
            "merged_knowledge_object_ids": ids,
            "status": "merged",
        }
        _complete_operation_receipt(session, receipt_id, status_value="ok", result=result)
        return result
