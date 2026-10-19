"""Worker heartbeats carrying each worker's declared deployment capabilities.

A serving worker periodically records the output of ``deployment_capabilities``
with a heartbeat time.  The API counts only workers whose heartbeat is fresh, so
a deployment whose workers are all offline declares no capabilities.  See ADR 0005.
"""

from __future__ import annotations

import json
import os
import platform
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from zhiheng.jobs.deployment_capabilities import PDF_BACKENDS

HEARTBEAT_INTERVAL_SECONDS = 15.0
HEARTBEAT_STALE_AFTER_SECONDS = 60.0
PDF_PARSER_BACKENDS = tuple(backend for backend in PDF_BACKENDS if backend != "none")
# Shared with the ``unsupported`` import terminal state for the same missing capability.
PDF_PARSING_UNAVAILABLE_CODE = "unsupported_pdf_parser"


def default_worker_id(role: str) -> str:
    """Unique per process, so two default-id workers on one host do not overwrite each other.

    Rows left by earlier processes simply stop being refreshed and expire.
    """

    return f"{role}@{platform.node()}:{os.getpid()}"


@dataclass(frozen=True)
class WorkerHeartbeat:
    """What a serving worker reports about itself, and how often."""

    worker_id: str
    role: str
    capabilities: Mapping[str, Mapping[str, Any]]
    interval_seconds: float = HEARTBEAT_INTERVAL_SECONDS

    @classmethod
    def declare(
        cls,
        role: str,
        capabilities: Mapping[str, Mapping[str, Any]],
        *,
        worker_id: str | None = None,
        interval_seconds: float = HEARTBEAT_INTERVAL_SECONDS,
    ) -> WorkerHeartbeat:
        return cls(
            worker_id=worker_id or default_worker_id(role),
            role=role,
            capabilities=capabilities,
            interval_seconds=interval_seconds,
        )


@dataclass(frozen=True)
class PdfParsingCapability:
    available: bool
    backends: tuple[str, ...]
    reason: str | None
    error_code: str | None


@dataclass(frozen=True)
class DeploymentCapabilities:
    """The deployment capabilities declared by live workers, without per-worker detail."""

    pdf_parsing: PdfParsingCapability
    live_workers: int
    stale_after_seconds: float


def record_worker_heartbeat(
    session: Session,
    heartbeat: WorkerHeartbeat,
    *,
    now: datetime | None = None,
) -> None:
    """Replace this worker's declaration; a restarted worker overwrites its old one."""

    session.execute(
        text(
            """
            INSERT INTO worker_heartbeats (worker_id, role, capabilities_json, heartbeat_at)
            VALUES (:worker_id, :role, :capabilities_json, :heartbeat_at)
            ON CONFLICT (worker_id) DO UPDATE SET
              role = excluded.role,
              capabilities_json = excluded.capabilities_json,
              heartbeat_at = excluded.heartbeat_at
            """
        ),
        {
            "worker_id": heartbeat.worker_id,
            "role": heartbeat.role,
            "capabilities_json": json.dumps(
                heartbeat.capabilities, ensure_ascii=False, sort_keys=True
            ),
            "heartbeat_at": _stored(now or datetime.now(UTC)),
        },
    )


def current_deployment_capabilities(
    session: Session,
    *,
    now: datetime | None = None,
    stale_after_seconds: float = HEARTBEAT_STALE_AFTER_SECONDS,
) -> DeploymentCapabilities:
    """Aggregate the declarations of workers whose heartbeat is still fresh."""

    cutoff = (now or datetime.now(UTC)) - timedelta(seconds=stale_after_seconds)
    rows = session.execute(
        text("SELECT capabilities_json FROM worker_heartbeats WHERE heartbeat_at >= :cutoff"),
        {"cutoff": _stored(cutoff)},
    ).scalars()
    live_workers = 0
    backends: set[str] = set()
    for raw in rows:
        live_workers += 1
        backend = _declared_pdf_parser(raw)
        if backend is not None:
            backends.add(backend)
    if backends:
        reason = None
    elif live_workers:
        reason = "no live worker declares PDF parsing; PDF uploads are unsupported"
    else:
        reason = (
            f"no live worker has reported in the last {int(stale_after_seconds)} seconds; "
            "PDF uploads are unsupported"
        )
    return DeploymentCapabilities(
        pdf_parsing=PdfParsingCapability(
            available=bool(backends),
            backends=tuple(sorted(backends)),
            reason=reason,
            error_code=None if backends else PDF_PARSING_UNAVAILABLE_CODE,
        ),
        live_workers=live_workers,
        stale_after_seconds=stale_after_seconds,
    )


def _declared_pdf_parser(raw: object) -> str | None:
    value = json.loads(raw) if isinstance(raw, str) else raw
    pdf = value.get("pdf_parsing") if isinstance(value, dict) else None
    if not isinstance(pdf, dict) or not pdf.get("configured"):
        return None
    declared = pdf.get("declared")
    return declared if declared in PDF_PARSER_BACKENDS else None


def _stored(moment: datetime) -> str:
    # Fixed-width UTC ISO 8601, so stored values compare as instants.
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).isoformat(timespec="seconds")
