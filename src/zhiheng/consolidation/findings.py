"""整理发现: what a consolidation run observes without proposing any change.

A finding is keyed by its kind, its subject and the subject's version. Each
run records what it observes: a new observation opens a finding, one no
longer observed is resolved, and one that comes back reopens. A dismissed
finding stays dismissed (dismissing is not 延后 and changes no knowledge); it
can only come back as a new finding once the subject has a new version.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from zhiheng.core.ids import json_text, new_id

# Only for the report: volatile information is still re-checked when it is
# next used (spec decision 12), and nothing is retired for going unused.
STALE_VOLATILE_AFTER = timedelta(days=90)
UNUSED_AFTER = timedelta(days=180)
# Goals, plans and temporary states (spec decision 12), by how they were
# recorded or by what they are about.
VOLATILE_TIME_SENSITIVITIES = ("current", "volatile")
VOLATILE_STATE_PREFIXES = ("goal.", "plan.")


@dataclass(frozen=True)
class Observation:
    kind: str
    subject_type: str
    subject_id: str
    subject_version: str
    detail: Mapping[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> tuple[str, str, str, str]:
        return (self.kind, self.subject_type, self.subject_id, self.subject_version)


class FindingNotFoundError(LookupError):
    pass


def iso_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def parse_utc(value: object) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(str(value))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def observe_conclusions(session: Session, owner: str, *, now: datetime) -> list[Observation]:
    rows = session.execute(
        text(
            """
            SELECT e.id, e.approved_version, e.knowledge_id, v.payload_json,
                   s.body AS source_body,
                   (SELECT min(av.approved_at) FROM conclusion_versions av
                    WHERE av.entry_id = e.id AND av.approved_at IS NOT NULL) AS first_approved_at,
                   EXISTS (
                     SELECT 1 FROM conclusion_relations r
                     WHERE r.owner_user_id = e.owner_user_id AND r.status = 'approved'
                       AND (r.left_id = e.id OR r.right_id = e.id)
                   ) AS related
            FROM conclusion_entries e
            JOIN conclusion_versions v ON v.entry_id = e.id AND v.version = e.approved_version
            JOIN conclusion_sources s ON s.id = e.source_id
            WHERE e.owner_user_id = :o AND e.status = 'formal'
            ORDER BY e.created_at, e.rowid
            """
        ),
        {"o": owner},
    ).mappings()
    cited = _cited_knowledge(session, owner, since=now - UNUSED_AFTER)
    observations: list[Observation] = []
    for row in rows:
        payload = json.loads(str(row["payload_json"]))
        premises = [p for p in payload.get("premises", []) if str(p.get("text", "")).strip()]
        classification = payload.get("classification") or {}
        detail = {"title": payload.get("title"), "claim": payload.get("claim")}
        kinds: list[str] = []
        if not premises:
            kinds.append("missing_premise")
        if not str(payload.get("excerpt") or "").strip() or not str(row["source_body"]).strip():
            kinds.append("missing_source")
        if any(not p.get("confirmed", False) for p in premises):
            kinds.append("unconfirmed_assumption")
        if not row["related"] and not classification.get("related_domain_ids"):
            kinds.append("isolated")
        approved_at = parse_utc(row["first_approved_at"])
        if (
            approved_at is not None
            and now - approved_at >= UNUSED_AFTER
            and row["knowledge_id"] not in cited
        ):
            kinds.append("unused")
        observations.extend(
            Observation(kind, "conclusion", str(row["id"]), str(row["approved_version"]), detail)
            for kind in kinds
        )
    return observations


def _cited_knowledge(session: Session, owner: str, *, since: datetime) -> set[str]:
    """Knowledge objects that an answer since then actually cited.

    An answer in a conversation is kept in its history; every answer, in a
    conversation or not, links the sources it used to its operation receipt.
    """
    rows = session.execute(
        text(
            """
            SELECT json_extract(c.value, '$.source_id')
            FROM answer_history h, json_each(h.response_json, '$.citations') c
            WHERE h.owner_user_id = :o
              AND json_extract(c.value, '$.source_type') = 'knowledge_object'
              AND datetime(h.created_at) > datetime(:since)
            UNION
            SELECT s.source_id
            FROM memory_operation_receipts r
            JOIN memory_operation_receipt_sources s ON s.receipt_id = r.id
            WHERE r.operation_type = 'answer_question' AND r.status = 'completed'
              AND s.source_type = 'knowledge_object'
              AND datetime(r.created_at) > datetime(:since)
            """
        ),
        {"o": owner, "since": iso_utc(since)},
    ).scalars()
    return {str(value) for value in rows if value is not None}


def observe_memory(session: Session, *, now: datetime) -> list[Observation]:
    """Volatile personal information that nobody has confirmed for 90 days."""
    rows = session.execute(
        text(
            """
            SELECT fm.id, fm.state_key, fm.current_version_id, fm.time_sensitivity,
                   fmv.created_at AS confirmed_at
            FROM formal_memories fm
            JOIN formal_memory_versions fmv ON fmv.id = fm.current_version_id
            WHERE fm.status = 'formal_current' AND fm.valid_to IS NULL
            ORDER BY fm.state_key
            """
        )
    ).mappings()
    observations = []
    for row in rows:
        volatile = str(row["time_sensitivity"]) in VOLATILE_TIME_SENSITIVITIES or str(
            row["state_key"]
        ).startswith(VOLATILE_STATE_PREFIXES)
        confirmed_at = parse_utc(row["confirmed_at"])
        if volatile and confirmed_at is not None and now - confirmed_at >= STALE_VOLATILE_AFTER:
            observations.append(
                Observation(
                    "stale_volatile",
                    "memory",
                    str(row["id"]),
                    str(row["current_version_id"]),
                    {"state_key": row["state_key"], "last_confirmed_at": iso_utc(confirmed_at)},
                )
            )
    return observations


class FindingStore:
    def record(
        self, session: Session, owner: str, observations: list[Observation], *, now: datetime
    ) -> int:
        """Bring the owner's findings in line with one run; returns how many are new."""
        existing = {
            (
                str(row["kind"]),
                str(row["subject_type"]),
                str(row["subject_id"]),
                str(row["subject_version"]),
            ): row
            for row in session.execute(
                text(
                    "SELECT id, kind, subject_type, subject_id, subject_version, status "
                    "FROM consolidation_findings WHERE owner_user_id = :o"
                ),
                {"o": owner},
            ).mappings()
        }
        at = iso_utc(now)
        created = 0
        observed = set()
        for observation in observations:
            observed.add(observation.key)
            row = existing.get(observation.key)
            if row is None:
                session.execute(
                    text(
                        "INSERT INTO consolidation_findings (id, owner_user_id, kind, "
                        "subject_type, subject_id, subject_version, detail_json, created_at, "
                        "updated_at) VALUES (:id, :o, :kind, :type, :subject, :version, "
                        ":detail, :at, :at)"
                    ),
                    {
                        "id": new_id(),
                        "o": owner,
                        "kind": observation.kind,
                        "type": observation.subject_type,
                        "subject": observation.subject_id,
                        "version": observation.subject_version,
                        "detail": json_text(dict(observation.detail)),
                        "at": at,
                    },
                )
                created += 1
            elif row["status"] == "resolved":
                self._set_status(session, str(row["id"]), "open", at)
        for key, row in existing.items():
            if key not in observed and row["status"] == "open":
                self._set_status(session, str(row["id"]), "resolved", at)
        return created

    @staticmethod
    def _set_status(session: Session, finding_id: str, status: str, at: str) -> None:
        session.execute(
            text("UPDATE consolidation_findings SET status = :s, updated_at = :at WHERE id = :id"),
            {"s": status, "at": at, "id": finding_id},
        )

    def list(self, session: Session, owner: str, *, status: str = "open") -> list[dict[str, Any]]:
        rows = session.execute(
            text(
                "SELECT id, kind, subject_type, subject_id, subject_version, status, "
                "detail_json, created_at, updated_at FROM consolidation_findings "
                "WHERE owner_user_id = :o AND (:s = 'all' OR status = :s) "
                "ORDER BY created_at, rowid"
            ),
            {"o": owner, "s": status},
        ).mappings()
        return [
            {
                **{k: v for k, v in row.items() if k != "detail_json"},
                "detail": json.loads(str(row["detail_json"])),
            }
            for row in rows
        ]

    def dismiss(
        self, session: Session, owner: str, finding_id: str, *, now: datetime
    ) -> dict[str, Any]:
        """Dismiss a finding for good; its subject and every pending item stay as they are."""
        current = session.execute(
            text("SELECT status FROM consolidation_findings WHERE id = :id AND owner_user_id = :o"),
            {"id": finding_id, "o": owner},
        ).scalar_one_or_none()
        if current is None:
            raise FindingNotFoundError(finding_id)
        if current != "dismissed":
            self._set_status(session, finding_id, "dismissed", iso_utc(now))
        return {"id": finding_id, "status": "dismissed"}
