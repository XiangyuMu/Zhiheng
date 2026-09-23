"""Conservative garbage collection for unreferenced evolution drafts."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import text
from sqlalchemy.orm import Session


@dataclass(frozen=True, slots=True)
class GCPlan:
    token: str
    artifact_ids: tuple[str, ...]
    expires_at: int


class OrphanArtifactGC:
    """Two-phase, retention-bound deletion; never touches approved artifacts."""

    def __init__(self, secret: str, *, retention_seconds: int = 7 * 24 * 3600) -> None:
        if not secret or retention_seconds <= 0:
            raise ValueError("GC requires a secret and positive retention")
        self._key = secret.encode()
        self._retention = retention_seconds

    def prepare(self, session: Session, *, now: int | None = None) -> GCPlan:
        current = int(time.time() if now is None else now)
        cutoff = datetime.fromtimestamp(current, UTC) - timedelta(seconds=self._retention)
        rows = (
            session.execute(
                text(
                    """SELECT id FROM evolution_artifacts
               WHERE status='draft' AND created_at <= :cutoff
               ORDER BY id"""
                ),
                {"cutoff": cutoff},
            )
            .scalars()
            .all()
        )
        ids = tuple(str(item) for item in rows if self._is_unreferenced(session, str(item)))
        expires = current + 3600
        payload = {"ids": list(ids), "expires_at": expires}
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        sig = hmac.new(self._key, raw, hashlib.sha256).hexdigest()
        token = base64.urlsafe_b64encode(raw + b"." + sig.encode()).decode()
        return GCPlan(token=token, artifact_ids=ids, expires_at=expires)

    def reap(self, session: Session, plan: GCPlan, *, now: int | None = None) -> int:
        current = int(time.time() if now is None else now)
        try:
            decoded = base64.urlsafe_b64decode(plan.token.encode()).split(b".", 1)
            raw, signature = decoded
            if not hmac.compare_digest(
                signature.decode(), hmac.new(self._key, raw, hashlib.sha256).hexdigest()
            ):
                raise ValueError
            payload = json.loads(raw)
        except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError("invalid orphan GC token") from exc
        if (
            payload.get("expires_at", 0) < current
            or tuple(payload.get("ids", ())) != plan.artifact_ids
        ):
            raise ValueError("expired or mismatched orphan GC token")
        deleted = 0
        for artifact_id in plan.artifact_ids:
            if not self._is_unreferenced(session, artifact_id):
                continue
            result = session.execute(
                text(
                    """DELETE FROM evolution_artifacts
                   WHERE id=:id AND status='draft'"""
                ),
                {"id": artifact_id},
            )
            deleted += int(cast(Any, result).rowcount or 0)
        return deleted

    @staticmethod
    def _is_unreferenced(session: Session, artifact_id: str) -> bool:
        checks = (
            ("SELECT 1 FROM proposal_state_events WHERE event_json LIKE :needle LIMIT 1",),
            ("SELECT 1 FROM maintenance_job_receipts WHERE output_refs_json LIKE :needle LIMIT 1",),
            ("SELECT 1 FROM evolution_artifacts WHERE source_ref=:id AND id<>:id LIMIT 1",),
        )
        for (statement,) in checks:
            params: dict[str, Any] = {"needle": f"%{artifact_id}%", "id": artifact_id}
            if session.execute(text(statement), params).first() is not None:
                return False
        return True
