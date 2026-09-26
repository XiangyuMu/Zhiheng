"""Extract reviewable memory candidates from persisted answer conversations."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from zhiheng.conclusions.extraction import ConversationConclusionDraftService
from zhiheng.core.ids import new_id, sha256_text
from zhiheng.db.session import session_scope
from zhiheng.memory import MemoryRepository
from zhiheng.memory.personal_updates import PersonalUpdate, PersonalUpdateService

MEMORY_EXTRACTION_EVENT = "conversation.persisted"
MEMORY_EXTRACTION_JOB_TYPE = "memory.extract_conversation"
logger = logging.getLogger(__name__)


class ConversationMemoryExtractor(Protocol):
    def extract(self, *, query: str, answer: str) -> list[ExtractedMemory]: ...


@dataclass(frozen=True, slots=True)
class ExtractedMemory:
    candidate_type: str
    memory_type: str
    state_key: str
    value: dict[str, Any]
    rationale: str
    confidence: float
    confidence_explanation: dict[str, Any]
    source_kind: str
    time_sensitivity: str = "persistent"
    valid_from: datetime | None = None
    valid_to: datetime | None = None


class HeuristicConversationMemoryExtractor:
    """Small deterministic extractor used when no model extraction provider is configured."""

    _RULES: tuple[tuple[str, str, str, str, str], ...] = (
        (
            "fact",
            "identity.name",
            r"(?:我叫|我的名字是|my name is)\s*([^，。,.!?！？\n]{1,80})",
            "用户明确陈述姓名",
            "显式陈述",
        ),
        (
            "fact",
            "role.current",
            r"(?:我是|我的职业是|i am)\s*([^，。,.!?！？\n]{1,80})",
            "用户明确陈述身份或职业",
            "显式陈述",
        ),
        (
            "preference",
            "preference.response_style",
            r"(?:我喜欢|我偏好|我希望回答|请用|i prefer|i like)\s*([^。.!！\n]{1,120})",
            "用户明确表达偏好",
            "显式偏好",
        ),
        (
            "goal",
            "goal.personal",
            (
                r"(?:我的目标是|我想要|我计划|我希望|my goal is|i want to|i plan to)"
                r"\s*([^。.!！\n]{1,160})"
            ),
            "用户明确表达目标",
            "显式目标",
        ),
        (
            "inference",
            "inference.stated",
            r"(?:我可能|我似乎|看起来我|i might|i seem to)\s*([^。.!！\n]{1,160})",
            "用户提供了带不确定性的陈述，按推断处理",
            "不确定性陈述",
        ),
    )

    def extract(self, *, query: str, answer: str) -> list[ExtractedMemory]:
        del answer
        found: list[ExtractedMemory] = []
        for memory_type, state_key, pattern, rationale, basis in self._RULES:
            match = re.search(pattern, query, flags=re.IGNORECASE)
            if match is None:
                continue
            if _is_non_assertive_context(query, match.start()):
                continue
            value = _clean(match.group(1))
            if not value:
                continue
            inferred = memory_type == "inference"
            confidence = 0.62 if inferred else 0.9
            found.append(
                ExtractedMemory(
                    candidate_type="inferred" if inferred else "explicit_extracted",
                    memory_type=memory_type,
                    state_key=state_key,
                    value={"text": value},
                    rationale=rationale,
                    confidence=confidence,
                    confidence_explanation={
                        "score": confidence,
                        "factors": {
                            "explicitness": 0.35 if inferred else 1.0,
                            "evidence_count": 1,
                            "consistency": 0.5,
                            "timeliness": 1.0,
                        },
                        "basis": basis,
                        "manual_confirmation_required": inferred,
                    },
                    source_kind="conversation_inferred" if inferred else "conversation_explicit",
                )
            )
        return found


@dataclass(frozen=True, slots=True)
class ClaimedMemoryExtractionJob:
    id: str
    payload: dict[str, Any]
    attempts: int


class MemoryExtractionJobExecutor:
    def __init__(
        self,
        *,
        extractor: ConversationMemoryExtractor | None = None,
        extractor_factory: Callable[[], ConversationMemoryExtractor] | None = None,
    ) -> None:
        self.extractor = extractor or HeuristicConversationMemoryExtractor()
        self.extractor_factory = extractor_factory

    def execute(
        self,
        session_factory: sessionmaker[Session],
        job: ClaimedMemoryExtractionJob,
    ) -> int:
        payload = job.payload
        extractor = self.extractor_factory() if self.extractor_factory else self.extractor
        with session_scope(session_factory) as session:
            row = (
                session.execute(
                    text(
                        """
                    SELECT id, conversation_id, owner_user_id, query, response_json
                    FROM answer_history
                    WHERE id = :history_id
                      AND conversation_id = :conversation_id
                      AND owner_user_id = :owner_user_id
                    """
                    ),
                    {
                        "history_id": str(payload["history_id"]),
                        "conversation_id": str(payload["conversation_id"]),
                        "owner_user_id": str(payload["owner_user_id"]),
                    },
                )
                .mappings()
                .first()
            )
            if row is None:
                raise ValueError("conversation turn not found")
            response = _json_object(row["response_json"])
            answer = str(response.get("answer", ""))
            extracted = extractor.extract(query=str(row["query"]), answer=answer)
            repository = MemoryRepository()
            personal_updates = PersonalUpdateService(repository)
            conclusion_drafts = ConversationConclusionDraftService()
            created = 0
            # Persist event candidates separately from profile memory. This is
            # intentionally conservative and keeps the original conversation
            # as the authoritative evidence source.
            query_text = str(row["query"])
            if any(
                marker in query_text.lower() for marker in ("面试", "interview", "会议", "meeting")
            ) and _event_memory_tables_available(session):
                title = "面试事件" if "面试" in query_text else "Interview event"
                summary = (query_text + ("\n" + answer if answer else ""))[:4000]
                event_id = new_id()
                version_id = new_id()
                session.execute(
                    text("""
                    INSERT OR IGNORE INTO event_memories
                      (id, owner_user_id, event_type, status, title, summary,
                       entities_json, source_conversation_id, source_history_id)
                    VALUES (:id, :owner, 'conversation_event', 'candidate', :title, :summary,
                            :entities, :conversation, :history)
                """),
                    {
                        "id": event_id,
                        "owner": str(row["owner_user_id"]),
                        "title": title,
                        "summary": summary,
                        "entities": json.dumps(
                            {"topics": ["RAG"] if "rag" in query_text.lower() else []}
                        ),  # noqa: E501
                        "conversation": str(row["conversation_id"]),
                        "history": str(row["id"]),
                    },
                )
                actual = session.execute(
                    text("SELECT id FROM event_memories WHERE source_history_id=:h AND title=:t"),
                    {"h": str(row["id"]), "t": title},
                ).scalar_one()  # noqa: E501
                session.execute(
                    text(
                        "INSERT OR IGNORE INTO event_memory_versions (id,event_memory_id,version_no,title,summary,payload_json) VALUES (:v,:e,1,:t,:s,:p)"
                    ),
                    {
                        "v": version_id,
                        "e": str(actual),
                        "t": title,
                        "s": summary,
                        "p": json.dumps({"query": query_text}),
                    },
                )  # noqa: E501
                session.execute(
                    text(
                        "INSERT OR IGNORE INTO event_memory_evidence (id,event_memory_id,event_version_id,conversation_id,history_id,excerpt,start_offset,end_offset,support_type,quote_hash,query_text,response_json,raw_sha256) VALUES (:id,:e,:v,:c,:h,:x,0,:end,'origin',:hash,:query,:response,:raw_hash)"
                    ),
                    {
                        "id": f"{actual}:origin",
                        "e": str(actual),
                        "v": version_id,
                        "c": str(row["conversation_id"]),
                        "h": str(row["id"]),
                        "x": summary,
                        "end": len(summary),
                        "hash": sha256_text(summary),
                        "query": query_text,
                        "response": str(row["response_json"]),
                        "raw_hash": sha256_text(query_text + "\n" + str(row["response_json"])),
                    },
                )  # noqa: E501
                session.execute(
                    text(
                        "INSERT OR IGNORE INTO event_confirmation_requests (id,event_memory_id,event_version_id,status,risk_level,proposed_value_hash,expires_at) VALUES (:id,:event,:version,'pending','medium',:hash,:expires)"
                    ),
                    {
                        "id": f"event-request:{actual}",
                        "event": str(actual),
                        "version": version_id,
                        "hash": sha256_text(summary),
                        "expires": datetime.now(UTC).replace(microsecond=0) + timedelta(days=1),
                    },
                )  # noqa: E501
            for item in extracted:
                result = personal_updates.apply(
                    session,
                    PersonalUpdate(
                        memory_type=item.memory_type,
                        state_key=item.state_key,
                        value=item.value,
                        source_kind=item.source_kind,
                        inferred=item.candidate_type == "inferred",
                        valid_from=item.valid_from,
                        valid_to=item.valid_to,
                        time_sensitivity=item.time_sensitivity,
                        rationale=item.rationale,
                        confidence=item.confidence,
                    ),
                    operation_key=(
                        f"job:{job.id}:{item.state_key}:"
                        f"{sha256_text(json.dumps(item.value, sort_keys=True))}"
                    ),
                    owner_user_id=str(row["owner_user_id"]),
                )
                candidate_id = result.get("candidate_id")
                if candidate_id is None:
                    created += 1
                    continue
                version_id = str(
                    session.execute(
                        text("SELECT current_version_id FROM memory_candidates WHERE id=:id"),
                        {"id": candidate_id},
                    ).scalar_one()
                )
                now = datetime.now(UTC)
                session.execute(
                    text(
                        """
                        UPDATE memory_candidates
                        SET confidence_explanation=:explanation,
                            valid_from=:valid_from, valid_to=:valid_to,
                            time_sensitivity=:time_sensitivity, extracted_at=:extracted_at
                        WHERE id=:id
                        """
                    ),
                    {
                        "id": candidate_id,
                        "explanation": json.dumps(
                            item.confidence_explanation,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        "valid_from": item.valid_from or now,
                        "valid_to": item.valid_to,
                        "time_sensitivity": item.time_sensitivity,
                        "extracted_at": now,
                    },
                )
                excerpt = str(row["query"])
                session.execute(
                    text(
                        """
                        INSERT INTO memory_candidate_evidence (
                          id, candidate_id, candidate_version_id, conversation_id,
                          message_id, message_start, message_end, excerpt,
                          support_type, extracted_at
                        )
                        VALUES (
                          :id, :candidate_id, :version_id, :conversation_id,
                          :message_id, :message_start, :message_end, :excerpt,
                          'supporting', :extracted_at
                        )
                        """
                    ),
                    {
                        "id": f"{candidate_id}:conversation",
                        "candidate_id": candidate_id,
                        "version_id": version_id,
                        "conversation_id": str(row["conversation_id"]),
                        "message_id": str(row["id"]),
                        "message_start": 0,
                        "message_end": len(excerpt),
                        "excerpt": excerpt,
                        "extracted_at": now,
                    },
                )
                created += 1
            created += conclusion_drafts.extract_and_persist(
                session,
                history_id=str(row["id"]),
                conversation_id=str(row["conversation_id"]),
                owner_user_id=str(row["owner_user_id"]),
                query=str(row["query"]),
                answer=answer,
            )
            return created


def process_memory_extraction_jobs_once(
    session_factory: sessionmaker[Session],
    executor: MemoryExtractionJobExecutor,
    *,
    worker_id: str,
    limit: int = 10,
) -> int:
    with session_scope(session_factory) as session:
        rows = (
            session.execute(
                text(
                    """
                SELECT id, payload_json, attempts
                FROM jobs
                WHERE job_type=:job_type AND status='pending'
                  AND available_at <= CURRENT_TIMESTAMP AND attempts < max_attempts
                ORDER BY available_at, id LIMIT :limit
                """
                ),
                {"job_type": MEMORY_EXTRACTION_JOB_TYPE, "limit": limit},
            )
            .mappings()
            .all()
        )
        claimed: list[ClaimedMemoryExtractionJob] = []
        for row in rows:
            session.execute(
                text(
                    """
                    UPDATE jobs
                    SET status='processing', lease_owner=:worker,
                        lease_expires_at=datetime('now', '+300 seconds'),
                        heartbeat_at=CURRENT_TIMESTAMP, attempts=attempts+1,
                        updated_at=CURRENT_TIMESTAMP
                    WHERE id=:id AND job_type=:job_type AND status='pending'
                    """
                ),
                {"id": row["id"], "job_type": MEMORY_EXTRACTION_JOB_TYPE, "worker": worker_id},
            )
            if int(session.execute(text("SELECT changes()")).scalar_one()) == 1:
                claimed.append(
                    ClaimedMemoryExtractionJob(
                        id=str(row["id"]),
                        payload=_json_object(row["payload_json"]),
                        attempts=int(row["attempts"]) + 1,
                    )
                )

    completed = 0
    for job in claimed:
        try:
            executor.execute(session_factory, job)
        except Exception as exc:
            _record_conclusion_extraction_failure(session_factory, job, exc)
            with session_scope(session_factory) as session:
                session.execute(
                    text(
                        """
                        UPDATE jobs
                        SET status=CASE WHEN attempts >= max_attempts THEN 'dead' ELSE 'failed' END,
                            updated_at=CURRENT_TIMESTAMP
                        WHERE id=:id AND status='processing'
                        """
                    ),
                    {"id": job.id},
                )
                session.execute(
                    text(
                        """
                        INSERT INTO job_attempts
                          (id, job_id, attempt_no, status, started_at, finished_at,
                           error_class, error_message)
                        VALUES (:attempt, :job, :attempt_no, 'failed',
                                CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, :error_class, :error_message)
                        """
                    ),
                    {
                        "attempt": f"{job.id}:{job.attempts}",
                        "job": job.id,
                        "attempt_no": job.attempts,
                        "error_class": type(exc).__name__,
                        "error_message": str(exc)[:1000],
                    },
                )
            continue
        with session_scope(session_factory) as session:
            session.execute(
                text(
                    """
                    UPDATE jobs
                    SET status='succeeded', lease_owner=NULL,
                        lease_expires_at=NULL, updated_at=CURRENT_TIMESTAMP
                    WHERE id=:id AND status='processing'
                    """
                ),
                {"id": job.id},
            )
        completed += 1
    return completed


def _record_conclusion_extraction_failure(
    session_factory: sessionmaker[Session],
    job: ClaimedMemoryExtractionJob,
    exc: Exception,
) -> None:
    """Persist a visible extraction failure after the job transaction rolls back."""
    payload = job.payload
    try:
        with session_scope(session_factory) as session:
            row = (
                session.execute(
                    text(
                        """
                        SELECT id, conversation_id, owner_user_id, query, response_json
                        FROM answer_history
                        WHERE id=:history_id
                          AND conversation_id=:conversation_id
                          AND owner_user_id=:owner_user_id
                        """
                    ),
                    {
                        "history_id": str(payload["history_id"]),
                        "conversation_id": str(payload["conversation_id"]),
                        "owner_user_id": str(payload["owner_user_id"]),
                    },
                )
                .mappings()
                .first()
            )
            if row is None:
                return
            response = _json_object(row["response_json"])
            ConversationConclusionDraftService().record_failure(
                session,
                history_id=str(row["id"]),
                conversation_id=str(row["conversation_id"]),
                owner_user_id=str(row["owner_user_id"]),
                query=str(row["query"]),
                answer=str(response.get("answer", "")),
                failure_code="EXTRACTION_FAILED",
                failure_reason=str(exc)[:1000],
            )
    except Exception as persistence_error:
        logger.exception("failed to persist conversation extraction review outcome")
        raise RuntimeError("conversation extraction review persistence failed") from persistence_error


def _event_memory_tables_available(session: Session) -> bool:
    required = {
        "event_memories",
        "event_memory_versions",
        "event_memory_evidence",
        "event_confirmation_requests",
    }
    rows = session.execute(
        text(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('event_memories','event_memory_versions',"
            "'event_memory_evidence','event_confirmation_requests')"
        )
    ).scalars()
    return set(rows) == required


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip(" \t\r\n，。,.!?！？")).strip()


def _is_non_assertive_context(text: str, offset: int) -> bool:
    """Reject quoted, hypothetical, and joking text before memory creation."""
    sentence = re.split(r"[。！？!?\n]", text[:offset])[-1].strip().lower()
    return any(
        marker in sentence
        for marker in (
            "引用",
            "据说",
            "他说",
            "她说",
            "例如",
            "假设",
            "如果",
            "开玩笑",
            "笑话",
            "quote",
            "hypothetical",
            "joke",
        )
    )


def _json_object(value: Any) -> dict[str, Any]:
    decoded = json.loads(value) if isinstance(value, str) else value
    if not isinstance(decoded, dict):
        return {}
    return dict(decoded)
