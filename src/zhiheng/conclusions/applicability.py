"""Version-bound applicability decisions that never rewrite approved knowledge."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from zhiheng.core.ids import json_text, new_id, sha256_json


class ConclusionApplicabilityService:
    def sweep(self, session: Session) -> int:
        """Persist known expiry; retrieval also checks expiry before a worker runs."""
        rows = (
            session.execute(
                text("""
            SELECT e.id, e.approved_version, v.payload_json
            FROM conclusion_entries e JOIN conclusion_versions v
              ON v.entry_id=e.id AND v.version=e.approved_version
            WHERE e.status='formal'
              AND (julianday(json_extract(v.payload_json,'$.valid_until'))<=julianday('now')
                OR EXISTS (SELECT 1 FROM json_each(v.payload_json,'$.premises') p
                  WHERE json_extract(p.value,'$.confirmed')=1
                    AND julianday(json_extract(p.value,'$.valid_until'))<=julianday('now')))

        """)
            )
            .mappings()
            .all()
        )
        count = 0
        for row in rows:
            payload = json.loads(str(row["payload_json"]))
            expiry = session.execute(
                text("""
                SELECT julianday(:deadline)<=julianday('now')
            """),
                {"deadline": payload.get("valid_until")},
            ).scalar()
            if expiry:
                reason, evidence = "validity_expired", {"valid_until": payload["valid_until"]}
            else:
                reason, evidence = "premise_expired", {"premises": payload.get("premises", [])}
            count += self._record(
                session,
                str(row["id"]),
                int(row["approved_version"]),
                "suspended",
                reason,
                evidence,
            )
        future_rows = (
            session.execute(
                text("""
            SELECT a.entry_id,a.version,a.evidence_json
            FROM conclusion_applicability_events a JOIN conclusion_entries e ON e.id=a.entry_id
            WHERE e.status='formal' AND a.version=e.approved_version
              AND a.state='review_required'
              AND json_extract(a.evidence_json,'$.future_fact')=1
              AND julianday(json_extract(a.evidence_json,'$.effective_at'))<=julianday('now')
        """)
            )
            .mappings()
            .all()
        )
        for row in future_rows:
            evidence = json.loads(str(row["evidence_json"]))
            evidence["certain"] = True
            count += self._record(
                session,
                str(row["entry_id"]),
                int(row["version"]),
                "suspended",
                "premise_changed",
                evidence,
            )
        return count + self._track_synthesis_basis(session)

    def _track_synthesis_basis(self, session: Session) -> int:
        """A synthesized conclusion follows the formal knowledge it was drawn from (#116).

        A basis entry that is paused or deleted puts the synthesis under
        applicability review; one that is revised or merged only raises a
        review reminder. The synthesis itself is never rewritten.
        """
        rows = (
            session.execute(
                text("""
            SELECT e.id, e.approved_version, b.value AS basis_json,
                   be.id AS basis_id, be.status AS basis_status,
                   be.approved_version AS basis_approved_version,
                   ko.lifecycle_status AS basis_lifecycle,
                   (SELECT ba.state FROM conclusion_applicability ba
                    WHERE ba.entry_id = be.id AND ba.version = be.approved_version
                   ) AS basis_applicability
            FROM conclusion_entries e
            JOIN conclusion_versions v ON v.entry_id=e.id AND v.version=e.approved_version
            JOIN json_each(v.payload_json,'$.basis') b
            LEFT JOIN conclusion_entries be
              ON be.id=json_extract(b.value,'$.entry_id') AND be.owner_user_id=e.owner_user_id
            LEFT JOIN knowledge_objects ko ON ko.id=be.knowledge_id
            WHERE e.status='formal'
              AND json_extract(v.payload_json,'$.origin')='system_synthesis'
        """)
            )
            .mappings()
            .all()
        )
        count = 0
        for row in rows:
            basis = json.loads(str(row["basis_json"]))
            evidence = {"basis_entry_id": basis["entry_id"], "basis_version": basis["version"]}
            if row["basis_id"] is None or row["basis_lifecycle"] in {
                "soft_deleted",
                "privacy_erased",
            }:
                state, reason = "suspended", "basis_removed"
            elif row["basis_status"] == "formal" and row["basis_applicability"] == "suspended":
                state, reason = "suspended", "basis_suspended"
            elif row["basis_status"] in {"superseded", "merged"} or (
                row["basis_approved_version"] != basis["version"]
            ):
                state, reason = "review_required", "basis_revised"
            else:
                continue
            count += self._record(
                session, str(row["id"]), int(row["approved_version"]), state, reason, evidence
            )
        return count

    def fact_changed(
        self,
        session: Session,
        owner: str,
        state_key: str,
        value: dict[str, Any],
        *,
        certain: bool,
        evidence: dict[str, Any],
    ) -> int:
        """Only explicit confirmed dependencies can invalidate an approved version."""
        rows = (
            session.execute(
                text("""
            SELECT e.id, e.approved_version, v.payload_json
            FROM conclusion_entries e JOIN conclusion_versions v
              ON v.entry_id=e.id AND v.version=e.approved_version
            WHERE e.owner_user_id=:owner AND e.status='formal'
        """),
                {"owner": owner},
            )
            .mappings()
            .all()
        )
        count = 0
        for row in rows:
            payload = json.loads(str(row["payload_json"]))
            for premise in payload.get("premises", []):
                if (
                    not isinstance(premise, dict)
                    or premise.get("confirmed") is not True
                    or premise.get("state_key") != state_key
                    or "value" not in premise
                    or premise["value"] == value
                ):
                    continue
                count += self._record(
                    session,
                    str(row["id"]),
                    int(row["approved_version"]),
                    "suspended" if certain else "review_required",
                    "premise_changed" if certain else "premise_change_uncertain",
                    {
                        **evidence,
                        "state_key": state_key,
                        "expected_value": premise["value"],
                        "observed_value": value,
                        "certain": certain,
                    },
                )
        return count

    def _record(
        self,
        session: Session,
        entry_id: str,
        version: int,
        state: str,
        reason: str,
        evidence: dict[str, Any],
    ) -> int:
        event_key = sha256_json(
            {
                "entry_id": entry_id,
                "version": version,
                "state": state,
                "reason": reason,
                "evidence": evidence,
            }
        )
        values = {
            "id": new_id(),
            "entry": entry_id,
            "version": version,
            "state": state,
            "reason": reason,
            "evidence": json_text(evidence),
            "event_key": event_key,
        }
        inserted = session.execute(
            text("""
            INSERT INTO conclusion_applicability_events
              (id,entry_id,version,state,reason,evidence_json,event_key)
            VALUES (:id,:entry,:version,:state,:reason,:evidence,:event_key)
            ON CONFLICT(event_key) DO NOTHING RETURNING id
        """),
            values,
        ).scalar_one_or_none()
        if inserted is None:
            return 0
        session.execute(
            text("""
            INSERT INTO conclusion_applicability (entry_id,version,state,reason,evidence_json)
            VALUES (:entry,:version,:state,:reason,:evidence)
            ON CONFLICT(entry_id) DO UPDATE SET version=excluded.version,
                state=excluded.state,reason=excluded.reason,evidence_json=excluded.evidence_json,
                updated_at=CURRENT_TIMESTAMP
            WHERE conclusion_applicability.version != excluded.version
               OR conclusion_applicability.state != 'suspended'
               OR excluded.state='suspended'
        """),
            values,
        )
        return 1

    def describe(self, session: Session, entry_id: str) -> dict[str, Any]:
        row = (
            session.execute(
                text("""
            SELECT a.* FROM conclusion_applicability a JOIN conclusion_entries e ON e.id=a.entry_id
            WHERE a.entry_id=:entry AND a.version=e.approved_version
        """),
                {"entry": entry_id},
            )
            .mappings()
            .first()
        )
        events = (
            session.execute(
                text("""
            SELECT version,state,reason,evidence_json,created_at
            FROM conclusion_applicability_events WHERE entry_id=:entry
            ORDER BY created_at,id
        """),
                {"entry": entry_id},
            )
            .mappings()
            .all()
        )
        history = [
            {
                "version": event["version"],
                "state": event["state"],
                "reason": event["reason"],
                "evidence": json.loads(event["evidence_json"]),
                "created_at": event["created_at"],
            }
            for event in events
        ]
        if row is None:
            return {
                "state": "active",
                "reason": None,
                "version": None,
                "evidence": None,
                "events": history,
            }
        return {
            "state": row["state"],
            "reason": row["reason"],
            "version": row["version"],
            "evidence": json.loads(row["evidence_json"]),
            "events": history,
        }
