from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.orm import Session

from zhiheng.core.ids import json_text, new_id


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
        if indexed:
            _enqueue_index_event(session, manifest, attempt_id, formal_block_count)

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
