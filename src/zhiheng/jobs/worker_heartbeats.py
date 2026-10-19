"""Worker heartbeats carrying each worker's declared deployment capabilities.

A worker periodically records the output of ``deployment_capabilities`` with a
heartbeat time.  The API counts only workers whose heartbeat is fresh, so a
deployment whose workers are all offline declares no capabilities.  See ADR 0005.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

HEARTBEAT_INTERVAL_SECONDS = 15.0
HEARTBEAT_STALE_AFTER_SECONDS = 60.0
PDF_PARSER_BACKENDS = ("deepdoc", "mineru")
_STORED_FORMAT = "%Y-%m-%d %H:%M:%S"


def record_worker_heartbeat(
    session: Session,
    *,
    worker_id: str,
    role: str,
    capabilities: dict[str, dict[str, Any]],
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
            "worker_id": worker_id,
            "role": role,
            "capabilities_json": json.dumps(capabilities, ensure_ascii=False, sort_keys=True),
            "heartbeat_at": _stored(now or datetime.now(UTC)),
        },
    )


def current_deployment_capabilities(
    session: Session,
    *,
    now: datetime | None = None,
    stale_after_seconds: float = HEARTBEAT_STALE_AFTER_SECONDS,
) -> dict[str, Any]:
    """Aggregate the declarations of workers whose heartbeat is still fresh."""

    cutoff = (now or datetime.now(UTC)) - timedelta(seconds=stale_after_seconds)
    rows = (
        session.execute(
            text(
                """
                SELECT worker_id, role, capabilities_json, heartbeat_at
                FROM worker_heartbeats
                WHERE heartbeat_at >= :cutoff
                ORDER BY worker_id
                """
            ),
            {"cutoff": _stored(cutoff)},
        )
        .mappings()
        .all()
    )
    workers = [
        {
            "worker_id": str(row["worker_id"]),
            "role": str(row["role"]),
            "heartbeat_at": _public(str(row["heartbeat_at"])),
            # ``detail`` can name internal service addresses; the API only
            # exposes what each worker declares.
            "capabilities": {
                name: {
                    "declared": capability.get("declared"),
                    "configured": bool(capability.get("configured")),
                }
                for name, capability in _capabilities(row["capabilities_json"]).items()
            },
        }
        for row in rows
    ]
    backends = sorted(
        {
            str(pdf["declared"])
            for worker in workers
            if (pdf := worker["capabilities"].get("pdf_parsing")) is not None
            and pdf["configured"]
            and pdf["declared"] in PDF_PARSER_BACKENDS
        }
    )
    if backends:
        reason = None
    elif workers:
        reason = "no live worker declares PDF parsing; PDF uploads are unsupported"
    else:
        reason = (
            f"no live worker has reported in the last {int(stale_after_seconds)} seconds; "
            "PDF uploads are unsupported"
        )
    return {
        "pdf_parsing": {"available": bool(backends), "backends": backends, "reason": reason},
        "workers": workers,
        "stale_after_seconds": stale_after_seconds,
    }


def _capabilities(raw: object) -> dict[str, dict[str, Any]]:
    value = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(value, dict):
        return {}
    return {
        str(name): capability for name, capability in value.items() if isinstance(capability, dict)
    }


def _stored(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).strftime(_STORED_FORMAT)


def _public(stored: str) -> str:
    return datetime.strptime(stored, _STORED_FORMAT).strftime("%Y-%m-%dT%H:%M:%SZ")
