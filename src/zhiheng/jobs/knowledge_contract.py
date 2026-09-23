from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from zhiheng.core.ids import sha256_json, sha256_text


class ImportPublicStatus(StrEnum):
    QUEUED = "queued"
    PROCESSING = "processing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DEAD_LETTER = "dead_letter"
    UNSUPPORTED = "unsupported"
    PARSE_FAILED = "parse_failed"
    AWAITING_CONFIRMATION = "awaiting_confirmation"


class JobStatus(StrEnum):
    QUEUED = "pending"
    PROCESSING = "processing"
    SUCCEEDED = "completed"
    FAILED = "failed"
    DEAD_LETTER = "dead"


class KnowledgeLifecycleStatus(StrEnum):
    FORMAL_CURRENT = "formal_current"
    AWAITING_CONFIRMATION = "awaiting_user_confirmation"
    CANDIDATE = "candidate"
    SOFT_DELETED = "soft_deleted"
    PRIVACY_ERASED = "privacy_erased"


@dataclass(frozen=True, slots=True)
class KnowledgeFailure:
    code: str
    stage: str
    retryable: bool
    redacted_summary: str


@dataclass(frozen=True, slots=True)
class KnowledgeStatusProjection:
    public_status: ImportPublicStatus
    job_status: JobStatus | None
    knowledge_lifecycle_status: str | None
    searchable: bool
    failure: KnowledgeFailure | None


def job_etag(row: dict[str, Any]) -> str:
    """Return a stable compare-and-swap token for a job status projection."""
    fields = {key: str(row.get(key)) for key in ("id", "status", "attempts", "updated_at")}
    return f"knowledge-job:{sha256_json(fields)}"


def retry_idempotency_key(operation_key: str) -> str:
    return f"knowledge-retry:{sha256_text(operation_key)}"


def project_import_status(
    *,
    job_status: str | None,
    lifecycle_status: str | None,
    searchable: bool,
    failure: KnowledgeFailure | None = None,
) -> KnowledgeStatusProjection:
    if lifecycle_status in {
        KnowledgeLifecycleStatus.AWAITING_CONFIRMATION.value,
        KnowledgeLifecycleStatus.CANDIDATE.value,
    }:
        return KnowledgeStatusProjection(
            public_status=ImportPublicStatus.AWAITING_CONFIRMATION,
            job_status=_job_status(job_status),
            knowledge_lifecycle_status=lifecycle_status,
            searchable=False,
            failure=failure,
        )
    if job_status == JobStatus.QUEUED.value:
        public_status = ImportPublicStatus.QUEUED
    elif job_status == JobStatus.PROCESSING.value:
        public_status = ImportPublicStatus.PROCESSING
    elif job_status == JobStatus.SUCCEEDED.value:
        public_status = ImportPublicStatus.SUCCEEDED if searchable else ImportPublicStatus.FAILED
    elif job_status == JobStatus.DEAD_LETTER.value:
        public_status = ImportPublicStatus.DEAD_LETTER
    elif job_status == JobStatus.FAILED.value:
        public_status = ImportPublicStatus.FAILED
    elif failure is not None and failure.code == "unsupported_media_type":
        public_status = ImportPublicStatus.UNSUPPORTED
    elif failure is not None and failure.code == "parse_failed":
        public_status = ImportPublicStatus.PARSE_FAILED
    else:
        public_status = ImportPublicStatus.FAILED
    return KnowledgeStatusProjection(
        public_status=public_status,
        job_status=_job_status(job_status),
        knowledge_lifecycle_status=lifecycle_status,
        searchable=searchable,
        failure=failure,
    )


def failure_from_row(
    *,
    error_class: str | None,
    error_message: str | None,
    payload: dict[str, Any] | None = None,
    job_status: str | None = None,
) -> KnowledgeFailure | None:
    if not error_class and not error_message and not payload:
        return None
    payload = payload or {}
    code = str(payload.get("failure_code") or "").strip()
    stage = str(payload.get("failure_stage") or "").strip()
    if not code:
        if error_class in {"UnsupportedMediaTypeError", "UnsupportedMediaType"}:
            code = "unsupported_media_type"
        elif error_class in {"ParseError", "ParserError"}:
            code = "parse_failed"
        else:
            code = "indexing_failed"
    if not stage:
        stage = "parse" if code == "parse_failed" else "index"
    retryable = bool(payload.get("retryable", job_status not in {"dead"}))
    message = _redact_summary(error_message or code)
    return KnowledgeFailure(
        code=code,
        stage=stage,
        retryable=retryable,
        redacted_summary=message[:512],
    )


def _job_status(value: str | None) -> JobStatus | None:
    if value is None:
        return None
    try:
        return JobStatus(value)
    except ValueError:
        return None


def _redact_summary(value: str) -> str:
    normalized = value.replace("\r", " ").replace("\n", " ").strip()
    normalized = re.sub(
        r"(?i)(bearer\s+|api[_-]?key\s*[=:]\s*)\S+",
        r"\1[redacted]",
        normalized,
    )
    normalized = re.sub(r"sk-[A-Za-z0-9_-]{8,}", "[redacted]", normalized)
    return normalized[:512]
