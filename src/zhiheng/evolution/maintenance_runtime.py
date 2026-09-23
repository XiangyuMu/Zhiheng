"""Reversible, lease-bound maintenance planning and execution.

Maintenance is deliberately separate from evolution publication. A run first
creates a complete plan, and only an explicit non-dry execution applies the
planned, reversible actions. Formal memories are never targets.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from zhiheng.core.ids import json_text, new_id


@dataclass(frozen=True, slots=True)
class MaintenanceAction:
    action_id: str
    target_type: str
    target_id: str
    action: str
    reason: str
    state: str = "pending"


@dataclass(frozen=True, slots=True)
class MaintenanceRun:
    run_id: str
    status: str
    dry_run: bool
    stats: dict[str, int]
    actions: tuple[MaintenanceAction, ...]


def _decode(value: Any) -> dict[str, Any]:
    parsed = json.loads(value) if isinstance(value, str) else value
    return dict(parsed) if isinstance(parsed, dict) else {}


class MaintenanceRuntime:
    """Plan and apply low-value cleanup in bounded, recoverable batches."""

    def __init__(
        self,
        *,
        candidate_confidence: float = 0.2,
        candidate_age_days: int = 30,
        lease_seconds: int = 300,
    ) -> None:
        if not 0 <= candidate_confidence <= 1:
            raise ValueError("candidate_confidence must be between 0 and 1")
        if candidate_age_days <= 0 or lease_seconds <= 0:
            raise ValueError("candidate_age_days and lease_seconds must be positive")
        self.candidate_confidence = candidate_confidence
        self.candidate_age_days = candidate_age_days
        self.lease_seconds = lease_seconds

    def plan(
        self,
        session: Session,
        *,
        idempotency_key: str,
        dry_run: bool = True,
        batch_size: int = 100,
    ) -> MaintenanceRun:
        if not idempotency_key.strip():
            raise ValueError("idempotency_key is required")
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        existing = session.execute(
            text("SELECT id FROM maintenance_runs WHERE idempotency_key = :key"),
            {"key": idempotency_key},
        ).scalar()
        if existing is not None:
            return self.load(session, str(existing))

        run_id = new_id()
        session.execute(
            text(
                """
                INSERT INTO maintenance_runs
                  (id, idempotency_key, status, dry_run, batch_size)
                VALUES (:id, :key, 'planned', :dry_run, :batch_size)
                """
            ),
            {"id": run_id, "key": idempotency_key, "dry_run": dry_run, "batch_size": batch_size},
        )
        actions = self._discover(session, run_id=run_id, batch_size=batch_size)
        stats = {
            "planned": len(actions),
            "candidate_evictions": sum(a.action == "evict_candidate" for a in actions),
            "orphan_indexes": sum(a.action == "retire_index" for a in actions),
            "duplicate_indexes": sum(a.action == "merge_index" for a in actions),
        }
        session.execute(
            text(
                """
                UPDATE maintenance_runs
                SET stats_json = :stats, updated_at = CURRENT_TIMESTAMP
                WHERE id = :id
                """
            ),
            {"id": run_id, "stats": json_text(stats)},
        )
        return MaintenanceRun(run_id, "planned", dry_run, stats, tuple(actions))

    def claim(self, session: Session, *, run_id: str, worker_id: str) -> bool:
        if not worker_id.strip():
            raise ValueError("worker_id is required")
        expires = datetime.now(UTC) + timedelta(seconds=self.lease_seconds)
        session.execute(
            text(
                """
                UPDATE maintenance_runs
                SET status = 'leased', lease_owner = :owner,
                    lease_expires_at = :expires, updated_at = CURRENT_TIMESTAMP
                WHERE id = :id
                  AND (
                    status = 'planned'
                    OR (status IN ('leased', 'running') AND lease_expires_at < CURRENT_TIMESTAMP)
                  )
                """
            ),
            {"id": run_id, "owner": worker_id, "expires": expires},
        )
        return int(session.execute(text("SELECT changes()")).scalar_one()) == 1

    def apply(self, session: Session, *, run_id: str, worker_id: str) -> MaintenanceRun:
        row = (
            session.execute(
                text(
                    "SELECT status, dry_run, lease_owner, lease_expires_at, stats_json "
                    "FROM maintenance_runs WHERE id = :id"
                ),
                {"id": run_id},
            )
            .mappings()
            .one()
        )
        if row["dry_run"]:
            return self.load(session, run_id)
        if row["lease_owner"] != worker_id:
            raise PermissionError("maintenance run is leased by another worker")
        if (
            row["lease_expires_at"] is not None
            and str(row["lease_expires_at"]) < datetime.now(UTC).isoformat()
        ):
            raise RuntimeError("maintenance lease expired")
        session.execute(
            text(
                "UPDATE maintenance_runs SET status='running', updated_at=CURRENT_TIMESTAMP WHERE id=:id"
            ),
            {"id": run_id},
        )
        actions = (
            session.execute(
                text(
                    """
                SELECT id, target_type, target_id, action, reason, state
                FROM maintenance_actions WHERE run_id = :id AND state = 'pending'
                ORDER BY id
                """
                ),
                {"id": run_id},
            )
            .mappings()
            .all()
        )
        for action in actions:
            self._apply_action(session, action)
        session.execute(
            text(
                """
                UPDATE maintenance_runs
                SET status='completed', lease_owner=NULL, lease_expires_at=NULL,
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=:id
                """
            ),
            {"id": run_id},
        )
        return self.load(session, run_id)

    def load(self, session: Session, run_id: str) -> MaintenanceRun:
        row = (
            session.execute(
                text("SELECT status, dry_run, stats_json FROM maintenance_runs WHERE id=:id"),
                {"id": run_id},
            )
            .mappings()
            .one()
        )
        actions = tuple(
            MaintenanceAction(
                str(item["id"]),
                str(item["target_type"]),
                str(item["target_id"]),
                str(item["action"]),
                str(item["reason"]),
                str(item["state"]),
            )
            for item in session.execute(
                text(
                    """
                    SELECT id, target_type, target_id, action, reason, state
                    FROM maintenance_actions WHERE run_id=:id ORDER BY id
                    """
                ),
                {"id": run_id},
            ).mappings()
        )
        return MaintenanceRun(
            str(run_id),
            str(row["status"]),
            bool(row["dry_run"]),
            {key: int(value) for key, value in _decode(row["stats_json"]).items()},
            actions,
        )

    def overview(self, session: Session) -> dict[str, Any]:
        runs = (
            session.execute(
                text(
                    """
                SELECT id, status, dry_run, stats_json, error_message, created_at, updated_at
                FROM maintenance_runs ORDER BY created_at DESC LIMIT 10
                """
                )
            )
            .mappings()
            .all()
        )
        pending = session.execute(
            text("SELECT count(*) FROM maintenance_actions WHERE state='pending'")
        ).scalar_one()
        return {
            "pending_actions": int(pending),
            "runs": [
                {
                    "id": str(row["id"]),
                    "status": str(row["status"]),
                    "dry_run": bool(row["dry_run"]),
                    "stats": _decode(row["stats_json"]),
                    "error": row["error_message"],
                    "created_at": str(row["created_at"]),
                    "updated_at": str(row["updated_at"]),
                }
                for row in runs
            ],
        }

    def _discover(
        self, session: Session, *, run_id: str, batch_size: int
    ) -> list[MaintenanceAction]:
        actions: list[MaintenanceAction] = []
        # Candidate memories are only ever marked rejected; formal_memories is
        # intentionally absent from this query.
        rows = (
            session.execute(
                text(
                    """
                SELECT id, confidence FROM memory_candidates
                WHERE status IN ('pending_confirmation', 'edited')
                  AND confidence <= :confidence
                  AND updated_at <= datetime('now', :age)
                ORDER BY updated_at, id LIMIT :limit
                """
                ),
                {
                    "confidence": self.candidate_confidence,
                    "age": f"-{self.candidate_age_days} days",
                    "limit": batch_size,
                },
            )
            .mappings()
            .all()
        )
        for row in rows:
            actions.append(
                self._insert_action(
                    session,
                    run_id,
                    "memory_candidate",
                    str(row["id"]),
                    "evict_candidate",
                    "low confidence and long-term unreviewed",
                )
            )

        # Derived embedding generations can be retired when explicitly marked
        # orphan. No active or formal knowledge generation is selected.
        rows = (
            session.execute(
                text(
                    """
                SELECT id FROM embedding_generations
                WHERE index_status IN ('orphan', 'orphaned', 'retired')
                ORDER BY id LIMIT :limit
                """
                ),
                {"limit": batch_size - len(actions)},
            )
            .mappings()
            .all()
        )
        for row in rows:
            actions.append(
                self._insert_action(
                    session,
                    run_id,
                    "derived_index",
                    str(row["id"]),
                    "retire_index",
                    "index generation is orphaned or already retired",
                )
            )
        return actions

    @staticmethod
    def _insert_action(
        session: Session,
        run_id: str,
        target_type: str,
        target_id: str,
        action: str,
        reason: str,
    ) -> MaintenanceAction:
        action_id = new_id()
        session.execute(
            text(
                """
                INSERT INTO maintenance_actions
                  (id, run_id, target_type, target_id, action, reason)
                VALUES (:id, :run_id, :target_type, :target_id, :action, :reason)
                """
            ),
            locals(),
        )
        return MaintenanceAction(action_id, target_type, target_id, action, reason)

    @staticmethod
    def _apply_action(session: Session, action: Any) -> None:
        target_type = str(action["target_type"])
        target_id = str(action["target_id"])
        if target_type == "memory_candidate":
            session.execute(
                text(
                    """
                    UPDATE memory_candidates SET status='rejected', updated_at=CURRENT_TIMESTAMP
                    WHERE id=:id AND status IN ('pending_confirmation', 'edited')
                    """
                ),
                {"id": target_id},
            )
            state = "applied" if int(session.execute(text("SELECT changes()")).scalar_one()) else "skipped"
        elif target_type == "derived_index":
            session.execute(
                text(
                    """
                    UPDATE embedding_generations SET index_status='retired'
                    WHERE id=:id AND index_status IN ('orphan', 'orphaned', 'retired')
                    """
                ),
                {"id": target_id},
            )
            state = "applied" if int(session.execute(text("SELECT changes()")).scalar_one()) else "skipped"
        else:
            state = "skipped"
        session.execute(
            text(
                """
                UPDATE maintenance_actions SET state=:state, applied_at=CURRENT_TIMESTAMP
                WHERE id=:id
                """
            ),
            {"id": action["id"], "state": state},
        )
