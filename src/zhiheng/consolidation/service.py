"""后台整理: the idle-time consolidation run (spec decision 23).

A run looks over the formal knowledge base and produces two kinds of result:

- suggestions that would change formal knowledge (merge, revision, conflict)
  become relation suggestions in 待处理, decided one by one like any other;
- observations that propose no change become 整理发现, which never count as
  pending and which the user may dismiss.

Nothing a run does changes formal knowledge or what answers may use.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from zhiheng.conclusions.relations import relation_kind
from zhiheng.conclusions.repository import ConclusionRepository
from zhiheng.consolidation.findings import (
    FindingStore,
    iso_utc,
    observe_conclusions,
    observe_memory,
    parse_utc,
)
from zhiheng.core.ids import new_id
from zhiheng.db.session import session_scope

# Relations a run may propose between two formal conclusions. A supplement or
# conditional coexistence would change nothing that is already formal.
CONSOLIDATION_KINDS = frozenset({"duplicate", "revision", "conflict"})
# 无对话和审核活动满 30 分钟视为空闲; a run happens at most once every 24 hours.
IDLE_AFTER = timedelta(minutes=30)
RUN_EVERY = timedelta(hours=24)
# Requests that are conversation or review activity: asking, and deciding or
# editing anything that 待处理 lists. Reading a page or a count is neither.
ACTIVITY_PATHS = (
    "/v1/answers",
    "/v1/conversations",
    "/v1/review",
    "/v1/conclusions",
    "/v1/memory",
    "/v1/personal-updates",
    "/v1/classifications",
    "/v1/classification-suggestions",
    "/v1/taxonomy",
)
# Classifying, deferring or restoring an unclassified item, and confirming an
# event, from routes that otherwise also import or edit material.
_ACTIVITY_PATTERN = re.compile(
    r"^/v1/knowledge/[^/]+/(classifications|unclassified/(defer|restore))$"
    r"|^/v1/events/[^/]+/confirmation$"
)
_READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


@dataclass(frozen=True)
class ConsolidationRun:
    id: str
    started_at: datetime
    suggestions_created: int
    findings_created: int


def is_activity(method: str, path: str) -> bool:
    """Whether a request is conversation or review activity that postpones a run."""
    if method.upper() in _READ_METHODS:
        return False
    return bool(_ACTIVITY_PATTERN.match(path)) or any(
        path == prefix or path.startswith(prefix + "/") for prefix in ACTIVITY_PATHS
    )


def record_activity(session_factory: sessionmaker[Session], *, now: datetime) -> None:
    with session_scope(session_factory) as session:
        session.execute(
            text(
                "INSERT INTO consolidation_activity (scope, last_active_at) VALUES ('user', :at) "
                "ON CONFLICT(scope) DO UPDATE SET last_active_at = "
                "max(last_active_at, excluded.last_active_at)"
            ),
            {"at": iso_utc(now)},
        )


def consolidation_due(session: Session, *, now: datetime) -> bool:
    """Idle for 30 minutes, and no run in the last 24 hours (spec scenario 31)."""
    last_active = parse_utc(
        session.execute(
            text("SELECT last_active_at FROM consolidation_activity WHERE scope = 'user'")
        ).scalar_one_or_none()
    )
    if last_active is not None and now - last_active < IDLE_AFTER:
        return False
    last_run = parse_utc(
        session.execute(text("SELECT max(started_at) FROM consolidation_runs")).scalar_one()
    )
    return last_run is None or now - last_run >= RUN_EVERY


class ConsolidationService:
    def __init__(self) -> None:
        self.repository = ConclusionRepository()
        self.findings = FindingStore()

    def run(self, session: Session, *, now: datetime) -> ConsolidationRun:
        suggestions = findings = 0
        # Personal memory belongs to the single local user, whoever that is.
        memory = observe_memory(session, now=now)
        # Compare everything before the first write: SQLite holds its write lock
        # from then until commit, and answers must not wait on the comparison.
        plans = [
            (
                owner,
                self._relation_candidates(session, owner),
                observe_conclusions(session, owner, now=now) + memory,
            )
            for owner in self._owners(session)
        ]
        for owner, candidates, observed in plans:
            suggestions += self._propose(session, owner, candidates)
            findings += self.findings.record(session, owner, observed, now=now)
        run = ConsolidationRun(new_id(), now, suggestions, findings)
        session.execute(
            text(
                "INSERT INTO consolidation_runs (id, started_at, suggestions_created, "
                "findings_created) VALUES (:id, :at, :suggestions, :findings)"
            ),
            {
                "id": run.id,
                "at": iso_utc(now),
                "suggestions": run.suggestions_created,
                "findings": run.findings_created,
            },
        )
        return run

    @staticmethod
    def _owners(session: Session) -> list[str]:
        rows = session.execute(text("SELECT id FROM auth_users ORDER BY id")).scalars()
        return [str(owner) for owner in rows]

    @staticmethod
    def _formal_entries(session: Session, owner: str) -> list[dict[str, Any]]:
        rows = session.execute(
            text(
                """
                SELECT e.id, e.source_id, e.approved_version, e.created_at, v.payload_json
                FROM conclusion_entries e
                JOIN conclusion_versions v
                  ON v.entry_id = e.id AND v.version = e.approved_version
                WHERE e.owner_user_id = :o AND e.status = 'formal'
                  AND e.current_version = e.approved_version
                ORDER BY e.created_at, e.rowid
                """
            ),
            {"o": owner},
        ).mappings()
        return [
            {**dict(row), "payload": cast(dict[str, Any], json.loads(str(row["payload_json"])))}
            for row in rows
        ]

    def _relation_candidates(
        self, session: Session, owner: str
    ) -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
        """(kind, newer, older) for each pair worth suggesting; reads only."""
        entries = self._formal_entries(session, owner)
        candidates = []
        for index, newer in enumerate(entries):
            for older in entries[:index]:
                kind = relation_kind(newer["payload"], older["payload"])
                if kind in CONSOLIDATION_KINDS and not self._already_considered(
                    session, owner, newer, older
                ):
                    candidates.append((str(kind), newer, older))
        return candidates

    def _propose(
        self,
        session: Session,
        owner: str,
        candidates: list[tuple[str, dict[str, Any], dict[str, Any]]],
    ) -> int:
        for kind, newer, older in candidates:
            self.repository.insert_relation(
                session,
                owner,
                left_id=str(newer["id"]),
                right_id=str(older["id"]),
                kind=kind,
                left_version=int(newer["approved_version"]),
                right_version=int(older["approved_version"]),
                left_source_id=str(newer["source_id"]),
                right_source_id=str(older["source_id"]),
                origin="consolidation",
            )
        return len(candidates)

    @staticmethod
    def _already_considered(
        session: Session, owner: str, newer: dict[str, Any], older: dict[str, Any]
    ) -> bool:
        """Whether this pair, at these versions, was already suggested or decided.

        A rejected suggestion blocks a new one until either side has a new
        version. A review-time suggestion still waiting on a draft that has
        since been approved on its own no longer reaches 待处理, so it does not
        block one.
        """
        return (
            session.execute(
                text(
                    """
                    SELECT 1 FROM conclusion_relations
                    WHERE owner_user_id = :o
                      AND ((left_id = :a AND left_version = :av
                            AND right_id = :b AND right_version = :bv)
                        OR (left_id = :b AND left_version = :bv
                            AND right_id = :a AND right_version = :av))
                      AND (status IN ('approved', 'rejected') OR origin = 'consolidation')
                    LIMIT 1
                    """
                ),
                {
                    "o": owner,
                    "a": newer["id"],
                    "av": newer["approved_version"],
                    "b": older["id"],
                    "bv": older["approved_version"],
                },
            ).first()
            is not None
        )


def run_consolidation_if_due(
    session_factory: sessionmaker[Session], *, now: datetime
) -> ConsolidationRun | None:
    """The worker's consolidation step; returns the run, or None when not due."""
    with session_scope(session_factory) as session:
        if not consolidation_due(session, now=now):
            return None
        return ConsolidationService().run(session, now=now)
