from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.orm import Session

from zhiheng.core.ids import json_text, new_id, sha256_text


class PdfFailureCode(StrEnum):
    """Stable parser failure codes used by publication and fallback decisions."""

    SOURCE_MISSING = "source_missing"
    SOURCE_HASH_MISMATCH = "source_hash_mismatch"
    MANIFEST_SCHEMA_INVALID = "manifest_schema_invalid"
    MANIFEST_HASH_MISMATCH = "manifest_hash_mismatch"
    PARSER_TIMEOUT = "parser_timeout"
    PARSER_UNAVAILABLE = "parser_unavailable"
    RESOURCE_LIMIT = "resource_limit"
    UNSUPPORTED_PDF = "unsupported_pdf"
    WHOLE_TASK_PARSE_FAILED = "whole_task_parse_failed"
    PARTIAL_PAGE_FAILED = "partial_page_failed"
    MODEL_PRIVACY_DENIED = "model_privacy_denied"
    MODEL_TIMEOUT = "model_timeout"
    MINERU_FAILED = "mineru_failed"
    LEASE_LOST = "lease_lost"


FALLBACK_FAILURE_CODES = frozenset(
    {
        PdfFailureCode.PARSER_TIMEOUT,
        PdfFailureCode.PARSER_UNAVAILABLE,
        PdfFailureCode.WHOLE_TASK_PARSE_FAILED,
    }
)


class PdfManifestRepository(Protocol):
    def persist_manifest(
        self,
        session: Session,
        manifest: dict[str, Any],
        *,
        manifest_uri: str,
        manifest_sha256: str,
    ) -> str: ...


@dataclass(frozen=True, slots=True)
class FallbackDecision:
    allowed: bool
    code: str


@dataclass(frozen=True, slots=True)
class PdfPublicationResult:
    attempt_id: str
    formal_block_count: int
    indexed: bool


def fallback_decision(
    failure_code: str | None,
    *,
    committed_page_count: int = 0,
    prior_mineru_attempt: bool = False,
) -> FallbackDecision:
    """Return the deterministic one-shot MinerU fallback decision.

    A fallback is valid only for a whole-task failure before any page was
    committed, and only when MinerU has not already been attempted.
    """

    if prior_mineru_attempt:
        return FallbackDecision(False, "mineru_attempt_already_exists")
    if committed_page_count > 0:
        return FallbackDecision(False, PdfFailureCode.PARTIAL_PAGE_FAILED.value)
    normalized = str(failure_code or "").strip()
    if normalized in {code.value for code in FALLBACK_FAILURE_CODES}:
        return FallbackDecision(True, normalized)
    return FallbackDecision(False, normalized or "unknown_failure")


def should_fallback_to_mineru(
    failure_code: str | None,
    *,
    committed_page_count: int = 0,
    prior_mineru_attempt: bool = False,
) -> bool:
    """Boolean compatibility helper for callers that only need the gate."""

    return fallback_decision(
        failure_code,
        committed_page_count=committed_page_count,
        prior_mineru_attempt=prior_mineru_attempt,
    ).allowed


def _materialize_pdf_knowledge(
    session: Session,
    manifest: dict[str, Any],
    *,
    attempt_id: str,
) -> tuple[str, str] | None:
    """Create formal knowledge rows from a complete PDF parse.

    The PDF evidence object remains the source of truth; extracted text is a
    content version and every chunk/span retains page and quote lineage.
    """
    if any(page.get("status") == "failed" for page in manifest.get("pages", ())):
        return None
    raw_blocks = list(manifest.get("blocks", ()))
    if not raw_blocks or any("reading_order" not in block for block in raw_blocks):
        return None
    blocks = [
        block
        for block in sorted(raw_blocks, key=lambda item: item["reading_order"])
        if block.get("status") == "formal" and str(block.get("text") or "")
    ]
    if not blocks:
        return None
    task_id = str(manifest.get("task_id") or "")
    source = manifest.get("source") or {}
    evidence_id = str(source.get("evidence_object_id") or "")
    task = (
        session.execute(
            text(
                """
            SELECT t.state, eo.source_metadata_json, eo.sha256
            FROM pdf_tasks t JOIN evidence_objects eo ON eo.id=t.evidence_object_id
            WHERE t.id=:task_id AND t.evidence_object_id=:evidence_id
            """
            ),
            {"task_id": task_id, "evidence_id": evidence_id},
        )
        .mappings()
        .first()
    )
    if task is None:
        raise ValueError("PDF task source not found for materialization")
    if str(task["state"]) != "parsed":
        return None
    existing = (
        session.execute(
            text(
                """
            SELECT ko.id AS knowledge_object_id, kv.id AS knowledge_version_id
            FROM knowledge_objects ko
            JOIN knowledge_versions kv ON kv.id=ko.current_version_id
            WHERE kv.content_version_id IN (
              SELECT id FROM content_versions WHERE evidence_object_id=:evidence_id
            )
            ORDER BY kv.version_no DESC LIMIT 1
            """
            ),
            {"evidence_id": evidence_id},
        )
        .mappings()
        .first()
    )
    if existing is not None:
        return str(existing["knowledge_object_id"]), str(existing["knowledge_version_id"])

    metadata = json.loads(str(task["source_metadata_json"] or "{}"))
    title = str(metadata.get("title") or "PDF document")
    domain = str(metadata.get("primary_domain_id") or "general")
    owner_user_id = metadata.get("owner_user_id")
    body_parts: list[str] = []
    offsets: list[tuple[int, int]] = []
    cursor = 0
    for block in blocks:
        value = str(block["text"])
        start = cursor
        body_parts.append(value)
        cursor += len(value)
        offsets.append((start, cursor))
        body_parts.append("\n\n")
        cursor += 2
    body = "".join(body_parts).rstrip("\n")
    body_hash = sha256_text(body)
    content_id = new_id()
    object_id = new_id()
    version_id = new_id()
    session.execute(
        text(
            """
            INSERT INTO content_versions
              (id,evidence_object_id,version_no,processor_name,processor_version,
               text_artifact_uri,content_sha256,status)
            VALUES (:id,:evidence_id,:version_no,'pdf-parser',:processor_version,
                    :artifact_uri,:content_sha256,'active')
            """
        ),
        {
            "id": content_id,
            "evidence_id": evidence_id,
            "version_no": int(
                session.execute(
                    text(
                        "SELECT COALESCE(max(version_no), 0) + 1 "
                        "FROM content_versions WHERE evidence_object_id=:id"
                    ),
                    {"id": evidence_id},
                ).scalar_one()
            ),
            "processor_version": str(
                (manifest.get("parser") or {}).get("schema_version") or "manifest-v1"
            ),
            "artifact_uri": str(manifest.get("manifest_uri") or ""),
            "content_sha256": body_hash,
        },
    )
    for block, (start, end) in zip(blocks, offsets, strict=True):
        span_id = new_id()
        session.execute(
            text(
                """
                INSERT INTO content_spans
                  (id,content_version_id,span_kind,start_offset,end_offset,page_no,section_path,quote_hash)
                VALUES (:id,:content_id,'pdf_block',:start,:end,:page_no,:section_path,:quote_hash)
                """
            ),
            {
                "id": span_id,
                "content_id": content_id,
                "start": start,
                "end": end,
                "page_no": int(block["page_no"]),
                "section_path": str(block.get("region_type") or "正文"),
                "quote_hash": str(block["quote_hash"]),
            },
        )
        session.execute(
            text(
                "UPDATE evidence_blocks SET content_version_id=:content_id "
                "WHERE attempt_id=:attempt_id AND block_key=:key"
            ),
            {"content_id": content_id, "attempt_id": attempt_id, "key": block["key"]},
        )
    session.execute(
        text(
            """
            INSERT INTO knowledge_objects
              (id,primary_domain_id,title,object_kind,record_type,lifecycle_status,
               visibility_scope,current_version_id,confirmation_generation,owner_user_id,sensitivity_level)
            VALUES (:id,:domain,:title,'document','knowledge','formal_current',
                    'formal',NULL,1,:owner,'private')
            """
        ),
        {"id": object_id, "domain": domain, "title": title, "owner": owner_user_id},
    )
    session.execute(
        text(
            """
            INSERT INTO knowledge_versions
              (id,knowledge_object_id,version_no,content_version_id,markdown_uri,summary,source_quality)
            VALUES (:id,:object_id,1,:content_id,NULL,:summary,'parser')
            """
        ),
        {"id": version_id, "object_id": object_id, "content_id": content_id, "summary": title},
    )
    session.execute(
        text("UPDATE knowledge_objects SET current_version_id=:version_id WHERE id=:object_id"),
        {"version_id": version_id, "object_id": object_id},
    )
    for number, (block, (start, end)) in enumerate(zip(blocks, offsets, strict=True)):
        span_id = session.execute(
            text(
                "SELECT id FROM content_spans "
                "WHERE content_version_id=:content_id AND start_offset=:start"
            ),
            {"content_id": content_id, "start": start},
        ).scalar_one()
        session.execute(
            text(
                """
                INSERT INTO chunks
                  (id,source_type,source_id,source_version_id,chunk_no,title,text,raw_text,segmented_text,
                   span_start,span_end,content_version_id,content_span_id,visibility_scope,confirmation_generation,status)
                VALUES (:id,'knowledge_object',:object_id,:version_id,:number,:title,
                        :text,:text,:text,
                        :start,:end,:content_id,:span_id,'formal',1,'ready')
                """
            ),
            {
                "id": new_id(),
                "object_id": object_id,
                "version_id": version_id,
                "number": number,
                "title": title,
                "text": str(block["text"]),
                "start": start,
                "end": end,
                "content_id": content_id,
                "span_id": span_id,
            },
        )
    return object_id, version_id


def publish_parse_result(
    session: Session,
    repository: PdfManifestRepository,
    manifest: dict[str, Any],
) -> PdfPublicationResult:
    """Persist a validated parse result and atomically enqueue indexing.

    The savepoint makes manifest rows and the index event one unit when this
    function is called inside a larger session transaction. A valid manifest
    with failed pages is retained; indexing is emitted only when at least one
    formal block exists.
    """

    with session.begin_nested():
        attempt_id = repository.persist_manifest(
            session,
            manifest,
            manifest_uri=str(manifest.get("manifest_uri") or ""),
            manifest_sha256=str(manifest.get("manifest_sha256") or ""),
        )
        formal_block_count = sum(
            1 for block in manifest.get("blocks", ()) if block.get("status") == "formal"
        )
        partial = any(page.get("status") == "failed" for page in manifest.get("pages", ()))
        indexed = formal_block_count > 0 and not partial
        materialized = (
            _materialize_pdf_knowledge(session, manifest, attempt_id=attempt_id)
            if indexed
            else None
        )
        if indexed:
            _enqueue_index_event(
                session,
                manifest,
                attempt_id,
                formal_block_count,
                materialized=materialized,
            )

    return PdfPublicationResult(
        attempt_id=str(attempt_id),
        formal_block_count=formal_block_count,
        indexed=indexed,
    )


def _enqueue_index_event(
    session: Session,
    manifest: dict[str, Any],
    attempt_id: str,
    formal_block_count: int,
    materialized: tuple[str, str] | None = None,
) -> None:
    source = manifest.get("source") or {}
    parser = manifest.get("parser") or {}
    task_id = str(manifest.get("task_id") or "")
    payload = {
        "task_id": task_id,
        "attempt_id": str(attempt_id),
        "evidence_object_id": str(source.get("evidence_object_id") or ""),
        "backend": str(parser.get("backend") or ""),
        "formal_block_count": formal_block_count,
        "schema_version": str(manifest.get("schema_version") or ""),
    }
    if materialized is not None:
        payload.update(
            {
                "knowledge_object_id": materialized[0],
                "knowledge_version_id": materialized[1],
            }
        )
    session.execute(
        text(
            """
            INSERT INTO outbox_events (
              id, event_type, aggregate_type, aggregate_id, payload_json, status
            )
            VALUES (
              :id, 'knowledge.index', 'pdf_parse_attempt', :aggregate_id, :payload, 'pending'
            )
            """
        ),
        {
            "id": new_id(),
            "aggregate_id": str(attempt_id),
            "payload": json_text(payload),
        },
    )


__all__ = [
    "FALLBACK_FAILURE_CODES",
    "FallbackDecision",
    "PdfFailureCode",
    "PdfPublicationResult",
    "fallback_decision",
    "publish_parse_result",
    "should_fallback_to_mineru",
]
