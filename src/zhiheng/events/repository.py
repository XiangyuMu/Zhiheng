from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import text
from sqlalchemy.orm import Session

from zhiheng.core.ids import new_id, sha256_text
from zhiheng.memory.repository import (
    MemoryRepository,
    StaleConfirmationError,
)


class EventRepository:
    @staticmethod
    def etag(item: dict[str, Any]) -> str:
        return sha256_text(
            json.dumps(
                {"id": item["id"], "version_id": item.get("version_id"), "status": item["status"]},
                sort_keys=True,
            )
        )

    def list_candidates(self, session: Session, owner: str) -> list[dict[str, Any]]:
        rows = session.execute(
            text("""
          SELECT e.*, v.id AS version_id, v.title AS version_title, v.summary AS version_summary,
                 q.id AS confirmation_request_id, ev.excerpt
          FROM event_memories e
          JOIN event_memory_versions v ON v.event_memory_id=e.id
            AND v.version_no=(SELECT max(version_no) FROM event_memory_versions WHERE event_memory_id=e.id)
          LEFT JOIN event_confirmation_requests q ON q.event_memory_id=e.id AND q.status='pending'
          LEFT JOIN event_memory_evidence ev ON ev.event_memory_id=e.id
          WHERE e.owner_user_id=:owner AND e.status='candidate' ORDER BY e.created_at DESC
        """),
            {"owner": owner},
        ).mappings()
        return [dict(r) for r in rows]

    def get(self, session: Session, owner: str, event_id: str) -> dict[str, Any] | None:
        row = (
            session.execute(
                text("""
          SELECT e.*, v.id AS version_id, v.version_no, v.title AS version_title,
                 v.summary AS version_summary, ev.excerpt, ev.quote_hash,
                 ev.history_id, ev.conversation_id
          FROM event_memories e
          LEFT JOIN event_memory_versions v ON v.event_memory_id=e.id
            AND v.version_no=(SELECT max(version_no) FROM event_memory_versions WHERE event_memory_id=e.id)
          LEFT JOIN event_memory_evidence ev ON ev.event_version_id=v.id
          WHERE e.id=:id AND e.owner_user_id=:owner
        """),
                {"id": event_id, "owner": owner},
            )
            .mappings()
            .first()
        )
        return dict(row) if row else None

    def edit(
        self,
        session: Session,
        owner: str,
        event_id: str,
        *,
        title: str,
        summary: str,
        operation_key: str,
        if_match: str | None = None,
    ) -> dict[str, Any]:
        event = self.get(session, owner, event_id)
        if not event or event["status"] != "candidate":
            raise StaleConfirmationError("event is not editable")
        if if_match and if_match.strip('"') not in {"*", self.etag(event)}:
            raise StaleConfirmationError("event changed after it was read")
        request_hash = sha256_text(
            json.dumps(
                {"event_id": event_id, "title": title, "summary": summary, "if_match": if_match},
                sort_keys=True,
            )
        )
        receipts = MemoryRepository()
        existing = receipts._receipt(session, operation_key, request_hash=request_hash)
        if existing is not None and existing["status"] != "started":
            return cast(dict[str, Any], json.loads(str(existing["result_json"])))
        receipt_id = receipts._insert_receipt(session, operation_key, "event_edit", request_hash)
        previous = str(event["version_id"])
        next_no = int(event["version_no"]) + 1
        version_id = new_id()
        session.execute(
            text(
                "INSERT INTO event_memory_versions (id,event_memory_id,version_no,title,summary,payload_json) VALUES (:id,:event,:no,:title,:summary,:payload)"
            ),
            {
                "id": version_id,
                "event": event_id,
                "no": next_no,
                "title": title,
                "summary": summary,
                "payload": json.dumps({"edited": True}),
            },
        )
        evidence = (
            session.execute(
                text(
                    "SELECT excerpt,conversation_id,history_id FROM event_memory_evidence WHERE event_version_id=:id LIMIT 1"
                ),
                {"id": previous},
            )
            .mappings()
            .first()
        )
        if evidence:
            excerpt = str(evidence["excerpt"])
            session.execute(
                text(
                    "INSERT INTO event_memory_evidence (id,event_memory_id,event_version_id,conversation_id,history_id,excerpt,start_offset,end_offset,support_type,quote_hash) VALUES (:id,:event,:version,:conversation,:history,:excerpt,0,:end,'origin',:hash)"
                ),
                {
                    "id": f"{version_id}:origin",
                    "event": event_id,
                    "version": version_id,
                    "conversation": evidence["conversation_id"],
                    "history": evidence["history_id"],
                    "excerpt": excerpt,
                    "end": len(excerpt),
                    "hash": sha256_text(excerpt),
                },
            )
        session.execute(
            text(
                "UPDATE event_confirmation_requests SET status='superseded' WHERE event_memory_id=:id AND status='pending'"
            ),
            {"id": event_id},
        )
        request_id = new_id()
        session.execute(
            text(
                "INSERT INTO event_confirmation_requests (id,event_memory_id,event_version_id,status,risk_level,proposed_value_hash,expires_at) VALUES (:id,:event,:version,'pending','medium',:hash,:expires)"
            ),
            {
                "id": request_id,
                "event": event_id,
                "version": version_id,
                "hash": sha256_text(summary),
                "expires": datetime.now(UTC) + timedelta(days=1),
            },
        )
        updated = dict(event)
        updated["version_id"] = version_id
        result = {
            "event_id": event_id,
            "version_id": version_id,
            "request_id": request_id,
            "status": "edited",
            "etag": self.etag(updated),
        }
        session.execute(
            text(
                "UPDATE memory_operation_receipts SET status='edited',result_json=:result,completed_at=CURRENT_TIMESTAMP WHERE id=:id"
            ),
            {"id": receipt_id, "result": json.dumps(result)},
        )
        return result

    def decide(
        self,
        session: Session,
        owner: str,
        event_id: str,
        decision: str,
        idempotency_key: str,
        if_match: str | None = None,
        request_id: str | None = None,
        expected_version_id: str | None = None,
    ) -> dict[str, Any]:
        if decision not in {"confirmed", "rejected"}:
            raise ValueError("decision must be confirmed or rejected")
        request_hash = sha256_text(
            json.dumps(
                {
                    "owner": owner,
                    "event_id": event_id,
                    "request_id": request_id,
                    "decision": decision,
                    "version_id": expected_version_id,
                    "if_match": if_match,
                },
                sort_keys=True,
            )
        )
        receipts = MemoryRepository()
        existing = receipts._receipt(session, idempotency_key, request_hash=request_hash)
        if existing is not None and existing["status"] != "started":
            return cast(dict[str, Any], json.loads(str(existing["result_json"])))
        event = self.get(session, owner, event_id)
        if not event or event["status"] != "candidate":
            raise StaleConfirmationError("event is not confirmable")
        if if_match and if_match.strip('"') not in {"*", self.etag(event)}:
            raise StaleConfirmationError("event changed after it was read")
        version = (
            session.execute(
                text(
                    "SELECT id,title,summary FROM event_memory_versions WHERE event_memory_id=:id ORDER BY version_no DESC LIMIT 1"
                ),
                {"id": event_id},
            )
            .mappings()
            .one()
        )
        request = (
            session.execute(
                text(
                    "SELECT * FROM event_confirmation_requests WHERE event_memory_id=:id AND status='pending' ORDER BY created_at DESC LIMIT 1"
                ),
                {"id": event_id},
            )
            .mappings()
            .first()
        )
        if request is None:
            raise StaleConfirmationError("event confirmation request missing")
        if request_id is not None and str(request["id"]) != request_id:
            raise StaleConfirmationError("event confirmation request does not match")
        if expected_version_id is not None and str(version["id"]) != expected_version_id:
            raise StaleConfirmationError("event version does not match")
        if str(request["event_version_id"]) != str(version["id"]):
            raise StaleConfirmationError("event version changed after confirmation request")
        current_hash = sha256_text(str(version["summary"]))
        if str(request["proposed_value_hash"]) != current_hash:
            raise StaleConfirmationError("event confirmation value hash mismatch")
        receipt_id = receipts._insert_receipt(
            session, idempotency_key, "event_confirmation", request_hash
        )
        expires = request["expires_at"]
        if isinstance(expires, datetime):
            expired = expires <= datetime.now(UTC)
        else:
            expired = datetime.fromisoformat(str(expires).replace("Z", "+00:00")) <= datetime.now(
                UTC
            )
        if expired:
            receipts._fail_receipt(session, receipt_id)
            raise StaleConfirmationError("event confirmation request is stale or expired")
        result: dict[str, Any]
        if decision == "rejected":
            session.execute(
                text(
                    "UPDATE event_memories SET status='rejected',updated_at=CURRENT_TIMESTAMP WHERE id=:id"
                ),
                {"id": event_id},
            )
            decision_id = new_id()
            session.execute(
                text(
                    "INSERT INTO event_confirmation_decisions (id,request_id,decision,event_version_id) VALUES (:id,:r,'rejected',:v)"
                ),
                {"id": decision_id, "r": request["id"], "v": version["id"]},
            )
            session.execute(
                text("UPDATE event_confirmation_requests SET status='rejected' WHERE id=:id"),
                {"id": request["id"]},
            )
            result = {"status": "rejected", "event_id": event_id}
        else:
            generation = int(
                session.execute(
                    text(
                        "SELECT coalesce(max(confirmation_generation),0)+1 FROM event_memories WHERE owner_user_id=:o"
                    ),
                    {"o": owner},
                ).scalar_one()
            )
            version = (
                session.execute(
                    text(
                        "SELECT id FROM event_memory_versions WHERE event_memory_id=:id ORDER BY version_no DESC LIMIT 1"
                    ),
                    {"id": event_id},
                )
                .mappings()
                .one()
            )
            session.execute(
                text(
                    "UPDATE event_memories SET status='formal_current',confirmation_generation=:g,updated_at=CURRENT_TIMESTAMP WHERE id=:id"
                ),
                {"g": generation, "id": event_id},
            )
            decision_id = new_id()
            session.execute(
                text(
                    "INSERT INTO event_confirmation_decisions (id,request_id,decision,event_version_id,generation) VALUES (:id,:r,'confirmed',:v,:g)"
                ),
                {"id": decision_id, "r": request["id"], "v": version["id"], "g": generation},
            )
            session.execute(
                text("UPDATE event_confirmation_requests SET status='confirmed' WHERE id=:id"),
                {"id": request["id"]},
            )
            event_job_key = f"event-index:{event_id}:{version['id']}:{generation}"
            session.execute(
                text(
                    "INSERT OR IGNORE INTO outbox_events (id,event_type,aggregate_type,aggregate_id,payload_json,status) VALUES (:id,'event.index_requested','event_memory',:event,:payload,'pending')"
                ),
                {
                    "id": event_job_key,
                    "event": event_id,
                    "payload": json.dumps(
                        {
                            "event_id": event_id,
                            "version_id": str(version["id"]),
                            "generation": generation,
                        }
                    ),
                },
            )
            result = {
                "status": "index_pending",
                "event_id": event_id,
                "version_id": str(version["id"]),
                "generation": generation,
            }
        session.execute(
            text(
                "UPDATE memory_operation_receipts SET status=:status,result_json=:result,completed_at=CURRENT_TIMESTAMP WHERE id=:id"
            ),
            {"id": receipt_id, "status": result["status"], "result": json.dumps(result)},
        )
        return result
