"""知识健康报告: the weekly, read-only report on the knowledge base (spec decision 24).

A background consolidation run generates a report once a week, covering the
last 7 days. The report is kept as a snapshot that is never changed or removed
(the table's triggers refuse both), so every past week stays as it was and
the trend can be read from them.

A snapshot holds ids, counts and kinds only. Titles and links are read when
the report is opened, so privacy erasure never has to reach into a snapshot
and an erased item simply shows as no longer available. Generating or opening
a report changes no pending item and no knowledge.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import quote

from sqlalchemy import RowMapping, text
from sqlalchemy.orm import Session

from zhiheng.consolidation.findings import (
    FindingStore,
    iso_utc,
    observe_conclusions,
    observe_memory,
    parse_utc,
)
from zhiheng.core.ids import json_text, new_id
from zhiheng.pending import PENDING_TYPES, PendingItem
from zhiheng.pending.inbox import to_utc
from zhiheng.pending.sources import build_inbox

REPORT_EVERY = timedelta(days=7)
PERIOD = timedelta(days=7)
SNAPSHOT_VERSION = 1
HANDLED_WITHIN = timedelta(days=7)


def pending_href(item_type: str) -> str:
    """待处理, filtered to one type."""
    return f"/review-center?type={quote(item_type)}"


# Where to act on an indicator that counts suggestions. The indicators that
# count 整理发现 are acted on through each finding's link to its entry.
METRIC_LINKS: dict[str, list[dict[str, str]]] = {
    "duplicate_suggestions": [
        {"label": "合并建议", "href": pending_href("relation_suggestion")},
    ],
    "unresolved_conflicts": [
        {"label": "结论冲突", "href": pending_href("relation_suggestion")},
        {"label": "个人信息冲突", "href": pending_href("personal_conflict")},
    ],
}


class HealthReports:
    def generate_due(self, session: Session, *, now: datetime) -> list[str]:
        """Generate a report for each owner whose last one ended a week or more ago."""
        owners = session.execute(text("SELECT id FROM auth_users ORDER BY id")).scalars()
        return [
            self._generate(session, str(owner), now=now)
            for owner in list(owners)
            if self._due(session, str(owner), now=now)
        ]

    @staticmethod
    def _last(session: Session, owner: str) -> RowMapping | None:
        return (
            session.execute(
                text(
                    "SELECT id, period_end FROM knowledge_health_reports "
                    "WHERE owner_user_id = :o ORDER BY julianday(period_end) DESC LIMIT 1"
                ),
                {"o": owner},
            )
            .mappings()
            .first()
        )

    def _due(self, session: Session, owner: str, *, now: datetime) -> bool:
        last = self._last(session, owner)
        last_end = None if last is None else parse_utc(last["period_end"])
        return last_end is None or now - last_end >= REPORT_EVERY

    def _generate(self, session: Session, owner: str, *, now: datetime) -> str:
        last = self._last(session, owner)
        # The last 7 days, or since the last report when idle days pushed this
        # one later, so that no change falls between two reports.
        start = iso_utc(now - PERIOD) if last is None else str(last["period_end"])
        end = iso_utc(now)
        pending = build_inbox().collect(session, owner)
        snapshot = {
            "version": SNAPSHOT_VERSION,
            "changes": {
                "knowledge": _knowledge_changes(session, owner, start, end),
                "memory": _memory_changes(session, start, end),
                "pending": _pending_overview(session, owner, pending, start, end),
            },
            # 整理发现 open once this run recorded them (spec decision 23).
            "findings": [
                {key: finding[key] for key in ("id", "kind", "subject_type", "subject_id")}
                for finding in FindingStore().list(session, owner, status="open")
            ],
            "metrics": {
                **_metrics(session, owner, pending, now=now),
                "handled_within_7_days_share": _handled_within_a_week(
                    session, owner, pending, end=now
                ),
            },
        }
        report_id = new_id()
        session.execute(
            text(
                "INSERT INTO knowledge_health_reports (id, owner_user_id, period_start, "
                "period_end, generated_at, previous_report_id, report_json) "
                "VALUES (:id, :o, :start, :end, :end, :previous, :report)"
            ),
            {
                "id": report_id,
                "o": owner,
                "start": start,
                "end": end,
                "previous": None if last is None else last["id"],
                "report": json_text(snapshot),
            },
        )
        return report_id

    @staticmethod
    def _previous_metrics(session: Session, row: Any) -> dict[str, dict[str, Any]] | None:
        if row["previous_report_id"] is None:
            return None
        previous = session.execute(
            text("SELECT report_json FROM knowledge_health_reports WHERE id = :id"),
            {"id": row["previous_report_id"]},
        ).scalar_one()
        metrics: dict[str, dict[str, Any]] = json.loads(str(previous))["metrics"]
        return metrics

    def list(self, session: Session, owner: str) -> list[dict[str, Any]]:
        """Every report, newest first, with its indicator values for the trend."""
        rows = session.execute(
            text(
                "SELECT id, period_start, period_end, generated_at, report_json "
                "FROM knowledge_health_reports WHERE owner_user_id = :o "
                "ORDER BY julianday(period_end) DESC"
            ),
            {"o": owner},
        ).mappings()
        return [
            {
                "id": row["id"],
                "period_start": row["period_start"],
                "period_end": row["period_end"],
                "generated_at": row["generated_at"],
                "metrics": {
                    name: metric.get("value")
                    for name, metric in json.loads(str(row["report_json"]))["metrics"].items()
                },
            }
            for row in rows
        ]

    def latest(self, session: Session, owner: str) -> dict[str, Any] | None:
        last = self._last(session, owner)
        return None if last is None else self.get(session, owner, str(last["id"]))

    def get(self, session: Session, owner: str, report_id: str) -> dict[str, Any] | None:
        row = (
            session.execute(
                text(
                    "SELECT id, period_start, period_end, generated_at, previous_report_id, "
                    "report_json FROM knowledge_health_reports "
                    "WHERE id = :id AND owner_user_id = :o"
                ),
                {"id": report_id, "o": owner},
            )
            .mappings()
            .first()
        )
        if row is None:
            return None
        snapshot = json.loads(str(row["report_json"]))
        labels = _Labels(session, owner)
        knowledge = snapshot["changes"]["knowledge"]
        return {
            "id": row["id"],
            "period_start": row["period_start"],
            "period_end": row["period_end"],
            "generated_at": row["generated_at"],
            "previous_report_id": row["previous_report_id"],
            "changes": {
                "knowledge": {
                    group: [labels.conclusion(item) for item in items]
                    for group, items in knowledge.items()
                },
                "memory": {
                    group: [labels.memory(item) for item in items]
                    for group, items in snapshot["changes"]["memory"].items()
                },
                "pending": {
                    **snapshot["changes"]["pending"],
                    "links": {item_type: pending_href(item_type) for item_type in PENDING_TYPES},
                },
            },
            "metrics": {
                name: {**metric, "links": METRIC_LINKS.get(name, [])}
                for name, metric in _with_change(
                    snapshot["metrics"], self._previous_metrics(session, row)
                ).items()
            },
            "findings": [labels.finding(item) for item in snapshot["findings"]],
        }


def _with_change(
    metrics: dict[str, dict[str, Any]], previous: dict[str, dict[str, Any]] | None
) -> dict[str, dict[str, Any]]:
    """Each indicator with its value in the previous report and the change since."""
    out = {}
    for name, metric in metrics.items():
        before = None if previous is None else previous.get(name, {}).get("value")
        value = metric.get("value")
        change = None if before is None or value is None else round(value - before, 4)
        out[name] = {**metric, "previous": before, "change": change}
    return out


def _in_period(column: str) -> str:
    return f"julianday({column}) >= julianday(:start) AND julianday({column}) < julianday(:end)"


def _knowledge_changes(
    session: Session, owner: str, start: str, end: str
) -> dict[str, list[dict[str, Any]]]:
    window = {"o": owner, "start": start, "end": end}
    decided = [
        dict(row)
        for row in session.execute(
            text(
                f"""
                SELECT r.kind, r.origin, r.left_id, r.right_id
                FROM conclusion_relation_events ev
                JOIN conclusion_relations r ON r.id = ev.relation_id
                WHERE ev.owner_user_id = :o AND ev.to_status = 'approved'
                  AND r.kind IN ('duplicate', 'revision') AND {_in_period("ev.created_at")}
                ORDER BY julianday(ev.created_at), ev.rowid
                """
            ),
            window,
        ).mappings()
    ]
    # A merge retires the newer duplicate into the older entry; an approved
    # consolidation revision retires the older entry the newer one revises.
    merged = [
        {"id": str(row["left_id"]), "into": {"id": str(row["right_id"])}}
        for row in decided
        if row["kind"] == "duplicate"
    ]
    revised = [
        {"id": str(row["left_id"]), "revises": {"id": str(row["right_id"])}}
        for row in decided
        if row["kind"] == "revision"
    ]
    # A draft approved as a merge or a revision is not new knowledge of its own.
    not_new = {
        str(entry_id)
        for entry_id in session.execute(
            text(
                "SELECT left_id FROM conclusion_relations WHERE owner_user_id = :o "
                "AND status = 'approved' AND origin = 'review' "
                "AND kind IN ('duplicate', 'revision')"
            ),
            window,
        ).scalars()
    }
    added = [
        {"id": str(entry_id)}
        for entry_id in session.execute(
            text(
                f"""
                SELECT e.id FROM conclusion_entries e
                WHERE e.owner_user_id = :o
                  AND e.id IN (
                    SELECT entry_id FROM conclusion_versions v
                    GROUP BY entry_id
                    HAVING {_in_period("min(v.approved_at)")}
                  )
                ORDER BY e.created_at, e.rowid
                """
            ),
            window,
        ).scalars()
        if str(entry_id) not in not_new
    ]
    paused = [
        {"id": str(row["entry_id"]), "reason": row["reason"]}
        for row in session.execute(
            text(
                f"""
                SELECT a.entry_id, min(a.reason) AS reason
                FROM conclusion_applicability_events a
                JOIN conclusion_entries e ON e.id = a.entry_id
                WHERE e.owner_user_id = :o AND a.state = 'suspended'
                  AND {_in_period("a.created_at")}
                GROUP BY a.entry_id
                ORDER BY min(julianday(a.created_at)), a.entry_id
                """
            ),
            window,
        ).mappings()
    ]
    return {"added": added, "revised": revised, "merged": merged, "paused": paused}


def _memory_changes(session: Session, start: str, end: str) -> dict[str, list[dict[str, Any]]]:
    """Personal information first remembered, or given a new version, in the period.

    Personal memory belongs to the single local user, whoever that is.
    """
    rows = session.execute(
        text(
            f"""
            SELECT fm.id, min(fmv.version_no) AS first_version
            FROM formal_memory_versions fmv
            JOIN formal_memories fm ON fm.id = fmv.formal_memory_id
            WHERE fm.status != 'privacy_erased' AND {_in_period("fmv.created_at")}
            GROUP BY fm.id
            ORDER BY min(julianday(fmv.created_at)), fm.id
            """
        ),
        {"start": start, "end": end},
    ).mappings()
    remembered: list[dict[str, Any]] = []
    updated: list[dict[str, Any]] = []
    for row in rows:
        (remembered if int(row["first_version"]) == 1 else updated).append({"id": str(row["id"])})
    return {"remembered": remembered, "updated": updated}


def _pending_overview(
    session: Session, owner: str, items: list[PendingItem], start: str, end: str
) -> dict[str, Any]:
    begin, finish = to_utc(start), to_utc(end)
    by_type = dict.fromkeys(PENDING_TYPES, 0)
    for item in items:
        if not item.deferred:
            by_type[item.type] += 1
    # Became pending in the period: what still is, and what was decided since.
    entered = {(item.type, item.id) for item in items if begin <= item.pending_since < finish}
    entered.update(
        (str(row["item_type"]), str(row["item_id"]))
        for row in session.execute(
            text(
                f"SELECT item_type, item_id FROM pending_resolutions "
                f"WHERE owner_user_id = :o AND {_in_period('pending_since')}"
            ),
            {"o": owner, "start": start, "end": end},
        ).mappings()
    )
    return {
        "open": sum(by_type.values()),
        "deferred": sum(1 for item in items if item.deferred),
        "by_type": by_type,
        "entered": len(entered),
    }


def _share(numerator: int, denominator: int) -> dict[str, Any]:
    value = None if denominator == 0 else round(numerator / denominator, 4)
    return {"value": value, "numerator": numerator, "denominator": denominator}


def _metrics(
    session: Session, owner: str, pending: list[PendingItem], *, now: datetime
) -> dict[str, dict[str, Any]]:
    """The health indicators, read from the knowledge base as it is now."""
    observed: dict[str, set[str]] = {}
    for observation in observe_conclusions(session, owner, now=now) + observe_memory(
        session, now=now
    ):
        observed.setdefault(observation.kind, set()).add(observation.subject_id)
    formal = int(
        session.execute(
            text(
                "SELECT count(*) FROM conclusion_entries e "
                "JOIN conclusion_sources s ON s.id = e.source_id "
                "WHERE e.owner_user_id = :o AND e.status = 'formal'"
            ),
            {"o": owner},
        ).scalar_one()
    )
    suggested = [
        str(item.detail.get("kind")) for item in pending if item.type == "relation_suggestion"
    ]
    # An approved conflict keeps both sides formal and stands until one is retired.
    standing = int(
        session.execute(
            text(
                "SELECT count(*) FROM conclusion_relations r "
                "JOIN conclusion_entries le ON le.id = r.left_id "
                "JOIN conclusion_entries re ON re.id = r.right_id "
                "WHERE r.owner_user_id = :o AND r.status = 'approved' AND r.kind = 'conflict' "
                "AND le.status = 'formal' AND re.status = 'formal'"
            ),
            {"o": owner},
        ).scalar_one()
    )
    personal = sum(1 for item in pending if item.type == "personal_conflict")
    return {
        "duplicate_suggestions": {"value": suggested.count("duplicate")},
        "explicit_premise_share": _share(formal - len(observed.get("missing_premise", ())), formal),
        "unresolved_conflicts": {"value": suggested.count("conflict") + personal + standing},
        "unconfirmed_assumption_conclusions": {
            "value": len(observed.get("unconfirmed_assumption", ()))
        },
        "isolated_entries": {"value": len(observed.get("isolated", ()))},
        "stale_volatile_memories": {"value": len(observed.get("stale_volatile", ()))},
    }


def _handled_within_a_week(
    session: Session, owner: str, pending: list[PendingItem], *, end: datetime
) -> dict[str, Any]:
    """Of the items that became pending 7 to 14 days before the report ends, the
    share decided within 7 days, so that every item counted had its full week.

    Items still pending, deferred ones included, count as not handled. An item
    that left 待处理 without the user deciding it is not counted at all.
    """
    since, until = end - 2 * HANDLED_WITHIN, end - HANDLED_WITHIN
    resolved = {
        (str(row["item_type"]), str(row["item_id"])): (
            to_utc(row["pending_since"]),
            to_utc(row["resolved_at"]),
        )
        for row in session.execute(
            text(
                "SELECT item_type, item_id, pending_since, resolved_at "
                "FROM pending_resolutions WHERE owner_user_id = :o"
            ),
            {"o": owner},
        ).mappings()
    }
    cohort = {
        key: resolved_at - became <= HANDLED_WITHIN
        for key, (became, resolved_at) in resolved.items()
        if since <= became < until
    }
    for item in pending:
        if since <= item.pending_since < until:
            cohort.setdefault((item.type, item.id), False)
    return _share(sum(cohort.values()), len(cohort))


class _Labels:
    """Live titles and links for the ids a snapshot holds."""

    def __init__(self, session: Session, owner: str) -> None:
        self.session = session
        self.owner = owner

    def conclusion(self, item: dict[str, Any]) -> dict[str, Any]:
        row = (
            self.session.execute(
                text(
                    "SELECT e.knowledge_id, v.payload_json FROM conclusion_entries e "
                    "JOIN conclusion_versions v ON v.entry_id = e.id "
                    "AND v.version = COALESCE(e.approved_version, e.current_version) "
                    "WHERE e.id = :id AND e.owner_user_id = :o"
                ),
                {"id": item["id"], "o": self.owner},
            )
            .mappings()
            .first()
        )
        nested = {
            key: self.conclusion(value)
            for key, value in item.items()
            if key in ("into", "revises") and isinstance(value, dict)
        }
        if row is None:
            return {**item, **nested, "title": None, "href": None}
        payload = json.loads(str(row["payload_json"]))
        return {
            **item,
            **nested,
            "title": payload.get("title") or payload.get("claim"),
            "href": None
            if row["knowledge_id"] is None
            else f"/knowledge-agent?knowledge={quote(str(row['knowledge_id']))}#library",
        }

    def memory(self, item: dict[str, Any]) -> dict[str, Any]:
        row = (
            self.session.execute(
                text(
                    "SELECT state_key, status FROM formal_memories "
                    "WHERE id = :id AND status != 'privacy_erased'"
                ),
                {"id": item["id"]},
            )
            .mappings()
            .first()
        )
        if row is None:
            return {**item, "title": None, "href": None}
        return {
            **item,
            "title": row["state_key"],
            "href": f"/memory-center?memory={quote(str(item['id']))}",
        }

    def finding(self, item: dict[str, Any]) -> dict[str, Any]:
        subject = {"id": item["subject_id"]}
        labelled = (
            self.conclusion(subject)
            if item["subject_type"] == "conclusion"
            else self.memory(subject)
        )
        return {**item, "title": labelled["title"], "href": labelled["href"]}
