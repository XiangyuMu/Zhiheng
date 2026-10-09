from __future__ import annotations

import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from math import isfinite
from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from zhiheng.core.config import Settings
from zhiheng.db.session import session_scope
from zhiheng.jobs.knowledge_indexing import ClaimedKnowledgeJob
from zhiheng.knowledge.object_store import knowledge_object_store_for_settings
from zhiheng.knowledge.pdf_publication import (
    PdfManifestRepository,
    PdfPublicationResult,
    fallback_decision,
    publish_parse_result,
)
from zhiheng.knowledge.pdf_repository import PdfRepository
from zhiheng.knowledge.pdf_worker import (
    ParserParseRequest,
    ParserProtocolError,
    ParserStatus,
    ParserUnavailableError,
    ParserWorkerClient,
)

KNOWLEDGE_PARSE_PDF_JOB_TYPE = "knowledge.parse_pdf"


class ControlledObjectStore(Protocol):
    def read_bytes(self, uri: str) -> bytes: ...


@dataclass(frozen=True, slots=True)
class PdfParseJobResult:
    publication: PdfPublicationResult
    parser_state: str


class PdfParseJobExecutor:
    """Execute one parser task and publish its validated manifest."""

    def __init__(
        self,
        parser_client: ParserWorkerClient,
        *,
        parser_backend: str = "deepdoc",
        fallback_parser_client: ParserWorkerClient | None = None,
        repository: PdfManifestRepository | None = None,
        object_store: ControlledObjectStore,
        poll_interval_seconds: float = 1.0,
        max_polls: int = 120,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if poll_interval_seconds < 0:
            raise ValueError("poll_interval_seconds must be non-negative")
        if max_polls <= 0:
            raise ValueError("max_polls must be positive")
        if parser_backend not in {"deepdoc", "mineru"}:
            raise ValueError("parser_backend must be deepdoc or mineru")
        self._parser_client = parser_client
        self._parser_backend = parser_backend
        self._fallback_parser_client = fallback_parser_client
        self._repository = repository
        self._object_store = object_store
        self._poll_interval_seconds = poll_interval_seconds
        self._max_polls = max_polls
        self._sleep = sleep

    def execute(
        self,
        session_factory: sessionmaker[Session],
        job: ClaimedKnowledgeJob,
    ) -> PdfParseJobResult:
        if job.job_type != KNOWLEDGE_PARSE_PDF_JOB_TYPE:
            raise ValueError(f"unsupported knowledge job type: {job.job_type}")

        request = _request_from_job(job)
        parser_client = self._client_for_request(request)
        fallback_attempted = False
        try:
            parser_status = self._run_attempt(parser_client, request)
        except (ParserUnavailableError, TimeoutError) as exc:
            if request.backend != "deepdoc" or self._fallback_parser_client is None:
                raise
            parser_client = self._fallback_parser_client
            fallback_attempted = True
            parser_status = self._run_fallback(request, _failure_code(exc))
        if parser_status.state == "failed":
            code = parser_status.failure_code or "whole_task_parse_failed"
            decision = fallback_decision(code)
            if (
                request.backend == "deepdoc"
                and not fallback_attempted
                and decision.allowed
                and self._fallback_parser_client is not None
            ):
                parser_client = self._fallback_parser_client
                fallback_attempted = True
                parser_status = self._run_fallback(request, decision.code)
            else:
                raise ParserProtocolError(f"parser task failed: {code}")
        if parser_status.manifest is None:
            raise ParserProtocolError("terminal parser status did not include manifest")

        manifest = parser_client.load_manifest(
            parser_status.manifest,
            read_bytes=self._object_store.read_bytes,
        )
        repository = self._repository or PdfRepository()
        with session_scope(session_factory) as session:
            _assert_current_lease(session, job)
            publication = publish_parse_result(session, repository, manifest)
        return PdfParseJobResult(publication=publication, parser_state=parser_status.state)

    def _client_for_request(self, request: ParserParseRequest) -> ParserWorkerClient:
        if request.backend == self._parser_backend:
            return self._parser_client
        if request.backend == "mineru" and self._fallback_parser_client is not None:
            return self._fallback_parser_client
        raise ParserProtocolError(f"parser backend is not configured: {request.backend}")

    def _run_attempt(
        self,
        parser_client: ParserWorkerClient,
        request: ParserParseRequest,
    ) -> ParserStatus:
        receipt = parser_client.submit(request)
        return self._poll(parser_client, receipt.attempt_id)

    def _run_fallback(
        self,
        request: ParserParseRequest,
        failure_code: str,
    ) -> ParserStatus:
        decision = fallback_decision(failure_code)
        if request.backend != "deepdoc" or not decision.allowed:
            raise ParserProtocolError(f"parser task failed: {failure_code}")
        parser_client = self._fallback_parser_client
        if parser_client is None:
            raise ParserProtocolError(f"parser task failed: {failure_code}")
        fallback_request = ParserParseRequest(
            task_id=request.task_id,
            attempt_id=f"{request.attempt_id}:mineru",
            lease_generation=request.lease_generation,
            backend="mineru",
            source_uri=request.source_uri,
            source_sha256=request.source_sha256,
            output_prefix=f"{request.output_prefix}/mineru",
            options_hash=request.options_hash,
            options=request.options,
            schema_version=request.schema_version,
            evidence_object_id=request.evidence_object_id,
        )
        try:
            status = self._run_attempt(parser_client, fallback_request)
            if status.state == "failed":
                code = status.failure_code or "whole_task_parse_failed"
                raise ParserProtocolError(f"MinerU fallback failed: {code}")
            return status
        except (ParserUnavailableError, TimeoutError) as exc:
            raise ParserProtocolError(f"MinerU fallback failed: {_failure_code(exc)}") from exc

    def _poll(self, parser_client: ParserWorkerClient, attempt_id: str) -> ParserStatus:
        for poll_number in range(self._max_polls):
            status = parser_client.status(attempt_id)
            if status.state in {"succeeded", "partial", "failed"}:
                return status
            if poll_number + 1 < self._max_polls:
                self._sleep(self._poll_interval_seconds)
        raise TimeoutError(f"parser task did not finish after {self._max_polls} polls")


def _failure_code(exc: Exception) -> str:
    if isinstance(exc, TimeoutError):
        return "parser_timeout"
    if isinstance(exc, ParserUnavailableError):
        return "parser_unavailable"
    return "whole_task_parse_failed"


def configured_pdf_parse_executor(settings: Settings) -> PdfParseJobExecutor | None:
    """Build the production parser executor from scoped environment settings."""

    parser_backend = _setting_or_env(
        settings,
        "pdf_parser_backend",
        "ZHIHENG_PDF_PARSER_BACKEND",
    ) or "deepdoc"
    if parser_backend not in {"deepdoc", "mineru"}:
        raise ValueError("PDF parser backend must be deepdoc or mineru")

    deepdoc_url = _setting_or_env(
        settings,
        "pdf_deepdoc_url",
        "ZHIHENG_PDF_DEEPDOC_URL",
    )
    deepdoc_token = _secret_or_env(
        settings,
        "pdf_deepdoc_token",
        "ZHIHENG_PDF_DEEPDOC_TOKEN",
    )
    timeout_seconds = _positive_float(
        _setting_or_env(
            settings,
            "pdf_parser_timeout_seconds",
            "ZHIHENG_PDF_PARSER_TIMEOUT_SECONDS",
        ),
        120.0,
    )
    poll_interval = _positive_float(
        _setting_or_env(
            settings,
            "pdf_parser_poll_interval_seconds",
            "ZHIHENG_PDF_POLL_INTERVAL_SECONDS",
        ),
        2.0,
    )
    max_polls = _positive_int(
        _setting_or_env(settings, "pdf_parser_max_polls", "ZHIHENG_PDF_MAX_POLLS"),
        900,
    )
    mineru_url = _setting_or_env(
        settings,
        "pdf_mineru_url",
        "ZHIHENG_PDF_MINERU_URL",
    )
    mineru_token = _secret_or_env(
        settings,
        "pdf_mineru_token",
        "ZHIHENG_PDF_MINERU_TOKEN",
    )
    mineru = (
        ParserWorkerClient(
            mineru_url,
            service_token=mineru_token,
            timeout_seconds=timeout_seconds,
        )
        if mineru_url and mineru_token
        else None
    )
    deepdoc = (
        ParserWorkerClient(
            deepdoc_url,
            service_token=deepdoc_token,
            timeout_seconds=timeout_seconds,
        )
        if deepdoc_url and deepdoc_token
        else None
    )
    if parser_backend == "mineru":
        if mineru is None:
            return None
        primary_client = mineru
        fallback_client = deepdoc
    else:
        if deepdoc is None:
            return None
        primary_client = deepdoc
        fallback_client = mineru
    return PdfParseJobExecutor(
        primary_client,
        parser_backend=parser_backend,
        fallback_parser_client=fallback_client,
        object_store=knowledge_object_store_for_settings(settings),
        poll_interval_seconds=poll_interval,
        max_polls=max_polls,
    )


def _setting_or_env(settings: Settings, attribute: str, env_name: str) -> str | None:
    value = getattr(settings, attribute, None)
    if value is None:
        value = os.environ.get(env_name)
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _secret_or_env(settings: Settings, attribute: str, env_name: str) -> str | None:
    value = getattr(settings, attribute, None)
    if value is not None:
        getter = getattr(value, "get_secret_value", None)
        value = getter() if callable(getter) else value
    if value is None:
        value = os.environ.get(env_name)
    if value is None:
        file_name = os.environ.get(f"{env_name}_FILE")
        if file_name:
            try:
                with open(file_name, encoding="utf-8") as handle:
                    value = handle.read()
            except OSError as exc:
                raise RuntimeError(
                    f"unable to read parser service token file: {file_name}"
                ) from exc
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _positive_float(value: str | None, default: float) -> float:
    if value is None:
        return default
    parsed = float(value)
    if not isfinite(parsed) or parsed <= 0:
        raise ValueError("PDF parser interval/timeout must be positive")
    return parsed


def _positive_int(value: str | None, default: int) -> int:
    if value is None:
        return default
    parsed = int(value)
    if parsed <= 0:
        raise ValueError("PDF parser max polls must be positive")
    return parsed


def _request_from_job(job: ClaimedKnowledgeJob) -> ParserParseRequest:
    payload: Mapping[str, Any] = job.payload
    task_id = _required_string(payload, "task_id")
    attempt_id = _string_or(payload, "parser_attempt_id", "attempt_id")
    if attempt_id is None:
        attempt_id = job.attempt_id or job.id
    backend = _string_or(payload, "backend") or "deepdoc"
    source_uri = _required_string(payload, "source_uri")
    source_sha256 = _required_string(payload, "source_sha256")
    options_hash = _required_string(payload, "options_hash")
    output_prefix = _required_string(payload, "output_prefix")
    options = payload.get("options", {})
    if not isinstance(options, Mapping):
        raise ValueError("job payload options must be an object")
    lease_generation = payload.get("lease_generation", job.attempts)
    if not isinstance(lease_generation, int) or lease_generation < 0:
        raise ValueError("job payload lease_generation must be a non-negative integer")
    schema_version = _string_or(payload, "schema_version") or "pdf-parser.manifest.v1"
    evidence_object_id = _string_or(payload, "evidence_object_id")
    return ParserParseRequest(
        task_id=task_id,
        attempt_id=attempt_id,
        lease_generation=lease_generation,
        backend=backend,
        source_uri=source_uri,
        source_sha256=source_sha256,
        output_prefix=output_prefix,
        options_hash=options_hash,
        options=options,
        schema_version=schema_version,
        evidence_object_id=evidence_object_id,
    )


def _assert_current_lease(session: Session, job: ClaimedKnowledgeJob) -> None:
    """Fence stale parser workers before they publish or enqueue indexing."""
    row = (
        session.execute(
            text("SELECT status, attempts, lease_owner FROM jobs WHERE id = :id"),
            {"id": job.id},
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise ParserProtocolError("lease_lost")
    if str(row["status"]) != "processing":
        raise ParserProtocolError("lease_lost")
    if int(row["attempts"] or 0) != int(job.attempts):
        raise ParserProtocolError("lease_lost")
    if job.lease_owner is not None and row["lease_owner"] != job.lease_owner:
        raise ParserProtocolError("lease_lost")


def _required_string(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"job payload {key} must be a non-empty string")
    return value


def _string_or(payload: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = payload.get(key)
        if value is not None:
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"job payload {key} must be a non-empty string")
            return value
    return None


__all__ = [
    "ControlledObjectStore",
    "KNOWLEDGE_PARSE_PDF_JOB_TYPE",
    "PdfParseJobExecutor",
    "PdfParseJobResult",
    "configured_pdf_parse_executor",
]
