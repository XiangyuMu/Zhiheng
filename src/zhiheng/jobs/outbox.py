from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from zhiheng.core.ids import json_text, new_id
from zhiheng.evolution.jobs import EvolutionJobType
from zhiheng.jobs.memory_extraction import MEMORY_EXTRACTION_JOB_TYPE

KNOWLEDGE_INDEX_JOB_TYPE = "knowledge.index"
KNOWLEDGE_PARSE_PDF_EVENT = "knowledge.parse_pdf"
KNOWLEDGE_PARSE_PDF_JOB_TYPE = "knowledge.parse_pdf"
KNOWLEDGE_INDEX_EVENTS = frozenset(
    {
        "evidence.ingested",
        "knowledge.soft_deleted",
        "knowledge.restored",
        "knowledge.reindex_requested",
        "knowledge_candidate.created",
        "knowledge_candidate.confirmed",
    }
)
NOOP_AUDIT_EVENTS = frozenset({"evolution.proposal.user_decision"})
PROPOSAL_VALIDATION_REQUESTED_EVENT = "evolution.proposal.validation_requested"
CLASSIFICATION_SUGGESTION_EVENT = "knowledge.classification_suggestion_requested"
CLASSIFICATION_SUGGESTION_JOB_TYPE = "classification.suggest"
CONVERSATION_MEMORY_EVENT = "conversation.persisted"
EVENT_INDEX_EVENT = "event.index_requested"


@dataclass(frozen=True)
class ClaimedOutboxEvent:
    id: str
    event_type: str
    aggregate_type: str
    aggregate_id: str
    payload: dict[str, Any]


class OutboxRepository:
    def claim_pending(self, session: Session, *, limit: int = 10) -> list[ClaimedOutboxEvent]:
        rows = (
            session.execute(
                text(
                    """
                SELECT id, event_type, aggregate_type, aggregate_id, payload_json
                FROM outbox_events
                WHERE status = 'pending'
                  AND available_at <= CURRENT_TIMESTAMP
                ORDER BY available_at, id
                LIMIT :limit
                """
                ),
                {"limit": limit},
            )
            .mappings()
            .all()
        )
        event_ids = [str(row["id"]) for row in rows]
        for event_id in event_ids:
            session.execute(
                text(
                    """
                    UPDATE outbox_events
                    SET status = 'processing', attempts = attempts + 1
                    WHERE id = :event_id
                      AND status = 'pending'
                    """
                ),
                {"event_id": event_id},
            )
        return [
            ClaimedOutboxEvent(
                id=str(row["id"]),
                event_type=str(row["event_type"]),
                aggregate_type=str(row["aggregate_type"]),
                aggregate_id=str(row["aggregate_id"]),
                payload=_json_object(row["payload_json"]),
            )
            for row in rows
        ]

    def enqueue_jobs_for_events(
        self,
        session: Session,
        events: Sequence[ClaimedOutboxEvent],
    ) -> int:
        created = 0
        for event in events:
            if event.event_type in NOOP_AUDIT_EVENTS:
                session.execute(
                    text(
                        """
                        UPDATE outbox_events
                        SET status = 'processed',
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id = :event_id
                        """
                    ),
                    {"event_id": event.id},
                )
                continue
            job_type = _job_type_for_event(event)
            if job_type is None:
                session.execute(
                    text(
                        """
                        UPDATE outbox_events
                        SET status = 'failed',
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id = :event_id
                        """
                    ),
                    {"event_id": event.id},
                )
                continue
            session.execute(
                text(
                    """
                    INSERT OR IGNORE INTO jobs (
                      id, job_type, idempotency_key, payload_json, status
                    )
                    VALUES (
                      :id, :job_type, :idempotency_key, :payload_json, 'pending'
                    )
                    """
                ),
                {
                    "id": new_id(),
                    "job_type": job_type,
                    "idempotency_key": event.id,
                    "payload_json": json_text(
                        {
                            "outbox_event_id": event.id,
                            "aggregate_type": event.aggregate_type,
                            "aggregate_id": event.aggregate_id,
                            **_payload_for_event(event),
                        }
                    ),
                },
            )
            created += int(session.execute(text("SELECT changes()")).scalar_one())
            session.execute(
                text("UPDATE outbox_events SET status = 'processed' WHERE id = :event_id"),
                {"event_id": event.id},
            )
        return created


def _job_type_for_event(event: ClaimedOutboxEvent) -> str | None:
    if event.event_type == KNOWLEDGE_PARSE_PDF_EVENT:
        return KNOWLEDGE_PARSE_PDF_JOB_TYPE
    if event.event_type in KNOWLEDGE_INDEX_EVENTS:
        return KNOWLEDGE_INDEX_JOB_TYPE
    if event.event_type == PROPOSAL_VALIDATION_REQUESTED_EVENT:
        return EvolutionJobType.PROPOSAL_EVALUATION.value
    if event.event_type == CLASSIFICATION_SUGGESTION_EVENT:
        return CLASSIFICATION_SUGGESTION_JOB_TYPE
    if event.event_type == CONVERSATION_MEMORY_EVENT:
        return MEMORY_EXTRACTION_JOB_TYPE
    if event.event_type == EVENT_INDEX_EVENT:
        return "event.index"
    if event.event_type in {item.value for item in EvolutionJobType}:
        return event.event_type
    if event.event_type == "strategy_release.promotion_requested":
        return EvolutionJobType.PROMOTION_REQUEST.value
    if event.event_type == "strategy_release.rollback_requested":
        return EvolutionJobType.ROLLBACK_REQUEST.value
    if event.event_type.startswith("evolution.maintenance"):
        return EvolutionJobType.MAINTENANCE.value
    return None


def _payload_for_event(event: ClaimedOutboxEvent) -> dict[str, Any]:
    payload = {str(key): value for key, value in event.payload.items() if value is not None}
    if event.event_type == "strategy_release.promotion_requested":
        return {"release_id": event.aggregate_id, **payload}
    if event.event_type == "strategy_release.rollback_requested":
        return {"release_id": event.aggregate_id, **payload}
    if event.event_type == PROPOSAL_VALIDATION_REQUESTED_EVENT:
        return {**payload, "proposal_id": event.aggregate_id}
    return payload


def _json_object(value: Any) -> dict[str, Any]:
    loaded = json.loads(value) if isinstance(value, str) else value
    if not isinstance(loaded, dict):
        return {}
    return dict(loaded)
